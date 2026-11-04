"""Banc d'essai de stratégies sur l'historique horaire réel.

Une stratégie est un objet appelable `strategy(view) -> {symbole: poids}` avec un attribut
`step_hours` (rythme de décision). Les poids sont >= 0 et leur somme <= 1 ; le reste est en USDC.

Règles qui rendent la triche impossible :
- `view` ne donne accès qu'aux bougies CLOSES à l'instant de la décision : demander l'avenir lève une erreur ;
- l'ordre est exécuté au prix d'ouverture de l'heure suivante ;
- frais réels du pool (palier connu, sinon 1 % prudent), passage USDC <-> WETH (0,05 %), gaz, impact de prix
  d'un pool à produit constant sur la liquidité du pool ;
- un pool sous le seuil de liquidité de la production (500 000 $) ne peut pas être acheté ;
- ordre minimum 2 USDC, comme en production.

Protocole d'évaluation (fixé avant tout résultat) :
- fenêtre commune à toutes les stratégies : tout l'historique après 30 jours de chauffe ;
- découpage 2/3 « apprentissage » puis 1/3 « contrôle » (hors échantillon), chaque partie avec un capital neuf ;
- pour un rythme quotidien, les 24 heures de décision possibles ; pour un rythme hebdomadaire, les 7 jours.
"""
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from crypto_agent.backtest import HOUR, Series, load_history
from crypto_agent.config import COSTS, RISK

DATA = Path(__file__).resolve().parent / "data" / "base-hourly-2026-11-04"
WARMUP_HOURS = 30 * 24
MIN_ORDER = RISK.min_order_usdc


class Panel:
    """Prix horaires alignés sur une grille commune ; une heure sans échange reprend la dernière clôture."""

    def __init__(self, series: dict[str, Series]):
        self.symbols = sorted(series)
        start = max(min(s.close) for s in series.values())
        end = min(max(s.close) for s in series.values())
        self.ts = list(range(start, end + HOUR, HOUR))
        self.meta = {s: series[s].meta for s in self.symbols}
        self.close: dict[str, list[float]] = {}
        self.open: dict[str, list[float]] = {}
        self.volume: dict[str, list[float]] = {}
        for sym in self.symbols:
            s = series[sym]
            last = s.close[max(t for t in s.close if t <= start)]
            c, o, v = [], [], []
            for t in self.ts:
                if t in s.close:
                    c.append(s.close[t])
                    o.append(s.open[t])
                    v.append(s.volume[t])
                    last = s.close[t]
                else:
                    c.append(last)
                    o.append(last)
                    v.append(0.0)
            self.close[sym], self.open[sym], self.volume[sym] = c, o, v

    def __len__(self) -> int:
        return len(self.ts)

    def liquidity(self, sym: str) -> float:
        return float(self.meta[sym].get("reserve_in_usd") or 0.0)

    def eligible(self, sym: str) -> bool:
        return self.liquidity(sym) >= RISK.min_liquidity_usd

    def fee_rate(self, sym: str, override: float | None = None) -> float:
        m = self.meta[sym]
        pool = override if override is not None else m.get("fee_tier")
        pool = COSTS.unknown_pool_fee if pool is None else pool
        return pool + (COSTS.hop_fee if m.get("quote") in ("WETH", "ETH") else 0.0)


class View:
    """Ce qu'une stratégie voit à la clôture de la bougie i, et rien d'autre."""

    __slots__ = ("_p", "i", "held")

    def __init__(self, panel: Panel, i: int, held: dict[str, float]):
        self._p, self.i, self.held = panel, i, held

    @property
    def symbols(self) -> list[str]:
        return list(self._p.symbols)

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self._p.ts[self.i] + HOUR, timezone.utc)

    def close(self, sym: str, hours_ago: int = 0) -> float | None:
        if hours_ago < 0:
            raise ValueError("l'avenir n'est pas visible")
        j = self.i - hours_ago
        return self._p.close[sym][j] if j >= 0 else None

    def closes(self, sym: str, n: int, every: int = 1) -> list[float] | None:
        """Les n dernières clôtures prises toutes les `every` heures, de la plus ancienne à la plus récente."""
        if self.i - (n - 1) * every < 0:
            return None
        return [self._p.close[sym][self.i - k * every] for k in range(n - 1, -1, -1)]

    def ret(self, sym: str, hours: int) -> float | None:
        old = self.close(sym, hours)
        return None if old is None else self.close(sym) / old - 1

    def volume(self, sym: str, hours: int = 24) -> float:
        return sum(self._p.volume[sym][max(0, self.i - hours + 1): self.i + 1])

    def liquidity(self, sym: str) -> float:
        return self._p.liquidity(sym)

    def eligible(self, sym: str) -> bool:
        return self._p.eligible(sym)


@dataclass
class Run:
    name: str
    start_ts: int
    end_ts: int
    marks: list[tuple[int, float]] = field(default_factory=list)   # (heure, valeur du portefeuille)
    trades: list[dict] = field(default_factory=list)
    refused: int = 0

    def summary(self) -> dict:
        first, last = self.marks[0][1], self.marks[-1][1]
        peak, mdd = -math.inf, 0.0
        for _, v in self.marks:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        daily = [v for k, (_, v) in enumerate(self.marks) if k % 24 == 0]
        rets = [b / a - 1 for a, b in zip(daily, daily[1:])]
        sharpe = (statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(365)
                  if len(rets) > 2 and statistics.stdev(rets) > 0 else None)
        return {
            "return_pct": round((last / first - 1) * 100, 2),
            "max_drawdown_pct": round(mdd * 100, 2),
            "sharpe": None if sharpe is None else round(sharpe, 2),
            "trades": len(self.trades),
            "fees_usdc": round(sum(t["fees"] for t in self.trades), 3),
        }


def _check_weights(w: dict, panel: Panel) -> dict[str, float]:
    if not isinstance(w, dict):
        raise TypeError("une stratégie doit renvoyer un dictionnaire de poids")
    out = {}
    for sym, x in w.items():
        if sym not in panel.symbols:
            raise ValueError(f"jeton inconnu : {sym}")
        x = float(x)
        if not math.isfinite(x) or x < 0:
            raise ValueError(f"poids invalide pour {sym} : {x}")
        if x > 0:
            out[sym] = x
    total = sum(out.values())
    if total > 1 + 1e-9:
        raise ValueError(f"somme des poids {total:.4f} > 1 : pas d'effet de levier")
    return out


def simulate(panel: Panel, strategy, *, start: int, end: int, capital: float = 50.0,
             fee_override: float | None = None, name: str = "") -> Run:
    """Décide aux indices start, start + step, ... (< end) et marque la valeur à chaque heure de [start, end]."""
    step = int(getattr(strategy, "step_hours", 24))
    cash = capital
    qty: dict[str, float] = {}
    run = Run(name or getattr(strategy, "name", type(strategy).__name__), panel.ts[start] + HOUR, panel.ts[end] + HOUR)
    last_index = len(panel) - 1
    for i in range(start, end + 1):
        value = cash + sum(q * panel.close[s][i] for s, q in qty.items())
        run.marks.append((panel.ts[i] + HOUR, value))
        if (i - start) % step or i >= end or i >= last_index:
            continue
        held = {s: q * panel.close[s][i] / value for s, q in qty.items()} if value > 0 else {}
        w = _check_weights(strategy(View(panel, i, held)), panel)
        # Exécution à l'ouverture de l'heure suivante.
        px = {s: panel.open[s][i + 1] for s in panel.symbols}
        equity = cash + sum(q * px[s] for s, q in qty.items())
        ts = panel.ts[i + 1]
        # Ventes d'abord, pour libérer du cash.
        for sym in sorted(qty):
            target = w.get(sym, 0.0) * equity
            current = qty[sym] * px[sym]
            if current - target < MIN_ORDER and not (target == 0 and current > 2 * COSTS.gas_usd):
                continue
            sell_qty = qty[sym] if target == 0 else (current - target) / px[sym]
            gross = sell_qty * px[sym]
            liq = panel.liquidity(sym)
            exec_px = px[sym] / (1 + 2 * gross / liq) if liq > 0 else 0.0
            out = sell_qty * exec_px
            rate = panel.fee_rate(sym, fee_override)
            proceeds = out * (1 - rate) - COSTS.gas_usd
            cash += proceeds
            qty[sym] -= sell_qty
            if qty[sym] <= 1e-15:
                del qty[sym]
            run.trades.append({"ts": ts, "symbol": sym, "side": "sell", "usdc": proceeds,
                               "fees": out * rate + COSTS.gas_usd})
        # Puis les achats.
        for sym, x in sorted(w.items()):
            target = x * equity
            current = qty.get(sym, 0.0) * px[sym]
            usd = min(target - current, cash - COSTS.gas_usd)
            if usd < MIN_ORDER:
                continue
            if not panel.eligible(sym):
                run.refused += 1
                continue
            liq = panel.liquidity(sym)
            impact = 2 * usd / liq
            if impact > COSTS.max_price_impact:
                run.refused += 1
                continue
            rate = panel.fee_rate(sym, fee_override)
            got = usd * (1 - rate) / (px[sym] * (1 + impact))
            qty[sym] = qty.get(sym, 0.0) + got
            cash -= usd + COSTS.gas_usd
            run.trades.append({"ts": ts, "symbol": sym, "side": "buy", "usdc": usd, "fees": usd * rate + COSTS.gas_usd})
    return run


# --- protocole --------------------------------------------------------------

def windows(panel: Panel) -> dict[str, tuple[int, int]]:
    """Fenêtres communes : complète, apprentissage (2/3), contrôle hors échantillon (1/3)."""
    first, last = WARMUP_HOURS, len(panel) - 2
    cut = first + (last - first) * 2 // 3
    return {"complete": (first, last), "apprentissage": (first, cut), "controle": (cut, last)}


def offsets(step_hours: int) -> list[int]:
    if step_hours >= 168:
        return list(range(0, 168, 24))
    if step_hours >= 24:
        return list(range(24))
    return [0]


def evaluate(panel: Panel, factory, *, fee_override: float | None = None) -> dict:
    """factory() crée une stratégie neuve (sans mémoire) pour chaque simulation."""
    probe = factory()
    step = int(getattr(probe, "step_hours", 24))
    out = {"name": getattr(probe, "name", type(probe).__name__), "step_hours": step}
    for wname, (a, b) in windows(panel).items():
        runs = [simulate(panel, factory(), start=a + k, end=b, fee_override=fee_override) for k in offsets(step)
                if a + k < b]
        sums = [r.summary() for r in runs]
        rets = [s["return_pct"] for s in sums]
        out[wname] = {
            "runs": len(runs),
            "return_median": round(statistics.median(rets), 2),
            "return_min": min(rets),
            "return_max": max(rets),
            "drawdown_median": round(statistics.median(s["max_drawdown_pct"] for s in sums), 2),
            "trades_median": statistics.median(s["trades"] for s in sums),
            "fees_median": round(statistics.median(s["fees_usdc"] for s in sums), 3),
            "refused_median": statistics.median(r.refused for r in runs),
            "start": datetime.fromtimestamp(runs[0].start_ts, timezone.utc).strftime("%Y-%m-%d"),
            "end": datetime.fromtimestamp(runs[0].end_ts, timezone.utc).strftime("%Y-%m-%d"),
            "all_returns": rets,
        }
    return out


def load_panel(folder: Path | str = DATA) -> Panel:
    return Panel(load_history(folder))
