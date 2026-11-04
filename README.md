# crypto-agent

Agent de trading crypto sur **Base**, guidé par l'attention mesurée sur **X**.
**Étape 1 : simulation uniquement.** Aucun code de ce dépôt ne peut signer une transaction.

## Deux livres fictifs, une référence
| Livre | Qui décide | Protections automatiques |
|---|---|---|
| `auto` | la stratégie systématique (`strategy.py`), à chaque cycle | toutes |
| `claude` | Claude, via l'outil MCP `paper_trade` | stop-loss, arrêt total ; mêmes garde-fous à l'achat |

Chaque livre démarre avec 50 USDC et se compare à **« tout le capital de départ placé en ETH au premier cycle »**. C'est le juge : si un livre ne fait pas mieux que ça, frais compris, il ne sert à rien.

## Fonctionnement d'un cycle (`python -m crypto_agent.cycle`)
1. **Attention X** : nombre exact de posts par heure (API de comptage de X). Ratio = posts des 6 dernières heures / moyenne par tranche de 6 h sur les 24 h précédentes. Si une seule heure manque, l'attention est « non mesurée » et la stratégie l'ignore. Il n'y a jamais de valeur par défaut.
2. **Prix** : DexScreener, une requête par jeton. Seules les paires Base cotées en USDC, WETH ou ETH sont retenues. Un jeton en erreur n'empêche pas les autres d'être traités.
3. **Frais réels** : palier du pool lu sur la blockchain quand c'est possible, sinon **1 % supposé** (prudent), plus 0,05 % si la paire est cotée en WETH.
4. **Décisions et ordres**, dans une seule transaction, avec un verrou contre les cycles simultanés et 30 minutes minimum entre deux cycles.

## Garde-fous (`config.py`, fractions du capital enregistré)
| Règle | Valeur (capital 50) |
|---|---|
| Taille max par jeton | 20 % (10 USDC), position existante comprise |
| Positions ouvertes max | 3 |
| Ordre minimum | 2 USDC |
| Stop-loss / prise de bénéfice | −15 % / +25 % (prise de bénéfice : livre `auto` seulement) |
| **Arrêt total** | capital sous 60 % (30 USDC) : tout est vendu, le livre est verrouillé |
| Levée de l'arrêt | **humaine uniquement** : `python -m crypto_agent.cycle --reset-halt auto` |
| Ordres d'entrée par jour | 6 max (les sorties ne sont jamais bloquées) |
| Rachat d'un jeton vendu | pas avant 24 h |
| Position sans prix depuis 6 h | comptée pour 0 dans le contrôle du risque |
| Liquidité / volume 24 h minimum | 500 000 $ / 250 000 $ |
| Impact de prix max à l'achat | 2 % |
| Jetons | liste blanche : WETH, cbBTC, AERO, VIRTUAL, DEGEN, BRETT |

## Coût des données X (pay-per-use, barème 2026 à confirmer dans la console X)
- Comptage : 0,005 $ par requête, soit environ **0,03 $ par cycle** pour 6 jetons (environ 0,90 $ par mois à un cycle par jour).
- Lecture de posts : **0,005 $ par post**. Collecter 50 posts par jeton à chaque cycle coûterait environ 45 $ par mois, presque tout le capital. La collecte est donc désactivée par défaut (`X_POSTS_PER_SYMBOL=0`), disponible à la demande via l'outil `x_collect`, et plafonnée par un budget mensuel (`X_MONTHLY_BUDGET_USD`, 5 $ par défaut) suivi dans le livre.

## Utilisation
```bash
pip install -e ".[dev]"
pytest -q                                      # 99 tests
python -m crypto_agent.cycle --init            # une seule fois : crée ledger/book.db
python -m crypto_agent.cycle                   # un cycle
python -m crypto_agent.cycle --report          # état des livres, sans réseau
python -m crypto_agent.mcp_server              # passerelle MCP (stdio)
claude mcp add crypto-agent -- python -m crypto_agent.mcp_server
```

Outils MCP : `market_snapshot`, `x_attention`, `x_collect` (payant), `x_search`, `x_budget`, `portfolio_status`, `paper_trade` (livre `claude`), `run_cycle`, `wallet_balances` (lecture seule). Aucun outil ne peut lever un arrêt d'urgence ni contourner un garde-fou.

## Persistance
Le livre `ledger/book.db` est **versionné** : les sessions cloud sont éphémères, et c'est cet historique qui décidera du passage à l'argent réel. La base refuse de se recréer en silence : sans `--init`, un livre absent est une erreur. Les posts X (`data/x.db`) ne sont pas versionnés.

## Variables d'environnement
- `X_BEARER_TOKEN` : jeton de l'API X v2. Sans lui, l'attention n'est pas mesurée et la stratégie ne suit que les prix.
- `X_MONTHLY_BUDGET_USD` (5), `X_POSTS_PER_SYMBOL` (0) : budget et collecte de posts automatique.
- `BASE_RPC_URL` : facultatif, `https://mainnet.base.org` par défaut. Une URL privée n'apparaît dans aucun message d'erreur ni journal.
- `START_CAPITAL_USDC` : lu seulement par `--init` (50 par défaut).
- `CRYPTO_AGENT_DB`, `CRYPTO_AGENT_X_DB` : emplacement des bases.

## Domaines réseau à autoriser
`api.dexscreener.com`, `mainnet.base.org`, `api.x.com`

## Passage à l'argent réel (étape 2)
Seulement si un livre bat la référence ETH, frais compris, sur plusieurs semaines. Il faudra :
- revérifier les adresses de la liste blanche sur basescan.org ;
- un wallet dédié, alimenté par le propriétaire, dont la clé est stockée en secret d'environnement ;
- un module d'exécution séparé, qui passe par les mêmes garde-fous (`strategy.entry_refusal`).
