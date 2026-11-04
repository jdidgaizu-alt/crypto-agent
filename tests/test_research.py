"""Banc d'essai de recherche : pas d'accès à l'avenir, coûts exacts, protocole."""
import pytest

from crypto_agent.backtest import HOUR, Series
from crypto_agent.config import COSTS
from research import harness, strategies
from research.harness import Panel, View, simulate

T = 1_790_000_000 - 1_790_000_000 % HOUR


def series(sym, closes, liq=5e6, fee=0.003, quote="USDC"):
    s = Series(sym, {"symbol": sym, "reserve_in_usd": liq, "fee_tier": fee, "quote": quote})
    for i, c in enumerate(closes):
        s.close[T + i * HOUR] = c
        s.open[T + i * HOUR] = closes[i - 1] if i else c
        s.volume[T + i * HOUR] = 1000.0
    return s


def panel(**cols):
    return Panel({sym: series(sym, *args) if isinstance(args, tuple) else series(sym, args) for sym, args in cols.items()})


def test_view_never_shows_the_future():
    p = panel(WETH=[1.0, 2.0, 3.0, 4.0])
    v = View(p, 2, {})
    assert v.close("WETH") == 3.0 and v.close("WETH", 2) == 1.0 and v.close("WETH", 3) is None
    assert v.closes("WETH", 3) == [1.0, 2.0, 3.0] and v.closes("WETH", 4) is None
    with pytest.raises(ValueError, match="avenir"):
        v.close("WETH", -1)


def test_orders_fill_at_the_next_open_with_exact_costs():
    s = series("WETH", [100.0] * 10 + [120.0] * 10)
    s.open[T + 10 * HOUR] = 110.0                        # l'heure suivante ouvre en hausse, au-dessus du signal (100)
    p = Panel({"WETH": s})
    seen = []

    class AllIn:
        step_hours = 1000

        def __call__(self, v):
            seen.append((v.i, v.close("WETH")))
            return {"WETH": 1.0}

    run = simulate(p, AllIn(), start=9, end=19)
    assert seen == [(9, 100.0)]                          # décision sur la clôture connue
    buy = run.trades[0]
    assert buy["ts"] == T + 10 * HOUR                    # exécution à l'heure suivante
    usd = 50 - COSTS.gas_usd
    qty = usd * (1 - 0.003) / (110.0 * (1 + 2 * usd / 5e6))   # au prix d'ouverture 110, ni 100 ni 120
    assert run.marks[-1][1] == pytest.approx(qty * 120.0, rel=1e-12)
    assert buy["fees"] == pytest.approx(usd * 0.003 + COSTS.gas_usd)


def test_weth_quoted_pools_pay_the_hop_and_unknown_fees_default_to_one_percent():
    p = panel(BRETT=([1.0] * 5, 5e6, 0.01, "WETH"), AERO=([1.0] * 5, 5e6, None, "USDC"))
    assert p.fee_rate("BRETT") == pytest.approx(0.01 + COSTS.hop_fee)
    assert p.fee_rate("AERO") == COSTS.unknown_pool_fee


def test_illiquid_pools_cannot_be_bought():
    p = panel(DEGEN=([1.0] * 10, 240_000))

    class Buy:
        step_hours = 1000

        def __call__(self, v):
            return {"DEGEN": 0.2}

    run = simulate(p, Buy(), start=0, end=9)
    assert run.trades == [] and run.refused == 1 and run.marks[-1][1] == 50


@pytest.mark.parametrize("weights,error", [({"WETH": 0.7, "AERO": 0.4}, "levier"), ({"WETH": -0.1}, "invalide"),
                                           ({"PEPE": 0.1}, "inconnu"), ({"WETH": float("nan")}, "invalide")])
def test_invalid_weights_are_rejected(weights, error):
    p = panel(WETH=[1.0] * 5, AERO=[1.0] * 5)

    class Bad:
        step_hours = 1000

        def __call__(self, v):
            return weights

    with pytest.raises(ValueError, match=error):
        simulate(p, Bad(), start=0, end=4)


def test_hold_strategy_loses_only_entry_costs_on_a_flat_market():
    p = panel(WETH=[2500.0] * 50)
    run = simulate(p, strategies.HoldEth(), start=0, end=49)
    loss = 50 - run.marks[-1][1]
    assert loss == pytest.approx(49.98 * 0.003 + COSTS.gas_usd + 49.98 * (1 - 0.003) * (2 * 49.98 / 5e6), rel=1e-3)
    assert len(run.trades) == 1                              # rééquilibrage sous 2 USDC : aucun ordre


def test_protocol_windows_and_offsets(monkeypatch):
    monkeypatch.setattr(harness, "WARMUP_HOURS", 48)
    p = panel(WETH=[2500.0 + k for k in range(24 * 12)], cbBTC=[60000.0] * (24 * 12))
    w = harness.windows(p)
    assert w["complete"][0] == 48 and w["apprentissage"][1] == w["controle"][0]
    res = harness.evaluate(p, strategies.TrendMajors)
    assert res["complete"]["runs"] == 24 and res["controle"]["runs"] == 24
    assert harness.offsets(168) == list(range(0, 168, 24))
