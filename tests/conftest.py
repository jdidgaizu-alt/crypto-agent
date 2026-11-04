"""Outillage commun : horloge fixe, bases temporaires, faux réseau (DexScreener, X, nœud Base)."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from crypto_agent import db, market, portfolio
from crypto_agent.config import NATIVE_ETH, USDC, WHITELIST, X_QUERIES
from crypto_agent.market import Quote

T0 = datetime(2026, 11, 3, 12, 0, 5, tzinfo=timezone.utc)
QUOTE_ADDR = {"USDC": USDC, "WETH": WHITELIST["WETH"], "ETH": NATIVE_ETH, "SCAM": "0x" + "5" * 40}


def pool_of(sym: str, quote: str = "USDC") -> str:
    return "0x" + hashlib.sha1(f"{sym}/{quote}".encode()).hexdigest()[:40]


def pair(sym, price=1.0, liq=5e6, vol=1e6, h1=0.0, h24=0.0, quote="USDC", chain="base", dex="uniswap",
         pool=None, base_symbol=None):
    return {
        "chainId": chain, "dexId": dex, "url": f"https://dexscreener.com/base/{sym}",
        "pairAddress": pool or pool_of(sym, quote),
        "baseToken": {"address": WHITELIST[sym], "symbol": base_symbol or sym},
        "quoteToken": {"address": QUOTE_ADDR[quote], "symbol": quote},
        "priceUsd": None if price is None else str(price),
        "liquidity": {"usd": liq}, "volume": {"h24": vol}, "priceChange": {"h1": h1, "h24": h24},
    }


def mkq(sym="AERO", price=1.0, liq=5e6, vol=1e6, h1=0.0, h24=0.0, quote="USDC", fee=0.003) -> Quote:
    return Quote(symbol=sym, address=WHITELIST.get(sym, "0x0"), price_usd=price, liquidity_usd=liq,
                 volume_24h_usd=vol, change_1h=h1, change_24h=h24, pair_url="", pair_address=pool_of(sym, quote),
                 dex_id="uniswap", quote_symbol=quote, fee_rate=fee)


class FakeNet:
    """Faux Internet. dex[sym] : liste de paires, ou un code HTTP d'erreur.
    x_rate(sym, heure) : posts par heure. rpc_fees[pool] : frais en millionièmes (absent = revert)."""

    def __init__(self):
        self.dex: dict = {}
        self.x_rate = lambda sym, hour: 100
        self.x_status = 200
        self.x_posts: dict[str, list[dict]] = {}
        self.rpc_fees: dict[str, int] = {}
        self.rpc_status = 200
        self.requests: list[httpx.Request] = []

    def set_prices(self, **kw):
        """set_prices(AERO=dict(price=1, h24=5), ...) : une paire USDC par jeton."""
        for sym, args in kw.items():
            self.dex[sym] = [pair(sym, **args)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.dexscreener.com":
            addr = path.rsplit("/", 1)[-1].lower()
            sym = next(s for s, a in WHITELIST.items() if a.lower() == addr)
            val = self.dex.get(sym, [])
            if isinstance(val, int):
                return httpx.Response(val, json={"error": "boom"})
            return httpx.Response(200, json={"schemaVersion": "1.0.0", "pairs": val})
        if host == "api.x.com":
            if self.x_status != 200:
                return httpx.Response(self.x_status, json={"title": "Too Many Requests"})
            q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
            sym = next(s for s, query in X_QUERIES.items() if query == q["query"])
            if path.endswith("/counts/recent"):
                start = datetime.fromisoformat(q["start_time"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(q["end_time"].replace("Z", "+00:00"))
                data, h = [], start
                while h < end:
                    data.append({"start": h.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                 "end": (h + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                 "tweet_count": self.x_rate(sym, h)})
                    h += timedelta(hours=1)
                return httpx.Response(200, json={"data": data, "meta": {"total_tweet_count": sum(
                    d["tweet_count"] for d in data)}})
            if path.endswith("/search/recent"):
                posts = self.x_posts.get(sym, [])
                if "since_id" in q:
                    posts = [p for p in posts if int(p["id"]) > int(q["since_id"])]
                offset = int(q.get("next_token", "0"))
                size = int(q["max_results"])
                page = posts[offset:offset + size]
                meta = {"result_count": len(page)}
                if page:
                    meta["newest_id"] = posts[0]["id"]
                if offset + size < len(posts):
                    meta["next_token"] = str(offset + size)
                return httpx.Response(200, json={"data": page, "meta": meta} if page else {"meta": meta})
        if host == "mainnet.base.org":
            if self.rpc_status != 200:
                return httpx.Response(self.rpc_status)
            body = json.loads(request.content)
            call = (body.get("params") or [{}])[0]
            if body["method"] == "eth_call" and isinstance(call, dict) and call.get("data") == "0xddca3f43":
                fee = self.rpc_fees.get(call["to"].lower())
                if fee is None:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                     "error": {"code": 3, "message": "execution reverted"}})
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(fee)})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x10"})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(market.time, "sleep", lambda s: None)


@pytest.fixture(autouse=True)
def no_x_token(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)


@pytest.fixture
def net():
    return FakeNet()


@pytest.fixture
def book_path(tmp_path):
    path = tmp_path / "book.db"
    conn = db.open_book(path, create=True)
    for name in ("auto", "claude"):
        portfolio.create_book(conn, name, 50.0, now=T0 - timedelta(days=1))
    conn.close()
    return path


@pytest.fixture
def book_conn(book_path):
    conn = db.open_book(book_path)
    yield conn
    conn.close()


@pytest.fixture
def x_conn(tmp_path):
    conn = db.open_x(tmp_path / "x.db")
    yield conn
    conn.close()


def all_flat(net: FakeNet, **overrides):
    """Tous les jetons cotés à plat (prix 2, aucune variation), sauf surcharges."""
    for sym in WHITELIST:
        args = dict(price=2.0, h1=0.0, h24=0.0)
        args.update(overrides.get(sym, {}))
        net.dex[sym] = [pair(sym, **args)]
