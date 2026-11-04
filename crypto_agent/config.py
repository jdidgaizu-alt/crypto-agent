"""Configuration : livres, garde-fous, coûts, budget X, liste blanche des jetons sur Base.

Les garde-fous sont des fractions du capital de départ ENREGISTRÉ dans la base
(table books), jamais d'une variable d'environnement : changer celle-ci après
coup ne peut pas désaligner les limites et le livre.
"""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAIN_ID = "base"
BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")

# Le livre (portefeuille, ordres, instantanés) est versionné dans le dépôt pour
# survivre aux sessions cloud éphémères. Les posts X, volumineux, ne le sont pas.
BOOK_DB_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", ROOT / "ledger" / "book.db"))
X_DB_PATH = Path(os.environ.get("CRYPTO_AGENT_X_DB", ROOT / "data" / "x.db"))

MIN_START_CAPITAL_USDC = 10.0   # en dessous, aucun ordre ne passerait (20 % du capital < ordre minimum de 2 USDC)


def start_capital() -> float:
    """Capital d'un livre neuf. Lu uniquement par `python -m crypto_agent.cycle --init`, au moment de créer le livre."""
    raw = os.environ.get("START_CAPITAL_USDC", "50")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"START_CAPITAL_USDC illisible : {raw!r}") from None
    if not value >= MIN_START_CAPITAL_USDC or value == float("inf"):
        raise ValueError(f"START_CAPITAL_USDC doit être un nombre d'au moins {MIN_START_CAPITAL_USDC:g} USDC")
    return value

BOOK_AUTO = "auto"      # stratégie systématique, exécutée par le cycle
BOOK_CLAUDE = "claude"  # décisions prises par Claude via la passerelle MCP
BOOKS = (BOOK_AUTO, BOOK_CLAUDE)

# Jetons autorisés. Chaque prix lu est contrôlé : si le symbole renvoyé par
# DexScreener ne correspond pas à l'adresse, le jeton est ignoré.
# À revérifier sur https://basescan.org avant tout passage en argent réel.
WHITELIST: dict[str, str] = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "VIRTUAL": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
    "DEGEN": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "BRETT": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
}
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
# Seules les paires cotées contre USDC, WETH ou ETH sont retenues. Un pool coté
# contre un jeton exotique peut afficher un prix ou une liquidité trompeurs, et
# le coût d'un achat financé en USDC n'y serait pas modélisable.
ALLOWED_QUOTES: dict[str, str] = {
    USDC.lower(): "USDC",
    WHITELIST["WETH"].lower(): "WETH",
    NATIVE_ETH: "ETH",
}

# Requêtes X par jeton (comptage horaire et collecte de posts).
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
    max_position_frac: float = 0.20          # 10 sur 50
    max_open_positions: int = 3
    kill_switch_frac: float = 0.60           # arrêt total et liquidation sous 30 sur 50
    stop_loss: float = -0.15
    take_profit: float = 0.25
    min_liquidity_usd: float = 500_000
    min_volume_24h_usd: float = 250_000
    max_entries_per_day: int = 6             # achats par jour ; les ventes ne sont ni comptées ni bloquées
    buy_threshold: float = 0.35
    exit_threshold: float = -0.30
    rebuy_cooldown_hours: float = 24         # pas de rachat d'un jeton vendu depuis moins de 24 h
    stale_quote_hours: float = 6             # au-delà, une position sans prix récent bloque les achats (risque inconnu)
    lost_quote_hours: float = 72             # au-delà, elle compte pour 0 dans le capital à risque
    min_cycle_interval_minutes: float = 30
    cash_reserve_usdc: float = 0.5
    min_order_usdc: float = 2.0


@dataclass(frozen=True)
class Costs:
    gas_usd: float = 0.02                    # coût d'une transaction sur Base, prudent
    unknown_pool_fee: float = 0.01           # frais supposés quand le palier du pool est inconnu (prudent)
    hop_fee: float = 0.0005                  # passage USDC <-> WETH quand la paire est cotée en WETH ou ETH
    max_price_impact: float = 0.02           # refus si l'impact estimé dépasse 2 %
    pool_fee_cache_days: float = 7


@dataclass(frozen=True)
class XBudget:
    """Barème pay-per-use publié par X en 2026 (à confirmer dans la console développeur) :
    0,005 $ par requête de comptage, 0,005 $ par post lu."""
    monthly_usd: float = float(os.environ.get("X_MONTHLY_BUDGET_USD", "5"))
    count_request_usd: float = 0.005
    post_read_usd: float = 0.005
    posts_per_symbol_per_cycle: int = int(os.environ.get("X_POSTS_PER_SYMBOL", "0") or 0)
    min_baseline_posts: float = 30.0         # sous ce nombre moyen de posts par tranche de 6 h, l'attention n'est pas mesurée
    max_posts_per_call: int = 100


RISK = Risk()
COSTS = Costs()
X_BUDGET = XBudget()

# Frais vérifiés à la main pour un pool donné (adresse en minuscules -> taux).
POOL_FEE_OVERRIDES: dict[str, float] = {}
