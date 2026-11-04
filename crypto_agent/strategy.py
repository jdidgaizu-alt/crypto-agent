"""Décisions : sorties d'abord, garde-fous partagés par tous les ordres, puis un score simple et lisible.

entry_refusal() est l'unique contrôle des achats : la stratégie automatique, l'exécution du
cycle et les ordres de Claude via MCP passent tous par lui, aucun chemin d'achat ne le contourne.
"""
import math
from dataclasses import dataclass

from .config import COSTS, RISK
from .market import Quote


@dataclass
class Decision:
    action: str          # "buy", "sell", "halt", "warn", "hold"
    symbol: str | None
    usdc: float
    reason: str


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def floor_cents(x: float) -> float:
    """Arrondi au centime INFÉRIEUR : un montant arrondi ne dépasse jamais un plafond ou le cash."""
    return math.floor(x * 100 + 1e-9) / 100


def exit_score(q: Quote, attention: float | None) -> float:
    """Tendance des prix + regain d'attention sur X. Sans mesure X, l'attention ne compte pas."""
    trend = _clip(q.change_24h, -20, 20) / 20         # -1 .. 1
    short = _clip(q.change_1h, -5, 5) / 5             # -1 .. 1
    buzz = 0.0 if attention is None else _clip(attention - 1, -1, 2) / 2   # -0,5 .. 1
    return 0.4 * trend + 0.2 * short + 0.4 * buzz


def score(q: Quote, attention: float | None) -> float:
    """Score d'entrée : pénalise en plus les hausses déjà extrêmes (achat en haut de pompe).
    Cette pénalité ne sert qu'à l'entrée : la sortie d'une position qui monte relève de la prise de bénéfice."""
    return exit_score(q, attention) - (0.8 if q.change_24h > 35 else 0.0)


def kill_switch(risk_equity: float, capital: float) -> bool:
    return risk_equity < capital * RISK.kill_switch_frac


def eligible(q: Quote) -> str | None:
    if q.liquidity_usd < RISK.min_liquidity_usd:
        return f"liquidité {q.liquidity_usd:,.0f} $ trop faible"
    if q.volume_24h_usd < RISK.min_volume_24h_usd:
        return f"volume 24 h {q.volume_24h_usd:,.0f} $ trop faible"
    return None


def entry_refusal(*, q: Quote, usdc: float, capital: float, cash: float, held_cost: float, open_count: int,
                  entries_count: int, halted: str | None, risk_equity: float, recently_sold: set[str],
                  held_value: float = 0.0, unpriced: list[str] | tuple = ()) -> str | None:
    """Raison de refuser un achat, ou None s'il respecte tous les garde-fous.
    held_cost / held_value : coût et valeur actuelle de la position déjà détenue sur ce jeton.
    unpriced : positions du livre sans prix récent (leur risque est inconnu)."""
    if halted:
        return f"livre à l'arrêt ({halted}) : seul un humain peut le relancer"
    if kill_switch(risk_equity, capital):
        return f"capital à risque {risk_equity:.2f} sous le seuil d'arrêt {capital * RISK.kill_switch_frac:.2f}"
    if unpriced:
        return f"pas de prix récent pour {', '.join(sorted(unpriced))} : risque du livre inconnu"
    if q.symbol in recently_sold:
        return f"{q.symbol} vendu il y a moins de {RISK.rebuy_cooldown_hours:g} h"
    if (why := eligible(q)):
        return why
    if not usdc >= RISK.min_order_usdc:
        return f"montant minimum {RISK.min_order_usdc:g} USDC"
    if held_cost > 0 and held_value / held_cost - 1 <= RISK.stop_loss:
        return f"renfort interdit : la position {q.symbol} est sous son stop-loss ({held_value / held_cost - 1:.1%})"
    cap = capital * RISK.max_position_frac
    if held_cost + usdc > cap + 1e-9:
        return f"position max {cap:.2f} USDC par jeton (déjà {held_cost:.2f} engagés sur {q.symbol})"
    if held_cost == 0 and open_count >= RISK.max_open_positions:
        return f"{RISK.max_open_positions} positions ouvertes au maximum"
    if entries_count >= RISK.max_entries_per_day:
        return f"plafond de {RISK.max_entries_per_day} achats par jour atteint"
    if usdc + COSTS.gas_usd > cash - RISK.cash_reserve_usdc + 1e-9:
        return "liquidités insuffisantes (réserve comprise)"
    return None


def decide(*, quotes: dict[str, Quote], attention: dict[str, float | None], positions: dict[str, dict],
           cash: float, risk_equity: float, capital: float, entries_today: int, halted: str | None,
           recently_sold: set[str], mode: str = "full", stale: list[str] | tuple = ()) -> list[Decision]:
    """mode « full » : la stratégie complète (livre auto).
    mode « guard » : protections seules, sans aucune initiative (livre claude) : stop-loss,
    arrêt total et liquidation. Ni prise de bénéfice, ni sortie sur signal, ni achat.
    stale : positions dont le prix connu est trop ancien (achats bloqués tant qu'il en reste)."""
    if mode not in ("full", "guard"):
        raise ValueError(f"mode inconnu : {mode}")
    out: list[Decision] = []
    selling: set[str] = set()

    # 1. Sorties, toujours évaluées, même à l'arrêt.
    for sym, pos in positions.items():
        q = quotes.get(sym)
        if q is None:
            out.append(Decision("warn", sym, 0, "pas de prix fiable : sortie impossible ce cycle"))
            continue
        pnl = (pos["qty"] * q.price_usd - pos["cost_usdc"]) / pos["cost_usdc"]
        s = exit_score(q, attention.get(sym))
        reason = None
        if pnl <= RISK.stop_loss:
            reason = f"stop-loss {pnl:.1%}"
        elif mode == "full" and pnl >= RISK.take_profit:
            reason = f"prise de bénéfice {pnl:.1%}"
        elif mode == "full" and s < RISK.exit_threshold:
            reason = f"signal retourné (score {s:.2f}, P&L {pnl:.1%})"
        if reason:
            out.append(Decision("sell", sym, 0, reason))
            selling.add(sym)

    # 2. Arrêt total : verrouillé, liquidation de tout ce qui a un prix, aucun achat.
    stop = halted
    if not stop and kill_switch(risk_equity, capital):
        stop = f"capital à risque {risk_equity:.2f} sous le seuil d'arrêt {capital * RISK.kill_switch_frac:.2f}"
        out.insert(0, Decision("halt", None, 0, stop))
    if stop:
        for sym in positions:
            if sym in quotes and sym not in selling:
                out.append(Decision("sell", sym, 0, f"liquidation : {stop}"))
        return out or [Decision("hold", None, 0, f"à l'arrêt : {stop}")]
    if mode == "guard":
        return out or [Decision("hold", None, 0, "surveillance : aucune protection déclenchée")]

    # 3. Entrées, dans la limite des garde-fous.
    unpriced = sorted(set(stale) | {s for s in positions if s not in quotes})
    open_count = len(positions) - len(selling)
    entries = entries_today
    remaining = cash
    ranked = sorted(((score(q, attention.get(s)), s, q) for s, q in quotes.items() if s not in positions),
                    key=lambda t: t[0], reverse=True)
    for sc, sym, q in ranked:
        if sc < RISK.buy_threshold:
            break
        if q.change_24h < 0 or sym in recently_sold or eligible(q):
            continue  # refus propres à ce jeton : on regarde le suivant
        size = floor_cents(min(capital * RISK.max_position_frac,
                               remaining - COSTS.gas_usd - RISK.cash_reserve_usdc))
        why = entry_refusal(q=q, usdc=size, capital=capital, cash=remaining, held_cost=0.0,
                            open_count=open_count, entries_count=entries, halted=None,
                            risk_equity=risk_equity, recently_sold=recently_sold, unpriced=unpriced)
        if why:
            out.append(Decision("warn", sym, 0, f"achat écarté : {why}"))
            break  # refus global (prix manquant, nombre de positions, plafond, cash) : inutile d'aller plus loin
        att = attention.get(sym)
        att_txt = f"×{att:.2f}" if att is not None else "non mesurée"
        out.append(Decision("buy", sym, size, f"score {sc:.2f} (24 h {q.change_24h:+.1f} %, "
                                              f"1 h {q.change_1h:+.1f} %, attention X {att_txt})"))
        open_count += 1
        entries += 1
        remaining -= size + COSTS.gas_usd
    return out or [Decision("hold", None, 0, "aucun signal")]
