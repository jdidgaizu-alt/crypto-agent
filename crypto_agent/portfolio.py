"""Portefeuille fictif : exécute les ordres aux vrais prix en comptant frais, gaz et impact de prix."""
import sqlite3
from datetime import datetime, timezone

from .config import COSTS, RISK


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn: sqlite3.Connection, capital: float = RISK.start_capital_usdc) -> None:
    conn.execute("INSERT OR IGNORE INTO cash(id, usdc) VALUES (1, ?)", (capital,))
    conn.commit()


def cash(conn: sqlite3.Connection) -> float:
    return conn.execute("SELECT usdc FROM cash WHERE id = 1").fetchone()["usdc"]


def positions(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM positions")}


def price_impact(usdc: float, liquidity_usd: float) -> float:
    """Approximation d'un pool à produit constant : impact ≈ 2 × montant / liquidité totale."""
    if liquidity_usd <= 0:
        return 1.0
    return 2 * usdc / liquidity_usd


def equity(conn: sqlite3.Connection, prices: dict[str, float]) -> float:
    total = cash(conn)
    for sym, p in positions(conn).items():
        total += p["qty"] * prices.get(sym, p["cost_usdc"] / p["qty"])
    return total


def trades_today(conn: sqlite3.Connection) -> int:
    day = now_iso()[:10]
    return conn.execute("SELECT COUNT(*) FROM trades WHERE substr(ts,1,10) = ?", (day,)).fetchone()[0]


def buy(conn, symbol: str, usdc: float, price: float, liquidity_usd: float, reason: str) -> dict:
    impact = price_impact(usdc, liquidity_usd)
    if impact > COSTS.max_price_impact:
        raise ValueError(f"impact de prix estimé {impact:.2%} > {COSTS.max_price_impact:.0%}")
    available = cash(conn)
    if usdc + COSTS.gas_usd > available + 1e-9:
        raise ValueError(f"liquidités insuffisantes ({available:.2f} USDC)")
    fees = usdc * COSTS.dex_fee + COSTS.gas_usd
    exec_price = price * (1 + impact)
    qty = usdc * (1 - COSTS.dex_fee) / exec_price
    conn.execute("UPDATE cash SET usdc = usdc - ? WHERE id = 1", (usdc + COSTS.gas_usd,))
    conn.execute(
        "INSERT INTO positions(symbol, qty, cost_usdc, opened_at) VALUES (?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET qty = qty + excluded.qty, cost_usdc = cost_usdc + excluded.cost_usdc",
        (symbol, qty, usdc + COSTS.gas_usd, now_iso()),
    )
    conn.execute(
        "INSERT INTO trades(ts, symbol, side, qty, price, usdc, fees_usdc, reason) VALUES (?,?,?,?,?,?,?,?)",
        (now_iso(), symbol, "buy", qty, exec_price, usdc, fees, reason),
    )
    conn.commit()
    return {"symbol": symbol, "side": "buy", "qty": qty, "price": exec_price, "fees": fees}


def sell_all(conn, symbol: str, price: float, liquidity_usd: float, reason: str) -> dict:
    pos = positions(conn).get(symbol)
    if not pos:
        raise ValueError(f"aucune position sur {symbol}")
    gross = pos["qty"] * price
    impact = min(price_impact(gross, liquidity_usd), 0.5)
    exec_price = price * (1 - impact)
    proceeds = pos["qty"] * exec_price * (1 - COSTS.dex_fee) - COSTS.gas_usd
    fees = pos["qty"] * exec_price * COSTS.dex_fee + COSTS.gas_usd
    conn.execute("UPDATE cash SET usdc = usdc + ? WHERE id = 1", (proceeds,))
    conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
    conn.execute(
        "INSERT INTO trades(ts, symbol, side, qty, price, usdc, fees_usdc, reason) VALUES (?,?,?,?,?,?,?,?)",
        (now_iso(), symbol, "sell", pos["qty"], exec_price, proceeds, fees, reason),
    )
    conn.commit()
    return {"symbol": symbol, "side": "sell", "proceeds": proceeds, "pnl": proceeds - pos["cost_usdc"]}
