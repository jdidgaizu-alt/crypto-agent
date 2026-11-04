"""Décisions et garde-fous."""
from crypto_agent import strategy
from crypto_agent.config import COSTS, RISK, WHITELIST

from conftest import mkq


def decide(quotes, positions=None, cash=50.0, risk_equity=None, capital=50.0, trades_today=0, halted=None,
           recently_sold=frozenset(), attention=None, mode="full"):
    positions = positions or {}
    return strategy.decide(quotes=quotes, attention=attention or {}, positions=positions, cash=cash,
                           risk_equity=cash if risk_equity is None else risk_equity, capital=capital,
                           trades_today=trades_today, halted=halted, recently_sold=set(recently_sold), mode=mode)


def pos(cost=10.0, qty=10.0):
    return {"qty": qty, "cost_usdc": cost}


def actions(ds):
    return [(d.action, d.symbol) for d in ds]


def test_kill_switch_liquidates_every_quoted_position_and_halts():
    quotes = {s: mkq(s, price=0.83) for s in ("AERO", "BRETT", "DEGEN")}
    positions = {s: pos() for s in quotes}
    ds = decide(quotes, positions, cash=5.0, risk_equity=29.9)
    assert ds[0].action == "halt"
    assert sorted(s for a, s in actions(ds) if a == "sell") == ["AERO", "BRETT", "DEGEN"]
    assert not any(a == "buy" for a, _ in actions(ds))


def test_stop_loss_still_fires_below_the_kill_threshold():
    ds = decide({"AERO": mkq(price=0.5)}, {"AERO": pos()}, cash=21, risk_equity=26)
    sells = [d for d in ds if d.action == "sell"]
    assert sells and sells[0].reason.startswith("stop-loss")


def test_halted_book_never_buys_even_after_recovery():
    quotes = {s: mkq(s, h24=15, h1=3) for s in WHITELIST}
    ds = decide(quotes, cash=50, risk_equity=50, halted="capital à risque 29.00 sous le seuil d'arrêt 30.00",
                attention={s: 3.0 for s in WHITELIST})
    assert actions(ds) == [("hold", None)]


def test_rally_above_35_percent_is_not_sold_as_a_reversal():
    q = mkq(price=1.178, h24=36, h1=1)
    ds = decide({"AERO": q}, {"AERO": pos(cost=10, qty=10)}, cash=40, risk_equity=51.78)
    assert not any(d.action == "sell" for d in ds)
    assert strategy.score(q, None) < strategy.exit_score(q, None)   # la pénalité ne joue qu'à l'entrée


def test_take_profit_and_signal_exit_only_in_full_mode():
    quotes = {"AERO": mkq(price=1.3)}
    assert decide(quotes, {"AERO": pos()})[0].reason.startswith("prise de bénéfice")
    assert actions(decide(quotes, {"AERO": pos()}, mode="guard")) == [("hold", None)]
    falling = {"AERO": mkq(price=0.95, h24=-20, h1=-5)}
    assert decide(falling, {"AERO": pos()})[0].reason.startswith("signal retourné")
    assert actions(decide(falling, {"AERO": pos()}, mode="guard")) == [("hold", None)]


def test_guard_mode_never_buys():
    quotes = {s: mkq(s, h24=15, h1=3) for s in WHITELIST}
    ds = decide(quotes, attention={s: 3.0 for s in WHITELIST}, mode="guard")
    assert actions(ds) == [("hold", None)]


def test_position_without_quote_is_flagged_not_silently_skipped():
    ds = decide({}, {"AERO": pos()}, cash=40, risk_equity=40)
    assert ("warn", "AERO") in actions(ds)


def test_size_and_count_limits_follow_the_stored_capital():
    quotes = {s: mkq(s, h24=15, h1=3) for s in WHITELIST}
    att = {s: 3.0 for s in WHITELIST}
    buys = [d for d in decide(quotes, attention=att) if d.action == "buy"]
    assert len(buys) == RISK.max_open_positions
    assert all(d.usdc == 10 for d in buys)
    buys100 = [d for d in decide(quotes, cash=100, capital=100, attention=att) if d.action == "buy"]
    assert all(d.usdc == 20 for d in buys100)
    # capital 100 : seuil d'arrêt à 60, donc 55 de capital à risque déclenche l'arrêt
    assert decide(quotes, cash=55, capital=100, attention=att)[0].action == "halt"


def test_buys_never_exceed_cash_including_gas_and_reserve():
    quotes = {s: mkq(s, h24=15, h1=3) for s in WHITELIST}
    ds = decide(quotes, cash=12.0, risk_equity=45, attention={s: 3.0 for s in WHITELIST})
    buys = [d for d in ds if d.action == "buy"]
    spent = sum(d.usdc + COSTS.gas_usd for d in buys)
    assert spent <= 12.0 - RISK.cash_reserve_usdc + 1e-9


def test_ineligible_top_token_does_not_block_the_next_one():
    quotes = {"BRETT": mkq("BRETT", liq=10_000, h24=19, h1=4), "AERO": mkq("AERO", h24=15, h1=3)}
    ds = decide(quotes, attention={"BRETT": 3.0, "AERO": 3.0})
    assert actions(ds) == [("buy", "AERO")]


def test_no_entry_on_falling_or_overheated_or_recently_sold_tokens():
    att = {"AERO": 3.0}
    assert actions(decide({"AERO": mkq(h24=-2, h1=3)}, attention=att)) == [("hold", None)]
    assert actions(decide({"AERO": mkq(h24=80, h1=3)}, attention=att)) == [("hold", None)]
    assert actions(decide({"AERO": mkq(h24=15, h1=3)}, attention=att, recently_sold={"AERO"})) == [("hold", None)]


def test_unmeasured_attention_is_ignored_not_treated_as_a_signal():
    q = mkq(h24=0, h1=0)
    assert strategy.score(q, None) == 0
    assert actions(decide({"AERO": q}, attention={"AERO": None})) == [("hold", None)]


def test_daily_cap_blocks_entries_but_never_exits():
    quotes = {"AERO": mkq(price=0.5), "BRETT": mkq("BRETT", h24=15, h1=3)}
    ds = decide(quotes, {"AERO": pos()}, cash=40, risk_equity=45, trades_today=RISK.max_trades_per_day,
                attention={"BRETT": 3.0})
    assert actions(ds) == [("sell", "AERO")]


def test_entry_refusal_covers_every_guard():
    q = mkq(h24=15)
    base = dict(q=q, usdc=10, capital=50, cash=50, held_cost=0.0, open_count=0, trades_count=0, halted=None,
                risk_equity=50, recently_sold=set())
    assert strategy.entry_refusal(**base) is None
    cases = {
        "à l'arrêt": dict(halted="stop"),
        "seuil d'arrêt": dict(risk_equity=29),
        "vendu il y a": dict(recently_sold={"AERO"}),
        "liquidité": dict(q=mkq(liq=1000, h24=15)),
        "volume": dict(q=mkq(vol=1000, h24=15)),
        "minimum": dict(usdc=1),
        "position max": dict(usdc=11),
        "déjà 5.00": dict(held_cost=5.0, usdc=6),
        "positions ouvertes": dict(open_count=3),
        "plafond": dict(trades_count=6),
        "liquidités insuffisantes": dict(cash=10.3),
    }
    for expected, override in cases.items():
        why = strategy.entry_refusal(**{**base, **override})
        assert why and expected in why, (expected, why)
