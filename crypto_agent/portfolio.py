"""Livres de simulation : ordres aux vrais prix, frais réels, impact de prix, valorisation prudente.

Deux livres séparés partagent ce code : « auto » (la stratégie systématique) et « claude »
(les décisions prises par Claude via MCP). Chacun a son capital, ses positions, sa
référence ETH et son arrêt d'urgence, pour que l'un ne fausse pas l'évaluation de l'autre.

buy() et sell_all() s'exécutent DANS une transaction ouverte par write_tx : la lecture
du cash ou de la position et l'écriture qui en dépend sont donc atomiques.
"""
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import fees
from .config import COSTS, RISK
from .db import read_tx, savepoint, write_tx
from .market import Quote


class Refused(ValueError):
    """Ordre refusé pour une raison attendue (montant, garde-fou, liquidité, cash)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


# --- livres -----------------------------------------------------------------

def check_amount(usdc) -> float:
    if isinstance(usdc, bool) or not isinstance(usdc, (int, float)):
        raise Refused("montant manquant ou non numérique")
    v = float(usdc)
    if not math.isfinite(v) or v <= 0:
        raise Refused("le montant doit être un nombre positif fini")
    return v


def create_book(conn: sqlite3.Connection, name: str, capital: float, now: datetime | None = None) -> None:
    capital = check_amount(capital)
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM books WHERE name = ?", (name,)).fetchone():
            raise Refused(f"le livre « {name} » existe déjà")
        conn.execute("INSERT INTO books(name, capital_usdc, cash_usdc, created_at) VALUES (?,?,?,?)",
                     (name, capital, capital, iso(now or utcnow())))


def get_book(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM books WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise LookupError(f"livre « {name} » absent : lancez `python -m crypto_agent.cycle --init`")
    return row


def positions(conn: sqlite3.Connection, book: str) -> dict[str, dict]:
    return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM positions WHERE book = ?", (book,))}


# --- ordres -----------------------------------------------------------------

def price_impact(usdc: float, liquidity_usd: float) -> float:
    """Pool à produit constant : prix moyen d'un achat = prix × (1 + 2 × montant / liquidité totale)."""
    if not liquidity_usd > 0:
        return math.inf
    return 2 * usdc / liquidity_usd


def buy(conn: sqlite3.Connection, book: str, q: Quote, usdc: float, reason: str,
        now: datetime | None = None) -> dict:
    now = now or utcnow()
    usdc = check_amount(usdc)
    if usdc < RISK.min_order_usdc:
        raise Refused(f"montant minimum {RISK.min_order_usdc:g} USDC")
    if not q.price_usd > 0:
        raise Refused(f"prix invalide pour {q.symbol}")
    impact = price_impact(usdc, q.liquidity_usd)
    if impact > COSTS.max_price_impact:
        raise Refused(f"impact de prix estimé {impact:.2%} > {COSTS.max_price_impact:.0%}")
    rate = fees.total_fee_rate(q)
    exec_price = q.price_usd * (1 + impact)
    qty = usdc * (1 - rate) / exec_price
    debit = usdc + COSTS.gas_usd
    fees_usdc = usdc * rate + COSTS.gas_usd
    with savepoint(conn):
        cur = conn.execute("UPDATE books SET cash_usdc = cash_usdc - ? WHERE name = ? AND cash_usdc >= ?",
                           (debit, book, debit))
        if cur.rowcount != 1:
            raise Refused(f"liquidités insuffisantes pour {debit:.2f} USDC")
        conn.execute(
            "INSERT INTO positions(book, symbol, qty, cost_usdc, opened_at, last_price, last_price_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(book, symbol) DO UPDATE SET qty = qty + excluded.qty, "
            "cost_usdc = cost_usdc + excluded.cost_usdc, last_price = excluded.last_price, "
            "last_price_at = excluded.last_price_at",
            (book, q.symbol, qty, debit, iso(now), q.price_usd, iso(now)),
        )
        conn.execute(
            "INSERT INTO trades(book, ts, symbol, side, qty, price, usdc, fees_usdc, fee_rate, pool, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (book, iso(now), q.symbol, "buy", qty, exec_price, usdc, fees_usdc, rate, q.pair_address, reason),
        )
    return {"symbol": q.symbol, "side": "buy", "qty": qty, "price": exec_price, "usdc": usdc,
            "fees": fees_usdc, "fee_rate": rate}


def sell_all(conn: sqlite3.Connection, book: str, q: Quote, reason: str, now: datetime | None = None) -> dict:
    now = now or utcnow()
    with savepoint(conn):
        pos = conn.execute("SELECT qty, cost_usdc FROM positions WHERE book = ? AND symbol = ?",
                           (book, q.symbol)).fetchone()
        if pos is None:
            raise Refused(f"aucune position sur {q.symbol}")
        if not (q.price_usd > 0 and q.liquidity_usd > 0):
            raise Refused(f"prix ou liquidité nuls sur {q.symbol} : vente impossible")
        rate = fees.total_fee_rate(q)
        gross = pos["qty"] * q.price_usd
        # Pool à produit constant, formule exacte : la recette tend vers 0 quand la liquidité s'effondre.
        exec_price = q.price_usd / (1 + 2 * gross / q.liquidity_usd)
        out = pos["qty"] * exec_price
        proceeds = out * (1 - rate) - COSTS.gas_usd
        fees_usdc = out * rate + COSTS.gas_usd
        cur = conn.execute("DELETE FROM positions WHERE book = ? AND symbol = ?", (book, q.symbol))
        if cur.rowcount != 1:
            raise RuntimeError("position modifiée pendant la vente")
        conn.execute("UPDATE books SET cash_usdc = cash_usdc + ? WHERE name = ?", (proceeds, book))
        conn.execute(
            "INSERT INTO trades(book, ts, symbol, side, qty, price, usdc, fees_usdc, fee_rate, pool, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (book, iso(now), q.symbol, "sell", pos["qty"], exec_price, proceeds, fees_usdc, rate,
             q.pair_address, reason),
        )
    return {"symbol": q.symbol, "side": "sell", "qty": pos["qty"], "price": exec_price,
            "proceeds": proceeds, "fees": fees_usdc, "pnl": proceeds - pos["cost_usdc"]}


# --- valorisation -----------------------------------------------------------

def mark_prices(conn: sqlite3.Connection, book: str, quotes: dict[str, Quote], now: datetime) -> None:
    """Retient le dernier prix observé de chaque position (à appeler dans une transaction)."""
    for sym, q in quotes.items():
        conn.execute("UPDATE positions SET last_price = ?, last_price_at = ? WHERE book = ? AND symbol = ?",
                     (q.price_usd, iso(now), book, sym))


@dataclass
class Valuation:
    cash: float
    equity: float                 # toutes les positions au dernier prix connu
    risk_equity: float            # sans les positions perdues de vue depuis plus de lost_quote_hours
    stale: list[str]              # prix de plus de stale_quote_hours : risque inconnu, achats bloqués
    lost: list[str]               # prix de plus de lost_quote_hours : comptées pour 0 dans le capital à risque
    positions: dict[str, dict]


def value(conn: sqlite3.Connection, book: str, now: datetime | None = None,
          live: dict[str, float] | None = None) -> Valuation:
    """Jamais au prix de revient : une position sans cotation garde son dernier prix observé.
    Au-delà de stale_quote_hours, elle bloque les achats (risque inconnu) ; au-delà de
    lost_quote_hours, elle sort du capital à risque. Une panne de prix isolée ne déclenche
    donc pas l'arrêt d'urgence, mais une position introuvable pendant des jours, si."""
    now = now or utcnow()
    stale_after = now - timedelta(hours=RISK.stale_quote_hours)
    lost_after = now - timedelta(hours=RISK.lost_quote_hours)
    with read_tx(conn):
        b = get_book(conn, book)
        pos = positions(conn, book)
    equity = risk = b["cash_usdc"]
    stale, lost = [], []
    for sym, p in pos.items():
        if live and sym in live:
            price, seen = live[sym], now
        else:
            price, seen = p["last_price"], parse_iso(p["last_price_at"])
        p["price_usd"] = price
        p["value_usdc"] = p["qty"] * price
        p["price_fresh"] = seen >= stale_after
        equity += p["value_usdc"]
        if seen < lost_after:
            lost.append(sym)
        else:
            risk += p["value_usdc"]
        if seen < stale_after:
            stale.append(sym)
    return Valuation(b["cash_usdc"], equity, risk, sorted(stale), sorted(lost), pos)


def entries_today(conn: sqlite3.Connection, book: str, now: datetime) -> int:
    """Achats du jour (UTC). Les ventes ne comptent pas : une sortie n'est jamais bloquée."""
    day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute("SELECT COUNT(*) FROM trades WHERE book = ? AND side = 'buy' AND ts >= ?",
                        (book, iso(day))).fetchone()[0]


def recently_sold(conn: sqlite3.Connection, book: str, now: datetime) -> set[str]:
    since = iso(now - timedelta(hours=RISK.rebuy_cooldown_hours))
    rows = conn.execute("SELECT DISTINCT symbol FROM trades WHERE book = ? AND side = 'sell' AND ts >= ?",
                        (book, since))
    return {r[0] for r in rows}


# --- référence ETH, arrêt, instantanés --------------------------------------

def ensure_benchmark(conn: sqlite3.Connection, book: str, weth_price: float, now: datetime) -> None:
    """Fixe la référence une seule fois : tout le capital de DÉPART placé en ETH.
    À appeler dans la transaction, avant tout ordre."""
    if weth_price and weth_price > 0:
        conn.execute("UPDATE books SET bench_weth_qty = capital_usdc / ?, bench_at = ? "
                     "WHERE name = ? AND bench_weth_qty IS NULL", (weth_price, iso(now), book))


def benchmark_value(conn: sqlite3.Connection, book: str, weth_price: float | None) -> float | None:
    b = get_book(conn, book)
    if b["bench_weth_qty"] is None or not weth_price:
        return None
    return b["bench_weth_qty"] * weth_price


def halt(conn: sqlite3.Connection, book: str, reason: str, now: datetime) -> None:
    conn.execute("UPDATE books SET halted_reason = ?, halted_at = ? WHERE name = ? AND halted_reason IS NULL",
                 (reason, iso(now), book))


def reset_halt(conn: sqlite3.Connection, book: str, now: datetime | None = None) -> dict | None:
    """Décision humaine uniquement (ligne de commande) : aucun outil MCP n'y donne accès.
    Le livre repart avec ce qu'il lui reste : le capital de référence des garde-fous devient
    la valeur actuelle, sinon le seuil d'arrêt, calculé sur l'ancien capital, l'arrêterait aussitôt.
    La référence ETH, elle, reste celle du départ : la comparaison continue d'inclure les pertes."""
    now = now or utcnow()
    with write_tx(conn):
        b = get_book(conn, book)
        if b["halted_reason"] is None:
            return None
        val = value(conn, book, now)
        if val.equity < 10:
            raise Refused(f"il ne reste que {val.equity:.2f} USDC : trop peu pour respecter l'ordre minimum")
        conn.execute("UPDATE books SET halted_reason = NULL, halted_at = NULL, capital_usdc = ? WHERE name = ?",
                     (val.equity, book))
    return {"old_capital": b["capital_usdc"], "new_capital": val.equity,
            "new_kill_threshold": val.equity * RISK.kill_switch_frac}


def snapshot(conn: sqlite3.Connection, book: str, now: datetime, val: Valuation,
             benchmark: float | None, note: dict) -> None:
    conn.execute(
        "INSERT INTO snapshots(book, ts, equity_usdc, risk_equity_usdc, benchmark_usdc, stale, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (book, iso(now), val.equity, val.risk_equity, benchmark, json.dumps(val.stale),
         json.dumps(note, ensure_ascii=False, default=str)),
    )


def last_snapshot_at(conn: sqlite3.Connection, book: str) -> datetime | None:
    row = conn.execute("SELECT MAX(ts) FROM snapshots WHERE book = ?", (book,)).fetchone()
    return parse_iso(row[0]) if row and row[0] else None


# --- verrou de cycle --------------------------------------------------------

def acquire_lock(conn: sqlite3.Connection, name: str, holder: str, now: datetime,
                 ttl_minutes: float = 15) -> bool:
    with write_tx(conn):
        row = conn.execute("SELECT acquired_at FROM locks WHERE name = ?", (name,)).fetchone()
        if row and parse_iso(row["acquired_at"]) > now - timedelta(minutes=ttl_minutes):
            return False
        conn.execute("INSERT OR REPLACE INTO locks(name, holder, acquired_at) VALUES (?,?,?)",
                     (name, holder, iso(now)))
    return True


def release_lock(conn: sqlite3.Connection, name: str, holder: str) -> None:
    with write_tx(conn):
        conn.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))
