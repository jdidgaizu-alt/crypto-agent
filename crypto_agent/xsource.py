"""Données X : comptage horaire des posts (signal d'attention) et collecte de posts (recherche RAG).

Le signal d'attention vient de l'API de comptage (GET /2/tweets/counts/recent) : le nombre
EXACT de posts par heure, quelle que soit la fréquence des cycles. Pour neutraliser le rythme
de la journée (on poste davantage aux heures américaines), les 6 dernières heures sont comparées
aux MÊMES heures des 6 jours précédents. Le ratio n'est calculé que si les 150 heures
nécessaires sont toutes connues et assez fournies ; sinon il vaut None (« non mesuré ») et la
stratégie l'ignore. Il n'y a jamais de valeur par défaut qui ressemble à un signal.

Lire des posts coûte 0,005 $ par post et un comptage 0,005 $ par requête (barème X 2026) :
chaque requête payante RÉSERVE d'abord son coût sur le budget mensuel enregistré dans le livre,
de façon atomique, puis l'ajuste au coût réel. Deux collectes simultanées ne peuvent pas dépasser le budget.

Nécessite la variable d'environnement X_BEARER_TOKEN.
"""
import os
import re
import sqlite3
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx

from .config import X_BUDGET
from .db import write_tx

COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
RECENT_HOURS = 6
BASELINE_DAYS = 6
WINDOW_HOURS = BASELINE_DAYS * 24 + RECENT_HOURS      # 150 h, dans les 7 jours que couvre l'API
CURSOR_MAX_AGE = timedelta(days=6, hours=12)           # au-delà, X refuserait since_id (fenêtre de 7 jours)


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
            body = r.json()
            if isinstance(body, dict):
                title = str(body.get("title", ""))[:80]
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
    """Dépenses du mois, réservations en cours comprises."""
    now = now or datetime.now(timezone.utc)
    row = book_conn.execute("SELECT COALESCE(SUM(usd), 0) FROM x_spend WHERE month = ?", (_month(now),)).fetchone()
    return float(row[0])


def _reserve(book_conn: sqlite3.Connection, kind: str, usd: float, now: datetime) -> None:
    """Réserve le coût maximal d'une requête AVANT de l'envoyer, dans une seule transaction."""
    with write_tx(book_conn):
        spent = spent_this_month(book_conn, now)
        if spent + usd > X_BUDGET.monthly_usd + 1e-9:
            raise XError(f"budget X du mois atteint ({spent:.3f} $ engagés sur {X_BUDGET.monthly_usd:.2f} $)")
        book_conn.execute(
            "INSERT INTO x_spend(month, kind, units, usd) VALUES (?,?,0,?) ON CONFLICT(month, kind) "
            "DO UPDATE SET usd = usd + excluded.usd", (_month(now), kind, usd))


def _settle(book_conn: sqlite3.Connection, kind: str, reserved: float, units: int, usd: float, now: datetime) -> None:
    """Remplace la réservation par le coût réel (0 si la requête a échoué : X ne la facture pas)."""
    with write_tx(book_conn):
        book_conn.execute("UPDATE x_spend SET units = units + ?, usd = usd + ? WHERE month = ? AND kind = ?",
                          (units, usd - reserved, _month(now), kind))


def _paid_get(book_conn: sqlite3.Connection, kind: str, reserve_usd: float, client: httpx.Client, url: str,
              params: dict, now: datetime, cost) -> dict:
    """Requête payante : réserve, envoie, puis facture cost(body) (ou rembourse en cas d'échec)."""
    _reserve(book_conn, kind, reserve_usd, now)
    try:
        body = _get(client, url, params)
    except BaseException:
        _settle(book_conn, kind, reserve_usd, 0, 0.0, now)
        raise
    units, usd = cost(body)
    _settle(book_conn, kind, reserve_usd, units, min(usd, reserve_usd), now)
    return body


# --- comptage et attention --------------------------------------------------

def _needed_hours(end: datetime) -> tuple[list[datetime], list[list[datetime]]]:
    recent = [end - timedelta(hours=k) for k in range(1, RECENT_HOURS + 1)]
    baselines = [[h - timedelta(days=d) for h in recent] for d in range(1, BASELINE_DAYS + 1)]
    return recent, baselines


def fetch_counts(x_conn: sqlite3.Connection, book_conn: sqlite3.Connection, symbol: str, query: str,
                 client: httpx.Client, now: datetime | None = None) -> int:
    """Récupère le nombre de posts par heure complète sur les 150 dernières heures.
    Ne paie rien si les 42 heures utiles à la mesure sont déjà connues (une heure close ne change plus)."""
    now = now or datetime.now(timezone.utc)
    end = _window_end(now)
    start = end - timedelta(hours=WINDOW_HOURS)
    have = {r[0] for r in x_conn.execute("SELECT hour_start FROM x_counts WHERE symbol = ? AND hour_start >= ? "
                                          "AND hour_start < ?", (symbol, _z(start), _z(end)))}
    recent, baselines = _needed_hours(end)
    if all(_z(h) in have for h in recent + [h for day in baselines for h in day]):
        return 0  # toutes les heures utiles sont déjà connues : rien à payer
    price = X_BUDGET.count_request_usd
    body = _paid_get(book_conn, "counts", price, client, COUNTS_URL,
                     {"query": query, "granularity": "hour", "start_time": _z(start), "end_time": _z(end)},
                     now, lambda b: (1, price))
    rows = []
    for b in body.get("data") or []:
        try:
            hour = datetime.fromisoformat(str(b["start"]).replace("Z", "+00:00"))
            count = int(b.get("tweet_count", b.get("post_count")))
        except (KeyError, TypeError, ValueError, AttributeError):
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
    """Posts des 6 dernières heures complètes / moyenne des MÊMES heures sur les 6 jours précédents.
    ratio = None si une seule heure manque, ou si la référence est trop maigre pour être mesurée."""
    now = now or datetime.now(timezone.utc)
    end = _window_end(now)
    recent, baselines = _needed_hours(end)
    rows = x_conn.execute("SELECT hour_start, count FROM x_counts WHERE symbol = ? AND hour_start >= ? "
                          "AND hour_start < ?", (symbol, _z(end - timedelta(hours=WINDOW_HOURS)), _z(end))).fetchall()
    have = {r["hour_start"]: r["count"] for r in rows}
    needed = recent + [h for day in baselines for h in day]
    missing = [h for h in needed if _z(h) not in have]
    if missing:
        return {"ratio": None, "recent": None, "baseline": None,
                "reason": f"{len(missing)} heure(s) sans comptage sur {len(needed)}"}
    total = lambda hours: sum(have[_z(h)] for h in hours)
    recent_posts = total(recent)
    baseline = statistics.mean(total(day) for day in baselines)
    if baseline < X_BUDGET.min_baseline_posts:
        return {"ratio": None, "recent": recent_posts, "baseline": baseline,
                "reason": f"trop peu de posts pour mesurer ({baseline:.0f} en moyenne sur ces heures)"}
    return {"ratio": recent_posts / baseline, "recent": recent_posts, "baseline": baseline, "reason": None}


# --- collecte de posts (RAG) ------------------------------------------------

def store_posts(x_conn: sqlite3.Connection, symbol: str, data: list) -> int:
    """Enregistre les posts (texte long compris) et leur lien avec le jeton. Renvoie le nombre de liens nouveaux."""
    n = 0
    with write_tx(x_conn):
        for t in data or []:
            if not isinstance(t, dict) or not all(isinstance(t.get(k), str) for k in ("id", "text", "created_at")):
                continue
            m = t.get("public_metrics") if isinstance(t.get("public_metrics"), dict) else {}
            note = t.get("note_tweet") if isinstance(t.get("note_tweet"), dict) else {}
            text = note.get("text") if isinstance(note.get("text"), str) else t["text"]
            x_conn.execute(
                "INSERT OR IGNORE INTO posts(id, created_at, author, text, likes, reposts, replies) "
                "VALUES (?,?,?,?,?,?,?)",
                (t["id"], t["created_at"], t.get("author_id"), unicodedata.normalize("NFKC", text),
                 int(m.get("like_count") or 0), int(m.get("retweet_count") or 0), int(m.get("reply_count") or 0)),
            )
            n += x_conn.execute("INSERT OR IGNORE INTO post_symbols(post_id, symbol) VALUES (?,?)",
                                (t["id"], symbol)).rowcount
    return n


def check_max_posts(max_posts) -> int:
    if isinstance(max_posts, bool) or not isinstance(max_posts, int) \
            or not 10 <= max_posts <= X_BUDGET.max_posts_per_call:
        raise ValueError(f"max_posts doit être un entier entre 10 et {X_BUDGET.max_posts_per_call}")
    return max_posts


def _save_cursor(x_conn: sqlite3.Connection, symbol: str, newest_id: str, now: datetime) -> None:
    with write_tx(x_conn):
        x_conn.execute("INSERT INTO x_cursor(symbol, newest_id, saved_at) VALUES (?,?,?) ON CONFLICT(symbol) "
                       "DO UPDATE SET newest_id = excluded.newest_id, saved_at = excluded.saved_at",
                       (symbol, newest_id, _z(now)))


def collect_posts(x_conn: sqlite3.Connection, book_conn: sqlite3.Connection, symbol: str, query: str,
                  max_posts: int, client: httpx.Client, now: datetime | None = None) -> dict:
    """Lit au plus max_posts posts nouveaux (depuis la dernière collecte), page par page."""
    now = now or datetime.now(timezone.utc)
    check_max_posts(max_posts)
    row = x_conn.execute("SELECT newest_id, saved_at FROM x_cursor WHERE symbol = ?", (symbol,)).fetchone()
    since_id = None
    if row and row["saved_at"] and now - datetime.fromisoformat(row["saved_at"].replace("Z", "+00:00")) < CURSOR_MAX_AGE:
        since_id = row["newest_id"]
    fetched = stored = 0
    next_token = None
    cursor_saved = False
    price = X_BUDGET.post_read_usd
    while True:
        size = min(100, max_posts - fetched)
        if size < 10:
            break
        params = {"query": query, "max_results": size,
                  "tweet.fields": "created_at,public_metrics,author_id,note_tweet"}
        if since_id:
            params["since_id"] = since_id
        if next_token:
            params["next_token"] = next_token
        body = _paid_get(book_conn, "posts", size * price, client, SEARCH_URL, params, now,
                         lambda b: (len(b.get("data") or []), len(b.get("data") or []) * price))
        data = body.get("data") if isinstance(body.get("data"), list) else []
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        stored += store_posts(x_conn, symbol, data)
        fetched += len(data)
        # Curseur enregistré dès la première page : un échec plus loin ne fera pas repayer ces posts.
        if not cursor_saved and meta.get("newest_id"):
            _save_cursor(x_conn, symbol, str(meta["newest_id"]), now)
            cursor_saved = True
        next_token = meta.get("next_token")
        if not next_token or not data:
            break
    return {"symbol": symbol, "fetched": fetched, "new_links": stored, "truncated": bool(next_token)}


_TERM = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def search(x_conn: sqlite3.Connection, text: str, symbol: str | None = None, limit: int = 20) -> list[dict]:
    """Recherche plein texte en langage naturel : un post qui contient AU MOINS un des mots est retenu,
    les plus pertinents d'abord. « $BRETT », « #Base », « AERO/WETH », « don't » deviennent des termes cités,
    jamais de la syntaxe FTS5."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit doit être un entier entre 1 et 100")
    terms = _TERM.findall(unicodedata.normalize("NFKC", text or ""))[:20]
    if not terms:
        raise ValueError("requête vide : donnez au moins un mot")
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
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
