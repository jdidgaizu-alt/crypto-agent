"""Backtest sur données synthétiques au résultat connu."""
import pytest

from crypto_agent import backtest
from crypto_agent.backtest import HOUR, Series

T = 1_790_000_000 - 1_790_000_000 % HOUR
POOL_LIQ = 5e6


def series(sym, closes, opens=None, vol=1e6, fee=0.003):
    s = Series(sym, {"symbol": sym, "address": "0x" + "1" * 40, "pool": "0x" + "2" * 40, "dex": "uniswap",
                     "quote": "USDC", "reserve_in_usd": POOL_LIQ, "fee_tier": fee})
    for i, c in enumerate(closes):
        ts = T + i * HOUR
        s.close[ts] = c
        s.open[ts] = opens[i] if opens else (closes[i - 1] if i else c)
        s.volume[ts] = vol / 24
    return s


def flat(sym, n, price=1.0):
    return series(sym, [price] * n)


def test_flat_market_never_trades():
    res = backtest.run({"WETH": flat("WETH", 200, 2500.0), "AERO": flat("AERO", 200)}, step_hours=1)
    s = res.summary()
    assert s["trades"] == 0 and s["final_equity"] == 50 and s["eth_hold_return_pct"] == 0


def jump(n, after, opens_override=None):
    """Prix plat à 1,0 pendant 40 h, saut à 1,25 (+25 % sur 1 h et sur 24 h : achat), puis `after`."""
    closes = [1.0] * 40 + [1.25] + [after] * (n - 41)
    opens = [closes[i - 1] if i else closes[0] for i in range(n)]
    for i, o in (opens_override or {}).items():
        opens[i] = o
    return closes, opens


def test_signal_is_filled_at_the_next_open_not_at_the_signal_price():
    closes, opens = jump(80, 1.25, {41: 1.30})          # l'heure suivante ouvre plus cher que le signal
    res = backtest.run({"WETH": flat("WETH", 80, 2500.0), "AERO": series("AERO", closes, opens)}, step_hours=1)
    buys = [t for t in res.trades if t["side"] == "buy"]
    assert len(buys) == 1, res.trades
    assert buys[0]["ts"] == T + 41 * HOUR                # décidé à la clôture de la bougie 40
    assert buys[0]["price"] == pytest.approx(1.30 * (1 + 2 * 10 / POOL_LIQ))
    assert buys[0]["fees"] == pytest.approx(10 * 0.003 + 0.02)


def test_crash_triggers_stop_loss_and_costs_are_counted():
    closes, opens = jump(120, 0.9)
    res = backtest.run({"WETH": flat("WETH", 120, 2500.0), "AERO": series("AERO", closes, opens)}, step_hours=1)
    sells = [t for t in res.trades if t["side"] == "sell"]
    assert len(sells) == 1 and "stop-loss" in sells[0]["reason"]
    buy = res.trades[0]
    loss = 50 - res.summary()["final_equity"]
    expected = 10.02 - buy["qty"] * 0.9 * (1 - 0.003) + 0.02     # achat à 1,25, revente à 0,9, frais et gaz
    assert loss == pytest.approx(expected, abs=0.01)


def test_hourly_cycle_buys_the_jump_then_stops_out_the_crash():
    closes, opens = jump(150, 0.5)
    data = {"WETH": flat("WETH", 150, 2500.0), "AERO": series("AERO", closes, opens)}
    s = backtest.run(data, step_hours=1).summary()
    assert s["trades"] == 2 and s["round_trips"] == 1 and s["win_rate_pct"] == 0


def test_benchmark_tracks_eth_and_basket_tracks_all_tokens():
    weth = series("WETH", [2500.0] * 30 + [5000.0] * 30)
    res = backtest.run({"WETH": weth, "AERO": flat("AERO", 60)}, step_hours=1)
    s = res.summary()
    assert s["eth_hold_return_pct"] == pytest.approx(100.0)
    assert s["basket_hold_return_pct"] == pytest.approx(50.0)


def test_fee_override_does_not_leak_between_runs():
    data = {"WETH": flat("WETH", 100, 2500.0), "AERO": flat("AERO", 100)}
    backtest.run(data, fee_override=0.05)
    assert data["AERO"].meta["fee_tier"] == 0.003
