"""Collecte des posts X (API v2, recherche récente) et recherche plein texte (RAG) dans la base locale.

Nécessite la variable d'environnement X_BEARER_TOKEN.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

API = "https://api.x.com/2/tweets/search/recent"


def collect(conn: sqlite3.Connection, symbol: str, query: str, max_results: int = 50,
            client: httpx.Client | None = None) -> int:
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise RuntimeError("X_BEARER_TOKEN absent : ajoutez-le comme secret de l'environnement.")
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        r = client.get(API, headers={"Authorization": f"Bearer {token}"}, params={
            "query": query, "max_results": max(10, min(max_results, 100)),
            "tweet.fields": "created_at,public_metrics,author_id",
        })
        r.raise_for_status()
        data = r.json().get("data") or []
    finally:
        if own:
            client.close()
    return store_posts(conn, symbol, data)


def store_posts(conn: sqlite3.Connection, symbol: str, data: list[dict]) -> int:
    n = 0
    for t in data:
        m = t.get("public_metrics") or {}
        cur = conn.execute(
            "INSERT OR IGNORE INTO posts(id, symbol, created_at, author, text, likes, reposts, replies) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (t["id"], symbol, t["created_at"], t.get("author_id"), t["text"],
             m.get("like_count", 0), m.get("retweet_count", 0), m.get("reply_count", 0)),
        )
        n += cur.rowcount
    conn.commit()
    return n


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.symbol, p.created_at, p.text, p.likes, p.reposts FROM posts_fts f "
        "JOIN posts p ON p.rowid = f.rowid WHERE posts_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def attention(conn: sqlite3.Connection, symbol: str, now: datetime | None = None) -> dict:
    """Attention récente sur un jeton : volume pondéré des 6 dernières heures
    comparé à la moyenne par tranche de 6 h sur les 24 h précédentes."""
    now = now or datetime.now(timezone.utc)

    def weight(since: datetime, until: datetime) -> float:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(1 + likes*0.1 + reposts*0.3), 0) AS w FROM posts "
            "WHERE symbol = ? AND created_at >= ? AND created_at < ?",
            (symbol, since.isoformat().replace("+00:00", "Z"), until.isoformat().replace("+00:00", "Z")),
        ).fetchone()
        return float(row["w"])

    recent = weight(now - timedelta(hours=6), now)
    before = weight(now - timedelta(hours=30), now - timedelta(hours=6)) / 4
    ratio = recent / before if before > 0 else (2.0 if recent > 0 else 1.0)
    return {"recent": recent, "baseline": before, "ratio": ratio}
