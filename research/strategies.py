"""Stratégies candidates, ENREGISTRÉES le 04/11/2026 AVANT de voir le moindre résultat.

Paramètres standards de la littérature, aucun réglage sur ces données. Toute stratégie ajoutée
après coup doit être signalée comme telle et jugée d'abord sur la période d'apprentissage.
"""
import math
import statistics

DAY = 24
WEEK = 7 * DAY


def _sma(xs: list[float]) -> float:
    return sum(xs) / len(xs)


class HoldEth:
    """Référence : tout en ETH, sans jamais vendre."""
    name, step_hours = "garder ETH", WEEK

    def __call__(self, v):
        return {"WETH": 1.0}


class HoldBtc:
    """Tout en BTC (cbBTC), sans jamais vendre."""
    name, step_hours = "garder BTC", WEEK

    def __call__(self, v):
        return {"cbBTC": 1.0}


class Basket:
    """Parts égales entre les jetons assez liquides, rééquilibrées chaque semaine."""
    name, step_hours = "panier liquide", WEEK

    def __call__(self, v):
        ok = [s for s in v.symbols if v.eligible(s)]
        return {s: 1 / len(ok) for s in ok}


class TrendMajors:
    """Suivi de tendance sur ETH et BTC : chacun 50 % s'il clôture au-dessus de sa moyenne 20 jours, sinon USDC."""
    name, step_hours = "tendance ETH+BTC (moyenne 20 j)", DAY

    def __call__(self, v):
        out = {}
        for s in ("WETH", "cbBTC"):
            daily = v.closes(s, 20, every=DAY)
            if daily and v.close(s) > _sma(daily):
                out[s] = 0.5
        return out


class TrendEthCross:
    """Croisement de moyennes sur ETH : 100 % ETH si la moyenne 10 jours est au-dessus de la moyenne 30 jours."""
    name, step_hours = "tendance ETH (10 j / 30 j)", DAY

    def __call__(self, v):
        daily = v.closes("WETH", 30, every=DAY)
        if daily and _sma(daily[-10:]) > _sma(daily):
            return {"WETH": 1.0}
        return {}


class CrossMomentum:
    """Momentum relatif : chaque semaine, les 2 jetons liquides les plus en hausse sur 14 jours, si leur hausse est positive."""
    name, step_hours = "momentum relatif 14 j (top 2)", WEEK

    def __call__(self, v):
        scored = [(r, s) for s in v.symbols if v.eligible(s) and (r := v.ret(s, 14 * DAY)) is not None and r > 0]
        top = [s for _, s in sorted(scored, reverse=True)[:2]]
        return {s: 0.5 for s in top}


class DipBuy:
    """Retour à la moyenne : achète 20 % d'un jeton liquide après une baisse de plus de 12 % en 24 h ;
    revend après 3 jours ou dès +8 %. Au plus 3 positions."""
    name, step_hours = "achat après forte baisse", DAY

    def __init__(self):
        self.entries: dict[str, tuple[float, int]] = {}   # jeton -> (prix d'entrée, indice)

    def __call__(self, v):
        for s, (px, i) in list(self.entries.items()):
            if v.i - i >= 3 * DAY or v.close(s) >= px * 1.08 or s not in v.held:
                del self.entries[s]
        for s in v.symbols:
            r = v.ret(s, DAY)
            if len(self.entries) < 3 and s not in self.entries and v.eligible(s) and r is not None and r < -0.12:
                self.entries[s] = (v.close(s), v.i)
        return {s: 0.2 for s in self.entries}


class VolTargetEth:
    """ETH à volatilité cible : poids = 40 % / volatilité annualisée des 14 derniers jours, plafonné à 100 %."""
    name, step_hours = "ETH à volatilité cible 40 %", DAY

    def __call__(self, v):
        xs = v.closes("WETH", 14 * DAY + 1)
        if not xs:
            return {}
        rets = [math.log(b / a) for a, b in zip(xs, xs[1:])]
        vol = statistics.pstdev(rets) * math.sqrt(24 * 365)
        return {"WETH": min(1.0, 0.40 / vol)} if vol > 0 else {"WETH": 1.0}


PREREGISTERED = [HoldEth, HoldBtc, Basket, TrendMajors, TrendEthCross, CrossMomentum, DipBuy, VolTargetEth]
