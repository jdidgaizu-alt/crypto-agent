# crypto-agent

Agent de trading crypto sur **Base**, guidé par les signaux de **X**.
**Étape 1 : simulation uniquement.** Aucun code de ce dépôt ne peut signer une transaction.

## Fonctionnement
1. **Collecte X** : posts récents par jeton (`xsource.py`), indexés en plein texte (SQLite FTS5) pour la recherche RAG.
2. **Marché** : prix, liquidité, volume et variations via DexScreener (`market.py`). Un jeton est refusé si le symbole renvoyé ne correspond pas à l'adresse attendue.
3. **Décision** (`strategy.py`) : un score qui combine la tendance des prix et le regain d'attention sur X, avec une pénalité si le jeton est déjà en surchauffe.
4. **Exécution fictive** (`portfolio.py`) : aux vrais prix, en comptant les frais de pool (0,3 %), le gaz et l'impact estimé sur le prix.
5. **Référence** : chaque instantané se compare à « tout mettre en ETH au départ et ne rien faire ».

## Garde-fous (`config.py`)
| Règle | Valeur |
|---|---|
| Capital de départ | 50 USDC |
| Taille max d'une position | 20 % (10 USDC) |
| Positions ouvertes max | 3 |
| Stop-loss / prise de bénéfice | −15 % / +25 % |
| Arrêt total | sous 60 % du capital (30 USDC) |
| Ordres par jour | 6 max |
| Liquidité / volume 24 h minimum | 500 000 $ / 250 000 $ |
| Impact de prix max | 2 % |
| Jetons | liste blanche : WETH, cbBTC, AERO, VIRTUAL, DEGEN, BRETT |

## Utilisation
```bash
pip install -e ".[dev]"
pytest -q                              # 13 tests
python -m crypto_agent.cycle           # un cycle complet
python -m crypto_agent.mcp_server      # passerelle MCP (stdio)
```
Ajouter la passerelle à Claude Code :
```bash
claude mcp add crypto-agent -- python -m crypto_agent.mcp_server
```

Outils MCP : `market_snapshot`, `x_collect`, `x_search`, `x_attention`, `portfolio_status`, `paper_trade`, `run_cycle`, `wallet_balances` (lecture seule).

## Variables d'environnement
- `X_BEARER_TOKEN` : jeton de l'API X v2. Sans lui, l'attention sur X reste neutre.
- `BASE_RPC_URL` : facultatif, `https://mainnet.base.org` par défaut.
- `START_CAPITAL_USDC` : facultatif, 50 par défaut.

## Domaines réseau à autoriser
`api.dexscreener.com`, `mainnet.base.org`, `api.x.com`

## Passage à l'argent réel (étape 2)
Seulement si la simulation bat la référence ETH, frais compris, sur plusieurs semaines. Il faudra :
- revérifier les adresses de la liste blanche sur basescan.org ;
- un wallet dédié, alimenté par le propriétaire, dont la clé est stockée en secret d'environnement ;
- un module d'exécution séparé qui respecte les mêmes garde-fous.
