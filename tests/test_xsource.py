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


def test_attention_is_unmeasured_until_every_needed_hour_is_counted(x_conn):
    a = xsource.attention(x_conn, "AERO", T0)
    assert a["ratio"] is None and "42 heure(s) sans comptage sur 42" in a["reason"]


def test_attention_compares_with_the_same_hours_of_previous_days(token, net, x_conn, book_conn):
    end = xsource._window_end(T0)                # 11:00 : à 12:00:05, l'heure en cours n'est pas complète
    recent = {end - timedelta(hours=k) for k in range(1, 7)}
    # Rythme quotidien marqué (300 posts/h de 05:00 à 11:00, 50 le reste du temps), puis un vrai regain
    # aujourd'hui sur ces mêmes heures (600/h). Le ratio doit voir le regain, pas le rythme de la journée.
    net.x_rate = lambda sym, h: 600 if h in recent else (300 if 5 <= h.hour < 11 else 50)
    assert counts(net, x_conn, book_conn) == xsource.WINDOW_HOURS
    a = xsource.attention(x_conn, "AERO", T0)
    assert a["recent"] == 3600 and a["baseline"] == 1800 and a["ratio"] == pytest.approx(2.0)


def test_daily_rhythm_alone_reads_as_neutral(token, net, x_conn, book_conn):
    net.x_rate = lambda sym, h: 300 if 5 <= h.hour < 11 else 50     # même rythme tous les jours
    counts(net, x_conn, book_conn)
    assert xsource.attention(x_conn, "AERO", T0)["ratio"] == pytest.approx(1.0)


def test_attention_does_not_depend_on_how_often_we_collect(token, net, x_conn, book_conn):
    for k in range(8):                                   # 8 cycles rapprochés, activité constante
        counts(net, x_conn, book_conn, now=T0 + timedelta(minutes=5 * k))
    assert xsource.attention(x_conn, "AERO", T0 + timedelta(minutes=40))["ratio"] == pytest.approx(1.0)


def test_known_hours_are_not_paid_for_twice(token, net, x_conn, book_conn):
    counts(net, x_conn, book_conn)
    paid = len(net.requests)
    assert counts(net, x_conn, book_conn, now=T0 + timedelta(seconds=20)) == 0     # aucune heure nouvelle : rien à payer
    assert len(net.requests) == paid
    assert xsource.spent_this_month(book_conn, T0) == pytest.approx(X_BUDGET.count_request_usd)
    counts(net, x_conn, book_conn, now=T0 + timedelta(hours=1))                   # une heure de plus : une requête
    assert len(net.requests) == paid + 1


def test_attention_becomes_unmeasured_when_counts_stop(token, net, x_conn, book_conn):
    counts(net, x_conn, book_conn)
    later = T0 + timedelta(hours=7)                      # panne X : plus aucun comptage depuis 7 h
    assert xsource.attention(x_conn, "AERO", later)["ratio"] is None   # ni 0 (vente), ni 2 (achat)


def test_thin_baseline_gives_no_ratio(token, net, x_conn, book_conn):
    net.x_rate = lambda sym, h: 2                       # 12 posts par tranche de 6 h : trop peu pour mesurer
    counts(net, x_conn, book_conn)
    a = xsource.attention(x_conn, "AERO", T0)
    assert a["ratio"] is None and "trop peu" in a["reason"]


def test_counts_request_only_complete_hours_within_seven_days(token, net, x_conn, book_conn):
    counts(net, x_conn, book_conn)
    q = dict(net.requests[-1].url.params)
    # T0 = 12:00:05 : X exige une fin au moins 10 s dans le passé, donc la fenêtre s'arrête à 11:00
    assert q["end_time"] == "2026-11-03T11:00:00Z" and q["start_time"] == "2026-10-28T05:00:00Z"
    assert q["granularity"] == "hour"


def test_budget_is_reserved_before_the_request_and_settled_after(token, net, x_conn, book_conn, book_path):
    from crypto_agent import db
    seen = []
    handler = net.handler

    def spy(request):
        other = db.open_book(book_path)        # ce qu'un appel concurrent verrait pendant la requête
        seen.append(xsource.spent_this_month(other, T0))
        other.close()
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(spy)) as c:
        xsource.fetch_counts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], c, T0)
    assert seen == [pytest.approx(X_BUDGET.count_request_usd)]           # déjà réservé pendant la requête
    assert xsource.spent_this_month(book_conn, T0) == pytest.approx(X_BUDGET.count_request_usd)


def test_budget_is_enforced_before_any_request(token, net, x_conn, book_conn, monkeypatch):
    monkeypatch.setattr(xsource, "X_BUDGET", X_BUDGET.__class__(monthly_usd=0.006))
    counts(net, x_conn, book_conn, sym="AERO")
    n = len(net.requests)
    with pytest.raises(xsource.XError, match="budget X du mois"):
        counts(net, x_conn, book_conn, sym="BRETT")
    assert len(net.requests) == n                         # refusé sans rien envoyer


def test_failed_requests_are_refunded(token, net, x_conn, book_conn):
    net.x_status = 429
    with pytest.raises(xsource.XError, match="429"):
        counts(net, x_conn, book_conn)
    assert xsource.spent_this_month(book_conn, T0) == 0


def test_bad_token_is_rejected_without_echoing_it(monkeypatch, net, x_conn, book_conn):
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET-TOKEN-XYZ\r")
    counts(net, x_conn, book_conn)                       # un \r final est retiré : rien ne casse
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET TOKEN-XYZ")
    with pytest.raises(xsource.XError) as e:
        counts(net, x_conn, book_conn, sym="BRETT")
    assert "SECRET" not in str(e.value)


def test_transport_errors_never_carry_the_token(monkeypatch, x_conn, book_conn):
    monkeypatch.setenv("X_BEARER_TOKEN", "SECRET-TOKEN-XYZ")

    def boom(request):
        raise httpx.LocalProtocolError(f"Illegal header value {request.headers['authorization']!r}")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        with pytest.raises(xsource.XError) as e:
            xsource.fetch_counts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], c, T0)
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


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


def test_cursor_is_saved_after_the_first_page_even_if_a_later_page_fails(token, net, x_conn, book_conn):
    posts = mkposts(150)

    def flaky(request):
        # X peut renvoyer moins de posts que demandé : première page de 50, puis une panne.
        if "next_token" in request.url.params:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": posts[:50], "meta": {"newest_id": posts[0]["id"], "next_token": "p2"}})

    with httpx.Client(transport=httpx.MockTransport(flaky)) as c:
        with pytest.raises(xsource.XError, match="503"):
            xsource.collect_posts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], 100, c, T0)
    row = x_conn.execute("SELECT newest_id FROM x_cursor WHERE symbol = 'AERO'").fetchone()
    assert row and row["newest_id"] == posts[0]["id"]
    assert xsource.spent_this_month(book_conn, T0) == pytest.approx(50 * X_BUDGET.post_read_usd)  # page 2 remboursée


def test_old_cursor_is_dropped_before_x_refuses_it(token, net, x_conn, book_conn):
    net.x_posts["AERO"] = mkposts(20)
    with net.client() as c:
        xsource.collect_posts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], 10, c, T0)
        xsource.collect_posts(x_conn, book_conn, "AERO", X_QUERIES["AERO"], 10, c, T0 + timedelta(days=8))
    assert "since_id" not in net.requests[-1].url.params


def test_long_posts_are_stored_in_full(x_conn):
    long_text = "aerodrome " * 60
    xsource.store_posts(x_conn, "AERO", [{"id": "9", "text": "aerodrome tronqué…", "created_at": "2026-11-03T10:00:00Z",
                                          "note_tweet": {"text": long_text}}])
    assert x_conn.execute("SELECT text FROM posts WHERE id = '9'").fetchone()[0] == long_text


def test_natural_questions_find_posts_containing_any_word(x_conn):
    xsource.store_posts(x_conn, "AERO", [
        {"id": "1", "text": "Aerodrome emissions cut announced", "created_at": "2026-11-03T10:00:00Z"},
        {"id": "2", "text": "unrelated chatter", "created_at": "2026-11-03T10:00:00Z"},
    ])
    hits = xsource.search(x_conn, "what is new with aerodrome emissions this week?")
    assert [h["id"] for h in hits] == ["1"]
