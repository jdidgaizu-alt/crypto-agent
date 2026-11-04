"""Bases SQLite.

- Le livre (book.db) : capital, positions, ordres, instantanés, frais de pools, verrous,
  dépenses X. Il est versionné : c'est l'historique qui décidera du passage à l'argent réel.
- Les données X (x.db) : posts collectés et comptages horaires. Reconstructibles, non versionnées.

Toute écriture passe par write_tx (BEGIN IMMEDIATE). Les lectures dont dépend une
écriture se font dans la même transaction : un ordre est atomique même quand
plusieurs outils MCP ou cycles tournent en parallèle.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    name TEXT PRIMARY KEY,
    capital_usdc REAL NOT NULL CHECK (capital_usdc > 0),
    cash_usdc REAL NOT NULL,
    created_at TEXT NOT NULL,
    bench_weth_qty REAL,          -- référence : capital de départ placé en ETH
    bench_at TEXT,
    halted_reason TEXT,           -- arrêt verrouillé ; seul un humain le lève
    halted_at TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    book TEXT NOT NULL REFERENCES books(name),
    symbol TEXT NOT NULL,
    qty REAL NOT NULL CHECK (qty > 0),
    cost_usdc REAL NOT NULL CHECK (cost_usdc > 0),
    opened_at TEXT NOT NULL,
    last_price REAL NOT NULL CHECK (last_price > 0),
    last_price_at TEXT NOT NULL,
    PRIMARY KEY (book, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    book TEXT NOT NULL REFERENCES books(name),
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    usdc REAL NOT NULL,
    fees_usdc REAL NOT NULL,
    fee_rate REAL NOT NULL,
    pool TEXT,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_book_ts ON trades(book, ts);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    book TEXT NOT NULL REFERENCES books(name),
    ts TEXT NOT NULL,
    equity_usdc REAL NOT NULL,        -- positions au dernier prix connu
    risk_equity_usdc REAL NOT NULL,   -- positions sans prix récent comptées pour 0
    benchmark_usdc REAL,              -- capital de départ placé en ETH, au même instant
    stale TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS snapshots_book_ts ON snapshots(book, ts);
CREATE TABLE IF NOT EXISTS pool_fees (
    pool TEXT PRIMARY KEY, fee REAL NOT NULL, source TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS x_spend (
    month TEXT NOT NULL, kind TEXT NOT NULL, units INTEGER NOT NULL, usd REAL NOT NULL,
    PRIMARY KEY (month, kind)
);
"""

X_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    pk INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    author TEXT,
    text TEXT NOT NULL,
    likes INTEGER NOT NULL DEFAULT 0,
    reposts INTEGER NOT NULL DEFAULT 0,
    replies INTEGER NOT NULL DEFAULT 0
);
-- Un post peut concerner plusieurs jetons : un lien par jeton.
CREATE TABLE IF NOT EXISTS post_symbols (
    post_id TEXT NOT NULL REFERENCES posts(id),
    symbol TEXT NOT NULL,
    PRIMARY KEY (post_id, symbol)
);
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(text, content='posts', content_rowid='pk');
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, text) VALUES (new.pk, new.text);
END;
CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, text) VALUES ('delete', old.pk, old.text);
END;
-- Nombre de posts par heure complète, fourni par l'API de comptage de X.
CREATE TABLE IF NOT EXISTS x_counts (
    symbol TEXT NOT NULL, hour_start TEXT NOT NULL, count INTEGER NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, hour_start)
);
CREATE TABLE IF NOT EXISTS x_cursor (symbol TEXT PRIMARY KEY, newest_id TEXT NOT NULL, saved_at TEXT);
"""


def _open(path: Path | str, schema: str, create: bool) -> sqlite3.Connection:
    memory = str(path) == ":memory:"
    if not memory:
        path = Path(path)
        if not path.exists():
            if not create:
                raise FileNotFoundError(
                    f"base absente : {path}. Créez le livre avec `python -m crypto_agent.cycle --init`."
                )
            path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None : aucune transaction implicite, toutes sont explicites (write_tx).
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(schema)
    return conn


def open_book(path: Path | str, create: bool = False) -> sqlite3.Connection:
    """Ouvre le livre. Refuse d'en créer un silencieusement : un livre neuf effacerait
    l'historique d'évaluation sans que personne ne s'en aperçoive."""
    return _open(path, BOOK_SCHEMA, create)


def open_x(path: Path | str) -> sqlite3.Connection:
    conn = _open(path, X_SCHEMA, create=True)
    # Base X créée par une version antérieure : colonne ajoutée sans perdre les posts.
    if "saved_at" not in {r[1] for r in conn.execute("PRAGMA table_info(x_cursor)")}:
        conn.execute("ALTER TABLE x_cursor ADD COLUMN saved_at TEXT")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Transaction d'écriture prise d'emblée (BEGIN IMMEDIATE), annulée en cas d'erreur."""
    if conn.in_transaction:
        raise RuntimeError("transaction déjà ouverte sur cette connexion")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        # Un COMMIT refusé (disque plein, verrou) ne doit pas laisser la transaction ouverte.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def read_tx(conn: sqlite3.Connection):
    """Lecture cohérente de plusieurs requêtes (une seule image de la base)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN DEFERRED")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")


@contextmanager
def savepoint(conn: sqlite3.Connection, name: str = "ordre"):
    """Sous-transaction : un ordre qui échoue à mi-chemin n'écrit rien.
    Exige une transaction déjà ouverte, sinon le SAVEPOINT en ouvrirait une différée."""
    if not conn.in_transaction:
        raise RuntimeError("ordre hors transaction : utilisez write_tx")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
