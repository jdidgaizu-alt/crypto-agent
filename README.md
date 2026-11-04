# crypto-agent

Agent de trading crypto sur **Base**, guidé par l'attention mesurée sur **X**.
**Étape 1 : simulation uniquement.** Aucun code de ce dépôt ne peut signer une transaction.

## Ce que dit la recherche (04/11/2026)
Huit stratégies, enregistrées **avant** de voir le moindre résultat, ont été rejouées sur 7 mois de prix horaires réels (`research/`), avec les frais réels et une exécution à l'ouverture de l'heure suivante. Rendement médian sur toutes les heures de décision possibles, 50 USDC au départ :

| Stratégie | 10/05 → 04/11 | Contrôle hors échantillon (06/09 → 04/11) | Pire / meilleur cas | Baisse max. médiane |
|---|---|---|---|---|
| Garder ETH (référence) | +1,1 % | −7,5 % | −1,0 / +6,3 % | −34 % |
| **Tendance ETH (moyenne 10 j > 30 j)** | **+16,7 %** | **+1,0 %** | **+11,0 / +40,1 %** | −13 % |
| Tendance ETH + BTC (moyenne 20 j) | +9,5 % | −6,2 % | −1,9 / +20,1 % | −11 % |
| Stratégie actuelle (prix seuls) | +3,1 % | −3,9 % | −22,0 / +18,1 % | −8 % |
| Garder BTC | −3,1 % | −0,8 % | | −29 % |
| Panier des jetons liquides | −9,5 % | −3,3 % | | −37 % |
| Momentum relatif 14 j | −7,7 % | +9,3 % | −20,2 / +9,4 % | −26 % |
| ETH à volatilité cible | −6,6 % | −8,5 % | | −32 % |

À retenir :
- **La stratégie actuelle n'a pas d'avantage démontré.** Son résultat dépend surtout de l'heure à laquelle elle tourne (de −22 % à +18 %). Acheter un jeton après une hausse de 15 à 25 % en 24 h n'est pas rentable de façon fiable.
- **Le suivi de tendance sur ETH est le seul candidat solide** : positif pour les 24 heures de décision testées et meilleur que l'ETH sur la période de contrôle. Il ne repose toutefois que sur environ 6 ordres et 7 mois de marché : c'est une piste, pas une preuve.
- **L'attention X n'a pas pu être testée** : X ne fournit pas d'historique gratuit. Seule la simulation en direct le dira.

Relancer : `python -m research.run_all` (données : `research/data/`, résultats : `research/results/`).

## Deux livres fictifs, une référence
| Livre | Qui décide | Protections automatiques |
|---|---|---|
| `auto` | la stratégie systématique (`strategy.py`), à chaque cycle | toutes |
| `claude` | Claude, via l'outil MCP `paper_trade` | stop-loss, arrêt total ; mêmes garde-fous à l'achat |

Chaque livre démarre avec 50 USDC et se compare à **« tout le capital de départ placé en ETH au premier cycle »**.

## Fonctionnement d'un cycle
1. **Attention X** : nombre exact de posts par heure (API de comptage de X). Les 6 dernières heures sont comparées aux **mêmes heures des 6 jours précédents**, pour neutraliser le rythme de la journée. Si une seule des 42 heures utiles manque, ou si la référence compte moins de 30 posts, l'attention est « non mesurée » et la stratégie l'ignore. Il n'y a jamais de valeur par défaut.
2. **Prix** : DexScreener, une requête par jeton. Seules les paires Base cotées en USDC, WETH ou ETH sont retenues. Un jeton en erreur ou à la réponse malformée n'empêche pas les autres d'être traités.
3. **Frais réels** : palier du pool lu sur la blockchain quand c'est possible, sinon **1 % supposé** (prudent), plus 0,05 % si la paire est cotée en WETH.
4. **Décisions et ordres**, dans une seule transaction, avec un verrou contre les cycles simultanés et 30 minutes minimum entre deux cycles. Chaque achat est revérifié au moment de son exécution, contre l'état réel du livre.

## Garde-fous (`config.py`, fractions du capital enregistré)
| Règle | Valeur (capital 50) |
|---|---|
| Taille max par jeton | 20 % (10 USDC), position existante comprise |
| Positions ouvertes max | 3 |
| Ordre minimum | 2 USDC, montants arrondis au centime inférieur |
| Stop-loss / prise de bénéfice | −15 % / +25 % (prise de bénéfice : livre `auto` seulement) |
| Renfort d'une position sous son stop-loss | interdit |
| **Arrêt total** | capital à risque sous 60 % (30 USDC) : tout est vendu, le livre est verrouillé |
| Levée de l'arrêt | **humaine uniquement** : `python -m crypto_agent.cycle --reset-halt auto` relance le livre avec ce qu'il lui reste (nouveau seuil : 60 % de ce reste) |
| Achats par jour | 6 max (les ventes ne comptent pas et ne sont jamais bloquées) |
| Rachat d'un jeton vendu | pas avant 24 h |
| Position sans prix depuis 6 h | achats bloqués (risque inconnu), position gardée à son dernier prix |
| Position sans prix depuis 72 h | comptée pour 0 dans le capital à risque |
| Liquidité / volume 24 h minimum | 500 000 $ / 250 000 $ |
| Impact de prix max à l'achat | 2 % |
| Jetons | liste blanche : WETH, cbBTC, AERO, VIRTUAL, DEGEN, BRETT |

## Coût des données X (pay-per-use, barème 2026 à confirmer dans la console X)
- Comptage : 0,005 $ par requête et par jeton, et seulement quand une heure utile manque. Un cycle par jour coûte environ **0,03 $**, soit environ 0,90 $ par mois. Un cycle par heure coûterait environ 22 $ par mois.
- Lecture de posts : **0,005 $ par post**. La collecte est désactivée par défaut (`X_POSTS_PER_SYMBOL=0`) et disponible à la demande via l'outil `x_collect`.
- Chaque requête payante réserve d'abord son coût sur le budget mensuel (`X_MONTHLY_BUDGET_USD`, 5 $ par défaut), puis l'ajuste au coût réel. Une requête refusée par X est remboursée. Deux collectes simultanées ne peuvent pas dépasser le budget.

## Utilisation
```bash
pip install -e ".[dev]"
pytest -q                                      # 139 tests
python -m crypto_agent.cycle --init            # une seule fois : crée ledger/book.db
scripts/cycle_and_commit.sh                    # un cycle, puis publication du livre sur GitHub
python -m crypto_agent.cycle --report          # état des livres, sans réseau
python -m crypto_agent.backtest research/data/base-hourly-2026-11-04 --step 24
python -m crypto_agent.mcp_server              # passerelle MCP (stdio)
claude mcp add crypto-agent -- python -m crypto_agent.mcp_server
```

Outils MCP : `market_snapshot`, `x_attention`, `x_collect` (payant), `x_search`, `x_budget`, `portfolio_status`, `paper_trade` (livre `claude`), `run_cycle`, `wallet_balances` (lecture seule). Aucun outil ne peut lever un arrêt d'urgence ni contourner un garde-fou.

## Persistance
Le livre `ledger/book.db` est **versionné** : les sessions cloud sont éphémères, et c'est cet historique qui décidera du passage à l'argent réel. Il ne survit à une session que s'il est publié : `scripts/cycle_and_commit.sh` fait le cycle **et** la publication. Le livre est une base binaire que git ne sait pas fusionner : le script part toujours de la dernière version publiée, et si un autre cycle a publié entre-temps, il s'arrête sans rien fusionner. Les cycles planifiés doivent donc s'enchaîner, jamais tourner en parallèle.

La base refuse de se recréer en silence : sans `--init`, un livre absent est une erreur. Les posts X (`data/x.db`) ne sont pas versionnés.

## Variables d'environnement
- `X_BEARER_TOKEN` : jeton de l'API X v2. Sans lui, l'attention n'est pas mesurée et la stratégie ne suit que les prix.
- `X_MONTHLY_BUDGET_USD` (5), `X_POSTS_PER_SYMBOL` (0) : budget et collecte de posts automatique.
- `BASE_RPC_URL` : facultatif, `https://mainnet.base.org` par défaut. Une URL privée n'apparaît dans aucun message d'erreur ni journal.
- `START_CAPITAL_USDC` : lu seulement par `--init` (50 par défaut, 10 au minimum).
- `CRYPTO_AGENT_DB`, `CRYPTO_AGENT_X_DB` : emplacement des bases.

## Domaines réseau à autoriser
`api.dexscreener.com`, `mainnet.base.org`, `api.x.com`

## Passage à l'argent réel (étape 2)
Seulement si un livre bat la référence ETH, frais compris, sur plusieurs semaines de simulation en direct. Il faudra :
- revérifier les adresses de la liste blanche sur basescan.org ;
- un wallet dédié, alimenté par le propriétaire, dont la clé est stockée en secret d'environnement ;
- un module d'exécution séparé, qui passe par les mêmes garde-fous (`strategy.entry_refusal`).
