"""Configuration : capital, garde-fous, liste blanche des jetons sur Base.

Toutes les valeurs de risque sont exprimées en fraction du capital de départ,
pour rester valables si le montant change.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

CHAIN_ID = "base"
BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
DB_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", Path(__file__).resolve().parent.parent / "data" / "agent.db"))

# Jetons autorisés. Les adresses sont contrôlées à chaque lecture de prix :
# si le symbole renvoyé par DexScreener ne correspond pas, le jeton est ignoré.
# À revérifier sur https://basescan.org avant tout passage en argent réel.
WHITELIST: dict[str, str] = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "VIRTUAL": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
    "DEGEN": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "BRETT": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
}
QUOTE = ("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

# Mots-clés suivis sur X pour chaque jeton.
X_QUERIES: dict[str, str] = {
    "WETH": "($ETH OR ethereum) lang:en -is:retweet",
    "cbBTC": "($BTC OR bitcoin) lang:en -is:retweet",
    "AERO": "($AERO OR aerodrome) lang:en -is:retweet",
    "VIRTUAL": "($VIRTUAL OR \"virtuals protocol\") lang:en -is:retweet",
    "DEGEN": "$DEGEN lang:en -is:retweet",
    "BRETT": "$BRETT lang:en -is:retweet",
}


@dataclass(frozen=True)
class Risk:
    start_capital_usdc: float = float(os.environ.get("START_CAPITAL_USDC", "50"))
    max_position_frac: float = 0.20      # 10 sur 50
    max_open_positions: int = 3
    kill_switch_frac: float = 0.60       # arrêt total sous 30 sur 50
    stop_loss: float = -0.15             # sortie d'une position à -15 %
    take_profit: float = 0.25            # sortie à +25 %
    min_liquidity_usd: float = 500_000
    min_volume_24h_usd: float = 250_000
    max_trades_per_day: int = 6


@dataclass(frozen=True)
class Costs:
    dex_fee: float = 0.003               # frais de pool typiques (0,3 %)
    gas_usd: float = 0.02                # coût d'une transaction sur Base, prudent
    max_price_impact: float = 0.02       # refus si l'impact estimé dépasse 2 %


RISK = Risk()
COSTS = Costs()
