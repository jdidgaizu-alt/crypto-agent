"""Passerelle MCP : donne à Claude l'accès aux prix sur Base, aux posts X indexés et au portefeuille fictif.

Lancement (stdio) : python -m crypto_agent.mcp_server
Aucun outil ne peut dépenser d'argent réel à l'étape 1.
"""
from dataclasses import asdict

import httpx
from mcp.server.mcpserver import MCPServer

from . import chain, cycle, portfolio, xsource
from .config import DB_PATH, QUOTE, WHITELIST, X_QUERIES
from .db import connect
from .market import fetch_quotes

server = MCPServer("crypto-agent", instructions=(
    "Prix des jetons autorisés sur Base, posts X indexés, portefeuille FICTIF de trading. "
    "Aucun ordre réel n'est possible : tout passe par le portefeuille simulé."
))


def _conn():
    conn = connect(DB_PATH)
    portfolio.init(conn)
    return conn


@server.tool(description="Prix, liquidité, volume et variations 1 h / 24 h des jetons autorisés sur Base.")
def market_snapshot() -> list[dict]:
    return [asdict(q) for q in fetch_quotes(WHITELIST).values()]


@server.tool(description="Collecte les posts X récents pour un jeton autorisé et les indexe. Renvoie le nombre de nouveaux posts.")
def x_collect(symbol: str) -> int:
    if symbol not in X_QUERIES:
        raise ValueError(f"jeton non autorisé : {symbol}")
    return xsource.collect(_conn(), symbol, X_QUERIES[symbol])


@server.tool(description="Recherche plein texte (syntaxe FTS5) dans les posts X déjà collectés.")
def x_search(query: str, limit: int = 20) -> list[dict]:
    return xsource.search(_conn(), query, limit)


@server.tool(description="Attention récente sur X pour un jeton : ratio des 6 dernières heures sur la moyenne précédente.")
def x_attention(symbol: str) -> dict:
    return xsource.attention(_conn(), symbol)


@server.tool(description="État du portefeuille fictif : liquidités, positions, valeur, comparaison avec « garder de l'ETH ».")
def portfolio_status() -> dict:
    conn = _conn()
    prices = {s: q.price_usd for s, q in fetch_quotes(WHITELIST).items()}
    last = conn.execute("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20")]
    return {"cash_usdc": portfolio.cash(conn), "positions": portfolio.positions(conn),
            "equity_usdc": portfolio.equity(conn, prices),
            "benchmark_usdc": last["benchmark_usdc"] if last else None, "recent_trades": trades}


@server.tool(description="Ordre FICTIF. side = 'buy' (usdc = montant) ou 'sell' (vend toute la position). reason obligatoire.")
def paper_trade(symbol: str, side: str, reason: str, usdc: float = 0.0) -> dict:
    if symbol not in WHITELIST:
        raise ValueError(f"jeton non autorisé : {symbol}")
    q = fetch_quotes({symbol: WHITELIST[symbol]})[symbol]
    conn = _conn()
    if side == "buy":
        return portfolio.buy(conn, symbol, usdc, q.price_usd, q.liquidity_usd, f"[manuel] {reason}")
    if side == "sell":
        return portfolio.sell_all(conn, symbol, q.price_usd, q.liquidity_usd, f"[manuel] {reason}")
    raise ValueError("side doit valoir 'buy' ou 'sell'")


@server.tool(description="Lance un cycle automatique complet (collecte X, prix, décisions, exécution fictive).")
def run_cycle() -> dict:
    return cycle.run(_conn())


@server.tool(description="Soldes réels (lecture seule) d'un wallet sur Base : ETH et USDC.")
def wallet_balances(address: str) -> dict:
    with httpx.Client(timeout=20) as c:
        return {"ETH": chain.eth_balance(address, c), "USDC": chain.erc20_balance(QUOTE[1], address, c),
                "block": chain.block_number(c)}


if __name__ == "__main__":
    server.run()
