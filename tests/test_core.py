from datetime import datetime, timedelta, timezone

import httpx
import pytest

from crypto_agent import cycle, market, portfolio, strategy, xsource
from crypto_agent.config import COSTS, RISK, WHITELIST
from crypto_agent.db import connect
from crypto_agent.market import Quote


@pytest.fixture
def conn():
    c = connect(":memory:")
    portfolio.init(c, 50.0)
    return c


def q(sym="AERO", price=1.0, liq=5_000_000, vol=2_000_000, h1=0.0, h24=0.0):
    return Quote(sym, WHITELIST.get(sym, "0x0"), price, liq, vol, h1, h24, "")


# --- portefeuille ---------------------------------------------------------

def test_buy_then_sell_flat_price_loses_only_costs(conn):
    portfolio.buy(conn, "AERO", 10, 1.0, 5_000_000, "test")
    assert portfolio.cash(conn) == pytest.approx(50 - 10 - COSTS.gas_usd)
    r = portfolio.sell_all(conn, "AERO", 1.0, 5_000_000, "test")
    lost = 50 - portfolio.cash(conn)
    # Perte = 2 × frais de pool + 2 × gaz + impact, rien d'autre.
    assert 0 < lost < 10 * 2 * COSTS.dex_fee + 2 * COSTS.gas_usd + 0.01
    assert r["pnl"] < 0
    assert portfolio.positions(conn) == {}


def test_buy_refused_when_impact_too_high(conn):
    with pytest.raises(ValueError, match="impact"):
        portfolio.buy(conn, "AERO", 10, 1.0, 500, "test")


def test_buy_refused_without_cash(conn):
    with pytest.raises(ValueError, match="liquidités"):
        portfolio.buy(conn, "AERO", 60, 1.0, 50_000_000, "test")


def test_equity_tracks_price(conn):
    portfolio.buy(conn, "AERO", 10, 1.0, 50_000_000, "test")
    assert portfolio.equity(conn, {"AERO": 2.0}) > portfolio.equity(conn, {"AERO": 1.0}) + 9


# --- stratégie et garde-fous ---------------------------------------------

def test_kill_switch_halts_everything():
    d = strategy.decide({"AERO": q(h24=10)}, {"AERO": 3}, {}, 29, 29, 0)
    assert [x.action for x in d] == ["halt"]


def test_stop_loss_and_take_profit():
    pos = {"AERO": {"qty": 10, "cost_usdc": 10}}
    assert strategy.decide({"AERO": q(price=0.8)}, {}, pos, 40, 48, 0)[0].reason.startswith("stop-loss")
    assert strategy.decide({"AERO": q(price=1.3)}, {}, pos, 40, 53, 0)[0].reason.startswith("prise")


def test_position_size_and_count_limits():
    quotes = {s: q(s, h24=15, h1=3) for s in WHITELIST}
    att = {s: 3.0 for s in WHITELIST}
    buys = [d for d in strategy.decide(quotes, att, {}, 50, 50, 0) if d.action == "buy"]
    assert len(buys) == RISK.max_open_positions
    assert all(d.usdc <= RISK.start_capital_usdc * RISK.max_position_frac for d in buys)
    assert sum(d.usdc for d in buys) <= 50


def test_no_buy_on_illiquid_or_overheated_token():
    d = strategy.decide({"BRETT": q("BRETT", liq=10_000, h24=15, h1=3)}, {"BRETT": 3}, {}, 50, 50, 0)
    assert d[0].action == "hold"
    d = strategy.decide({"BRETT": q("BRETT", h24=80, h1=3)}, {"BRETT": 3}, {}, 50, 50, 0)
    assert d[0].action == "hold"


def test_daily_trade_cap():
    quotes = {s: q(s, h24=15, h1=3) for s in WHITELIST}
    d = strategy.decide(quotes, {s: 3 for s in WHITELIST}, {}, 50, 50, RISK.max_trades_per_day)
    assert d[0].action == "hold"


# --- marché ---------------------------------------------------------------

def test_quote_rejects_symbol_mismatch():
    pair = {"chainId": "base", "baseToken": {"address": WHITELIST["AERO"], "symbol": "SCAM"},
            "priceUsd": "1", "liquidity": {"usd": 1e7}}
    assert market.parse_quote("AERO", WHITELIST["AERO"], pair) is None


def test_pick_best_pair_ignores_other_chains():
    a = WHITELIST["AERO"]
    pairs = [
        {"chainId": "ethereum", "baseToken": {"address": a}, "liquidity": {"usd": 9e9}},
        {"chainId": "base", "baseToken": {"address": a.lower()}, "liquidity": {"usd": 1e6}, "id": "ok"},
        {"chainId": "base", "baseToken": {"address": a}, "liquidity": {"usd": 1e3}},
    ]
    assert market.pick_best_pair(pairs, a)["id"] == "ok"


def test_parse_real_dexscreener_pairs():
    """Extrait d'une vraie réponse DexScreener du 03/11/2026 (AERO sur Base, deux pools)."""
    a = WHITELIST["AERO"]
    pairs = [
        {"chainId": "base", "dexId": "uniswap", "baseToken": {"address": a, "symbol": "AERO"},
         "priceUsd": "0.6325", "volume": {"h24": 408631.22}, "priceChange": {"h1": -0.76, "h24": 1.82},
         "liquidity": {"usd": 177755.57}, "url": "u"},
        {"chainId": "base", "dexId": "aerodrome", "baseToken": {"address": a, "symbol": "AERO"},
         "priceUsd": "0.6340", "volume": {"h24": 492656.08}, "priceChange": {"m5": -0.13, "h1": -0.57, "h24": 1.82},
         "liquidity": {"usd": 5434981.89}, "url": "https://dexscreener.com/base/0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d"},
    ]
    qt = market.parse_quote("AERO", a, market.pick_best_pair(pairs, a))
    assert qt.price_usd == 0.634 and qt.liquidity_usd == pytest.approx(5434981.89)
    assert qt.change_1h == -0.57 and qt.volume_24h_usd == pytest.approx(492656.08)


def test_fetch_quotes_queries_each_token_separately():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"pairs": []})

    market.fetch_quotes(WHITELIST, httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(seen) == len(WHITELIST) and all("," not in p for p in seen)


# --- X / RAG --------------------------------------------------------------

def test_x_store_search_and_attention(conn):
    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    iso = lambda h: (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")
    posts = [{"id": str(i), "created_at": iso(1), "text": f"aerodrome volume exploding {i}",
              "public_metrics": {"like_count": 10}} for i in range(8)]
    posts += [{"id": f"o{i}", "created_at": iso(20), "text": "aerodrome quiet day"} for i in range(4)]
    assert xsource.store_posts(conn, "AERO", posts) == 12
    assert xsource.store_posts(conn, "AERO", posts) == 0  # pas de doublons
    assert len(xsource.search(conn, "exploding")) == 8
    assert xsource.attention(conn, "AERO", now)["ratio"] > 5


# --- cycle complet avec une API de prix simulée ----------------------------

def test_full_cycle_with_mocked_dexscreener(conn, monkeypatch):
    def handler(request):
        pairs = [{"chainId": "base", "url": "", "baseToken": {"address": a, "symbol": s},
                  "priceUsd": "2.0", "liquidity": {"usd": 5e6}, "volume": {"h24": 1e6},
                  "priceChange": {"h1": 3, "h24": 18}} for s, a in WHITELIST.items()
                 if a.lower() in str(request.url).lower()]
        return httpx.Response(200, json={"pairs": pairs})

    real = market.fetch_quotes
    monkeypatch.setattr(cycle, "fetch_quotes",
                        lambda t: real(t, httpx.Client(transport=httpx.MockTransport(handler))))
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    log = cycle.run(conn)
    assert log["benchmark_usdc"] == pytest.approx(50)
    assert any(d["action"] == "buy" for d in log["decisions"])
    assert log["equity_usdc"] < 50  # frais payés, prix inchangés
    assert any("X_BEARER_TOKEN" in e for e in log["errors"])
