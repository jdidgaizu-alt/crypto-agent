"""Un cycle : attention X, prix, frais, puis pour chaque livre décisions, ordres fictifs et instantané.

- Livre « auto » : la stratégie complète.
- Livre « claude » : protections seules (stop-loss, arrêt total et liquidation). Claude
  y décide de ses achats via MCP ; le cycle ne prend jamais d'initiative à sa place.

Les décisions et les ordres d'un cycle forment UNE transaction : deux cycles lancés en même
temps ne peuvent pas acheter deux fois le même jeton ni vendre deux fois la même position.
Un verrou empêche en plus deux cycles de se chevaucher, et un intervalle minimum évite de
multiplier les ordres en relançant le cycle.

Ligne de commande :
    python -m crypto_agent.cycle --init              crée les livres (START_CAPITAL_USDC, 50 par défaut)
    python -m crypto_agent.cycle                     un cycle
    python -m crypto_agent.cycle --report            état des livres, sans réseau
    python -m crypto_agent.cycle --reset-halt auto   lève l'arrêt d'urgence d'un livre (décision humaine)
"""
import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from . import fees, market, portfolio, strategy, xsource
from .config import (BOOK_AUTO, BOOK_CLAUDE, BOOK_DB_PATH, BOOKS, DEFAULT_START_CAPITAL_USDC, RISK, WHITELIST,
                     X_BUDGET, X_DB_PATH, X_QUERIES)
from .db import open_book, open_x, write_tx
from .market import Quote

MODES = {BOOK_AUTO: "full", BOOK_CLAUDE: "guard"}
LOCK = "cycle"


def _run_book(conn: sqlite3.Connection, book: str, mode: str, quotes: dict[str, Quote],
              attention: dict[str, float | None], now: datetime, errors: list[str]) -> dict:
    """Décisions et ordres d'un livre, dans la transaction ouverte par run()."""
    out: dict = {"mode": mode, "decisions": []}
    portfolio.mark_prices(conn, book, quotes, now)
    weth = quotes.get("WETH")
    if weth:
        portfolio.ensure_benchmark(conn, book, weth.price_usd, now)
    b = portfolio.get_book(conn, book)
    if b["bench_weth_qty"] is None:
        out["skipped"] = "pas encore de prix WETH : référence non fixée, aucun ordre"
        decisions = []
    else:
        val = portfolio.value(conn, book, now)
        decisions = strategy.decide(
            quotes=quotes, attention=attention, positions=val.positions, cash=val.cash,
            risk_equity=val.risk_equity, capital=b["capital_usdc"],
            trades_today=portfolio.trades_today(conn, book, now), halted=b["halted_reason"],
            recently_sold=portfolio.recently_sold(conn, book, now), mode=mode,
        )
    for d in decisions:
        entry = {"action": d.action, "symbol": d.symbol, "usdc": d.usdc, "reason": d.reason}
        try:
            if d.action == "buy":
                entry["result"] = portfolio.buy(conn, book, quotes[d.symbol], d.usdc, d.reason, now)
            elif d.action == "sell":
                entry["result"] = portfolio.sell_all(conn, book, quotes[d.symbol], d.reason, now)
            elif d.action == "halt":
                portfolio.halt(conn, book, d.reason, now)
        except portfolio.Refused as e:
            entry["refused"] = str(e)
        out["decisions"].append(entry)
    val = portfolio.value(conn, book, now)
    bench = portfolio.benchmark_value(conn, book, weth.price_usd if weth else None)
    portfolio.snapshot(conn, book, now, val, bench,
                       {"mode": mode, "decisions": out["decisions"], "errors": errors[:30]})
    out.update(equity_usdc=round(val.equity, 4), risk_equity_usdc=round(val.risk_equity, 4),
               benchmark_usdc=None if bench is None else round(bench, 4), cash_usdc=round(val.cash, 4),
               stale=val.stale, positions=val.positions,
               halted=portfolio.get_book(conn, book)["halted_reason"])
    return out


def run(book_conn: sqlite3.Connection, x_conn: sqlite3.Connection, *, force: bool = False,
        now: datetime | None = None, http: httpx.Client | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    log: dict = {"ts": portfolio.iso(now), "x": {}, "books": {}, "errors": []}
    portfolio.get_book(book_conn, BOOK_AUTO)  # échoue clairement si la base n'est pas initialisée
    holder = uuid.uuid4().hex
    if not portfolio.acquire_lock(book_conn, LOCK, holder, now):
        log["skipped"] = "un autre cycle est en cours"
        return log
    own = http is None
    http = http or httpx.Client(timeout=20)
    try:
        last = portfolio.last_snapshot_at(book_conn, BOOK_AUTO)
        if not force and last and now - last < timedelta(minutes=RISK.min_cycle_interval_minutes):
            log["skipped"] = f"dernier cycle il y a moins de {RISK.min_cycle_interval_minutes:g} min"
            return log

        # 1. Attention X (comptages exacts) et, si demandé, collecte de posts pour la recherche.
        attention: dict[str, float | None] = {}
        if xsource.configured():
            for sym, query in X_QUERIES.items():
                try:
                    xsource.fetch_counts(x_conn, book_conn, sym, query, http, now)
                    if X_BUDGET.posts_per_symbol_per_cycle >= 10:
                        xsource.collect_posts(x_conn, book_conn, sym, query,
                                              X_BUDGET.posts_per_symbol_per_cycle, http, now)
                except (xsource.XError, ValueError) as e:
                    log["errors"].append(f"X {sym} : {e}")
                a = xsource.attention(x_conn, sym, now)
                attention[sym] = a["ratio"]
                log["x"][sym] = a
        else:
            log["errors"].append("X non configuré (X_BEARER_TOKEN absent) : attention non mesurée, ignorée")

        # 2. Prix et frais réels des pools.
        quotes, price_errors = market.fetch_quotes(WHITELIST, http)
        for sym, e in price_errors.items():
            log["errors"].append(f"prix {sym} : {e}")
        log["fees"] = fees.resolve(book_conn, quotes, http, now)

        # 3. Décisions et ordres de chaque livre, atomiques.
        with write_tx(book_conn):
            for book, mode in MODES.items():
                if book_conn.execute("SELECT 1 FROM books WHERE name = ?", (book,)).fetchone():
                    log["books"][book] = _run_book(book_conn, book, mode, quotes, attention, now, log["errors"])
        log["x_spent_month_usd"] = round(xsource.spent_this_month(book_conn, now), 4)
        return log
    finally:
        if own:
            http.close()
        portfolio.release_lock(book_conn, LOCK, holder)


def report(book_conn: sqlite3.Connection) -> dict:
    """État des livres d'après le dernier instantané, sans appel réseau."""
    out = {}
    for b in book_conn.execute("SELECT * FROM books ORDER BY name").fetchall():
        snap = book_conn.execute("SELECT ts, equity_usdc, risk_equity_usdc, benchmark_usdc, stale FROM snapshots "
                                 "WHERE book = ? ORDER BY ts DESC, id DESC LIMIT 1", (b["name"],)).fetchone()
        n = book_conn.execute("SELECT COUNT(*) FROM trades WHERE book = ?", (b["name"],)).fetchone()[0]
        out[b["name"]] = {
            "capital_usdc": b["capital_usdc"], "cash_usdc": round(b["cash_usdc"], 4),
            "halted": b["halted_reason"], "trades": n,
            "positions": sorted(portfolio.positions(book_conn, b["name"])),
            "last_snapshot": dict(snap) if snap else None,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m crypto_agent.cycle")
    p.add_argument("--init", action="store_true", help="crée la base et les livres")
    p.add_argument("--report", action="store_true", help="état des livres, sans réseau")
    p.add_argument("--reset-halt", metavar="LIVRE", choices=BOOKS, help="lève l'arrêt d'urgence d'un livre")
    p.add_argument("--force", action="store_true", help="ignore l'intervalle minimum entre deux cycles")
    args = p.parse_args(argv)

    if args.init:
        conn = open_book(BOOK_DB_PATH, create=True)
        for name in BOOKS:
            try:
                portfolio.create_book(conn, name, DEFAULT_START_CAPITAL_USDC)
                print(f"livre « {name} » créé avec {DEFAULT_START_CAPITAL_USDC:g} USDC")
            except portfolio.Refused as e:
                print(e)
        print(f"base : {BOOK_DB_PATH}")
        return 0
    conn = open_book(BOOK_DB_PATH)
    if args.reset_halt:
        ok = portfolio.reset_halt(conn, args.reset_halt)
        print("arrêt levé" if ok else "ce livre n'était pas à l'arrêt")
        return 0
    if args.report:
        print(json.dumps(report(conn), indent=2, ensure_ascii=False, default=str))
        return 0
    x = open_x(X_DB_PATH)
    print(json.dumps(run(conn, x, force=args.force), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
