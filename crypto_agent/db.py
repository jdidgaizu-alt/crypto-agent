"""Base SQLite : portefeuille fictif, journal des décisions, posts X indexés (FTS5)."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash (id INTEGER PRIMARY KEY CHECK (id = 1), usdc REAL NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, qty REAL NOT NULL, cost_usdc REAL NOT NULL, opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, price REAL NOT NULL, usdc REAL NOT NULL, fees_usdc REAL NOT NULL,
    reason TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT NOT NULL, equity_usdc REAL NOT NULL, benchmark_usdc REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS benchmark (id INTEGER PRIMARY KEY CHECK (id = 1), weth_qty REAL NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, created_at TEXT NOT NULL, author TEXT,
    text TEXT NOT NULL, likes INTEGER DEFAULT 0, reposts INTEGER DEFAULT 0, replies INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(text, symbol UNINDEXED, content='posts', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, text, symbol) VALUES (new.rowid, new.text, new.symbol);
END;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
