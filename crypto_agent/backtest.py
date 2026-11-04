"""Backtest : rejoue la stratégie sur l'historique horaire RÉEL, avec le même code d'ordres, de frais et de garde-fous.

Règles pour ne pas tricher avec l'avenir :
- une décision prise à l'instant t ne voit que les bougies closes à t ;
- elle est exécutée au prix d'OUVERTURE de l'heure suivante, pas au prix qui l'a déclenchée ;
- frais du pool réel (palier connu, sinon 1 % prudent), gaz, impact de prix sur la liquidité du pool.

Limites connues (à rappeler avec chaque résultat) :
- l'attention X n'a pas d'historique ici : seule la partie « prix » de la stratégie est testée ;
- la liquidité est celle d'aujourd'hui, pas celle de l'époque ;
- les jetons ont été choisis aujourd'hui, parmi ceux qui ont survécu (biais du survivant).

Usage :
    python -m crypto_agent.backtest DOSSIER_HISTORIQUE [--step 24] [--fee 0.003]
Le dossier contient SYMBOLE.csv (ts,open,high,low,close,volume_usd) et SYMBOLE.json (pool, quote, reserve_in_usd, fee_tier).
"""
import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import portfolio, strategy
from .config import BOOK_AUTO, RISK
from .db import open_book, write_tx
from .market import Quote

HOUR = 3600


@dataclass
class Series:
    symbol: str
    meta: dict
    close: dict[int, float] = field(default_factory=dict)
    open: dict[int, float] = field(default_factory=dict)
    volume: dict[int, float] = field(default_factory=dict)


def load_history(folder: Path | str) -> dict[str, Series]:
    out = {}
    for meta_path in sorted(Path(folder).glob("*.json")):
        meta = json.loads(meta_path.read_text())
        s = Series(meta["symbol"], meta)
        with open(meta_path.with_suffix(".csv")) as f:
            for row in csv.DictReader(f):
                ts = int(row["ts"])
                s.open[ts], s.close[ts] = float(row["open"]), float(row["close"])
                s.volume[ts] = float(row["volume_usd"])
        out[s.symbol] = s
    return out


class Market:
    """Prix connus à chaque heure. Une heure sans échange reprend la dernière clôture, volume nul."""

    def __init__(self, series: dict[str, Series], fee_override: float | None = None):
        self.series = series
        self.fee_override = fee_override
        self.start = max(min(s.close) for s in series.values())
        self.end = min(max(s.close) for s in series.values())
        self.hours = list(range(self.start, self.end + HOUR, HOUR))
        self._close: dict[str, dict[int, float]] = {}
        self._open: dict[str, dict[int, float]] = {}
        for sym, s in series.items():
            last = None
            closes, opens = {}, {}
            for h in range(min(s.close), self.end + HOUR, HOUR):
                if h in s.close:
                    closes[h], opens[h] = s.close[h], s.open[h]
                    last = s.close[h]
                elif last is not None:
                    closes[h] = opens[h] = last
            self._close[sym], self._open[sym] = closes, opens

    def quote(self, sym: str, candle: int, price: float | None = None) -> Quote | None:
        """Cotation telle qu'on la verrait à la clôture de la bougie `candle`."""
        closes = self._close[sym]
        if candle not in closes or candle - 24 * HOUR not in closes or candle - HOUR not in closes:
            return None
        s = self.series[sym]
        c = closes[candle]
        vol = sum(s.volume.get(candle - k * HOUR, 0.0) for k in range(24))
        return Quote(
            symbol=sym, address=s.meta.get("address", ""), price_usd=price if price is not None else c,
            liquidity_usd=float(s.meta.get("reserve_in_usd") or 0), volume_24h_usd=vol,
            change_1h=(c / closes[candle - HOUR] - 1) * 100, change_24h=(c / closes[candle - 24 * HOUR] - 1) * 100,
            pair_url="", pair_address=str(s.meta.get("pool", "")), dex_id=str(s.meta.get("dex", "")),
            quote_symbol=str(s.meta.get("quote", "USDC")),
            fee_rate=self.fee_override if self.fee_override is not None else s.meta.get("fee_tier"),
        )

    def next_open(self, sym: str, candle: int) -> float | None:
        return self._open[sym].get(candle + HOUR)

    def close(self, sym: str, candle: int) -> float | None:
        return self._close[sym].get(candle)


@dataclass
class Result:
    label: str
    start: str
    end: str
    equity: list[tuple[int, float]]
    benchmark: list[tuple[int, float]]      # tout le capital en ETH au départ
    basket: list[tuple[int, float]]         # capital réparti à parts égales entre les jetons au départ
    trades: list[dict]
    halted: str | None

    def summary(self) -> dict:
        final = self.equity[-1][1]
        bench = self.benchmark[-1][1]
        peak, mdd = -math.inf, 0.0
        for _, v in self.equity:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        sells = [t for t in self.trades if t["side"] == "sell"]
        wins = [t for t in sells if t.get("pnl", 0) > 0]
        return {
            "label": self.label, "start": self.start, "end": self.end,
            "final_equity": round(final, 2), "return_pct": round((final / self.equity[0][1] - 1) * 100, 2),
            "eth_hold_equity": round(bench, 2), "eth_hold_return_pct": round((bench / self.benchmark[0][1] - 1) * 100, 2),
            "vs_eth_usdc": round(final - bench, 2), "max_drawdown_pct": round(mdd * 100, 2),
            "basket_hold_return_pct": round((self.basket[-1][1] / self.basket[0][1] - 1) * 100, 2),
            "trades": len(self.trades), "round_trips": len(sells),
            "win_rate_pct": round(100 * len(wins) / len(sells), 1) if sells else None,
            "fees_usdc": round(sum(t["fees"] for t in self.trades), 2), "halted": self.halted,
        }


def run(series: dict[str, Series], *, step_hours: int = 24, capital: float = 50.0, fee_override: float | None = None,
        start: int | None = None, end: int | None = None, label: str = "") -> Result:
    """Un cycle toutes les step_hours heures. À chaque cycle (clôture de la bougie `candle`) :
    décisions sur les prix connus, exécution à l'ouverture de l'heure suivante, valorisation."""
    m = Market(series, fee_override)
    first = max(m.start + 25 * HOUR, start or 0)
    last = min(m.end - HOUR, end or m.end)          # il faut l'ouverture de l'heure suivante pour exécuter
    conn = open_book(":memory:", create=True)
    portfolio.create_book(conn, BOOK_AUTO, capital, now=datetime.fromtimestamp(first, timezone.utc))
    equity, bench, basket, trades = [], [], [], []
    weth_qty, basket_qty = None, None
    for candle in range(first, last + 1, step_hours * HOUR):
        t = candle + HOUR                                              # instant de la décision
        now = datetime.fromtimestamp(t, timezone.utc)
        quotes = {s: q for s in series if (q := m.quote(s, candle))}
        fills = {s: m.quote(s, candle, price=m.next_open(s, candle)) for s in quotes}
        with write_tx(conn):
            portfolio.mark_prices(conn, BOOK_AUTO, quotes, now)
            b = portfolio.get_book(conn, BOOK_AUTO)
            val = portfolio.value(conn, BOOK_AUTO, now)
            decisions = strategy.decide(
                quotes=quotes, attention={s: None for s in quotes}, positions=val.positions, cash=val.cash,
                risk_equity=val.risk_equity, capital=b["capital_usdc"],
                entries_today=portfolio.entries_today(conn, BOOK_AUTO, now), halted=b["halted_reason"],
                recently_sold=portfolio.recently_sold(conn, BOOK_AUTO, now), mode="full", stale=val.stale,
            )
            for d in decisions:
                try:
                    if d.action == "buy" and fills.get(d.symbol):
                        r = portfolio.buy(conn, BOOK_AUTO, fills[d.symbol], d.usdc, d.reason, now)
                        trades.append({"ts": t, **r, "reason": d.reason})
                    elif d.action == "sell" and fills.get(d.symbol):
                        r = portfolio.sell_all(conn, BOOK_AUTO, fills[d.symbol], d.reason, now)
                        trades.append({"ts": t, **r, "reason": d.reason})
                    elif d.action == "halt":
                        portfolio.halt(conn, BOOK_AUTO, d.reason, now)
                except portfolio.Refused:
                    pass
        live = {s: p for s in series if (p := m.close(s, candle))}
        val = portfolio.value(conn, BOOK_AUTO, now, live=live)
        if weth_qty is None and live.get("WETH"):
            weth_qty = capital / live["WETH"]
        if basket_qty is None and len(live) == len(series):
            basket_qty = {s: capital / len(series) / p for s, p in live.items()}
        equity.append((t, val.equity))
        bench.append((t, weth_qty * live["WETH"] if weth_qty and live.get("WETH") else capital))
        basket.append((t, sum(q * live.get(s, 0.0) for s, q in basket_qty.items()) if basket_qty else capital))
    halted = portfolio.get_book(conn, BOOK_AUTO)["halted_reason"]
    conn.close()
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
    return Result(label or f"un cycle toutes les {step_hours} h", iso(first + HOUR), iso(last + HOUR),
                  equity, bench, basket, trades, halted)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m crypto_agent.backtest")
    p.add_argument("folder")
    p.add_argument("--step", type=int, default=24, help="heures entre deux cycles (24 = un cycle par jour)")
    p.add_argument("--fee", type=float, default=None, help="impose un palier de frais à tous les pools")
    p.add_argument("--capital", type=float, default=50.0)
    args = p.parse_args(argv)
    res = run(load_history(args.folder), step_hours=args.step, capital=args.capital, fee_override=args.fee)
    print(json.dumps({"summary": res.summary(), "trades": res.trades}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
