"""Un cycle complet : collecte X, prix, décisions, exécution fictive, instantané comparé au « garder de l'ETH »."""
import json
import os
import sqlite3

from . import portfolio, strategy, xsource
from .config import DB_PATH, WHITELIST, X_QUERIES
from .db import connect
from .market import fetch_quotes


def benchmark_value(conn: sqlite3.Connection, weth_price: float) -> float:
    """Référence : tout le capital de départ placé en ETH au premier cycle, sans plus y toucher.
    Doit être appelée avant toute exécution d'ordre du premier cycle."""
    row = conn.execute("SELECT weth_qty FROM benchmark WHERE id = 1").fetchone()
    if row is None:
        start = portfolio.equity(conn, {"WETH": weth_price})
        conn.execute("INSERT INTO benchmark(id, weth_qty, ts) VALUES (1, ?, ?)",
                     (start / weth_price, portfolio.now_iso()))
        conn.commit()
        return start
    return row["weth_qty"] * weth_price


def run(conn: sqlite3.Connection | None = None, collect_x: bool = True) -> dict:
    conn = conn or connect(DB_PATH)
    portfolio.init(conn)
    log: dict = {"x": {}, "decisions": [], "errors": []}

    if collect_x and os.environ.get("X_BEARER_TOKEN"):
        for sym, q in X_QUERIES.items():
            try:
                log["x"][sym] = xsource.collect(conn, sym, q)
            except Exception as e:  # une source en panne ne doit pas bloquer le cycle
                log["errors"].append(f"X {sym}: {e}")
    elif collect_x:
        log["errors"].append("X non collecté : X_BEARER_TOKEN absent (attention neutre = 1)")

    quotes = fetch_quotes(WHITELIST)
    prices = {s: q.price_usd for s, q in quotes.items()}
    attention = {s: xsource.attention(conn, s)["ratio"] for s in quotes}
    eq = portfolio.equity(conn, prices)
    if "WETH" in prices:
        benchmark_value(conn, prices["WETH"])  # fixe la référence avant tout ordre

    decisions =strategy.decide(quotes, attention, portfolio.positions(conn), portfolio.cash(conn),
                                eq, portfolio.trades_today(conn))
    for d in decisions:
        entry = {"action": d.action, "symbol": d.symbol, "usdc": d.usdc, "reason": d.reason}
        try:
            if d.action == "buy":
                q = quotes[d.symbol]
                entry["result"] = portfolio.buy(conn, d.symbol, d.usdc, q.price_usd, q.liquidity_usd, d.reason)
            elif d.action == "sell":
                q = quotes[d.symbol]
                entry["result"] = portfolio.sell_all(conn, d.symbol, q.price_usd, q.liquidity_usd, d.reason)
        except ValueError as e:
            entry["refused"] = str(e)
        log["decisions"].append(entry)

    eq = portfolio.equity(conn, prices)
    bench = benchmark_value(conn, prices["WETH"]) if "WETH" in prices else None
    conn.execute("INSERT INTO snapshots(ts, equity_usdc, benchmark_usdc, note) VALUES (?,?,?,?)",
                 (portfolio.now_iso(), eq, bench, json.dumps(log, ensure_ascii=False)[:4000]))
    conn.commit()
    log.update(equity_usdc=round(eq, 4), benchmark_usdc=bench and round(bench, 4),
               cash_usdc=round(portfolio.cash(conn), 4), positions=portfolio.positions(conn))
    return log


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False, default=str))
