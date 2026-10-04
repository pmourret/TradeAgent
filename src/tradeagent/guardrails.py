"""Garde-fous en dur entre l'agent et l'exchange.

Principe : l'agent propose, ce module dispose. Aucune de ces règles n'est dans un prompt,
aucune n'est modifiable par l'agent. Un ordre trop gros est réduit (« clamped »), un ordre
interdit est refusé avec la raison, qui est journalisée.

Règle de conception : les ventes ne sont jamais bloquées par un plafond de fréquence ou de
perte, car sortir d'une position doit toujours rester possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .config import CostsConfig, GuardrailsConfig, RiskTiersConfig
from .models import BUY, SELL, Decision, OrderRequest
from .portfolio import Portfolio


NORMAL = "normal"
CAUTIOUS = "cautious"
DEFENSIVE = "defensive"


def pct(value: float) -> float:
    return value / 100.0


def floor_to(value: float, decimals: int) -> float:
    """Arrondi vers le bas à `decimals` décimales (via Decimal : pas de surprise de flottants)."""
    step = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_DOWN))


@dataclass
class DayState:
    """Compteurs de la journée (UTC)."""

    date: str
    start_equity: float
    trades: int = 0
    buys: int = 0


@dataclass(frozen=True)
class Verdict:
    approved: bool
    reason: str
    order: OrderRequest | None = None


def _reject(reason: str) -> Verdict:
    return Verdict(False, reason)


class Guardrails:
    def __init__(self, cfg: GuardrailsConfig, costs: CostsConfig,
                 symbols: tuple[str, ...], quote_currency: str,
                 tiers: RiskTiersConfig | None = None) -> None:
        self._cfg = cfg
        self._costs = costs
        self._symbols = set(symbols)
        self._quote = quote_currency
        self._tiers = tiers or RiskTiersConfig()

    def tier_for(self, equity: float, peak_equity: float) -> str:
        """Palier de risque d'après le drawdown depuis le plus-haut. La mort, elle, vient du kill switch."""
        if peak_equity <= 0:
            return NORMAL
        drawdown_pct = (1 - equity / peak_equity) * 100
        if drawdown_pct >= self._tiers.defensive_drawdown_pct:
            return DEFENSIVE
        if drawdown_pct >= self._tiers.cautious_drawdown_pct:
            return CAUTIOUS
        return NORMAL

    def _size_factor(self, tier: str) -> float:
        if tier == DEFENSIVE:
            return 0.0
        return self._tiers.cautious_size_factor if tier == CAUTIOUS else 1.0

    def check(self, decision: Decision, portfolio: Portfolio, day: DayState, now: float,
              tier: str = NORMAL) -> Verdict:
        if decision.action not in (BUY, SELL):
            return _reject("pas un ordre")
        symbol = decision.symbol
        if symbol not in self._symbols:
            return _reject(f"symbole {symbol!r} hors de la liste autorisée")
        quote = portfolio.quotes.get(symbol)
        if quote is None:
            return _reject(f"pas de prix pour {symbol}")
        age = now - quote.timestamp
        if age > self._cfg.max_price_age_s:
            return _reject(f"prix périmé ({age:.0f}s > {self._cfg.max_price_age_s:.0f}s)")
        equity = portfolio.equity
        if equity <= 0:
            return _reject("equity nulle")

        if decision.action == BUY:
            return self._check_buy(decision, portfolio, day, equity, tier)
        return self._check_sell(decision, portfolio)

    def _check_buy(self, decision: Decision, portfolio: Portfolio, day: DayState, equity: float,
                   tier: str = NORMAL) -> Verdict:
        cfg = self._cfg
        symbol = decision.symbol
        price = portfolio.price(symbol)

        if tier == DEFENSIVE:
            return _reject("palier défensif (drawdown important) : achats bloqués, ventes seulement")
        factor = self._size_factor(tier)

        if day.buys >= cfg.max_buys_per_day:
            return _reject(f"plafond de {cfg.max_buys_per_day} achats par jour atteint")
        floor = day.start_equity * (1 - pct(cfg.max_daily_loss_pct))
        if equity < floor:
            return _reject(
                f"perte journalière max atteinte (equity {equity:.2f} < {floor:.2f}) : "
                "plus d'achat avant demain (UTC)"
            )

        # Marge pour les frais et le glissement, sinon l'exchange refuserait l'ordre.
        headroom = (1 + self._costs.fee_rate) * (1 + self._costs.slippage_bps / 10_000)
        limits = {
            "request": decision.amount_quote,
            "max_order_pct": equity * pct(cfg.max_order_pct) * factor,
            "max_position_pct": equity * pct(cfg.max_position_pct) * factor - portfolio.position_value(symbol),
            "max_total_exposure_pct": equity * pct(cfg.max_total_exposure_pct) * factor - portfolio.exposure,
            "available_cash": portfolio.cash / headroom,
        }
        binding = min(limits, key=limits.get)  # en cas d'égalité, "request" l'emporte
        allowed = limits[binding]
        if allowed < cfg.min_order_quote:
            return _reject(
                f"achat refusé : montant autorisé {max(0.0, allowed):.2f} {self._quote} < minimum "
                f"{cfg.min_order_quote:.2f} (limite active : {binding})"
            )
        quantity = floor_to(allowed / price, cfg.quantity_decimals)
        if quantity <= 0:
            return _reject("quantité nulle après arrondi")

        order = OrderRequest(symbol, BUY, quantity, price)
        suffix = f" [palier prudent x{factor:g}]" if tier == CAUTIOUS else ""
        if binding == "request":
            return Verdict(True, "approuvé" + suffix, order)
        return Verdict(
            True,
            f"approuvé, réduit de {decision.amount_quote:.2f} à {allowed:.2f} {self._quote} par {binding}{suffix}",
            order,
        )

    def _check_sell(self, decision: Decision, portfolio: Portfolio) -> Verdict:
        cfg = self._cfg
        symbol = decision.symbol
        price = portfolio.price(symbol)
        held = portfolio.positions.get(symbol, 0.0)

        if held * price < cfg.min_order_quote:
            return _reject(
                f"rien à vendre : position de {held * price:.2f} {self._quote} "
                f"< minimum {cfg.min_order_quote:.2f}"
            )
        quantity = min(decision.amount_quote / price, held)
        note = ""
        if (held - quantity) * price < cfg.min_order_quote:
            # Le reste serait de la poussière invendable : on sort de la position en entier.
            quantity = held
            note = " (sortie complète : le reste serait de la poussière)"
        else:
            quantity = floor_to(quantity, cfg.quantity_decimals)
            if quantity <= 0 or quantity * price < cfg.min_order_quote:
                return _reject(f"vente trop petite (minimum {cfg.min_order_quote:.2f} {self._quote})")

        order = OrderRequest(symbol, SELL, quantity, price)
        requested = decision.amount_quote
        if abs(order.notional - requested) < 0.005 and not note:
            return Verdict(True, "approuvé", order)
        return Verdict(True, f"approuvé, vente de {order.notional:.2f} {self._quote}{note}", order)

    def describe_limits(self, portfolio: Portfolio, day: DayState, tier: str = NORMAL) -> dict[str, float]:
        """Les limites telles que l'agent a le droit de les connaître (lecture seule)."""
        cfg = self._cfg
        equity = portfolio.equity
        factor = self._size_factor(tier)
        return {
            "max_order_quote": round(equity * pct(cfg.max_order_pct) * factor, 2),
            "max_position_quote_per_symbol": round(equity * pct(cfg.max_position_pct) * factor, 2),
            "max_total_exposure_quote": round(equity * pct(cfg.max_total_exposure_pct) * factor, 2),
            "min_order_quote": cfg.min_order_quote,
            "buys_left_today": max(0, cfg.max_buys_per_day - day.buys),
            "buys_blocked_below_equity": round(day.start_equity * (1 - pct(cfg.max_daily_loss_pct)), 2),
        }
