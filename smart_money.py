#!/usr/bin/env python3
"""
smart_money.py — Alertes « smart money » sur les memecoins Solana (ALERTES ET ANALYSE UNIQUEMENT : aucun achat).

Inspiré de « I Built a Grok Bot Team To Find My Next 100X Memecoin », avec des données gratuites :
  - GeckoTerminal (sans clé) : memecoins Solana en tendance / qui ont le plus monté ;
  - Helius (offre gratuite, 1M crédits/mois, clé HELIUS_API_KEY) : lecture de la blockchain (qui a acheté quoi, quand) ;
  - DexScreener (sans clé) : prix, capitalisation, liquidité, âge d'un jeton ;
  - RugCheck (sans clé) : contrôle anti-arnaque ;
  - Telegram (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) : alertes.

Il n'existe pas de classement gratuit et fiable des « meilleurs traders » Solana : on le construit nous-mêmes.
  1. discover (1 fois/jour) : pour les memecoins qui ont fortement monté, on lit leurs PREMIERS achats sur la
     blockchain ; un portefeuille entré tôt sur plusieurs gagnants (hors snipers du tout premier bloc) devient un
     « smart wallet ». Fenêtre glissante de SMART_WINDOW_DAYS jours.
  2. monitor (toutes les 15 min) : nouveaux achats des smart wallets ; quand >= CONVERGENCE_MIN portefeuilles
     différents achètent le même jeton en moins de CONVERGENCE_WINDOW_MIN minutes → alerte Telegram (jeton,
     capitalisation, liquidité, âge, RugCheck, acheteurs). Chaque alerte est ensuite suivie (prix +1 h, +24 h, +7 j)
     pour juger dans le temps si ces alertes valent quelque chose.
  3. status : état lisible (pour le terminal et le dashboard, --json).

État : smart_wallets.json, sm_data.json (à côté de ce fichier, committés par le workflow).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(r"D:\coinbase\.env"))  # en local : les clés de l'app Coinbase
except ImportError:
    pass

HERE = Path(__file__).resolve().parent
WALLETS_PATH = HERE / "smart_wallets.json"
STATE_PATH = HERE / "sm_data.json"  # pas « *_state.json » : ignoré par le .gitignore du dépôt

QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",   # SOL enveloppé
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
UA = {"User-Agent": "Mozilla/5.0 (compatible; SmartMoneyBot/1.0)", "Accept": "application/json"}

# Découverte
DISCOVER_MIN_GAIN_PCT = 100.0     # le jeton doit avoir au moins doublé sur 24 h…
DISCOVER_MIN_LIQ_USD = 20_000     # …avec une vraie liquidité (écarte les micro-pools manipulables)
DISCOVER_MAX_TOKENS = 25          # gagnants analysés par passage
EARLY_MIN_S, EARLY_MAX_S = 30, 3 * 3600  # entrée « humaine » : ni sniper des premières secondes, ni retardataire
CLUSTER_TOLERANCE_S = 3           # mêmes jetons achetés avec les mêmes délais (à 3 s près) = un seul acteur
SMART_WINDOW_DAYS = 14
SMART_MIN_HITS = 2                # entré tôt sur au moins 2 gagnants différents
SMART_MAX_WALLETS = 80            # budget de crédits Helius : 80 portefeuilles x 96 passages/jour
# Alertes
CONVERGENCE_MIN = 3
CONVERGENCE_WINDOW_MIN = 60
ALERT_COOLDOWN_H = 24
BOT_MAX_TX_PER_RUN = 60           # un portefeuille qui fait plus de 60 transactions en 15 min est un robot : écarté
BOT_MAX_BUYS_PER_HOUR = 8         # plus de 8 jetons différents achetés en une heure : robot de trading, écarté
ALERT_MIN_MCAP, ALERT_MIN_LIQ = 50_000, 10_000  # sous ces montants : micro-jeton manipulable, pas d'alerte
ALERT_MAX_PER_RUN = 3


def now_ts() -> int:
    return int(time.time())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Helius (RPC Solana) — 1 crédit par appel standard
# --------------------------------------------------------------------------
class Helius:
    def __init__(self, key: str):
        if not key:
            sys.exit("HELIUS_API_KEY manquante (.env en local, secret GitHub dans le cloud).")
        self.url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        self.credits = 0

    def call(self, method: str, params: list):
        for attempt in range(5):
            try:
                r = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
                self.credits += 1
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"{method}: {j['error']}")
                time.sleep(0.11)  # offre gratuite : 10 requêtes/s
                return j["result"]
            except (requests.RequestException, ValueError):
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{method}: Helius ne répond pas")

    def signatures(self, address: str, limit: int = 1000, before: str | None = None, until: str | None = None) -> list:
        opts = {"limit": limit}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def tx(self, sig: str) -> dict | None:
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


def owner_deltas(tx: dict) -> dict:
    """{propriétaire: {mint: variation}} d'une transaction (soldes de jetons avant/après), plus la variation de SOL natif
    sous la clé « SOL » (frais compris)."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return {}
    out: dict = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            owner, mint = b.get("owner"), b.get("mint")
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if owner and mint:
                d = out.setdefault(owner, {})
                d[mint] = d.get(mint, 0.0) + sign * amt
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    for i, k in enumerate(keys):
        if k in out:
            out[k]["SOL"] = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    return out


def buys_in_tx(tx: dict, only_owner: str | None = None) -> list:
    """[(acheteur, mint)] : un propriétaire dont un memecoin augmente et dont le SOL/USDC/USDT diminue."""
    res = []
    for owner, d in owner_deltas(tx).items():
        if only_owner and owner != only_owner:
            continue
        spent = d.get("SOL", 0) + sum(d.get(m, 0) for m in QUOTE_MINTS)
        if spent >= 0:
            continue
        for mint, delta in d.items():
            if mint != "SOL" and mint not in QUOTE_MINTS and delta > 0:
                res.append((owner, mint))
    return res


# --------------------------------------------------------------------------
# Sources gratuites sans clé
# --------------------------------------------------------------------------
def gecko_winners() -> list:
    """Memecoins Solana en tendance qui ont au moins doublé sur 24 h, avec une liquidité suffisante."""
    seen, out = set(), []
    for duration in ("24h", "6h"):
        for page in (1, 2, 3):
            try:
                r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
                                 params={"duration": duration, "page": page}, headers=UA, timeout=20)
                data = r.json().get("data", [])
            except (requests.RequestException, ValueError):
                continue
            for p in data:
                a = p["attributes"]
                mint = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                gain = float((a.get("price_change_percentage") or {}).get("h24") or 0)
                liq = float(a.get("reserve_in_usd") or 0)
                if mint in seen or mint in QUOTE_MINTS or gain < DISCOVER_MIN_GAIN_PCT or liq < DISCOVER_MIN_LIQ_USD:
                    continue
                seen.add(mint)
                out.append({"mint": mint, "name": a.get("name"), "gain_24h": gain, "liq": liq})
            time.sleep(2.2)  # GeckoTerminal gratuit : 30 appels/min
    return sorted(out, key=lambda x: -x["gain_24h"])[:DISCOVER_MAX_TOKENS]


def dexscreener(mint: str) -> dict:
    try:
        pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", headers=UA, timeout=20).json().get("pairs") or []
    except (requests.RequestException, ValueError):
        return {}
    if not pairs:
        return {}
    p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    return {"symbol": p["baseToken"].get("symbol"), "name": p["baseToken"].get("name"), "price": float(p.get("priceUsd") or 0),
            "mcap": p.get("marketCap") or p.get("fdv"), "liq": (p.get("liquidity") or {}).get("usd"),
            "created": (p.get("pairCreatedAt") or 0) // 1000, "url": p.get("url"),
            "vol_h1": (p.get("volume") or {}).get("h1"), "chg_h1": (p.get("priceChange") or {}).get("h1")}


def rugcheck(mint: str) -> dict:
    """Résumé RugCheck : score (0-100), risques (nom + niveau « warn » ou « danger »), liquidité verrouillée.
    {} si RugCheck ne répond pas."""
    try:
        j = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", headers=UA, timeout=20).json()
        risks = [{"name": r.get("name"), "level": r.get("level")} for r in j.get("risks") or []]
        return {"score": j.get("score_normalised"), "risks": [r["name"] for r in risks][:4],
                "dangers": [r["name"] for r in risks if r["level"] == "danger"], "lp_locked": j.get("lpLockedPct")}
    except (requests.RequestException, ValueError):
        return {}


DRY_RUN = False  # --no-send : rien n'est envoyé sur Telegram (tests)


def telegram(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if DRY_RUN or not token or not chat:
        print("(Telegram non configuré)\n" + text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
    except requests.RequestException as exc:
        print(f"Telegram : {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# 1. Découverte des smart wallets
# --------------------------------------------------------------------------
def early_buyers(h: Helius, mint: str) -> "tuple[list, int | None]":
    """Acheteurs « tôt mais humains » d'un jeton : [(portefeuille, secondes après le lancement)], et l'instant du
    lancement. Un memecoin en vogue fait des dizaines de transactions par seconde au début : ses 1 000 premières ne
    couvrent que les 20 premières secondes (robots). getTransactionsForAddress (Helius) permet de sauter directement à
    un instant donné : on lit des tranches à partir de 30 s, 5 min, 30 min et 2 h après le lancement (fenêtre
    EARLY_MIN_S → EARLY_MAX_S). 10 crédits par tranche de 100 transactions."""
    first = (h.call("getTransactionsForAddress", [mint, {"sortOrder": "asc", "limit": 1, "transactionDetails": "signatures"}]) or {}).get("data") or []
    if not first or not first[0].get("blockTime"):
        return [], None
    t0 = first[0]["blockTime"]
    txs, seen = [], set()
    for start, n in ((EARLY_MIN_S, 300), (300, 300), (1800, 200), (7200, 200)):
        if t0 + start >= now_ts() - 60:
            break  # jeton trop récent : cette tranche n'existe pas encore (Helius renverrait une erreur)
        token, got = None, 0
        while got < n:
            opts = {"sortOrder": "asc", "limit": 100, "transactionDetails": "full", "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1, "filters": {"blockTime": {"gte": t0 + start, "lt": t0 + EARLY_MAX_S}}}
            if token:
                opts["paginationToken"] = token
            res = h.call("getTransactionsForAddress", [mint, opts]) or {}
            data = res.get("data") or []
            for tx in data:
                sig = tx["transaction"]["signatures"][0]
                if sig not in seen:
                    seen.add(sig); txs.append(tx)
            got += len(data)
            token = res.get("paginationToken")
            if not data or not token:
                break
    out = []
    for tx in txs:
        offset = (tx.get("blockTime") or t0) - t0
        if not EARLY_MIN_S <= offset <= EARLY_MAX_S:
            continue
        for owner, m in buys_in_tx(tx):
            if m == mint:
                out.append((owner, offset))
    return out, t0


def cmd_discover(args) -> None:
    h = Helius(os.environ.get("HELIUS_API_KEY", "").strip())
    wallets = load(WALLETS_PATH, {})
    winners = gecko_winners()
    print(f"{len(winners)} gagnants à analyser")
    t = now_ts()
    for w in winners:
        try:
            buyers, t0 = early_buyers(h, w["mint"])
        except RuntimeError as exc:  # un jeton illisible ne doit pas arrêter toute la découverte
            print(f"  {w['name'][:28]:28} ignoré : {str(exc)[:100]}")
            continue
        uniq = {}
        for owner, offset in buyers:
            uniq.setdefault(owner, offset)
        print(f"  {w['name'][:28]:28} +{w['gain_24h']:6.0f} % · {len(uniq):3} premiers acheteurs · crédits {h.credits}")
        for owner, offset in uniq.items():
            rec = wallets.setdefault(owner, {"hits": {}, "first_seen": iso(t)})
            rec["hits"][w["mint"]] = {"name": w["name"], "at": iso(t), "offset_s": offset}
    cutoff = t - SMART_WINDOW_DAYS * 86400
    for owner in list(wallets):
        rec = wallets[owner]
        rec["hits"] = {m: x for m, x in rec["hits"].items() if datetime.fromisoformat(x["at"]).timestamp() >= cutoff}
        if not rec["hits"] and not rec.get("bot"):
            del wallets[owner]
    # Un smart wallet doit être un portefeuille personnel (compte du System Program), pas un compte de programme.
    unchecked = [o for o, r in wallets.items() if len(r["hits"]) >= SMART_MIN_HITS and "personal" not in r]
    for i in range(0, len(unchecked), 100):
        chunk = unchecked[i:i + 100]
        accs = (h.call("getMultipleAccounts", [chunk, {"encoding": "base64"}]) or {}).get("value") or []
        for o, acc in zip(chunk, accs):
            wallets[o]["personal"] = bool(acc) and acc.get("owner") == "11111111111111111111111111111111"
    save(WALLETS_PATH, wallets)
    smart = smart_list(wallets)
    print(f"Acteurs distincts parmi les smart wallets : {len(set(actor_map(wallets, smart).values()))}")
    print(f"Portefeuilles suivis : {len(wallets)} · smart wallets (>= {SMART_MIN_HITS} gagnants) : {len(smart)} · crédits Helius {h.credits}")


def actor_map(wallets: dict, smart: list) -> dict:
    """{portefeuille: acteur} : des portefeuilles entrés sur les MÊMES gagnants avec les MÊMES délais (à
    CLUSTER_TOLERANCE_S près) sont presque sûrement un seul acteur (plusieurs portefeuilles d'une même personne ou d'un
    groupe d'initiés). Une alerte compte des acteurs, pas des portefeuilles."""
    actor = {w: w for w in smart}
    def same(a, b):
        ha, hb = wallets[a]["hits"], wallets[b]["hits"]
        common = set(ha) & set(hb)
        return len(common) >= 2 and len(common) >= 0.8 * min(len(ha), len(hb)) and all(
            abs(ha[m]["offset_s"] - hb[m]["offset_s"]) <= CLUSTER_TOLERANCE_S for m in common)
    for i, a in enumerate(smart):
        for b in smart[:i]:
            if same(a, b):
                old, new = actor[a], actor[b]
                for w in actor:
                    if actor[w] == old:
                        actor[w] = new
    return actor


def smart_list(wallets: dict) -> list:
    ok = [(o, r) for o, r in wallets.items() if len(r["hits"]) >= SMART_MIN_HITS and not r.get("bot") and r.get("personal", True)]
    ok.sort(key=lambda x: (-len(x[1]["hits"]), x[0]))
    return [o for o, _ in ok[:SMART_MAX_WALLETS]]


# --------------------------------------------------------------------------
# 2. Surveillance et alertes de convergence
# --------------------------------------------------------------------------
def fmt_usd(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    return f"{v / 1e6:.2f} M$" if v >= 1e6 else f"{v / 1e3:.0f} k$"


def cmd_monitor(args) -> None:
    h = Helius(os.environ.get("HELIUS_API_KEY", "").strip())
    wallets = load(WALLETS_PATH, {})
    state = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    smart = smart_list(wallets)
    t = now_ts()
    new_buys = 0
    for w in smart:
        last = state["last_sig"].get(w)
        sigs = h.signatures(w, 100, until=last) if last else h.signatures(w, 1)
        if not sigs:
            continue
        state["last_sig"][w] = sigs[0]["signature"]
        if not last:
            continue  # 1er passage : on se cale sur la dernière transaction, sans relire l'historique
        if len(sigs) > BOT_MAX_TX_PER_RUN:
            wallets[w]["bot"] = True
            continue
        for s in sigs:
            if s.get("err") or (s.get("blockTime") or 0) < t - 3600:
                continue
            tx = h.tx(s["signature"])
            for owner, mint in buys_in_tx(tx or {}, only_owner=w):
                state["buys"].append({"wallet": w, "mint": mint, "ts": s.get("blockTime") or t})
                new_buys += 1
    state["buys"] = [b for b in state["buys"] if b["ts"] >= t - 86400]
    # Robots de trading : trop d'achats de jetons différents sur la dernière heure → écartés définitivement.
    per_wallet: dict = {}
    for b in state["buys"]:
        if b["ts"] >= t - 3600:
            per_wallet.setdefault(b["wallet"], set()).add(b["mint"])
    for w, mints in per_wallet.items():
        if len(mints) > BOT_MAX_BUYS_PER_HOUR and w in wallets:
            wallets[w]["bot"] = True
    bots = {w for w, r in wallets.items() if r.get("bot")}
    # Convergence : >= CONVERGENCE_MIN smart wallets différents sur le même jeton en moins de CONVERGENCE_WINDOW_MIN min
    recent = [b for b in state["buys"] if b["ts"] >= t - CONVERGENCE_WINDOW_MIN * 60 and b["wallet"] not in bots]
    by_mint: dict = {}
    for b in recent:
        by_mint.setdefault(b["mint"], set()).add(b["wallet"])
    alerted = {a["mint"]: a["ts"] for a in state["alerts"]}
    actors = actor_map(wallets, smart)
    sent = 0
    for mint, ws in sorted(by_mint.items(), key=lambda kv: -len(kv[1])):
        n_actors = len({actors.get(w, w) for w in ws})
        if n_actors < CONVERGENCE_MIN or t - alerted.get(mint, 0) < ALERT_COOLDOWN_H * 3600 or sent >= ALERT_MAX_PER_RUN:
            continue
        info = dexscreener(mint)
        if float(info.get("mcap") or 0) < ALERT_MIN_MCAP or float(info.get("liq") or 0) < ALERT_MIN_LIQ:
            continue  # micro-jeton (souvent encore en phase de lancement pump.fun, sans liquidité réelle)
        rc = rugcheck(mint)
        if rc.get("dangers"):
            # Filtre RugCheck : un risque de niveau « danger » (créateur ayant déjà fait des arnaques, création de jetons ou
            # gel des portefeuilles encore possibles, détenteur quasi unique…) = pas d'alerte. Noté pour l'onglet du
            # dashboard, pour vérifier plus tard que ce filtre écarte bien des jetons qui chutent.
            state.setdefault("blocked", []).append({"mint": mint, "ts": t, "symbol": info.get("symbol"), "dangers": rc["dangers"],
                                                    "actors": n_actors, "mcap": info.get("mcap"), "price": info.get("price")})
            state["blocked"] = state["blocked"][-200:]
            continue
        sent += 1
        alert = {"mint": mint, "ts": t, "wallets": sorted(ws), "actors": n_actors, "symbol": info.get("symbol"), "name": info.get("name"),
                 "price": info.get("price"), "mcap": info.get("mcap"), "liq": info.get("liq"), "url": info.get("url"),
                 "rug": rc, "perf": {}}
        state["alerts"].append(alert)
        age_h = (t - info["created"]) / 3600 if info.get("created") else None
        lines = [f"🧠 SMART MONEY — {n_actors} acteurs ({len(ws)} portefeuilles) achètent {info.get('symbol') or mint[:6]}",
                 "",
                 f"Capitalisation : {fmt_usd(info.get('mcap'))} · liquidité : {fmt_usd(info.get('liq'))}"
                 + (f" · âge : {age_h:.0f} h" if age_h is not None else ""),
                 (f"RugCheck : risque {rc.get('score', '?')}/100 · aucun risque grave" + (f" (avertissements : {', '.join(rc['risks'])})" if rc.get("risks") else "")
                  if rc else "RugCheck : indisponible au moment de l'alerte ⚠️ — vérifiez sur rugcheck.xyz")
                 + (f" · LP verrouillée {rc['lp_locked']:.0f} %" if rc.get("lp_locked") is not None else ""),
                 "Acheteurs : " + ", ".join(f"{x[:4]}…{x[-4:]} ({len(wallets.get(x, {}).get('hits', {}))} gagnants)" for x in sorted(ws)),
                 info.get("url") or f"https://dexscreener.com/solana/{mint}",
                 "",
                 "⚠️ Alerte d'information uniquement — memecoin : risque de perte totale."]
        telegram("\n".join(lines))
    # Suivi des alertes : que fait le jeton ensuite ? (jugement des alertes dans le temps)
    for a in state["alerts"]:
        age = t - a["ts"]
        for lab, delay in (("1h", 3600), ("24h", 86400), ("7j", 7 * 86400)):
            if lab not in a["perf"] and age >= delay and a.get("price"):
                p = dexscreener(a["mint"]).get("price")
                if p:
                    a["perf"][lab] = (p / a["price"] - 1) * 100
    save(STATE_PATH, state)
    save(WALLETS_PATH, wallets)
    print(f"{len(smart)} smart wallets surveillés · {new_buys} nouveaux achats · alertes au total {len(state['alerts'])} · crédits Helius {h.credits}")


def cmd_status(args) -> None:
    wallets = load(WALLETS_PATH, {})
    state = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    smart = smart_list(wallets)
    out = {
        "smart_wallets": [{"wallet": w, "hits": len(wallets[w]["hits"]), "tokens": [x["name"] for x in wallets[w]["hits"].values()]} for w in smart],
        "tracked_wallets": len(wallets), "bots": sum(1 for r in wallets.values() if r.get("bot")),
        "recent_buys": sorted(state["buys"], key=lambda b: -b["ts"])[:50],
        "alerts": sorted(state["alerts"], key=lambda a: -a["ts"]),
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"Smart wallets : {len(smart)} (sur {out['tracked_wallets']} suivis, {out['bots']} robots écartés)")
    for s in out["smart_wallets"][:15]:
        print(f"  {s['wallet'][:6]}…{s['wallet'][-4:]}  {s['hits']} gagnants : {', '.join(n[:18] for n in s['tokens'][:4])}")
    print(f"Alertes : {len(out['alerts'])}")
    for a in out["alerts"][:10]:
        perf = " · ".join(f"{k} {v:+.0f} %" for k, v in a.get("perf", {}).items())
        print(f"  {iso(a['ts'])[:16]} {a.get('symbol') or a['mint'][:6]} · {len(a['wallets'])} portefeuilles · cap {fmt_usd(a.get('mcap'))} · {perf}")


def main() -> None:
    p = argparse.ArgumentParser(description="Alertes smart money sur les memecoins Solana (aucun achat).")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover", help="Met à jour la liste des smart wallets (1 fois/jour).")
    mo = sub.add_parser("monitor", help="Surveille leurs achats et alerte en cas de convergence (toutes les 15 min).")
    mo.add_argument("--no-send", action="store_true", help="Test : n'envoie rien sur Telegram.")
    st = sub.add_parser("status", help="État : smart wallets, derniers achats, alertes et leur suite.")
    st.add_argument("--json", action="store_true")
    args = p.parse_args()
    global DRY_RUN
    DRY_RUN = bool(getattr(args, "no_send", False))
    {"discover": cmd_discover, "monitor": cmd_monitor, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
