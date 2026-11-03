"""Prix, liquidité et volumes des jetons sur Base, via l'API publique DexScreener (sans clé)."""
from dataclasses import dataclass

import httpx

from .config import CHAIN_ID

API = "https://api.dexscreener.com"


@dataclass
class Quote:
    symbol: str
    address: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    change_1h: float      # en %
    change_24h: float     # en %
    pair_url: str


def pick_best_pair(pairs: list[dict], address: str) -> dict | None:
    """Garde la paire Base la plus liquide où le jeton est l'actif de base."""
    addr = address.lower()
    candidates = [
        p for p in pairs
        if p.get("chainId") == CHAIN_ID and p.get("baseToken", {}).get("address", "").lower() == addr
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


def parse_quote(symbol: str, address: str, pair: dict) -> Quote | None:
    returned = pair.get("baseToken", {}).get("symbol", "")
    if returned.upper() != symbol.upper():
        return None  # adresse incohérente avec le symbole attendu : on refuse
    return Quote(
        symbol=symbol,
        address=address,
        price_usd=float(pair["priceUsd"]),
        liquidity_usd=float((pair.get("liquidity") or {}).get("usd") or 0),
        volume_24h_usd=float((pair.get("volume") or {}).get("h24") or 0),
        change_1h=float((pair.get("priceChange") or {}).get("h1") or 0),
        change_24h=float((pair.get("priceChange") or {}).get("h24") or 0),
        pair_url=pair.get("url", ""),
    )


def fetch_quotes(tokens: dict[str, str], client: httpx.Client | None = None) -> dict[str, Quote]:
    """Une requête par jeton : l'API tronque la réponse à 30 paires, toutes chaînes confondues,
    donc une requête groupée fait disparaître les jetons les moins représentés (constaté le 03/11/2026)."""
    own = client is None
    client = client or httpx.Client(timeout=20)
    out: dict[str, Quote] = {}
    try:
        for sym, addr in tokens.items():
            r = client.get(f"{API}/latest/dex/tokens/{addr}")
            r.raise_for_status()
            pairs = r.json().get("pairs") or []
            pair = pick_best_pair(pairs, addr)
            q = parse_quote(sym, addr, pair) if pair else None
            if q:
                # Volume du jeton = somme de tous ses pools sur Base, pas seulement le principal.
                q.volume_24h_usd = sum(
                    float((p.get("volume") or {}).get("h24") or 0) for p in pairs
                    if p.get("chainId") == CHAIN_ID
                    and p.get("baseToken", {}).get("address", "").lower() == addr.lower()
                )
                out[sym] = q
    finally:
        if own:
            client.close()
    return out
