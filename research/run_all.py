"""Évalue toutes les stratégies enregistrées avec le même protocole et écrit les résultats.

    python -m research.run_all            # écrit research/results/preregistered.json
"""
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from crypto_agent import backtest
from research import harness, strategies

OUT = Path(__file__).resolve().parent / "results" / "preregistered.json"


def production_strategy(panel: harness.Panel, data) -> dict:
    """La stratégie de production (crypto_agent.strategy), rejouée par crypto_agent.backtest sur les mêmes fenêtres."""
    out = {"name": "stratégie actuelle (production, prix seuls)", "step_hours": 24}
    for wname, (a, b) in harness.windows(panel).items():
        rets, dds, trades, fees = [], [], [], []
        for k in harness.offsets(24):
            if a + k >= b:
                continue
            s = backtest.run(data, step_hours=24, start=panel.ts[a + k], end=panel.ts[b]).summary()
            rets.append(s["return_pct"])
            dds.append(s["max_drawdown_pct"])
            trades.append(s["trades"])
            fees.append(s["fees_usdc"])
        out[wname] = {
            "runs": len(rets), "return_median": round(statistics.median(rets), 2),
            "return_min": min(rets), "return_max": max(rets),
            "drawdown_median": round(statistics.median(dds), 2), "trades_median": statistics.median(trades),
            "fees_median": round(statistics.median(fees), 3), "all_returns": rets,
        }
    return out


def main() -> int:
    data = backtest.load_history(harness.DATA)
    panel = harness.Panel(data)
    results = [harness.evaluate(panel, cls) for cls in strategies.PREREGISTERED]
    results.append(production_strategy(panel, data))
    eth = next(r for r in results if r["name"] == "garder ETH")
    for r in results:
        for w in ("complete", "apprentissage", "controle"):
            r[w]["vs_eth_median"] = round(r[w]["return_median"] - eth[w]["return_median"], 2)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "windows": {k: [panel.ts[a], panel.ts[b]] for k, (a, b) in harness.windows(panel).items()},
                               "results": results}, ensure_ascii=False, indent=1))
    print(f"{'stratégie':44} {'complète':>9} {'apprent.':>9} {'contrôle':>9} {'pire':>8} {'meilleur':>9} {'DD méd.':>8} {'ordres':>7}")
    for r in results:
        c, a, o = r["complete"], r["apprentissage"], r["controle"]
        print(f"{r['name'][:44]:44} {c['return_median']:+8.1f}% {a['return_median']:+8.1f}% {o['return_median']:+8.1f}% "
              f"{c['return_min']:+7.1f}% {c['return_max']:+8.1f}% {c['drawdown_median']:+7.1f}% {c['trades_median']:>7}")
    w = harness.windows(panel)
    fmt = lambda i: datetime.fromtimestamp(panel.ts[i], timezone.utc).strftime("%d/%m")
    print(f"\nfenêtres : complète {fmt(w['complete'][0])}→{fmt(w['complete'][1])}, "
          f"apprentissage →{fmt(w['apprentissage'][1])}, contrôle {fmt(w['controle'][0])}→{fmt(w['controle'][1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
