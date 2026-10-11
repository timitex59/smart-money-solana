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
# Candidates repérées sur UN seul gagnant : leur rentabilité réelle est mesurée (VET_PER_RUN par découverte, ~100 crédits
# chacune) et celles qui sont réellement rentables deviennent smart wallets sans attendre un 2e gagnant. Écartées
# d'office : entrée avant VET_MIN_OFFSET_S (robots qui achètent dès l'ouverture), portefeuille de moins de
# VET_MIN_AGE_DAYS jours (portefeuille jetable), plus de VET_MAX_TOKENS jetons en 30 jours (robot).
VET_PER_RUN = 60                  # ~70 crédits chacune : ~4 000/jour, dans la part journalière du budget
VET_MIN_OFFSET_S = 60
VET_MIN_AGE_DAYS = 7
VET_MAX_TOKENS = 140
# Alertes
CONVERGENCE_MIN = 3
CONVERGENCE_WINDOW_MIN = 60
ALERT_COOLDOWN_H = 24
BOT_MAX_TX_PER_RUN = 60           # un portefeuille qui fait plus de 60 transactions en 15 min est un robot : écarté
BOT_MAX_BUYS_PER_HOUR = 8         # plus de 8 jetons différents achetés en une heure : robot de trading, écarté
ALERT_MIN_MCAP, ALERT_MIN_LIQ = 50_000, 10_000  # sous ces montants : micro-jeton manipulable, pas d'alerte
ALERT_MAX_PER_RUN = 3
# Alerte trop tardive (bilan des premières alertes : toutes à -39/-75 % en 1 h, les acheteurs revendaient déjà) :
ALERT_MAX_SOLD_PCT = 20           # un acheteur a déjà revendu >= 20 % de ses jetons -> pas d'alerte
ALERT_MAX_RUNUP_PCT = 50          # prix déjà > +50 % au-dessus du prix d'entrée moyen des acheteurs -> pas d'alerte
ALERT_REQUIRE_X = True            # analyse X (Grok) indisponible -> pas d'alerte
# Signaux fantômes (comparaison, AUCUN message) : dès que 1, 2 puis 3 acteurs RENTABLES achètent un même jeton (fenêtre
# de convergence), le prix est noté puis suivi à +1 h, +24 h, +7 j. Même filtres que les alertes (taille, retard).
SHADOW_LEVELS = (1, 2, 3)
SHADOW_KEEP_DAYS = 45                # tout le mois d'observation, pour le bilan
HELIUS_MONTHLY_BUDGET = 850_000    # sur 1M gratuits : au-delà de sa part du jour, la surveillance saute des passages
# Rentabilité réelle des smart wallets (P&L en SOL sur PNL_DAYS jours : vendu + valeur de ce qui est encore détenu − dépensé)
PNL_DAYS = 30
PNL_MAX_PAGES = 10                # 1 000 dernières transactions au plus par portefeuille (100 crédits)
PNL_REFRESH_DAYS = 7              # historique reconstruit chaque semaine ; entre-temps, mis à jour en continu par la surveillance
PNL_BACKFILL_PER_RUN = 25         # portefeuilles reconstruits par passage de découverte (budget de crédits)
PROFIT_MIN_TOKENS, PROFIT_MIN_WINRATE = 5, 0.35  # « rentable » : >= 5 jetons tradés, >= 35 % de jetons gagnants…
PROFIT_MIN_SOL, PROFIT_MIN_ROI = 1.0, 10.0       # …et au moins +1 SOL et +10 % sur les sommes engagées (pas un simple 0 %)
ALERT_MIN_PROFITABLE = 2          # une alerte exige au moins 2 acheteurs réellement rentables
TOP_N = 5
# Analyse X (Twitter) de chaque alerte par Grok (xAI, payant à l'usage : ~0,14 $ par analyse) — clé GROK_API_KEY
GROK_ENABLED = False              # analyse X écartée pour l'instant (choix utilisateur du 11/10/2026) : ni appel, ni filtre
GROK_MODEL = "grok-4.20-0309-non-reasoning"
GROK_MAX_PER_DAY = 10             # garde-fou de dépense : 10 analyses/jour au plus (~1,40 $)
BATCH_FROM = 10                   # à partir de 10 nouvelles transactions : lecture groupée (moins de crédits Helius)


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
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"{method}: {j['error']}")  # réponse en échec : non facturée par Helius
                # Barème Helius : 1 crédit par appel standard ; getTransactionsForAddress = 10 crédits par tranche de 100
                # transactions complètes renvoyées (10 au minimum).
                if method == "getTransactionsForAddress":
                    n = len((j.get("result") or {}).get("data") or [])
                    self.credits += 10 * max(1, -(-n // 100))
                else:
                    self.credits += 1
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


def dex_batch(mints: list) -> dict:
    """{mint: {price (USD), mcap, liq}} par lots de 30 (DexScreener, pool le plus liquide). Absent = introuvable."""
    out = {}
    mints = list(dict.fromkeys(mints))
    for i in range(0, len(mints), 30):
        try:
            pairs = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]), headers=UA, timeout=20).json()
        except (requests.RequestException, ValueError):
            continue
        for p in pairs if isinstance(pairs, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m and liq >= (out.get(m, {}).get("liq") or -1):
                out[m] = {"price": float(p.get("priceUsd") or 0), "mcap": p.get("marketCap") or p.get("fdv"), "liq": liq,
                          "symbol": (p.get("baseToken") or {}).get("symbol")}
        time.sleep(0.25)
    return out


def late_reasons(ws, mint: str, ledgers: dict, price_usd: float, usd: float) -> list:
    """Raisons pour lesquelles un signal arrive trop tard : acheteurs qui revendent déjà, prix déjà envolé."""
    late, entries = [], []
    for w in ws:
        tk = (ledgers.get(w) or {}).get("tokens", {}).get(mint)
        if not tk or tk["bought"] <= 0:
            continue
        sold_pct = tk["sold"] / tk["bought"] * 100
        if sold_pct >= ALERT_MAX_SOLD_PCT:
            late.append(f"{w[:4]}… a déjà revendu {sold_pct:.0f} %")
        if tk["spent"] > 0:
            entries.append(tk["spent"] / tk["bought"])  # prix d'entrée moyen en SOL par jeton
    price_sol = price_usd / usd if usd else 0
    if entries and price_sol:
        runup = (price_sol / (sum(entries) / len(entries)) - 1) * 100
        if runup > ALERT_MAX_RUNUP_PCT:
            late.append(f"prix déjà +{runup:.0f} % au-dessus de leur entrée")
    return late


def shadow_track(state: dict, wallets: dict, by_mint: dict, actors: dict, ledgers: dict, t: int, usd: float) -> None:
    """Note les signaux fantômes 1/2/3 acteurs rentables et met à jour leur performance (aucun message)."""
    sh = state.setdefault("shadow", [])
    have = {(r["mint"], r["level"]) for r in sh if t - r["ts"] < ALERT_COOLDOWN_H * 3600}
    new = []
    for mint, ws in by_mint.items():
        prof = [w for w in ws if wallets.get(w, {}).get("pnl", {}).get("profitable")]
        n = len({actors.get(w, w) for w in prof})
        for lvl in SHADOW_LEVELS:
            if n >= lvl and (mint, lvl) not in have:
                new.append((mint, lvl, prof))
    todo_perf = [r for r in sh if any(lab not in r["perf"] and t - r["ts"] >= d for lab, d in (("1h", 3600), ("24h", 86400), ("7j", 7 * 86400)))]
    info = dex_batch([m for m, _, _ in new] + [r["mint"] for r in todo_perf])
    for mint, lvl, prof in new:
        i = info.get(mint)
        if not i or not i["price"]:
            continue
        reason = None
        if float(i.get("mcap") or 0) < ALERT_MIN_MCAP or float(i.get("liq") or 0) < ALERT_MIN_LIQ:
            reason = "micro-jeton"
        else:
            late = late_reasons(prof, mint, ledgers, i["price"], usd)
            reason = ("trop tard : " + " ; ".join(late)) if late else None
        sh.append({"mint": mint, "symbol": i.get("symbol"), "ts": t, "level": lvl, "price": i["price"], "mcap": i.get("mcap"),
                   "filtered": reason, "perf": {}})
    for r in todo_perf:
        for lab, d in (("1h", 3600), ("24h", 86400), ("7j", 7 * 86400)):
            if lab in r["perf"] or t - r["ts"] < d:
                continue
            p = (info.get(r["mint"]) or {}).get("price")
            if p:
                r["perf"][lab] = (p / r["price"] - 1) * 100
            elif t - r["ts"] >= d + 6 * 3600:
                r["perf"][lab] = -100.0  # plus aucun marché depuis des heures : jeton mort
    state["shadow"] = [r for r in sh if t - r["ts"] < SHADOW_KEEP_DAYS * 86400]


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
# Rentabilité réelle des portefeuilles (P&L en SOL)
# --------------------------------------------------------------------------
WSOL = "So11111111111111111111111111111111111111112"


def sol_usd() -> float:
    try:
        pairs = requests.get(f"https://api.dexscreener.com/tokens/v1/solana/{WSOL}", headers=UA, timeout=20).json()
        usdc = [p for p in pairs if (p.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT")]
        return float(max(usdc or pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))["priceUsd"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return 0.0


def swaps_in_tx(tx: dict, owner: str, usd_per_sol: float) -> list:
    """[(mint, variation de quantité, variation en SOL)] des échanges de `owner` dans la transaction : achat = jeton +
    / SOL −, vente = jeton − / SOL +. SOL natif + SOL enveloppé ; USDC/USDT convertis en SOL. Les échanges jeton contre
    jeton (sans SOL ni dollar) sont ignorés."""
    d = owner_deltas(tx).get(owner)
    if not d:
        return []
    quote = d.get("SOL", 0) + d.get(WSOL, 0)
    for m in ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"):
        if d.get(m) and usd_per_sol:
            quote += d[m] / usd_per_sol
    tokens = {m: q for m, q in d.items() if m != "SOL" and m not in QUOTE_MINTS and abs(q) > 0}
    if len(tokens) != 1 or abs(quote) < 1e-6:
        return []
    (mint, q), = tokens.items()
    if (q > 0) == (quote > 0):
        return []  # jeton reçu ET SOL reçu (ou l'inverse) : pas un échange (airdrop, transfert…)
    return [(mint, q, quote)]


def ledger_apply(led: dict, tx: dict, owner: str, usd_per_sol: float) -> None:
    """Enregistre les achats/ventes de `tx` dans le registre du portefeuille. Une vente d'un jeton jamais acheté dans la
    fenêtre est ignorée (sinon un jeton acheté avant la fenêtre ferait un faux gain)."""
    ts = tx.get("blockTime") or now_ts()
    for mint, q, sol in swaps_in_tx(tx, owner, usd_per_sol):
        tk = led["tokens"].get(mint)
        if q > 0:
            tk = led["tokens"].setdefault(mint, {"spent": 0.0, "recv": 0.0, "bought": 0.0, "sold": 0.0, "first": ts, "last": ts})
            tk["spent"] += -sol
            tk["bought"] += q
        elif tk:
            tk["recv"] += sol
            tk["sold"] += -q
        else:
            continue
        tk["last"] = max(tk["last"], ts)


def prices_sol(mints: list, usd_per_sol: float) -> dict:
    """{mint: prix en SOL} (DexScreener, par lots de 30, pool le plus liquide). Absent = sans valeur (jeton mort)."""
    out = {}
    for i in range(0, len(mints), 30):
        try:
            pairs = requests.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]), headers=UA, timeout=20).json()
        except (requests.RequestException, ValueError):
            continue
        best = {}
        for p in pairs if isinstance(pairs, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if m and liq >= float((best.get(m, {}).get("liquidity") or {}).get("usd") or -1):
                best[m] = p
        for m, p in best.items():
            if (p.get("quoteToken") or {}).get("address") == WSOL and p.get("priceNative"):
                out[m] = float(p["priceNative"])
            elif p.get("priceUsd") and usd_per_sol:
                out[m] = float(p["priceUsd"]) / usd_per_sol
        time.sleep(0.25)
    return out


def wallet_stats(led: dict, prices: dict) -> dict:
    """P&L (SOL) = vendu + valeur de ce qui est encore détenu − dépensé ; taux de jetons gagnants ; nombre de jetons."""
    pnl = spent = 0.0
    wins = n = 0
    for mint, tk in led["tokens"].items():
        if tk["spent"] <= 0:
            continue
        held = max(tk["bought"] - tk["sold"], 0.0)
        val = tk["recv"] + held * prices.get(mint, 0.0) - tk["spent"]
        pnl += val
        spent += tk["spent"]
        n += 1
        wins += val > 0
    st = {"pnl_sol": pnl, "spent_sol": spent, "roi_pct": pnl / spent * 100 if spent else None, "tokens": n,
          "win_rate": wins / n if n else None}
    st["profitable"] = (n >= PROFIT_MIN_TOKENS and pnl >= PROFIT_MIN_SOL and (st["roi_pct"] or 0) >= PROFIT_MIN_ROI
                        and (st["win_rate"] or 0) >= PROFIT_MIN_WINRATE)
    return st


def backfill(h: Helius, wallet: str, usd_per_sol: float, t: int) -> dict:
    """Registre des PNL_DAYS derniers jours du portefeuille (getTransactionsForAddress, du plus récent au plus ancien)."""
    led = {"tokens": {}, "synced": t, "backfilled": t}
    token, txs = None, []
    for _ in range(PNL_MAX_PAGES):
        opts = {"sortOrder": "desc", "limit": 100, "transactionDetails": "full", "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 1, "filters": {"blockTime": {"gte": t - PNL_DAYS * 86400}}}
        if token:
            opts["paginationToken"] = token
        res = h.call("getTransactionsForAddress", [wallet, opts]) or {}
        txs += res.get("data") or []
        token = res.get("paginationToken")
        if not res.get("data") or not token:
            break
    for tx in sorted(txs, key=lambda x: x.get("blockTime") or 0):  # ordre chronologique : achats avant ventes
        if not (tx.get("meta") or {}).get("err"):
            ledger_apply(led, tx, wallet, usd_per_sol)
    led["partial"] = bool(token)  # historique plus long que PNL_MAX_PAGES pages : P&L calculé sur la partie récente
    return led


def refresh_pnl(h: Helius, wallets: dict, state: dict, t: int, max_backfill: int) -> None:
    """Reconstruit l'historique des smart wallets qui n'en ont pas (ou trop ancien), purge ce qui sort de la fenêtre,
    puis recalcule P&L et classement de tous les smart wallets."""
    usd = sol_usd()
    ledgers = state.setdefault("ledger", {})
    smart = smart_list(wallets)
    todo = [w for w in smart if t - ledgers.get(w, {}).get("backfilled", 0) > PNL_REFRESH_DAYS * 86400][:max_backfill]
    done = 0
    for w in todo:
        if not budget_ok(state, t):
            print("  budget Helius du jour atteint : reconstruction reportée")
            break
        before = h.credits
        try:
            ledgers[w] = backfill(h, w, usd, t)
            done += 1
        except RuntimeError as exc:
            print(f"  historique de {w[:6]}… illisible : {str(exc)[:80]}")
        add_credits(state, t, h.credits - before)
    for w in list(ledgers):
        if w not in wallets:
            del ledgers[w]
            continue
        ledgers[w]["tokens"] = {m: tk for m, tk in ledgers[w]["tokens"].items() if tk["first"] >= t - PNL_DAYS * 86400}
    held = sorted({m for w in smart if w in ledgers for m, tk in ledgers[w]["tokens"].items()
                   if tk["bought"] - tk["sold"] > tk["bought"] * 0.01})
    prices = prices_sol(held, usd)
    for w in smart:
        if w in ledgers:
            wallets[w]["pnl"] = {**wallet_stats(ledgers[w], prices), "at": iso(t), "partial": ledgers[w].get("partial", False)}
    n_ok = sum(1 for w in smart if wallets[w].get("pnl", {}).get("profitable"))
    print(f"Rentabilité : {done} historiques reconstruits · {n_ok} portefeuilles rentables sur {len(smart)}")


def ranking(wallets: dict) -> list:
    """Smart wallets rentables, du plus au moins rentable (P&L en SOL sur PNL_DAYS jours)."""
    ok = [(w, r["pnl"]) for w, r in wallets.items() if r.get("pnl", {}).get("profitable") and not r.get("bot")]
    return sorted(ok, key=lambda x: -x[1]["pnl_sol"])


def fmt_sol(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f} SOL"


def wallet_line(w: str, p, rank: dict) -> str:
    if not p:
        return f"{w[:4]}…{w[-4:]} (rentabilité pas encore mesurée)"
    tag = f" · #{rank[w]}" if w in rank else ""
    roi = f" ({p['roi_pct']:+.0f} %)" if p.get("roi_pct") is not None else ""
    return (f"{w[:4]}…{w[-4:]} : {fmt_sol(p['pnl_sol'])} sur {PNL_DAYS} j{roi}, "
            f"{round((p.get('win_rate') or 0) * 100)} % de jetons gagnants{tag}")


def top_lines(wallets: dict) -> list:
    rk = ranking(wallets)[:TOP_N]
    if not rk:
        return []
    return [f"🏆 TOP {TOP_N} rentables ({PNL_DAYS} j) : " + " · ".join(f"#{i} {w[:4]}…{w[-4:]} {fmt_sol(p['pnl_sol'])}"
                                                               for i, (w, p) in enumerate(rk, 1)), ""]


def send_top(wallets: dict, state: dict, t: int) -> None:
    rk = ranking(wallets)
    if not rk:
        telegram("🏆 TOP SMART MONEY\n\nAucun portefeuille encore mesuré comme rentable (historiques en cours de reconstruction).")
        return
    recent = {}
    for b in state.get("buys", []):
        if b["ts"] >= t - 86400:
            recent.setdefault(b["wallet"], []).append(b["mint"])
    names = {}
    lines = [f"🏆 TOP {TOP_N} SMART MONEY — les plus rentables sur {PNL_DAYS} jours", ""]
    for i, (w, p) in enumerate(rk[:TOP_N], 1):
        lines.append(f"{i}. {w[:4]}…{w[-4:]} · {fmt_sol(p['pnl_sol'])} ({p['roi_pct']:+.0f} %) · "
                     f"{round(p['win_rate'] * 100)} % gagnants · {p['tokens']} jetons")
        mints = list(dict.fromkeys(recent.get(w, [])))[:4]
        for m in mints:
            if m not in names:
                names[m] = dexscreener(m).get("symbol") or m[:6]
        lines.append("   achats 24 h : " + (", ".join(names[m] for m in mints) if mints else "aucun"))
        lines.append(f"   https://solscan.io/account/{w}")
    lines += ["", f"{len(rk)} portefeuilles rentables sur {len(smart_list(wallets))} suivis."]
    telegram("\n".join(lines))


# --------------------------------------------------------------------------
# Analyse X (Twitter) par Grok
# --------------------------------------------------------------------------
GROK_VERDICTS = {"green": "🟢 rien d'alarmant", "orange": "🟠 prudence", "red": "🔴 signaux d'arnaque"}


def grok_x_analysis(state: dict, t: int, mint: str, info: dict) -> dict:
    """Ce que disent les posts récents sur X à propos du jeton : buzz, comptes, signaux d'arnaque, verdict
    green/orange/red. {} si pas de clé, limite du jour atteinte ou erreur (l'alerte part alors sans cette analyse)."""
    key = os.environ.get("GROK_API_KEY", "").strip()
    day = iso(t)[:10]
    used = state.setdefault("grok", {})
    if not key:
        return {"error": "clé GROK_API_KEY absente"}
    if used.get(day, {}).get("n", 0) >= GROK_MAX_PER_DAY:
        return {"error": f"limite de {GROK_MAX_PER_DAY} analyses par jour atteinte"}
    sym = info.get("symbol") or "?"
    prompt = (
        f"Analyse les posts récents sur X (Twitter) à propos du memecoin Solana « {sym} » ({info.get('name') or ''}), "
        f"adresse du contrat {mint}. Cherche par le symbole ${sym} ET par l'adresse du contrat (plusieurs jetons peuvent "
        "porter le même nom : ne retiens que ce qui concerne CE contrat). Réponds UNIQUEMENT par un objet JSON, sans texte "
        'autour, de la forme : {"verdict": "green|orange|red", "buzz": "...", "accounts": "...", "scam_signals": "...", '
        '"links": ["url d\'un post", "..."]}. Règles du verdict : "red" si des comptes crédibles signalent une arnaque, un '
        "rug, un « bundle » (offre concentrée chez des portefeuilles liés au créateur), un créateur qui vend ou des "
        'portefeuilles d\'initiés ; "orange" si les informations sont rares, contradictoires ou si le buzz paraît '
        'artificiel ; "green" si le buzz semble organique sans signal d\'arnaque. Champs en français, 1 à 2 phrases '
        "chacun, au plus 2 liens."
    )
    try:
        r = requests.post("https://api.x.ai/v1/responses", headers={"Authorization": f"Bearer {key}"}, timeout=150,
                          json={"model": GROK_MODEL, "input": [{"role": "user", "content": prompt}], "tools": [{"type": "x_search"}]})
        j = r.json()
        if not r.ok:
            return {"error": f"xAI {r.status_code} : {str(j)[:120]}"}
    except (requests.RequestException, ValueError) as exc:
        return {"error": f"xAI injoignable : {exc}"}
    cost = ((j.get("usage") or {}).get("cost_in_usd_ticks") or 0) / 1e10
    rec = used.setdefault(day, {"n": 0, "usd": 0.0})
    rec["n"] += 1
    rec["usd"] += cost
    for d in sorted(used)[:-40]:
        del used[d]
    text = "".join(c.get("text", "") for o in j.get("output", []) if o.get("type") == "message"
                   for c in o.get("content", []) if c.get("type") == "output_text")
    try:
        a = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return {"error": "réponse de Grok illisible", "cost_usd": cost}
    a["verdict"] = a.get("verdict") if a.get("verdict") in GROK_VERDICTS else "orange"
    a["links"] = [u for u in (a.get("links") or []) if isinstance(u, str) and u.startswith("http")][:2]
    a["cost_usd"] = cost
    return a


def grok_lines(a: dict) -> list:
    if not a or a.get("error"):
        return [f"X (Grok) : analyse indisponible{' — ' + a['error'] if a and a.get('error') else ''}"]
    out = [f"X (Grok) : {GROK_VERDICTS[a['verdict']]}"]
    for lab, k in (("Buzz", "buzz"), ("Comptes", "accounts"), ("Signaux", "scam_signals")):
        if a.get(k):
            out.append(f"• {lab} : {a[k]}")
    out += [f"• {u}" for u in a["links"]]
    return out


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
        keep = rec.get("promoted") and rec.get("pnl", {}).get("profitable")
        if not rec["hits"] and not rec.get("bot") and not keep:
            del wallets[owner]
    # Un smart wallet doit être un portefeuille personnel (compte du System Program), pas un compte de programme.
    unchecked = [o for o, r in wallets.items() if "personal" not in r and (len(r["hits"]) >= SMART_MIN_HITS or (
        len(r["hits"]) == 1 and "vetted" not in r and next(iter(r["hits"].values()))["offset_s"] >= VET_MIN_OFFSET_S))]
    for i in range(0, len(unchecked), 100):
        chunk = unchecked[i:i + 100]
        accs = (h.call("getMultipleAccounts", [chunk, {"encoding": "base64"}]) or {}).get("value") or []
        for o, acc in zip(chunk, accs):
            wallets[o]["personal"] = bool(acc) and acc.get("owner") == "11111111111111111111111111111111"
    save(WALLETS_PATH, wallets)
    st = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    add_credits(st, t, h.credits)
    save(STATE_PATH, st)
    st = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    vet_candidates(h, wallets, st, t)
    save(STATE_PATH, st)
    save(WALLETS_PATH, wallets)
    smart = smart_list(wallets)
    refresh_pnl(h, wallets, st, t, PNL_BACKFILL_PER_RUN)
    save(STATE_PATH, st)
    save(WALLETS_PATH, wallets)
    send_top(wallets, st, t)
    print(f"Acteurs distincts parmi les smart wallets : {len(set(actor_map(wallets, smart).values()))}")
    print(f"Portefeuilles suivis : {len(wallets)} · smart wallets (>= {SMART_MIN_HITS} gagnants) : {len(smart)} · crédits Helius {h.credits}")


def vet_candidates(h: "Helius", wallets: dict, state: dict, t: int) -> None:
    """Mesure la rentabilité réelle des candidates à 1 gagnant (les plus récentes d'abord) et promeut les rentables."""
    usd = sol_usd()
    ledgers = state.setdefault("ledger", {})
    todo = [o for o, r in wallets.items() if len(r["hits"]) == 1 and not r.get("bot") and "vetted" not in r
            and r.get("personal", True) and next(iter(r["hits"].values()))["offset_s"] >= VET_MIN_OFFSET_S]
    # Ordre : un jeton après l'autre (les plus récents d'abord) pour couvrir tous les gagnants ; dans chaque jeton,
    # d'abord les entrées entre 1 et 15 min (zone des meilleurs profils trouvés en test, la plus tardive d'abord car les
    # robots se concentrent juste après l'ouverture), puis les entrées plus tardives.
    groups = {}
    for o in todo:
        m, hit = next(iter(wallets[o]["hits"].items()))
        off = hit["offset_s"]
        groups.setdefault(m, []).append(((off > 900, -off if off <= 900 else off), hit["at"], o))
    queues = sorted((sorted(g) for g in groups.values()), key=lambda g: max(x[1] for x in g), reverse=True)
    todo = []
    while any(queues):
        for q in queues:
            if q:
                todo.append(q.pop(0)[2])
    promoted = vetted = 0
    for o in todo[:VET_PER_RUN]:
        if not budget_ok(state, t):
            print("  budget Helius du jour atteint : vérification des candidates reportée")
            break
        before = h.credits
        rec = wallets[o]
        try:
            oldest = (h.call("getTransactionsForAddress", [o, {"sortOrder": "asc", "limit": 1, "transactionDetails": "signatures"}]) or {}).get("data") or []
            age_d = (t - oldest[0]["blockTime"]) / 86400 if oldest and oldest[0].get("blockTime") else 0
            if age_d < VET_MIN_AGE_DAYS:
                rec["vetted"] = {"at": iso(t), "result": f"portefeuille récent ({age_d:.1f} j)"}
                continue
            led = backfill(h, o, usd, t)
            st = wallet_stats(led, prices_sol(list(led["tokens"]), usd))
        except RuntimeError as exc:
            print(f"  candidate {o[:6]}… illisible : {str(exc)[:80]}")
            continue
        finally:
            add_credits(state, t, h.credits - before)
        vetted += 1
        if st["tokens"] > VET_MAX_TOKENS:
            rec["bot"] = True
            rec["vetted"] = {"at": iso(t), "result": f"robot ({st['tokens']} jetons en {PNL_DAYS} j)"}
        elif st["profitable"]:
            rec["promoted"] = iso(t)
            rec["vetted"] = {"at": iso(t), "result": "rentable : promue smart wallet"}
            rec["pnl"] = {**st, "at": iso(t), "partial": led.get("partial", False)}
            ledgers[o] = led
            promoted += 1
        else:
            rec["vetted"] = {"at": iso(t), "result": f"non rentable ({st['pnl_sol']:+.2f} SOL)"}
    print(f"Candidates à 1 gagnant : {vetted} vérifiées, {promoted} promues smart wallets ({len(todo)} en attente)")


def actor_map(wallets: dict, smart: list) -> dict:
    """{portefeuille: acteur} : des portefeuilles entrés sur les MÊMES gagnants avec les MÊMES délais (à
    CLUSTER_TOLERANCE_S près) sont presque sûrement un seul acteur (plusieurs portefeuilles d'une même personne ou d'un
    groupe d'initiés). Une alerte compte des acteurs, pas des portefeuilles."""
    actor = {w: w for w in smart}
    def same(a, b):
        ha, hb = wallets[a]["hits"], wallets[b]["hits"]
        common = set(ha) & set(hb)
        need = 1 if min(len(ha), len(hb)) == 1 else 2
        return len(common) >= need and len(common) >= 0.8 * min(len(ha), len(hb)) and all(
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
    """Smart wallets surveillés : >= SMART_MIN_HITS gagnants, ou candidate promue (rentable) tant qu'elle le reste.
    Les réellement rentables passent en premier dans la limite de SMART_MAX_WALLETS."""
    ok = [(o, r) for o, r in wallets.items()
          if (len(r["hits"]) >= SMART_MIN_HITS or (r.get("promoted") and r.get("pnl", {}).get("profitable")))
          and not r.get("bot") and r.get("personal", True)]
    ok.sort(key=lambda x: (not x[1].get("pnl", {}).get("profitable"), -len(x[1]["hits"]), x[0]))
    return [o for o, _ in ok[:SMART_MAX_WALLETS]]


# --------------------------------------------------------------------------
# 2. Surveillance et alertes de convergence
# --------------------------------------------------------------------------
def fmt_usd(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    return f"{v / 1e6:.2f} M$" if v >= 1e6 else f"{v / 1e3:.0f} k$"


def budget_ok(state: dict, t: int) -> bool:
    """Garde-fou du quota Helius gratuit : la part du jour (budget mensuel / jours du mois) ne doit pas être dépassée.
    Les crédits de chaque jour sont tenus dans state["credits"] (découverte comprise)."""
    day = iso(t)[:10]
    used = state.get("credits", {}).get(day, 0)
    return used < HELIUS_MONTHLY_BUDGET / 31


def add_credits(state: dict, t: int, n: int) -> None:
    c = state.setdefault("credits", {})
    c[iso(t)[:10]] = c.get(iso(t)[:10], 0) + n
    for d in sorted(c)[:-40]:  # 40 jours d'historique suffisent
        del c[d]


def cmd_monitor(args) -> None:
    h = Helius(os.environ.get("HELIUS_API_KEY", "").strip())
    wallets = load(WALLETS_PATH, {})
    state = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    if not budget_ok(state, now_ts()):
        print(f"Budget Helius du jour atteint ({state['credits'][iso(now_ts())[:10]]} crédits) : passage sauté.")
        return
    smart = smart_list(wallets)
    t = now_ts()
    new_buys = 0
    usd = sol_usd()
    ledgers = state.setdefault("ledger", {})
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
        todo = [s for s in sigs if not s.get("err")]
        if len(todo) >= BATCH_FROM:
            # Portefeuille actif : toutes ses nouvelles transactions en un appel (10 crédits les 100) au lieu d'un
            # getTransaction (1 crédit) par transaction.
            res = h.call("getTransactionsForAddress", [w, {
                "sortOrder": "desc", "limit": 100, "transactionDetails": "full", "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 1,
                "filters": {"blockTime": {"gte": min(s.get("blockTime") or t for s in todo)}}}]) or {}
            wanted = {s["signature"] for s in todo}
            txs = [x for x in res.get("data") or [] if x["transaction"]["signatures"][0] in wanted]
        else:
            txs = [x for x in (h.tx(s["signature"]) for s in todo) if x]
        led = ledgers.get(w)
        for tx in sorted(txs, key=lambda x: x.get("blockTime") or 0):
            bt = tx.get("blockTime") or t
            for owner, mint in buys_in_tx(tx, only_owner=w):
                state["buys"].append({"wallet": w, "mint": mint, "ts": bt})
                new_buys += 1
            if led and bt > led.get("synced", 0):  # suivi en temps réel de la rentabilité (achats et ventes)
                ledger_apply(led, tx, w, usd)
                led["synced"] = bt
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
    try:
        shadow_track(state, wallets, by_mint, actors, ledgers, t, usd)
    except Exception as exc:  # la comparaison ne doit jamais empêcher les vraies alertes
        print(f"signaux fantômes : {exc}")
    rank = {w: i for i, (w, _) in enumerate(ranking(wallets), 1)}
    sent = 0
    for mint, ws in sorted(by_mint.items(), key=lambda kv: -len(kv[1])):
        n_actors = len({actors.get(w, w) for w in ws})
        if n_actors < CONVERGENCE_MIN or t - alerted.get(mint, 0) < ALERT_COOLDOWN_H * 3600 or sent >= ALERT_MAX_PER_RUN:
            continue
        profitable = [w for w in ws if wallets.get(w, {}).get("pnl", {}).get("profitable")]
        if len({actors.get(w, w) for w in profitable}) < ALERT_MIN_PROFITABLE:
            continue  # pas assez d'acheteurs réellement rentables (P&L mesuré sur 30 jours) : simple flair passé
        info = dexscreener(mint)
        if float(info.get("mcap") or 0) < ALERT_MIN_MCAP or float(info.get("liq") or 0) < ALERT_MIN_LIQ:
            continue  # micro-jeton (souvent encore en phase de lancement pump.fun, sans liquidité réelle)
        # Retard : les acheteurs suivis ont-ils déjà commencé à revendre, ou le prix a-t-il déjà décollé depuis leur entrée ?
        late = late_reasons(ws, mint, ledgers, float(info.get("price") or 0), usd)
        if late:
            state.setdefault("blocked", []).append({"mint": mint, "ts": t, "symbol": info.get("symbol"), "dangers": ["Trop tard : " + " ; ".join(late)],
                                                    "actors": n_actors, "mcap": info.get("mcap"), "price": info.get("price")})
            state["blocked"] = state["blocked"][-200:]
            continue
        rc = rugcheck(mint)
        if rc.get("dangers"):
            # Filtre RugCheck : un risque de niveau « danger » (créateur ayant déjà fait des arnaques, création de jetons ou
            # gel des portefeuilles encore possibles, détenteur quasi unique…) = pas d'alerte. Noté pour l'onglet du
            # dashboard, pour vérifier plus tard que ce filtre écarte bien des jetons qui chutent.
            state.setdefault("blocked", []).append({"mint": mint, "ts": t, "symbol": info.get("symbol"), "dangers": rc["dangers"],
                                                    "actors": n_actors, "mcap": info.get("mcap"), "price": info.get("price")})
            state["blocked"] = state["blocked"][-200:]
            continue
        xa = grok_x_analysis(state, t, mint, info) if GROK_ENABLED else {}
        if GROK_ENABLED and ALERT_REQUIRE_X and xa.get("error"):
            state.setdefault("blocked", []).append({"mint": mint, "ts": t, "symbol": info.get("symbol"), "dangers": ["X : analyse indisponible (" + str(xa["error"])[:60] + ")"],
                                                    "actors": n_actors, "mcap": info.get("mcap"), "price": info.get("price")})
            state["blocked"] = state["blocked"][-200:]
            continue
        if xa.get("verdict") == "red":
            # Filtre X : la communauté signale une arnaque (bundle, rug, initiés…) — ce que RugCheck ne voit pas toujours.
            state.setdefault("blocked", []).append({"mint": mint, "ts": t, "symbol": info.get("symbol"), "dangers": ["X : " + (xa.get("scam_signals") or "signaux d'arnaque")],
                                                    "actors": n_actors, "mcap": info.get("mcap"), "price": info.get("price")})
            state["blocked"] = state["blocked"][-200:]
            continue
        sent += 1
        alert = {"mint": mint, "ts": t, "wallets": sorted(ws), "actors": n_actors, "profitable": len(profitable), "symbol": info.get("symbol"), "name": info.get("name"),
                 "price": info.get("price"), "mcap": info.get("mcap"), "liq": info.get("liq"), "url": info.get("url"),
                 "rug": rc, "x": {k: xa.get(k) for k in ("verdict", "buzz", "accounts", "scam_signals", "links", "error")}, "perf": {}}
        state["alerts"].append(alert)
        age_h = (t - info["created"]) / 3600 if info.get("created") else None
        lines = [f"🧠 SMART MONEY — {n_actors} acteurs dont {len(profitable)} rentables achètent {info.get('symbol') or mint[:6]}",
                 "",
                 f"Capitalisation : {fmt_usd(info.get('mcap'))} · liquidité : {fmt_usd(info.get('liq'))}"
                 + (f" · âge : {age_h:.0f} h" if age_h is not None else ""),
                 (f"RugCheck : risque {rc.get('score', '?')}/100 · aucun risque grave" + (f" (avertissements : {', '.join(rc['risks'])})" if rc.get("risks") else "")
                  if rc else "RugCheck : indisponible au moment de l'alerte ⚠️ — vérifiez sur rugcheck.xyz")
                 + (f" · LP verrouillée {rc['lp_locked']:.0f} %" if rc.get("lp_locked") is not None else ""),
                 "",
                 *((grok_lines(xa) + [""]) if GROK_ENABLED else []),
                 "Acheteurs (rentabilité réelle) :",
                 *("• " + wallet_line(x, wallets.get(x, {}).get("pnl"), rank)
                   for x in sorted(ws, key=lambda x: -(wallets.get(x, {}).get("pnl", {}).get("pnl_sol") or -1e9))),
                 "",
                 info.get("url") or f"https://dexscreener.com/solana/{mint}",
                 "",
                 *top_lines(wallets),
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
    add_credits(state, t, h.credits)
    save(STATE_PATH, state)
    save(WALLETS_PATH, wallets)
    print(f"{len(smart)} smart wallets surveillés · {new_buys} nouveaux achats · alertes au total {len(state['alerts'])} · crédits Helius {h.credits}")


def cmd_pnl(args) -> None:
    h = Helius(os.environ.get("HELIUS_API_KEY", "").strip())
    wallets = load(WALLETS_PATH, {})
    state = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    t = now_ts()
    refresh_pnl(h, wallets, state, t, SMART_MAX_WALLETS)
    save(STATE_PATH, state)
    save(WALLETS_PATH, wallets)
    send_top(wallets, state, t)
    print(f"crédits Helius {h.credits}")


def shadow_report(state: dict) -> dict:
    """Par niveau (1/2/3 acteurs rentables) : nombre de signaux retenus et performance moyenne / médiane / % positifs."""
    out = {}
    for lvl in SHADOW_LEVELS:
        rows = [r for r in state.get("shadow", []) if r["level"] == lvl and not r.get("filtered")]
        rep_lvl = {"signals": len(rows), "filtered": sum(1 for r in state.get("shadow", []) if r["level"] == lvl and r.get("filtered"))}
        for lab in ("1h", "24h", "7j"):
            v = sorted(r["perf"][lab] for r in rows if lab in r["perf"])
            if v:
                rep_lvl[lab] = {"n": len(v), "avg": sum(v) / len(v), "median": v[len(v) // 2], "pos_pct": 100 * sum(x > 0 for x in v) / len(v)}
        out[lvl] = rep_lvl
    return out


def cmd_shadow(args) -> None:
    rep_ = shadow_report(load(STATE_PATH, {}))
    for lvl, r in rep_.items():
        line = f"{lvl} acteur(s) rentable(s) : {r['signals']} signaux retenus ({r['filtered']} filtrés)"
        for lab in ("1h", "24h", "7j"):
            if lab in r:
                x = r[lab]
                line += f" | {lab} : moy {x['avg']:+.1f} %, méd {x['median']:+.1f} %, {x['pos_pct']:.0f} % positifs (n={x['n']})"
        print(line)


def cmd_status(args) -> None:
    wallets = load(WALLETS_PATH, {})
    state = load(STATE_PATH, {"last_sig": {}, "buys": [], "alerts": []})
    smart = smart_list(wallets)
    out = {
        "smart_wallets": [{"wallet": w, "hits": len(wallets[w]["hits"]), "tokens": [x["name"] for x in wallets[w]["hits"].values()],
                           "pnl": wallets[w].get("pnl")} for w in smart],
        "tracked_wallets": len(wallets), "bots": sum(1 for r in wallets.values() if r.get("bot")),
        "recent_buys": sorted(state["buys"], key=lambda b: -b["ts"])[:50],
        "alerts": sorted(state["alerts"], key=lambda a: -a["ts"]),
        "shadow": shadow_report(state),
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
    sub.add_parser("discover", help="Met à jour la liste des smart wallets, leur rentabilité et envoie le TOP 5 (1 fois/jour).")
    pn = sub.add_parser("pnl", help="Mesure la rentabilité réelle de tous les smart wallets et envoie le TOP 5.")
    pn.add_argument("--no-send", action="store_true", help="Test : n'envoie rien sur Telegram.")
    mo = sub.add_parser("monitor", help="Surveille leurs achats et alerte en cas de convergence (toutes les 15 min).")
    mo.add_argument("--no-send", action="store_true", help="Test : n'envoie rien sur Telegram.")
    sub.add_parser("shadow", help="Compare la rentabilité des signaux fantômes 1 / 2 / 3 acteurs rentables.")
    st = sub.add_parser("status", help="État : smart wallets, derniers achats, alertes et leur suite.")
    st.add_argument("--json", action="store_true")
    args = p.parse_args()
    global DRY_RUN
    DRY_RUN = bool(getattr(args, "no_send", False))
    {"discover": cmd_discover, "monitor": cmd_monitor, "pnl": cmd_pnl, "status": cmd_status, "shadow": cmd_shadow}[args.cmd](args)


if __name__ == "__main__":
    main()
