"""Passerelle MCP, appelée comme le ferait Claude (validation des arguments comprise)."""
import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from crypto_agent import config, db, mcp_server, portfolio

from conftest import all_flat


@pytest.fixture
def gateway(monkeypatch, book_path, tmp_path, net):
    monkeypatch.setattr(config, "BOOK_DB_PATH", book_path)
    monkeypatch.setattr(config, "X_DB_PATH", tmp_path / "x.db")
    monkeypatch.setattr(mcp_server, "http_client", net.client)
    all_flat(net, WETH=dict(price=2500.0))
    return net


def call(name, **args):
    """Résultat tel que Claude le reçoit : du JSON dans le contenu texte."""
    res = asyncio.run(mcp_server.server.call_tool(name, args))
    assert not res.is_error
    if res.structured_content is not None:
        return res.structured_content.get("result", res.structured_content)
    return json.loads("".join(c.text for c in res.content))


def refused(name, **args) -> str:
    with pytest.raises(ToolError) as e:
        call(name, **args)
    return str(e.value)


def test_every_refusal_reaches_claude_with_its_reason(gateway):
    assert "montant manquant" in refused("paper_trade", symbol="AERO", side="buy", reason="test d'achat")
    assert "positif" in refused("paper_trade", symbol="AERO", side="buy", reason="test d'achat", usdc=-100)
    assert "position max" in refused("paper_trade", symbol="AERO", side="buy", reason="test d'achat", usdc=25)
    assert "aucune position" in refused("paper_trade", symbol="AERO", side="sell", reason="test de vente")
    assert "non autorisé" in refused("paper_trade", symbol="PEPE", side="buy", reason="test d'achat", usdc=5)
    assert "raison" in refused("paper_trade", symbol="AERO", side="buy", reason="", usdc=5)
    assert refused("paper_trade", symbol="AERO", side="short", reason="test d'achat", usdc=5)   # schéma
    assert "non autorisé" in refused("x_attention", symbol="ETH")
    assert "limit" in refused("x_search", query="base", limit=-1)
    assert "adresse invalide" in refused("wallet_balances", address="0x123")


def test_manual_trades_go_to_the_claude_book_through_every_guard(gateway, book_path):
    r = call("paper_trade", symbol="$aero", side="buy", reason="regain d'attention sur X", usdc=10)
    assert r["result"]["symbol"] == "AERO"
    for s in ("BRETT", "DEGEN"):
        call("paper_trade", symbol=s, side="buy", reason="test de remplissage", usdc=10)
    assert "positions ouvertes" in refused("paper_trade", symbol="VIRTUAL", side="buy", reason="4e position", usdc=5)
    assert "position max" in refused("paper_trade", symbol="AERO", side="buy", reason="renfort trop gros", usdc=2)
    conn = db.open_book(book_path)
    assert set(portfolio.positions(conn, "claude")) == {"AERO", "BRETT", "DEGEN"}
    assert portfolio.positions(conn, "auto") == {}
    assert all(r[0].startswith("[claude]") for r in conn.execute("SELECT reason FROM trades"))
    conn.close()


def test_illiquid_tokens_and_halted_books_are_refused(gateway, book_path):
    all_flat(gateway, WETH=dict(price=2500.0), BRETT=dict(liq=40_000))
    assert "liquidité" in refused("paper_trade", symbol="BRETT", side="buy", reason="petit pool", usdc=5)
    conn = db.open_book(book_path)
    with db.write_tx(conn):
        conn.execute("UPDATE books SET cash_usdc = 25 WHERE name = 'claude'")
    conn.close()
    assert "seuil d'arrêt" in refused("paper_trade", symbol="AERO", side="buy", reason="sous le seuil", usdc=5)
    conn = db.open_book(book_path)
    assert portfolio.get_book(conn, "claude")["halted_reason"]          # l'arrêt est verrouillé malgré le refus
    conn.close()
    assert "à l'arrêt" in refused("paper_trade", symbol="AERO", side="buy", reason="toujours à l'arrêt", usdc=5)


def test_portfolio_status_compares_with_the_live_benchmark(gateway):
    call("paper_trade", symbol="AERO", side="buy", reason="première position", usdc=10)
    all_flat(gateway, WETH=dict(price=2750.0))                          # ETH +10 % depuis la référence
    st = call("portfolio_status")
    claude = st["books"]["claude"]
    assert claude["benchmark_usdc"] == pytest.approx(55.0)
    assert claude["vs_benchmark_usdc"] == pytest.approx(claude["equity_usdc"] - 55.0)
    assert st["books"]["auto"]["benchmark_usdc"] is None                # jamais fixée : aucun cycle encore


def test_search_tools_accept_natural_queries_and_cost_nothing(gateway, tmp_path):
    x = db.open_x(tmp_path / "x.db")
    from crypto_agent import xsource
    xsource.store_posts(x, "BRETT", [{"id": "1", "text": "$BRETT breakout on #Base",
                                      "created_at": "2026-11-03T10:00:00.000Z"}])
    x.close()
    hits = asyncio.run(mcp_server.server.call_tool("x_search", {"query": "$BRETT #Base", "symbol": "brett"}))
    assert "breakout" in str(hits.structured_content or [c.text for c in hits.content])
    assert call("x_budget")["spent_month_usd"] == 0


def test_tools_fail_cleanly_and_leave_no_lock(gateway, book_path):
    for _ in range(3):
        refused("paper_trade", symbol="AERO", side="buy", reason="montant invalide", usdc=float("inf"))
    import sqlite3
    other = sqlite3.connect(book_path, timeout=0.2, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    other.close()


def test_run_cycle_through_the_gateway(gateway):
    log = call("run_cycle")
    assert set(log["books"]) == {"auto", "claude"}
    assert "dernier cycle" in call("run_cycle")["skipped"]


def _hold(book_path, book, sym, hours_ago, price=2.0, qty=5.0, cost=10.02):
    from datetime import datetime, timedelta, timezone
    seen = portfolio.iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    conn = db.open_book(book_path)
    with db.write_tx(conn):
        conn.execute("INSERT INTO positions(book, symbol, qty, cost_usdc, opened_at, last_price, last_price_at) "
                     "VALUES (?,?,?,?,?,?,?)", (book, sym, qty, cost, seen, price, seen))
        conn.execute("UPDATE books SET cash_usdc = cash_usdc - ? WHERE name = ?", (cost, book))
    conn.close()


def test_paper_trade_reprices_the_whole_book_before_judging_risk(gateway, book_path):
    _hold(book_path, "claude", "BRETT", hours_ago=30)          # prix vieux de 30 h : sans réévaluation, achat bloqué
    r = call("paper_trade", symbol="AERO", side="buy", reason="diversification", usdc=5)
    assert r["result"]["symbol"] == "AERO" and r["stale"] == []
    conn = db.open_book(book_path)
    brett = portfolio.positions(conn, "claude")["BRETT"]
    assert brett["last_price"] == 2.0 and portfolio.parse_iso(brett["last_price_at"]) > portfolio.utcnow() - \
        __import__("datetime").timedelta(minutes=5)
    conn.close()


def test_unpriced_position_blocks_buys_without_latching_a_false_halt(gateway, book_path):
    _hold(book_path, "claude", "BRETT", hours_ago=96, cost=24.0)   # perdue de vue depuis 4 jours, cash = 26
    gateway.dex["BRETT"] = 500
    msg = refused("paper_trade", symbol="AERO", side="buy", reason="tentative d'achat", usdc=5)
    assert "achat refusé" in msg
    conn = db.open_book(book_path)
    assert portfolio.get_book(conn, "claude")["halted_reason"] is None   # valorisation incomplète : pas d'arrêt
    conn.close()


def test_sell_with_an_amount_is_refused(gateway):
    call("paper_trade", symbol="AERO", side="buy", reason="position de test", usdc=5)
    assert "toute la position" in refused("paper_trade", symbol="AERO", side="sell", reason="vente partielle", usdc=2)


def test_x_collect_checks_its_arguments_before_paying(gateway, monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    assert "max_posts" in refused("x_collect", symbol="AERO", max_posts=5)
    assert not [r for r in gateway.requests if r.url.host == "api.x.com"]


def test_status_survives_a_token_removed_from_the_whitelist(gateway, book_path):
    _hold(book_path, "claude", "PEPE", hours_ago=1)
    st = call("portfolio_status")
    assert "PEPE" in st["books"]["claude"]["positions"] and "liste blanche" in st["errors"]["PEPE"]


def test_market_snapshot_reports_the_fee_actually_charged(gateway):
    snap = call("market_snapshot")
    aero = next(q for q in snap["quotes"] if q["symbol"] == "AERO")
    assert aero["total_fee_rate"] == 0.01 and "1.0%" in snap["fee_sources"]["AERO"]
