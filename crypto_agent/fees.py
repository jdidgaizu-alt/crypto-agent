"""Frais réels d'un ordre : palier du pool + passage USDC <-> WETH quand la paire n'est pas cotée en USDC.

Le palier est lu sur la blockchain quand c'est possible (pools à liquidité concentrée),
mis en cache une semaine, et sinon remplacé par une valeur prudente (1 %) : sous-estimer
les frais rendrait la simulation plus belle que la réalité.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import chain
from .config import COSTS, POOL_FEE_OVERRIDES
from .db import write_tx
from .market import Quote


def total_fee_rate(q: Quote) -> float:
    pool = q.fee_rate if q.fee_rate is not None else COSTS.unknown_pool_fee
    hop = COSTS.hop_fee if q.quote_symbol in ("WETH", "ETH") else 0.0
    return pool + hop


def resolve(conn: sqlite3.Connection, quotes: dict[str, Quote], client: httpx.Client,
            now: datetime | None = None) -> dict[str, str]:
    """Renseigne q.fee_rate pour chaque cotation. Renvoie la source retenue par jeton."""
    now = now or datetime.now(timezone.utc)
    fresh_after = (now - timedelta(days=COSTS.pool_fee_cache_days)).isoformat(timespec="seconds")
    sources: dict[str, str] = {}
    rpc_down = False
    for sym, q in quotes.items():
        pool = q.pair_address.lower()
        if pool in POOL_FEE_OVERRIDES:
            q.fee_rate, sources[sym] = POOL_FEE_OVERRIDES[pool], "vérifié à la main"
            continue
        row = conn.execute("SELECT fee, source FROM pool_fees WHERE pool = ? AND checked_at >= ?",
                           (pool, fresh_after)).fetchone()
        if row:
            q.fee_rate, sources[sym] = row["fee"], row["source"]
            continue
        if not rpc_down and chain.ADDRESS.match(q.pair_address):
            try:
                fee = chain.pool_fee(q.pair_address, client)
            except chain.RPCUnavailable:
                rpc_down = True  # inutile d'interroger le nœud pour les jetons suivants
            except Exception:  # noqa: BLE001 — pas un pool à liquidité concentrée, ou réponse inattendue
                pass           # valeur prudente
            else:
                with write_tx(conn):
                    conn.execute(
                        "INSERT OR REPLACE INTO pool_fees(pool, fee, source, checked_at) VALUES (?,?,?,?)",
                        (pool, fee, "blockchain", now.isoformat(timespec="seconds")),
                    )
                q.fee_rate, sources[sym] = fee, "blockchain"
                continue
        q.fee_rate, sources[sym] = None, f"inconnu : {COSTS.unknown_pool_fee:.1%} supposé"
    return sources
