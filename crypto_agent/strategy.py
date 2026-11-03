"""Décisions : garde-fous d'abord, puis un score simple et lisible (prix + attention sur X).

Le score est volontairement simple pour l'étape 1 : on veut savoir si l'attention
sur X apporte quelque chose, pas empiler des indicateurs.
"""
from dataclasses import dataclass

from .config import COSTS, RISK
from .market import Quote


@dataclass
class Decision:
    action: str          # "buy", "sell", "hold", "halt"
    symbol: str | None
    usdc: float
    reason: str


def kill_switch(equity: float) -> bool:
    return equity < RISK.start_capital_usdc * RISK.kill_switch_frac


def eligible(q: Quote) -> str | None:
    if q.liquidity_usd < RISK.min_liquidity_usd:
        return f"liquidité {q.liquidity_usd:,.0f} $ trop faible"
    if q.volume_24h_usd < RISK.min_volume_24h_usd:
        return f"volume 24 h {q.volume_24h_usd:,.0f} $ trop faible"
    return None


def score(q: Quote, attention_ratio: float) -> float:
    """> 0 : signal d'achat. Combine une tendance modérée et un regain d'attention sur X.
    Pénalise les hausses déjà extrêmes (achat en haut de pompe)."""
    trend = max(min(q.change_24h, 20), -20) / 20          # -1 .. 1
    short = max(min(q.change_1h, 5), -5) / 5              # -1 .. 1
    buzz = max(min(attention_ratio - 1, 2), -1) / 2       # -0,5 .. 1
    overheated = 1.0 if q.change_24h > 35 else 0.0
    return 0.4 * trend + 0.2 * short + 0.4 * buzz - 0.8 * overheated


def decide(quotes: dict[str, Quote], attention: dict[str, float], positions: dict[str, dict],
           cash: float, equity: float, trades_today: int, buy_threshold: float = 0.35) -> list[Decision]:
    if kill_switch(equity):
        return [Decision("halt", None, 0, f"capital {equity:.2f} sous le seuil d'arrêt "
                                           f"{RISK.start_capital_usdc * RISK.kill_switch_frac:.2f}")]
    out: list[Decision] = []
    # 1. Sorties : stop-loss, prise de bénéfice, signal retourné.
    for sym, pos in positions.items():
        q = quotes.get(sym)
        if not q:
            continue
        pnl = (pos["qty"] * q.price_usd - pos["cost_usdc"]) / pos["cost_usdc"]
        s = score(q, attention.get(sym, 1.0))
        if pnl <= RISK.stop_loss:
            out.append(Decision("sell", sym, 0, f"stop-loss {pnl:.1%}"))
        elif pnl >= RISK.take_profit:
            out.append(Decision("sell", sym, 0, f"prise de bénéfice {pnl:.1%}"))
        elif s < -0.3:
            out.append(Decision("sell", sym, 0, f"signal retourné (score {s:.2f}, P&L {pnl:.1%})"))
    # 2. Entrées, dans la limite des garde-fous.
    budget_trades = RISK.max_trades_per_day - trades_today - len(out)
    open_after = len(positions) - len(out)
    remaining = cash
    ranked = sorted(
        ((score(q, attention.get(s, 1.0)), s, q) for s, q in quotes.items() if s not in positions),
        reverse=True,
    )
    for sc, sym, q in ranked:
        # Taille plafonnée, en gardant de quoi payer le gaz et une petite réserve.
        size = round(min(RISK.start_capital_usdc * RISK.max_position_frac, remaining - 0.5), 2)
        if budget_trades <= 0 or open_after >= RISK.max_open_positions or size < 2:
            break
        if sc < buy_threshold:
            break
        if (why := eligible(q)):
            continue
        out.append(Decision("buy", sym, size, f"score {sc:.2f} (24 h {q.change_24h:+.1f} %, "
                                              f"1 h {q.change_1h:+.1f} %, attention ×{attention.get(sym, 1.0):.2f})"))
        budget_trades -= 1
        open_after += 1
        remaining -= size + COSTS.gas_usd
    return out or [Decision("hold", None, 0, "aucun signal")]
