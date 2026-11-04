"""Données X : comptage horaire des posts (signal d'attention) et collecte de posts (recherche RAG).

Le signal d'attention vient de l'API de comptage (GET /2/tweets/counts/recent) : le nombre
EXACT de posts par heure, quelle que soit la fréquence des cycles. Il n'est calculé que si
les 30 heures nécessaires sont toutes connues ; sinon il vaut None (« non mesuré ») et la
stratégie l'ignore. Il n'y a jamais de valeur par défaut qui ressemble à un signal.

Lire des posts coûte 0,005 $ par post (barème X 2026) : la collecte est donc optionnelle,
plafonnée par appel et par un budget mensuel enregistré dans le livre.

Nécessite la variable d'environnement X_BEARER_TOKEN.
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from .config import X_BUDGET
from .db import write_tx

COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
RECENT_HOURS = 6
BASELINE_HOURS = 24


class XError(RuntimeError):
    """Erreur X sans donnée sensible (jamais le jeton, jamais l'en-tête)."""


def configured() -> bool:
    return bool(os.environ.get("X_BEARER_TOKEN", "").strip())


def _token() -> str:
    raw = os.environ.get("X_BEARER_TOKEN")
    if not raw or not raw.strip():
        raise XError("X_BEARER_TOKEN absent : ajoutez-le comme secret de l'environnement")
    tok = raw.strip()
    if not tok.isascii() or any(c.isspace() or not c.isprintable() for c in tok):
        raise XError("X_BEARER_TOKEN invalide (espace ou caractère de contrôle) : corrigez le secret")
    return tok


def _get(client: httpx.Client, url: str, params: dict) -> dict:
    try:
        r = client.get(url, params=params, headers={"Authorization": f"Bearer {_token()}"})
    except httpx.HTTPError as e:
        raise XError(f"X injoignable ({type(e).__name__})") from None
    if r.status_code != 200:
        title = ""
        try:
            title = str(r.json().get("title", ""))[:80]
        except ValueError:
            pass
        raise XError(f"X a répondu {r.status_code}{' : ' + title if title else ''}")
    try:
        body = r.json()
    except ValueError:
        raise XError("réponse X illisible") from None
    if not isinstance(body, dict):
        raise XError("réponse X inattendue")
    return body


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_end(now: datetime) -> datetime:
    """Fin de la dernière heure complète (X exige une fin au moins 10 s dans le passé)."""
    t = now.astimezone(timezone.utc) - timedelta(seconds=30)
    return t.replace(minute=0, second=0, microsecond=0)


# --- budget -----------------------------------------------------------------

def _month(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def spent_this_month(book_conn: sqlite3.Connection, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    row = book_conn.execute("SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE month = ?", (_month(now),)).fetchone()
    return float(row[0])


def _check_budget(book_conn: sqlite3.Connection, usd: float, now: datetime) -> None:
    spent = spent_this_month(book_conn, now)
    if spent + usd > X_BUDGET.monthly_usd + 1e-9:
        raise XError(f"budget X du mois atteint ({spent:.3f} $ dépensés sur {X_BUDGET.monthly_usd:.2f} $)")


def _record(book_conn: sqlite3.Connection, kind: str, units: int, usd: float, now: datetime) -> None:
    with write_tx(book_conn):
        book_conn.execute(
            "INSERT INTO x_spend(month, kind, units, usd) VALUES (?,?,?,?) ON CONFLICT(month, kind) "
            "DO UPDATE SET units = units + excluded.units, usd = usd + excluded.usd",
            (_month(now), kind, units, usd),
        )


# --- comptage et attention --------------------------------------------------

def fetch_counts(x_conn: sqlite3.Connection, book_conn: sqlite3.Connection, symbol: str, query: str,
                 client: httpx.Client, now: datetime | None = None) -> int:
    """Récupère le nombre de posts par heure complète sur les 30 dernières heures."""
    now = now or datetime.now(timezone.utc)
    end = _window_end(now)
    start = end - timedelta(hours=RECENT_HOURS + BASELINE_HOURS)
    _check_budget(book_conn, X_BUDGET.count_request_usd, now)
    body = _get(client, COUNTS_URL, {"query": query, "granularity": "hour",
                                     "start_time": _z(start), "end_time": _z(end)})
    _record(book_conn, "counts", 1, X_BUDGET.count_request_usd, now)
    rows = []
    for b in body.get("data") or []:
        try:
            hour = datetime.fromisoformat(str(b["start"]).replace("Z", "+00:00"))
            count = int(b.get("tweet_count", b.get("post_count")))
        except (KeyError, TypeError, ValueError):
            continue
        if count >= 0 and hour.minute == 0 and start <= hour < end:
            rows.append((symbol, _z(hour), count, _z(now)))
    with write_tx(x_conn):
        x_conn.executemany(
            "INSERT INTO x_counts(symbol, hour_start, count, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(symbol, hour_start) DO UPDATE SET count = excluded.count, fetched_at = excluded.fetched_at",
            rows,
        )
    return len(rows)


def attention(x_conn: sqlite3.Connection, symbol: str, now: datetime | None = None) -> dict:
    """Posts des 6 dernières heures complètes / moyenne par tranche de 6 h sur les 24 h d'avant.
    ratio = None si une seule heure manque, ou si la période de référence est vide."""
    now = now or datetime.now(timezone.utc)
    end = _window_end(now)
    hours = [end - timedelta(hours=k) for k in range(1, RECENT_HOURS + BASELINE_HOURS + 1)]
    rows = x_conn.execute("SELECT hour_start, count FROM x_counts WHERE symbol = ? AND hour_start >= ? "
                          "AND hour_start < ?", (symbol, _z(hours[-1]), _z(end))).fetchall()
    have = {r["hour_start"]: r["count"] for r in rows}
    missing = [h for h in hours if _z(h) not in have]
    if missing:
        return {"ratio": None, "recent": None, "baseline": None,
                "reason": f"{len(missing)} heure(s) sans comptage sur {len(hours)}"}
    recent = sum(have[_z(h)] for h in hours[:RECENT_HOURS])
    baseline = sum(have[_z(h)] for h in hours[RECENT_HOURS:]) / (BASELINE_HOURS / RECENT_HOURS)
    if baseline <= 0:
        return {"ratio": None, "recent": recent, "baseline": baseline, "reason": "aucun post sur la période de référence"}
    return {"ratio": recent / baseline, "recent": recent, "baseline": baseline, "reason": None}


# --- collecte de posts (RAG) ------------------------------------------------

def store_posts(x_conn: sqlite3.Connection, symbol: str, data: list) -> int:
    """Enregistre les posts et leur lien avec le jeton. Renvoie le nombre de liens nouveaux."""
    n = 0
    with write_tx(x_conn):
        for t in data or []:
            if not isinstance(t, dict) or not all(isinstance(t.get(k), str) for k in ("id", "text", "created_at")):
                continue
            m = t.get("public_metrics") if isinstance(t.get("public_metrics"), dict) else {}
            x_conn.execute(
                "INSERT OR IGNORE INTO posts(id, created_at, author, text, likes, reposts, replies) "
                "VALUES (?,?,?,?,?,?,?)",
                (t["id"], t["created_at"], t.get("author_id"), t["text"],
                 int(m.get("like_count") or 0), int(m.get("retweet_count") or 0), int(m.get("reply_count") or 0)),
            )
            n += x_conn.execute("INSERT OR IGNORE INTO post_symbols(post_id, symbol) VALUES (?,?)",
                                (t["id"], symbol)).rowcount
    return n


def collect_posts(x_conn: sqlite3.Connection, book_conn: sqlite3.Connection, symbol: str, query: str,
                  max_posts: int, client: httpx.Client, now: datetime | None = None) -> dict:
    """Lit au plus max_posts posts nouveaux (depuis la dernière collecte), page par page."""
    now = now or datetime.now(timezone.utc)
    if isinstance(max_posts, bool) or not isinstance(max_posts, int) \
            or not 10 <= max_posts <= X_BUDGET.max_posts_per_call:
        raise ValueError(f"max_posts doit être un entier entre 10 et {X_BUDGET.max_posts_per_call}")
    row = x_conn.execute("SELECT newest_id FROM x_cursor WHERE symbol = ?", (symbol,)).fetchone()
    since_id = row["newest_id"] if row else None
    fetched = stored = 0
    newest = next_token = None
    while True:
        size = min(100, max_posts - fetched)
        if size < 10:
            break
        _check_budget(book_conn, size * X_BUDGET.post_read_usd, now)
        params = {"query": query, "max_results": size, "tweet.fields": "created_at,public_metrics,author_id"}
        if since_id:
            params["since_id"] = since_id
        if next_token:
            params["next_token"] = next_token
        body = _get(client, SEARCH_URL, params)
        data = body.get("data") if isinstance(body.get("data"), list) else []
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        _record(book_conn, "posts", len(data), len(data) * X_BUDGET.post_read_usd, now)
        stored += store_posts(x_conn, symbol, data)
        fetched += len(data)
        newest = newest or meta.get("newest_id")
        next_token = meta.get("next_token")
        if not next_token or not data:
            break
    if newest:
        with write_tx(x_conn):
            x_conn.execute("INSERT INTO x_cursor(symbol, newest_id) VALUES (?,?) "
                           "ON CONFLICT(symbol) DO UPDATE SET newest_id = excluded.newest_id", (symbol, str(newest)))
    return {"symbol": symbol, "fetched": fetched, "new_links": stored, "truncated": bool(next_token)}


_TERM = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def search(x_conn: sqlite3.Connection, text: str, symbol: str | None = None, limit: int = 20) -> list[dict]:
    """Recherche plein texte en langage naturel : « $BRETT », « #Base », « AERO/WETH », « don't »
    deviennent des termes cités, jamais de la syntaxe FTS5."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit doit être un entier entre 1 et 100")
    terms = _TERM.findall(text or "")[:20]
    if not terms:
        raise ValueError("requête vide : donnez au moins un mot")
    match = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
    sym_filter = "AND EXISTS (SELECT 1 FROM post_symbols s WHERE s.post_id = p.id AND s.symbol = ?)" if symbol else ""
    params = [match] + ([symbol] if symbol else []) + [limit]
    rows = x_conn.execute(
        f"""WITH hits AS (
                SELECT f.rowid AS pk, f.rank AS rank FROM posts_fts f JOIN posts p ON p.pk = f.rowid
                WHERE posts_fts MATCH ? {sym_filter} ORDER BY f.rank LIMIT ?)
            SELECT p.id, p.created_at, p.text, p.likes, p.reposts,
                   (SELECT group_concat(symbol) FROM post_symbols s WHERE s.post_id = p.id) AS symbols
            FROM hits h JOIN posts p ON p.pk = h.pk ORDER BY h.rank""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
