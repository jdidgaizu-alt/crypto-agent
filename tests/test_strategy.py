"""Décisions et garde-fous."""
from crypto_agent import strategy
from crypto_agent.config import COSTS, RISK, WHITELIST

from conftest import mkq


def decide(quotes, positions=None, cash=50.0, risk_equity=None, capital=50.0, entries_today=0, halted=None,
           recently_sold=frozenset(), attention=None, mode="full", stale=()):
    positions = positions or {}
    return strategy.decide(quotes=quotes, attention=attention or {}, positions=positions, cash=cash,
                           risk_equity=cash if risk_equity is None else risk_equity, capital=capital,
                           entries_today=entries_today, halted=halted, recently_sold=set(recently_sold), mode=mode,
                           stale=stale)


def pos(cost=10.0, qty=10.0):
    return {"qty": qty, "cost_usdc": cost}


def actions(ds, kinds=("buy", "sell", "halt", "hold", "warn")):
    return [(d.action, d.symbol) for d in ds if d.action in kinds]


ALL_UP = {s: mkq(s, h24=15, h1=3) for s in WHITELIST}
BUZZ = {s: 3.0 for s in WHITELIST}


def test_pinned_risk_parameters():
    """Les valeurs annoncées dans le README : une modification doit être volontaire."""
    assert (RISK.stop_loss, RISK.take_profit, RISK.kill_switch_frac) == (-0.15, 0.25, 0.60)
    assert (RISK.max_position_frac, RISK.max_open_positions, RISK.max_entries_per_day) == (0.20, 3, 6)
    assert (RISK.min_liquidity_usd, RISK.min_volume_24h_usd, RISK.min_order_usdc) == (500_000, 250_000, 2.0)
    assert (RISK.stale_quote_hours, RISK.lost_quote_hours, RISK.rebuy_cooldown_hours) == (6, 72, 24)
    assert (COSTS.gas_usd, COSTS.unknown_pool_fee, COSTS.hop_fee, COSTS.max_price_impact) == (0.02, 0.01, 0.0005, 0.02)


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
    ds = decide(ALL_UP, cash=50, risk_equity=50, halted="capital à risque 29.00 sous le seuil d'arrêt 30.00",
                attention=BUZZ)
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
    assert actions(decide(ALL_UP, attention=BUZZ, mode="guard")) == [("hold", None)]


def test_position_without_quote_is_flagged_and_blocks_entries():
    ds = decide({"BRETT": mkq("BRETT", h24=15, h1=3)}, {"AERO": pos()}, cash=40, risk_equity=50,
                attention={"BRETT": 3.0})
    assert ("warn", "AERO") in actions(ds)
    assert not any(d.action == "buy" for d in ds)
    assert any("pas de prix récent pour AERO" in d.reason for d in ds if d.action == "warn")


def test_stale_position_blocks_entries_even_when_quoted_again():
    ds = decide({"AERO": mkq(), "BRETT": mkq("BRETT", h24=15, h1=3)}, {"AERO": pos()}, cash=40, risk_equity=50,
                attention={"BRETT": 3.0}, stale=["AERO"])
    assert not any(d.action == "buy" for d in ds)


def test_size_and_count_limits_follow_the_stored_capital():
    buys = [d for d in decide(ALL_UP, attention=BUZZ) if d.action == "buy"]
    assert len(buys) == RISK.max_open_positions and all(d.usdc == 10 for d in buys)
    buys100 = [d for d in decide(ALL_UP, cash=100, capital=100, attention=BUZZ) if d.action == "buy"]
    assert all(d.usdc == 20 for d in buys100)
    # capital 100 : seuil d'arrêt à 60, donc 55 de capital à risque déclenche l'arrêt
    assert decide(ALL_UP, cash=55, capital=100, attention=BUZZ)[0].action == "halt"


def test_sizes_are_rounded_down_never_past_the_cap_or_the_cash():
    # 20 % de 49,97 = 9,994 : arrondi à 9,99 (un arrondi à 10,00 dépasserait le plafond et serait refusé)
    buys = [d for d in decide(ALL_UP, cash=49.97, capital=49.97, attention=BUZZ) if d.action == "buy"]
    assert buys and all(d.usdc == 9.99 for d in buys)
    for cash in (10.516, 12.0, 21.004, 30.0):
        ds = decide(ALL_UP, cash=cash, risk_equity=45, attention=BUZZ)
        spent = sum(d.usdc + COSTS.gas_usd for d in ds if d.action == "buy")
        assert spent <= cash - RISK.cash_reserve_usdc + 1e-9
        for d in ds:
            if d.action == "buy":
                assert d.usdc == strategy.floor_cents(d.usdc) and d.usdc >= RISK.min_order_usdc


def test_ineligible_top_token_does_not_block_the_next_one():
    quotes = {"BRETT": mkq("BRETT", liq=10_000, h24=19, h1=4), "AERO": mkq("AERO", h24=15, h1=3)}
    ds = decide(quotes, attention={"BRETT": 3.0, "AERO": 3.0})
    assert actions(ds, ("buy",)) == [("buy", "AERO")]


def test_no_entry_on_falling_or_overheated_or_recently_sold_tokens():
    att = {"AERO": 3.0}
    assert actions(decide({"AERO": mkq(h24=-2, h1=3)}, attention=att)) == [("hold", None)]
    assert actions(decide({"AERO": mkq(h24=80, h1=3)}, attention=att)) == [("hold", None)]
    assert actions(decide({"AERO": mkq(h24=15, h1=3)}, attention=att, recently_sold={"AERO"})) == [("hold", None)]


def test_unmeasured_attention_is_ignored_not_treated_as_a_signal():
    q = mkq(h24=0, h1=0)
    assert strategy.score(q, None) == 0
    assert actions(decide({"AERO": q}, attention={"AERO": None})) == [("hold", None)]


def test_daily_cap_counts_entries_only_and_never_blocks_exits():
    quotes = {"AERO": mkq(price=0.5), "BRETT": mkq("BRETT", h24=15, h1=3)}
    capped = decide(quotes, {"AERO": pos()}, cash=40, risk_equity=45, entries_today=RISK.max_entries_per_day,
                    attention={"BRETT": 3.0})
    assert actions(capped, ("buy", "sell")) == [("sell", "AERO")]
    assert any("plafond de 6 achats" in d.reason for d in capped if d.action == "warn")
    room = decide(quotes, {"AERO": pos()}, cash=40, risk_equity=45, entries_today=RISK.max_entries_per_day - 1,
                  attention={"BRETT": 3.0})
    assert actions(room, ("buy", "sell")) == [("sell", "AERO"), ("buy", "BRETT")]


def test_entry_refusal_covers_every_guard():
    q = mkq(h24=15)
    base = dict(q=q, usdc=10, capital=50, cash=50, held_cost=0.0, open_count=0, entries_count=0, halted=None,
                risk_equity=50, recently_sold=set())
    assert strategy.entry_refusal(**base) is None
    cases = {
        "à l'arrêt": dict(halted="stop"),
        "seuil d'arrêt": dict(risk_equity=29),
        "pas de prix récent pour BRETT": dict(unpriced=["BRETT"]),
        "vendu il y a": dict(recently_sold={"AERO"}),
        "liquidité": dict(q=mkq(liq=1000, h24=15)),
        "volume": dict(q=mkq(vol=1000, h24=15)),
        "minimum": dict(usdc=1),
        "renfort interdit": dict(held_cost=5.0, held_value=4.0, usdc=2),
        "position max": dict(usdc=11),
        "déjà 5.00": dict(held_cost=5.0, held_value=5.0, usdc=6),
        "positions ouvertes": dict(open_count=3),
        "plafond": dict(entries_count=6),
        "liquidités insuffisantes": dict(cash=10.3),
    }
    for expected, override in cases.items():
        why = strategy.entry_refusal(**{**base, **override})
        assert why and expected in why, (expected, why)
    # un renfort sur une position en gain modéré reste permis, dans la limite de taille
    assert strategy.entry_refusal(**{**base, "held_cost": 5.0, "held_value": 5.5, "usdc": 5}) is None
