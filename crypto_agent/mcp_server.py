"""Passerelle MCP : prix sur Base, attention et posts X, livres de simulation.

Lancement (stdio) : python -m crypto_agent.mcp_server
Aucun outil ne peut dépenser d'argent réel, contourner un garde-fou ni lever un arrêt d'urgence.
Les ordres de Claude vont dans le livre « claude », séparé de la stratégie automatique.
"""
import functools
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import chain, config, cycle, fees, market, portfolio, strategy, xsource
from .config import BOOK_CLAUDE, BOOKS, USDC, WHITELIST, X_BUDGET, X_QUERIES
from .db import open_book, open_x, write_tx

server = MCPServer("crypto-agent", instructions=(
    "Simulation de trading sur la blockchain Base. Prix des jetons autorisés, attention mesurée sur X, "
    "recherche dans les posts X collectés, et deux livres FICTIFS : « auto » (stratégie systématique) et "
    "« claude » (tes décisions, via paper_trade). Aucun ordre réel n'est possible. Lire des posts X "
    "coûte de l'argent réel (budget mensuel plafonné) : x_collect seulement quand c'est utile."
))

# Les URL de requêtes ne doivent pas finir dans les journaux (une URL RPC privée contient sa clé).
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

_SYMBOLS = {s.lower(): s for s in WHITELIST}
_EXPECTED = (ValueError, LookupError, RuntimeError, OSError, sqlite3.Error)


def http_client() -> httpx.Client:
    """Client HTTP des outils (remplacé dans les tests)."""
    return httpx.Client(timeout=20)


@contextmanager
def _book_db():
    conn = open_book(config.BOOK_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _x_db():
    conn = open_x(config.X_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _tool(description: str):
    """Enregistre un outil dont les refus attendus remontent à Claude avec leur raison."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except httpx.HTTPError as e:
                raise ToolError(f"erreur réseau ({type(e).__name__})") from None
            except _EXPECTED as e:
                raise ToolError(str(e)[:400] or type(e).__name__) from None
        return server.tool(description=description)(wrapper)
    return deco


def _symbol(s: str) -> str:
    sym = _SYMBOLS.get(str(s).strip().lstrip("$").lower())
    if not sym:
        raise ToolError(f"jeton non autorisé : {s!r}. Choix : {', '.join(WHITELIST)}")
    return sym


@_tool("Prix, liquidité, volume, variations 1 h / 24 h et pool retenu pour chaque jeton autorisé sur Base.")
def market_snapshot() -> dict:
    with http_client() as c:
        quotes, errors = market.fetch_quotes(WHITELIST, c)
    return {"quotes": [asdict(q) for q in quotes.values()], "errors": errors}


@_tool("Attention sur X pour un jeton : posts des 6 dernières heures / moyenne des 24 h précédentes. "
       "ratio = None quand la mesure est incomplète. Lit les comptages déjà récupérés (aucun coût).")
def x_attention(symbol: str) -> dict:
    sym = _symbol(symbol)
    with _x_db() as x:
        return xsource.attention(x, sym)


@_tool("Récupère les comptages horaires X d'un jeton et lit jusqu'à max_posts posts nouveaux (10 à 100) "
       "pour la recherche. COÛTE de l'argent réel : 0,005 $ par post lu, dans la limite du budget mensuel.")
def x_collect(symbol: str, max_posts: int = 20) -> dict:
    sym = _symbol(symbol)
    with _book_db() as book, _x_db() as x, http_client() as c:
        hours = xsource.fetch_counts(x, book, sym, X_QUERIES[sym], c)
        res = xsource.collect_posts(x, book, sym, X_QUERIES[sym], max_posts, c)
        res.update(hours_counted=hours, attention=xsource.attention(x, sym),
                   spent_month_usd=round(xsource.spent_this_month(book), 4), budget_month_usd=X_BUDGET.monthly_usd)
        return res


@_tool("Recherche en langage naturel dans les posts X déjà collectés (aucun coût). "
       "symbol filtre sur un jeton ; limit entre 1 et 100.")
def x_search(query: str, symbol: str | None = None, limit: int = 20) -> list[dict]:
    sym = _symbol(symbol) if symbol else None
    with _x_db() as x:
        return xsource.search(x, query, sym, limit)


@_tool("Dépenses X du mois en cours et budget mensuel.")
def x_budget() -> dict:
    with _book_db() as book:
        return {"spent_month_usd": round(xsource.spent_this_month(book), 4), "budget_month_usd": X_BUDGET.monthly_usd}


@_tool("État des deux livres fictifs aux prix actuels : liquidités, positions, valeur, "
       "comparaison avec « tout le capital de départ en ETH », arrêt éventuel, derniers ordres.")
def portfolio_status() -> dict:
    with _book_db() as book:
        held = {r[0] for r in book.execute("SELECT DISTINCT symbol FROM positions")} | {"WETH"}
        with http_client() as c:
            quotes, errors = market.fetch_quotes({s: WHITELIST[s] for s in held}, c)
        live = {s: q.price_usd for s, q in quotes.items()}
        out: dict = {"errors": errors, "books": {}}
        for name in BOOKS:
            b = portfolio.get_book(book, name)
            val = portfolio.value(book, name, live=live)
            bench = portfolio.benchmark_value(book, name, live.get("WETH"))
            trades = [dict(r) for r in book.execute(
                "SELECT ts, symbol, side, qty, price, usdc, fees_usdc, reason FROM trades WHERE book = ? "
                "ORDER BY id DESC LIMIT 10", (name,))]
            out["books"][name] = {
                "capital_usdc": b["capital_usdc"], "cash_usdc": val.cash, "equity_usdc": val.equity,
                "risk_equity_usdc": val.risk_equity, "benchmark_usdc": bench,
                "vs_benchmark_usdc": None if bench is None else val.equity - bench,
                "halted": b["halted_reason"], "stale": val.stale, "positions": val.positions,
                "recent_trades": trades,
            }
        return out


@_tool("Ordre FICTIF dans le livre « claude ». side='buy' avec usdc (montant), ou side='sell' "
       "(vend toute la position). reason obligatoire. Mêmes garde-fous que la stratégie : taille max, "
       "nombre de positions, plafond quotidien, liquidité, arrêt d'urgence.")
def paper_trade(symbol: str, side: Literal["buy", "sell"], reason: str, usdc: float | None = None) -> dict:
    sym = _symbol(symbol)
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ToolError("donnez la raison de l'ordre (5 caractères minimum)")
    now = portfolio.utcnow()
    with _book_db() as book, http_client() as c:
        quotes, errors = market.fetch_quotes({sym: WHITELIST[sym], "WETH": WHITELIST["WETH"]}, c)
        if sym not in quotes:
            raise ToolError(f"pas de cotation fiable pour {sym} : {errors.get(sym, 'inconnue')}")
        fees.resolve(book, quotes, c, now)
        q = quotes[sym]
        refusal = None
        with write_tx(book):
            portfolio.mark_prices(book, BOOK_CLAUDE, quotes, now)
            if "WETH" in quotes:
                portfolio.ensure_benchmark(book, BOOK_CLAUDE, quotes["WETH"].price_usd, now)
            b = portfolio.get_book(book, BOOK_CLAUDE)
            if b["bench_weth_qty"] is None:
                raise portfolio.Refused("pas de prix WETH : la référence ETH n'est pas fixée, ordre refusé")
            if side == "buy":
                amount = portfolio.check_amount(usdc)
                val = portfolio.value(book, BOOK_CLAUDE, now)
                pos = val.positions.get(sym)
                why = strategy.entry_refusal(
                    q=q, usdc=amount, capital=b["capital_usdc"], cash=val.cash,
                    held_cost=pos["cost_usdc"] if pos else 0.0, open_count=len(val.positions),
                    trades_count=portfolio.trades_today(book, BOOK_CLAUDE, now), halted=b["halted_reason"],
                    risk_equity=val.risk_equity, recently_sold=portfolio.recently_sold(book, BOOK_CLAUDE, now))
                if why:
                    # L'arrêt doit survivre au refus : on valide la transaction, puis on refuse.
                    if not b["halted_reason"] and strategy.kill_switch(val.risk_equity, b["capital_usdc"]):
                        portfolio.halt(book, BOOK_CLAUDE, why, now)
                    refusal = f"achat refusé : {why}"
                else:
                    result = portfolio.buy(book, BOOK_CLAUDE, q, amount, f"[claude] {reason}", now)
            elif side == "sell":
                result = portfolio.sell_all(book, BOOK_CLAUDE, q, f"[claude] {reason}", now)
            else:
                raise portfolio.Refused("side doit valoir 'buy' ou 'sell'")
            if refusal is None:
                val = portfolio.value(book, BOOK_CLAUDE, now)
                weth = quotes.get("WETH")
                portfolio.snapshot(book, BOOK_CLAUDE, now, val,
                                   portfolio.benchmark_value(book, BOOK_CLAUDE, weth.price_usd if weth else None),
                                   {"mode": "manual", "decisions": [result], "errors": list(errors.values())})
        if refusal:
            raise ToolError(refusal)
        return {"result": result, "cash_usdc": val.cash, "equity_usdc": val.equity}


@_tool("Lance un cycle : comptages X, prix, protections et stratégie automatique, instantanés. "
       "Refusé si le dernier cycle date de moins de 30 minutes.")
def run_cycle() -> dict:
    with _book_db() as book, _x_db() as x, http_client() as c:
        return cycle.run(book, x, http=c)


@_tool("Soldes réels (lecture seule) d'un wallet sur Base : ETH et USDC.")
def wallet_balances(address: str) -> dict:
    chain.check_address(address)
    with http_client() as c:
        return {"ETH": chain.eth_balance(address, c), "USDC": chain.erc20_balance(USDC, address, c),
                "block": chain.block_number(c)}


if __name__ == "__main__":
    server.run()
