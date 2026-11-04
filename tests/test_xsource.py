"""Données X : attention par comptage, budget, collecte de posts, recherche."""
from datetime import timedelta

import httpx
import pytest

from crypto_agent import xsource
from crypto_agent.config import X_BUDGET, X_QUERIES

from conftest import T0


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "AAAA-test-token")


def counts(net, x_conn, book_conn, sym="AERO", now=T0):
    with net.client() as c:
        return xsource.fetch_counts(x_conn, book_conn, sym, X_QUERIES[sym], c, now)


def test_attention_is_unmeasured_until_30_hours_are_counted(x_conn):
    a = xsource.attention(x_conn, "AERO", T0)
    assert a["ratio"] is None and "30" in a["reason"]


def test_attention_ratio_from_hourly_counts(token, net, x_conn, book_conn):
    end = xsource._window_end(T0)            # 11:00 : à 12:00:05, l'heure en cours n'est pas complète
    net.x_rate = lambda sym, h: 300 if h >= end - timedelta(hours=6) else 100
    assert counts(net, x_conn, book_conn) == 30
    a = xsource.attention(x_conn, "AERO", T0)
    assert a["recent"] == 1800 and a["baseline"] == 600 and a["ratio"] == pytest.approx(3.0)


def test_attention_does_not_depend_on_how_often_we_collect(token, net, x_conn, book_conn):
    for k in range(8):                                   # 8 collectes rapprochées, activité constante
        counts(net, x_conn, book_conn, now=T0 + timedelta(minutes=5 * k))
    assert xsource.attention(x_conn, "AERO", T0 + timedelta(minutes=40))["ratio"] == pytest.approx(1.0)


def test_attention_becomes_unmeasured_when_counts_stop(token, net, x_conn, book_conn):
    counts(net, x_conn, book_conn)
    later = T0 + timedelta(hours=7)                      # panne X : plus aucun comptage depuis 7 h
    a = xsource.attention(x_conn, "AERO", later)
    assert a["ratio"] is None                            # jamais 0 (qui ferait vendre), jamais 2 (qui ferait acheter)


def test_zero_baseline_gives_no_ratio(token, net, x_conn, book_conn):
    net.x_rate = lambda sym, h: 0
    counts(net, x_conn, book_conn)
    assert xsource.attention(x_conn, "AERO", T0)["ratio"] is None


def test_counts_request_only_complete_hours(token, net, x_conn, book_conn):
    counts(net, x_conn, book_conn)
    q = dict(net.requests[-1].url.params)
    # T0 = 12:00:05 : X exige une fin au moins 10 s dans le passé, donc la fenêtre s'arrête à 11:00
    assert q["end_time"] == "2026-11-03T11:00:00Z" and q["start_time"] == "2026-11-02T05:00:00Z"
    assert q["granularity"] == "hour"


def test_budget_is_recorded_and_enforced(token, net, x_conn, book_conn, monkeypatch):
    counts(net, x_conn, book_conn)
    assert xsource.spent_this_month(book_conn, T0) == pytest.approx(X_BUDGET.count_request_usd)
    monkeypatch.setattr(xsource, "X_BUDGET", X_BUDGET.__class__(monthly_usd=0.006))
    with pytest.raises(xsource.XError, match="budget X du mois"):
        counts(net, x_conn, book_conn)
        counts(net, x_conn, book_conn)


def test_bad_token_is_rejected_without_echoing_it(monkeypatch, net, x_conn, book_conn):
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET-TOKEN-XYZ\r")
    # la valeur est nettoyée (strip) : un \r final ne casse rien
    counts(net, x_conn, book_conn)
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET TOKEN-XYZ")
    with pytest.raises(xsource.XError) as e:
        counts(net, x_conn, book_conn)
    assert "SECRET" not in str(e.value)


def test_transport_errors_never_carry_the_token(monkeypatch, x_conn, book_conn):
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET-TOKEN-XYZ")

    def boom(request):
        raise httpx.LocalProtocolError(f"Illegal header value {request.headers['authorization']!r}")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(xsource.XError) as e:
            xsource.fetch_counts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], c, T0)
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_http_errors_are_reported_with_status_only(token, net, x_conn, book_conn):
    net.x_status = 429
    with pytest.raises(xsource.XError, match="429"):
        counts(net, x_conn, book_conn)
    assert xsource.spent_this_month(book_conn, T0) == 0      # une requête refusée n'est pas comptée


# --- collecte de posts et recherche -----------------------------------------

def mkposts(n, start=1000, text="aerodrome volume"):
    return [{"id": str(start + n - i), "text": f"{text} {i}", "created_at": "2026-11-03T11:00:00.000Z",
             "public_metrics": {"like_count": 1}} for i in range(n)]


def test_collect_paginates_respects_max_and_resumes_after_the_cursor(token, net, x_conn, book_conn):
    net.x_posts["AERO"] = mkposts(150)
    with net.client() as c:
        r = xsource.collect_posts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], 100, c, T0)
    assert r["fetched"] == 100 and r["truncated"] is True
    assert xsource.spent_this_month(book_conn, T0) == pytest.approx(100 * X_BUDGET.post_read_usd)
    net.x_posts["AERO"] = mkposts(5, start=2000) + mkposts(150)
    with net.client() as c:
        r = xsource.collect_posts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], 50, c, T0)
    assert r["fetched"] == 5                              # seulement les posts plus récents que le curseur


@pytest.mark.parametrize("bad", [0, 5, 101, -1, True, 20.5])
def test_collect_rejects_bad_sizes(token, net, x_conn, book_conn, bad):
    with pytest.raises(ValueError):
        xsource.collect_posts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], bad, net.client(), T0)


def test_a_post_can_belong_to_several_tokens(x_conn):
    p = [{"id": "7", "text": "$DEGEN and $BRETT pumping", "created_at": "2026-11-03T10:00:00.000Z"}]
    assert xsource.store_posts(x_conn, "DEGEN", p) == 1
    assert xsource.store_posts(x_conn, "BRETT", p) == 1
    assert xsource.store_posts(x_conn, "BRETT", p) == 0
    assert xsource.search(x_conn, "pumping", "BRETT")[0]["symbols"] in ("DEGEN,BRETT", "BRETT,DEGEN")


@pytest.mark.parametrize("query", ["$BRETT", "#Base", "@jessepollak", "AERO/WETH", "don't", "base-chain",
                                   "symbol:BRETT", '"unbalanced', "NEAR(a b)", "a OR"])
def test_search_accepts_natural_text(x_conn, query):
    xsource.store_posts(x_conn, "BRETT", [{"id": "1", "text": "$BRETT on #Base by @jessepollak, AERO/WETH don't "
                                           "base-chain symbol BRETT unbalanced near a b or",
                                           "created_at": "2026-11-03T10:00:00.000Z"}])
    assert [r["id"] for r in xsource.search(x_conn, query)] == ["1"]


@pytest.mark.parametrize("limit", [0, -1, 101, True])
def test_search_limit_is_bounded(x_conn, limit):
    with pytest.raises(ValueError):
        xsource.search(x_conn, "base", limit=limit)


def test_empty_query_is_refused(x_conn):
    with pytest.raises(ValueError, match="vide"):
        xsource.search(x_conn, "$$$ ###")
