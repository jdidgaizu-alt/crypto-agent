"""Prix, liquidité et volumes des jetons sur Base, via l'API publique DexScreener (sans clé)."""
import math
import time
from dataclasses import dataclass

import httpx

from .config import ALLOWED_QUOTES, CHAIN_ID

API = "https://api.dexscreener.com"


@dataclass
class Quote:
    symbol: str
    address: str
    price_usd: float
    liquidity_usd: float      # liquidité du pool retenu (celui où l'ordre serait exécuté)
    volume_24h_usd: float     # volume du jeton sur tous ses pools Base cotés en USDC/WETH/ETH
    change_1h: float          # en %
    change_24h: float         # en %
    pair_url: str
    pair_address: str
    dex_id: str
    quote_symbol: str         # USDC, WETH ou ETH
    fee_rate: float | None = None   # palier du pool, renseigné par fees.resolve


def _num(x) -> float | None:
    """Nombre fini, ou None si absent ou illisible."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _is_candidate(p: dict, address: str) -> bool:
    if not isinstance(p, dict) or p.get("chainId") != CHAIN_ID:
        return False
    base = p.get("baseToken") or {}
    quote = p.get("quoteToken") or {}
    if str(base.get("address", "")).lower() != address.lower():
        return False
    if str(quote.get("address", "")).lower() not in ALLOWED_QUOTES:
        return False
    price = _num(p.get("priceUsd"))
    return price is not None and price > 0


def _liquidity(p: dict) -> float:
    return _num((p.get("liquidity") or {}).get("usd")) or 0.0


def pick_best_pair(pairs: list, address: str) -> dict | None:
    """La paire Base la plus liquide où le jeton est l'actif de base, cotée en USDC, WETH ou ETH."""
    candidates = [p for p in pairs or [] if _is_candidate(p, address)]
    return max(candidates, key=_liquidity) if candidates else None


def parse_quote(symbol: str, address: str, pair: dict, pairs: list) -> Quote | None:
    base = pair.get("baseToken") or {}
    if str(base.get("symbol", "")).upper() != symbol.upper():
        return None  # adresse incohérente avec le symbole attendu : on refuse
    change = pair.get("priceChange") or {}
    volume = sum(
        _num((p.get("volume") or {}).get("h24")) or 0.0 for p in pairs if _is_candidate(p, address)
    )
    return Quote(
        symbol=symbol,
        address=address,
        price_usd=_num(pair.get("priceUsd")),
        liquidity_usd=_liquidity(pair),
        volume_24h_usd=volume,
        change_1h=_num(change.get("h1")) or 0.0,
        change_24h=_num(change.get("h24")) or 0.0,
        pair_url=str(pair.get("url", "")),
        pair_address=str(pair.get("pairAddress", "")),
        dex_id=str(pair.get("dexId", "")),
        quote_symbol=ALLOWED_QUOTES[str((pair.get("quoteToken") or {}).get("address", "")).lower()],
    )


def _get_pairs(client: httpx.Client, address: str, retries: int, backoff: float) -> list:
    for attempt in range(retries + 1):
        r = client.get(f"{API}/latest/dex/tokens/{address}")
        if (r.status_code == 429 or r.status_code >= 500) and attempt < retries:
            time.sleep(backoff * (attempt + 1))
            continue
        r.raise_for_status()
        pairs = r.json().get("pairs")
        return pairs if isinstance(pairs, list) else []
    return []  # inatteignable


def fetch_quotes(tokens: dict[str, str], client: httpx.Client | None = None,
                 retries: int = 1, backoff: float = 1.0) -> tuple[dict[str, Quote], dict[str, str]]:
    """Une requête par jeton : l'API tronque sa réponse à 30 paires, toutes chaînes confondues,
    donc une requête groupée fait disparaître des jetons (constaté le 03/11/2026).
    Un jeton en erreur n'empêche pas de coter les autres : il figure dans le second dictionnaire."""
    own = client is None
    client = client or httpx.Client(timeout=20)
    quotes: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    try:
        for sym, addr in tokens.items():
            try:
                pairs = _get_pairs(client, addr, retries, backoff)
            except httpx.HTTPStatusError as e:
                errors[sym] = f"DexScreener a répondu {e.response.status_code}"
                continue
            except (httpx.HTTPError, ValueError) as e:
                errors[sym] = f"DexScreener injoignable ou illisible ({type(e).__name__})"
                continue
            pair = pick_best_pair(pairs, addr)
            if pair is None:
                errors[sym] = "aucune paire Base cotée en USDC/WETH/ETH avec un prix"
                continue
            q = parse_quote(sym, addr, pair, pairs)
            if q is None:
                errors[sym] = "symbole renvoyé différent du symbole attendu"
                continue
            quotes[sym] = q
    finally:
        if own:
            client.close()
    return quotes, errors
