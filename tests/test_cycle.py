"""Cycles complets sur faux réseau."""
import threading
from datetime import timedelta

import pytest

from crypto_agent import cycle, db, portfolio
from crypto_agent.config import RISK, WHITELIST

from conftest import T0, all_flat


def run(net, book_conn, x_conn, now=T0, force=False):
    with net.client() as c:
        return cycle.run(book_conn, x_conn, now=now, http=c, force=force)


def n_trades(conn, book="auto", side=None):
    sql = "SELECT COUNT(*) FROM trades WHERE book = ?" + (" AND side = ?" if side else "")
    return conn.execute(sql, (book, side) if side else (book,)).fetchone()[0]


def test_flat_market_without_x_does_nothing_and_snapshots_both_books(net, book_conn, x_conn):
    all_flat(net)
    log = run(net, book_conn, x_conn)
    assert n_trades(book_conn) == 0
    assert set(log["books"]) == {"auto", "claude"}
    assert log["books"]["auto"]["benchmark_usdc"] == pytest.approx(50.0)
    assert any("X non configuré" in e for e in log["errors"])


def test_no_orders_until_weth_is_quoted_then_benchmark_is_start_capital(net, book_conn, x_conn):
    all_flat(net, AERO=dict(h24=15, h1=3))
    net.dex["WETH"] = 503
    log = run(net, book_conn, x_conn)
    assert "référence non fixée" in log["books"]["auto"]["skipped"] and n_trades(book_conn) == 0
    all_flat(net, AERO=dict(h24=15, h1=3), WETH=dict(price=2500.0))
    log = run(net, book_conn, x_conn, now=T0 + timedelta(hours=1))
    assert portfolio.get_book(book_conn, "auto")["bench_weth_qty"] == pytest.approx(50 / 2500)


def test_a_failing_token_does_not_stop_the_stop_loss(net, book_conn, x_conn):
    all_flat(net)
    with db.write_tx(book_conn):
        portfolio.ensure_benchmark(book_conn, "auto", 2.0, T0)
    from conftest import mkq
    with db.write_tx(book_conn):
        portfolio.buy(book_conn, "auto", mkq("AERO", price=2.0), 10, "test", T0 - timedelta(hours=2))
    all_flat(net, AERO=dict(price=1.0))
    net.dex["BRETT"] = 500
    log = run(net, book_conn, x_conn)
    sells = [d for d in log["books"]["auto"]["decisions"] if d["action"] == "sell"]
    assert sells and sells[0]["reason"].startswith("stop-loss")
    assert any("prix BRETT" in e for e in log["errors"])


def test_kill_switch_liquidates_latches_and_survives_recovery(net, book_conn, x_conn):
    from conftest import mkq
    all_flat(net)
    with db.write_tx(book_conn):
        portfolio.ensure_benchmark(book_conn, "auto", 2.0, T0)
        for s in ("AERO", "BRETT", "DEGEN"):
            portfolio.buy(book_conn, "auto", mkq(s, price=2.0), 10, "test", T0 - timedelta(days=2))
        book_conn.execute("UPDATE books SET cash_usdc = 5 WHERE name = 'auto'")
    all_flat(net, AERO=dict(price=1.6), BRETT=dict(price=1.6), DEGEN=dict(price=1.6))   # -20 % : capital ≈ 29
    log = run(net, book_conn, x_conn)
    actions = [d["action"] for d in log["books"]["auto"]["decisions"]]
    assert actions[0] == "halt" and actions.count("sell") == 3
    assert portfolio.positions(book_conn, "auto") == {}
    assert portfolio.get_book(book_conn, "auto")["halted_reason"]
    # le marché remonte : toujours à l'arrêt, aucun achat
    all_flat(net, AERO=dict(h24=15, h1=3))
    log = run(net, book_conn, x_conn, now=T0 + timedelta(days=2))
    assert n_trades(book_conn, side="buy") == 3
    res = portfolio.reset_halt(book_conn, "auto", T0 + timedelta(days=2))   # décision humaine (ligne de commande)
    assert res["new_capital"] == pytest.approx(portfolio.get_book(book_conn, "auto")["cash_usdc"])


def _two_positions_then_aero_unpriced(net, book_conn, bought_at):
    from conftest import mkq
    all_flat(net)
    with db.write_tx(book_conn):
        portfolio.ensure_benchmark(book_conn, "auto", 2.0, T0)
        for s in ("AERO", "BRETT"):
            portfolio.buy(book_conn, "auto", mkq(s, price=2.0), 10, "test", bought_at)
        book_conn.execute("UPDATE books SET cash_usdc = 20 WHERE name = 'auto'")
    all_flat(net, DEGEN=dict(h24=15, h1=3))
    net.dex["AERO"] = 500                  # AERO n'a plus de prix


def test_one_day_price_outage_blocks_entries_but_does_not_liquidate(net, book_conn, x_conn, monkeypatch):
    from crypto_agent import xsource
    monkeypatch.setattr(xsource, "configured", lambda: True)
    monkeypatch.setattr(xsource, "fetch_counts", lambda *a, **k: 0)
    monkeypatch.setattr(xsource, "attention", lambda conn, sym, now=None: {"ratio": 3.0, "reason": None})
    _two_positions_then_aero_unpriced(net, book_conn, T0 - timedelta(hours=24))
    auto = run(net, book_conn, x_conn)["books"]["auto"]
    assert auto["stale"] == ["AERO"] and auto["lost"] == [] and auto["halted"] is None
    assert not any(d["action"] in ("halt", "sell", "buy") for d in auto["decisions"])
    assert any("pas de prix récent pour AERO" in d["reason"] for d in auto["decisions"] if d["action"] == "warn")


def test_position_lost_for_days_counts_zero_and_triggers_the_kill_switch(net, book_conn, x_conn):
    _two_positions_then_aero_unpriced(net, book_conn, T0 - timedelta(days=4))
    auto = run(net, book_conn, x_conn)["books"]["auto"]
    assert auto["lost"] == ["AERO"] and auto["risk_equity_usdc"] < 30
    assert auto["decisions"][0]["action"] == "halt"
    assert ("sell", "BRETT") in [(d["action"], d["symbol"]) for d in auto["decisions"]]


def test_a_refused_sell_does_not_open_a_fourth_position(net, book_conn, x_conn, monkeypatch):
    from conftest import mkq, pair
    from crypto_agent import xsource
    monkeypatch.setattr(xsource, "configured", lambda: True)
    monkeypatch.setattr(xsource, "fetch_counts", lambda *a, **k: 0)
    monkeypatch.setattr(xsource, "attention", lambda conn, sym, now=None: {"ratio": 3.0, "reason": None})
    all_flat(net)
    with db.write_tx(book_conn):
        portfolio.ensure_benchmark(book_conn, "auto", 2.0, T0)
        for s in ("AERO", "BRETT", "DEGEN"):
            portfolio.buy(book_conn, "auto", mkq(s, price=2.0), 10, "test", T0 - timedelta(hours=2))
    all_flat(net, VIRTUAL=dict(h24=15, h1=3))
    net.dex["AERO"] = [pair("AERO", price=1.0, liq=0.0)]      # stop-loss décidé, mais vente impossible
    auto = run(net, book_conn, x_conn)["books"]["auto"]
    sells = [d for d in auto["decisions"] if d["action"] == "sell"]
    assert sells and "vente impossible" in sells[0]["refused"]
    buys = [d for d in auto["decisions"] if d["action"] == "buy"]
    assert buys and "3 positions ouvertes" in buys[0]["refused"]   # revérifié à l'exécution
    assert len(portfolio.positions(book_conn, "auto")) == 3


def test_a_cycle_charges_exactly_and_snapshots_the_comparison(net, book_conn, x_conn, monkeypatch):
    from crypto_agent import xsource
    from crypto_agent.config import COSTS
    monkeypatch.setattr(xsource, "configured", lambda: True)
    monkeypatch.setattr(xsource, "fetch_counts", lambda *a, **k: 0)
    monkeypatch.setattr(xsource, "attention",
                        lambda conn, sym, now=None: {"ratio": 3.0 if sym == "AERO" else None, "reason": None})
    all_flat(net, AERO=dict(price=2.0, h24=15, h1=3), WETH=dict(price=2500.0))
    log = run(net, book_conn, x_conn)
    bought = [d["result"] for d in log["books"]["auto"]["decisions"] if d["action"] == "buy" and "result" in d]
    assert [b["symbol"] for b in bought] == ["AERO"]
    assert portfolio.get_book(book_conn, "auto")["cash_usdc"] == pytest.approx(50 - 10 - COSTS.gas_usd, abs=1e-12)
    assert bought[0]["fee_rate"] == COSTS.unknown_pool_fee            # nœud injoignable : 1 % supposé
    # Le lendemain, AERO +10 % et ETH +10 % : l'instantané enregistré compare les deux au même instant.
    all_flat(net, AERO=dict(price=2.2), WETH=dict(price=2750.0))
    run(net, book_conn, x_conn, now=T0 + timedelta(days=1))
    snap = book_conn.execute("SELECT equity_usdc, benchmark_usdc FROM snapshots WHERE book = 'auto' "
                             "ORDER BY id DESC LIMIT 1").fetchone()
    qty = bought[0]["qty"]
    assert snap["equity_usdc"] == pytest.approx(50 - 10.02 + qty * 2.2, abs=1e-9)
    assert snap["benchmark_usdc"] == pytest.approx(55.0)


def test_claude_book_gets_stop_loss_but_no_initiative(net, book_conn, x_conn):
    from conftest import mkq
    all_flat(net, BRETT=dict(h24=15, h1=3))
    with db.write_tx(book_conn):
        portfolio.ensure_benchmark(book_conn, "claude", 2.0, T0)
        portfolio.buy(book_conn, "claude", mkq("AERO", price=2.0), 10, "[claude] test", T0 - timedelta(hours=3))
        portfolio.buy(book_conn, "claude", mkq("DEGEN", price=1.0), 10, "[claude] test", T0 - timedelta(hours=3))
    all_flat(net, AERO=dict(price=1.0), DEGEN=dict(price=2.0), BRETT=dict(h24=15, h1=3))
    log = run(net, book_conn, x_conn)
    claude = [(d["action"], d["symbol"]) for d in log["books"]["claude"]["decisions"]]
    assert claude == [("sell", "AERO")]          # stop-loss ; DEGEN à +98 % reste, BRETT n'est pas acheté


def test_cycles_too_close_are_skipped_unless_forced(net, book_conn, x_conn):
    all_flat(net)
    run(net, book_conn, x_conn)
    log = run(net, book_conn, x_conn, now=T0 + timedelta(minutes=RISK.min_cycle_interval_minutes - 1))
    assert "skipped" in log
    log = run(net, book_conn, x_conn, now=T0 + timedelta(minutes=5), force=True)
    assert "skipped" not in log


def test_overlapping_cycles_buy_only_once(net, book_path, tmp_path, monkeypatch):
    from crypto_agent import xsource
    all_flat(net, AERO=dict(h24=15, h1=3), BRETT=dict(h24=15, h1=3), DEGEN=dict(h24=15, h1=3),
             VIRTUAL=dict(h24=15, h1=3))
    # attention forcée à ×3 pour déclencher des achats
    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    monkeypatch.setattr(xsource, "fetch_counts", lambda *a, **k: 30)
    monkeypatch.setattr(xsource, "attention", lambda conn, sym, now=None: {"ratio": 3.0, "reason": None})
    barrier = threading.Barrier(4)
    logs = []

    def go(i):
        conn = db.open_book(book_path)
        x = db.open_x(tmp_path / f"x{i}.db")
        barrier.wait()
        try:
            logs.append(run(net, conn, x))
        finally:
            conn.close()
            x.close()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = db.open_book(book_path)
    assert n_trades(conn, side="buy") == RISK.max_open_positions
    assert len(logs) == 4 and sum("skipped" in log for log in logs) == 3
    conn.close()


def test_report_works_offline(net, book_conn, x_conn):
    all_flat(net)
    run(net, book_conn, x_conn)
    rep = cycle.report(book_conn)
    assert rep["auto"]["last_snapshot"]["equity_usdc"] == pytest.approx(50)
    assert rep["claude"]["cash_usdc"] == 50


def test_cli_refuses_without_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cycle, "BOOK_DB_PATH", tmp_path / "absent.db")
    with pytest.raises(FileNotFoundError):
        cycle.main(["--report"])
    monkeypatch.setattr(cycle, "BOOK_DB_PATH", tmp_path / "new.db")
    assert cycle.main(["--init"]) == 0
    assert cycle.main(["--init"]) == 0                 # idempotent : ne recrée rien
    out = capsys.readouterr().out
    assert "créé avec 50 USDC" in out and "existe déjà" in out
    assert cycle.main(["--report"]) == 0
