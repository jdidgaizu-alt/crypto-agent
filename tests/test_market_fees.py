"""Prix DexScreener, frais réels des pools, nœud Base."""
import httpx
import pytest

from crypto_agent import chain, config, fees, market
from crypto_agent.config import WHITELIST

from conftest import T0, FakeNet, pair, pool_of


def fetch(net, tokens=None):
    with net.client() as c:
        return market.fetch_quotes(tokens or WHITELIST, c)


def test_parse_real_dexscreener_pairs():
    """Extrait d'une vraie réponse DexScreener du 03/11/2026 (AERO sur Base, deux pools USDC)."""
    a = WHITELIST["AERO"]
    usdc = config.USDC
    pairs = [
        {"chainId": "base", "dexId": "uniswap", "pairAddress": "0xE5B5f522E98B5a2baAe212d4dA66b865B781DB97",
         "baseToken": {"address": a, "symbol": "AERO"}, "quoteToken": {"address": usdc, "symbol": "USDC"},
         "priceUsd": "0.6325", "volume": {"h24": 408631.22}, "priceChange": {"h1": -0.76, "h24": 1.82},
         "liquidity": {"usd": 177755.57}, "url": "u"},
        {"chainId": "base", "dexId": "aerodrome", "pairAddress": "0x6cDcb1C4A4D1C3C6d054b27AC5B77e89eAFb971d",
         "baseToken": {"address": a, "symbol": "AERO"}, "quoteToken": {"address": usdc, "symbol": "USDC"},
         "priceUsd": "0.6340", "volume": {"h24": 492656.08}, "priceChange": {"m5": -0.13, "h1": -0.57, "h24": 1.82},
         "liquidity": {"usd": 5434981.89}, "url": "https://dexscreener.com/base/0x6cdc"},
    ]
    best = market.pick_best_pair(pairs, a)
    q = market.parse_quote("AERO", a, best, pairs)
    assert q.price_usd == 0.634 and q.liquidity_usd == pytest.approx(5434981.89)
    assert q.volume_24h_usd == pytest.approx(408631.22 + 492656.08)   # tous les pools du jeton
    assert q.dex_id == "aerodrome" and q.quote_symbol == "USDC" and q.change_1h == -0.57


def test_one_request_per_token(net):
    for sym in WHITELIST:
        net.dex[sym] = [pair(sym)]
    quotes, errors = fetch(net)
    paths = [r.url.path for r in net.requests]
    assert len(paths) == len(WHITELIST) and all("," not in p for p in paths)
    assert set(quotes) == set(WHITELIST) and errors == {}


def test_one_failing_token_does_not_drop_the_others(net):
    for sym in WHITELIST:
        net.dex[sym] = [pair(sym)]
    net.dex["BRETT"] = 500
    net.dex["DEGEN"] = [pair("DEGEN", price=None)]
    quotes, errors = fetch(net)
    assert set(quotes) == set(WHITELIST) - {"BRETT", "DEGEN"}
    assert "500" in errors["BRETT"] and "prix" in errors["DEGEN"]
    assert len([r for r in net.requests if r.url.path.endswith(WHITELIST["BRETT"])]) == 2   # une relance


def test_exotic_quote_tokens_and_other_chains_are_ignored(net):
    net.dex["AERO"] = [pair("AERO", price=6.34, liq=9.9e9, quote="SCAM"),
                       pair("AERO", price=9.0, liq=9e9, chain="ethereum"),
                       pair("AERO", price=0.63, liq=5e6, quote="USDC")]
    quotes, _ = fetch(net, {"AERO": WHITELIST["AERO"]})
    assert quotes["AERO"].price_usd == 0.63 and quotes["AERO"].volume_24h_usd == 1e6


def test_symbol_mismatch_is_refused(net):
    net.dex["AERO"] = [pair("AERO", base_symbol="SCAM")]
    quotes, errors = fetch(net, {"AERO": WHITELIST["AERO"]})
    assert quotes == {} and "symbole" in errors["AERO"]


def test_weth_quoted_pair_is_kept_and_flagged(net):
    net.dex["BRETT"] = [pair("BRETT", quote="WETH", liq=1e6)]
    quotes, _ = fetch(net, {"BRETT": WHITELIST["BRETT"]})
    assert quotes["BRETT"].quote_symbol == "WETH"


# --- frais ---------------------------------------------------------------------

def test_fee_read_on_chain_then_cached(net, book_conn):
    net.dex["AERO"] = [pair("AERO")]
    net.rpc_fees[pool_of("AERO")] = 500
    quotes, _ = fetch(net, {"AERO": WHITELIST["AERO"]})
    with net.client() as c:
        src = fees.resolve(book_conn, quotes, c, T0)
    assert src["AERO"] == "blockchain" and quotes["AERO"].fee_rate == 0.0005
    assert fees.total_fee_rate(quotes["AERO"]) == 0.0005
    net.rpc_fees.clear()
    quotes, _ = fetch(net, {"AERO": WHITELIST["AERO"]})
    with net.client() as c:
        fees.resolve(book_conn, quotes, c, T0)
    assert quotes["AERO"].fee_rate == 0.0005          # lu dans le cache, pas sur la blockchain


def test_unknown_or_unreachable_fee_falls_back_to_prudent_default(net, book_conn):
    for sym in ("AERO", "BRETT"):
        net.dex[sym] = [pair(sym)]
    quotes, _ = fetch(net, {s: WHITELIST[s] for s in ("AERO", "BRETT")})
    with net.client() as c:
        src = fees.resolve(book_conn, quotes, c, T0)          # revert : pas un pool concentré
    assert quotes["AERO"].fee_rate is None and "1.0%" in src["AERO"]
    assert fees.total_fee_rate(quotes["AERO"]) == 0.01
    net.rpc_status = 403
    n_before = len(net.requests)
    with net.client() as c:
        fees.resolve(book_conn, quotes, c, T0)
    assert len(net.requests) - n_before == 1                  # nœud injoignable : on n'insiste pas


def test_rpc_errors_never_reveal_the_rpc_url(monkeypatch):
    monkeypatch.setattr(chain, "BASE_RPC_URL", "https://base.example/v2/SECRET-KEY-123")

    def boom(request):
        raise httpx.ConnectError("connexion refusée vers https://base.example/v2/SECRET-KEY-123")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(chain.RPCUnavailable) as e:
            chain.block_number(c)
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_addresses_are_validated():
    with pytest.raises(ValueError, match="adresse invalide"):
        chain.check_address("0x123")
    assert chain.check_address(WHITELIST["AERO"]) == WHITELIST["AERO"]


def test_malformed_replies_only_drop_the_token_concerned(net):
    for sym in WHITELIST:
        net.dex[sym] = [pair(sym)]
    handler = net.handler

    def odd(request):
        if request.url.path.endswith(WHITELIST["AERO"]):
            return httpx.Response(200, json=["pas", "un", "objet"])
        if request.url.path.endswith(WHITELIST["BRETT"]):
            return httpx.Response(200, json={"pairs": [{**pair("BRETT"), "baseToken": "texte"}]})
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(odd)) as c:
        quotes, errors = market.fetch_quotes(WHITELIST, c)
    assert set(quotes) == set(WHITELIST) - {"AERO", "BRETT"}
    assert "AERO" in errors and "BRETT" in errors


def test_misconfigured_rpc_url_is_reported_without_crashing(monkeypatch):
    monkeypatch.setattr(chain, "BASE_RPC_URL", "https://base.example/KEY\r\n")
    with httpx.Client() as c:
        with pytest.raises(chain.RPCUnavailable) as e:
            chain.block_number(c)
    assert "KEY" not in str(e.value)


def test_non_object_rpc_reply_is_a_clean_error():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2]))) as c:
        with pytest.raises(chain.RPCUnavailable, match="objet JSON"):
            chain.block_number(c)
