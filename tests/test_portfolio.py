"""Comptabilité des livres : coûts exacts, montants invalides, atomicité, valorisation prudente."""
import math
import sqlite3
import threading
from datetime import timedelta

import pytest

from crypto_agent import db, portfolio
from crypto_agent.config import COSTS, RISK
from crypto_agent.db import write_tx

from conftest import T0, mkq


def buy(conn, q, usdc, book="auto", now=T0):
    with write_tx(conn):
        return portfolio.buy(conn, book, q, usdc, "test", now)


def sell(conn, q, book="auto", now=T0):
    with write_tx(conn):
        return portfolio.sell_all(conn, book, q, "test", now)


def cash(conn, book="auto"):
    return portfolio.get_book(conn, book)["cash_usdc"]


# --- coûts exacts (une sous-facturation doit faire échouer les tests) --------

def test_round_trip_costs_exactly_fees_gas_and_impact(book_conn):
    q = mkq(price=1.0, liq=5e6, fee=0.003)
    r = buy(book_conn, q, 10)
    impact = 2 * 10 / 5e6
    qty = 10 * (1 - 0.003) / (1 + impact)
    assert r["qty"] == pytest.approx(qty, rel=1e-12)
    assert cash(book_conn) == pytest.approx(50 - 10 - COSTS.gas_usd, abs=1e-12)
    s = sell(book_conn, q)
    out = qty / (1 + 2 * qty / 5e6)
    expected_cash = 50 - 10 - COSTS.gas_usd + out * (1 - 0.003) - COSTS.gas_usd
    assert cash(book_conn) == pytest.approx(expected_cash, abs=1e-12)
    lost = 50 - cash(book_conn)
    # 0,3 % à l'achat + 0,3 % à la vente + 2 gaz + impact : environ 0,1 USDC, ni plus ni MOINS.
    assert lost == pytest.approx(0.03 + 0.02 + 0.03 + 0.02, abs=2e-3)
    assert s["pnl"] == pytest.approx(-lost, abs=1e-12)


def test_unknown_pool_fee_is_charged_at_the_prudent_default(book_conn):
    r = buy(book_conn, mkq(fee=None), 10)
    assert r["fee_rate"] == COSTS.unknown_pool_fee == 0.01
    assert r["fees"] == pytest.approx(10 * 0.01 + COSTS.gas_usd)


def test_weth_quoted_pair_pays_the_usdc_weth_hop(book_conn):
    r = buy(book_conn, mkq(quote="WETH", fee=0.01), 10)
    assert r["fee_rate"] == pytest.approx(0.01 + COSTS.hop_fee)


def test_sell_into_thin_liquidity_follows_constant_product(book_conn):
    q = mkq(price=1.0, liq=5e6, fee=0.003)
    r = buy(book_conn, q, 10)
    before = cash(book_conn)
    thin = mkq(price=1.0, liq=8.0, fee=0.003)
    s = sell(book_conn, thin)
    exact = r["qty"] / (1 + 2 * r["qty"] / 8.0)          # x*y=k : bien moins que la moitié de la valeur
    assert s["proceeds"] == pytest.approx(exact * 0.997 - COSTS.gas_usd, abs=1e-9)
    assert cash(book_conn) - before < 3.0


def test_sell_with_zero_liquidity_is_refused_and_position_kept(book_conn):
    buy(book_conn, mkq(), 10)
    with pytest.raises(portfolio.Refused, match="vente impossible"):
        sell(book_conn, mkq(liq=0))
    assert "AERO" in portfolio.positions(book_conn, "auto")


# --- montants invalides ------------------------------------------------------

@pytest.mark.parametrize("amount", [0, 0.0, -100, -0.02, math.nan, math.inf, -math.inf, None, True, "10"])
def test_invalid_amounts_are_refused_without_any_write(book_conn, amount):
    with pytest.raises(portfolio.Refused):
        buy(book_conn, mkq(), amount)
    assert cash(book_conn) == 50
    assert portfolio.positions(book_conn, "auto") == {}
    assert book_conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_orders_refuse_to_run_outside_a_transaction(book_conn):
    with pytest.raises(RuntimeError, match="hors transaction"):
        portfolio.buy(book_conn, "auto", mkq(), 10, "test", T0)


def test_insufficient_cash_and_excess_impact_are_refused(book_conn):
    with pytest.raises(portfolio.Refused, match="liquidités"):
        buy(book_conn, mkq(liq=1e9), 60)
    with pytest.raises(portfolio.Refused, match="impact"):
        buy(book_conn, mkq(liq=500), 10)
    assert cash(book_conn) == 50


# --- atomicité ---------------------------------------------------------------

def _parallel(n, fn):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as e:  # noqa: BLE001
            results[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_sells_of_one_position_credit_cash_once(book_path, book_conn):
    buy(book_conn, mkq(), 10)
    before = cash(book_conn)

    def go(i):
        conn = db.open_book(book_path)
        try:
            return sell(conn, mkq())
        finally:
            conn.close()

    results = _parallel(4, go)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert all(isinstance(r, portfolio.Refused) for r in results if not isinstance(r, dict))
    sold = next(r for r in results if isinstance(r, dict))
    assert cash(book_conn) == pytest.approx(before + sold["proceeds"], abs=1e-12)
    assert book_conn.execute("SELECT COUNT(*) FROM trades WHERE side = 'sell'").fetchone()[0] == 1


def test_concurrent_buys_cannot_overdraw(book_path, book_conn):
    def go(i):
        conn = db.open_book(book_path)
        try:
            return buy(conn, mkq(sym=["AERO", "BRETT", "DEGEN", "VIRTUAL", "cbBTC", "WETH"][i]), 10)
        finally:
            conn.close()

    results = _parallel(6, go)
    assert sum(isinstance(r, dict) for r in results) == 4      # 4 × 10,02 = 40,08 ; le 5e dépasserait 50
    assert cash(book_conn) >= 0


def test_failed_transaction_rolls_back_and_releases_the_lock(book_path, book_conn):
    with pytest.raises(ZeroDivisionError):
        with write_tx(book_conn):
            book_conn.execute("UPDATE books SET cash_usdc = 0 WHERE name = 'auto'")
            1 / 0
    assert cash(book_conn) == 50
    other = sqlite3.connect(book_path, timeout=0.2, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")   # échouerait (« database is locked ») si le verrou était resté pris
    other.execute("ROLLBACK")
    other.close()


# --- valorisation ------------------------------------------------------------

def test_missing_quote_values_position_at_last_price_not_cost(book_conn):
    buy(book_conn, mkq(price=2.0), 10)
    with write_tx(book_conn):
        portfolio.mark_prices(book_conn, "auto", {"AERO": mkq(price=0.2)}, T0 + timedelta(hours=1))
    val = portfolio.value(book_conn, "auto", T0 + timedelta(hours=2))      # plus de cotation depuis 1 h
    pos = val.positions["AERO"]
    assert val.equity == pytest.approx(val.cash + pos["qty"] * 0.2)
    assert val.equity < 50 - 8                                              # la chute de 90 % est visible
    assert val.stale == [] and val.risk_equity == pytest.approx(val.equity)


def test_stale_position_counts_zero_in_risk_equity(book_conn):
    buy(book_conn, mkq(price=2.0), 10)
    later = T0 + timedelta(hours=RISK.stale_quote_hours + 1)
    val = portfolio.value(book_conn, "auto", later)
    assert val.stale == ["AERO"]
    assert val.risk_equity == pytest.approx(val.cash)
    assert val.equity > val.risk_equity


def test_benchmark_is_the_start_capital_in_eth_whatever_happened_before(book_conn):
    buy(book_conn, mkq(), 10)                    # un ordre (et ses frais) avant la référence
    with write_tx(book_conn):
        portfolio.ensure_benchmark(book_conn, "auto", 2500.0, T0)
        portfolio.ensure_benchmark(book_conn, "auto", 9999.0, T0)   # fixée une seule fois
    assert portfolio.benchmark_value(book_conn, "auto", 2500.0) == pytest.approx(50.0)
    assert portfolio.benchmark_value(book_conn, "auto", 2750.0) == pytest.approx(55.0)


def test_create_book_refuses_duplicates_and_bad_capital(book_conn):
    with pytest.raises(portfolio.Refused, match="existe déjà"):
        portfolio.create_book(book_conn, "auto", 50)
    with pytest.raises(portfolio.Refused):
        portfolio.create_book(book_conn, "autre", -5)


def test_open_book_refuses_to_create_a_new_book_silently(tmp_path):
    with pytest.raises(FileNotFoundError, match="--init"):
        db.open_book(tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()
