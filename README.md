# Smart money Solana — alertes memecoins (aucun achat)

Repère les portefeuilles entrés **tôt mais pas en robot** (30 s → 3 h après le lancement) sur plusieurs memecoins
Solana qui ont ensuite fortement monté, surveille leurs achats et envoie une **alerte Telegram** quand au moins
3 acteurs distincts achètent le même jeton en moins d'une heure (capitalisation ≥ 50 k$, liquidité ≥ 10 k$,
contrôle RugCheck). Chaque alerte est suivie à +1 h, +24 h et +7 j pour juger la méthode dans le temps.

**Alertes et analyse uniquement** : aucune clé d'échange, aucun ordre, aucun portefeuille.
Les memecoins comportent un risque de perte totale ; rien ici n'est un conseil d'investissement.

## Fonctionnement
- `python smart_money.py discover` — met à jour la liste des smart wallets (1 fois/jour).
- `python smart_money.py monitor` — surveille leurs achats et alerte (toutes les 15 min) ; `--no-send` pour tester.
- `python smart_money.py status [--json]` — smart wallets, derniers achats, alertes et leur suite.

Données gratuites : [Helius](https://www.helius.dev) (offre gratuite), GeckoTerminal, DexScreener, RugCheck.
Exécution : GitHub Actions (`workflow_dispatch`, déclenché par cron-job.org). Secrets : `HELIUS_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Pause : variable de dépôt `SMART_MONEY_PAUSED = true`.
