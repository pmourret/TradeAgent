"""Boucle principale : observer, décider, filtrer, exécuter, journaliser.

Un cycle :
  1. si le kill switch est actif, on ne fait rien ;
  2. photo du portefeuille (prix + soldes), mise à jour des compteurs du jour et du plus-haut ;
  3. palier de risque (selon le drawdown) puis seuils de mort, AVANT de consulter l'agent. Ces seuils jugent
     l'equity NETTE DU LOYER : le coût d'API de la vie en cours sort de la mise. Un bot qui ne gagne pas plus
     qu'il ne dépense en inférence finit par mourir, même sans perdre un centime en trading ;
  4. l'agent décide ; toute erreur de sa part vaut « ne rien faire » et compte comme erreur ;
     un « skip » (cadence, budget épuisé) n'est ni une décision ni une erreur ;
  5. les garde-fous valident ou réduisent l'ordre ; seul un ordre validé atteint l'exchange.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .agents import Agent, MarketView
from .config import Config
from .exchange import Exchange, ExchangeError
from .guardrails import NORMAL, DayState, Guardrails, Verdict
from .killswitch import KillSwitch
from .market import summarize_candles
from .models import BUY, HOLD, SELL, Decision, Fill
from .portfolio import Portfolio, base_currency
from .storage import Storage

log = logging.getLogger("tradeagent")


@dataclass
class CycleResult:
    status: str  # stopped | dead | halted | skip | hold | rejected | filled | error
    note: str = ""
    decision: Decision | None = None
    verdict: Verdict | None = None
    fill: Fill | None = None


class Engine:
    def __init__(self, cfg: Config, exchange: Exchange, agent: Agent, storage: Storage,
                 guardrails: Guardrails, killswitch: KillSwitch, stake: float,
                 clock: Callable[[], float] = time.time) -> None:
        self._cfg = cfg
        self._exchange = exchange
        self._agent = agent
        self._storage = storage
        self._guardrails = guardrails
        self._ks = killswitch
        self._stake = stake
        self._clock = clock

    # -- observation -----------------------------------------------------
    def snapshot(self) -> Portfolio:
        quotes = {s: self._exchange.get_quote(s) for s in self._cfg.symbols}
        balances = self._exchange.get_balances()
        positions = {s: balances.get(base_currency(s), 0.0) for s in self._cfg.symbols}
        cash = balances.get(self._cfg.quote_currency, 0.0)
        return Portfolio(self._cfg.quote_currency, cash, positions, quotes)

    def _roll_day(self, equity: float, now: float) -> DayState:
        today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
        raw = self._storage.get("day")
        day = DayState(**raw) if raw and raw["date"] == today else DayState(today, equity)
        self._storage.set("day", asdict(day))
        return day

    def _rent(self) -> float:
        """Le loyer : ce que l'inférence a coûté depuis le début de la vie en cours."""
        life = self._storage.get("life") or {}
        return self._storage.llm_spend_since(float(life.get("started", 0.0)))

    def _update_peak(self, equity: float) -> float:
        peak = max(self._storage.get("peak_equity", self._stake), equity)
        self._storage.set("peak_equity", peak)
        return peak

    def _track_tier(self, tier: str, now: float, equity: float, peak: float) -> None:
        previous = self._storage.get("risk_tier", NORMAL)
        if tier == previous:
            return
        self._storage.set("risk_tier", tier)
        drawdown = (1 - equity / peak) * 100 if peak > 0 else 0.0
        message = f"palier de risque {previous} -> {tier} (drawdown {drawdown:.1f} % depuis {peak:.2f})"
        self._storage.record_event(now, "warning", message)
        log.warning(message)

    def _market_summary(self, now: float) -> dict[str, dict[str, Any]]:
        market = self._cfg.market
        summary: dict[str, dict[str, Any]] = {}
        for symbol in self._cfg.symbols:
            try:
                candles = self._exchange.get_candles(symbol, market.timeframe, market.candles)
            except ExchangeError as exc:
                self._storage.record_event(now, "warning", f"bougies indisponibles pour {symbol} : {exc}")
                continue
            summary[symbol] = summarize_candles(candles, market.timeframe)
        return summary

    def _market_view(self, portfolio: Portfolio, day: DayState, now: float, tier: str) -> MarketView:
        positions = {
            s: {
                "quantity": q,
                "price": portfolio.price(s),
                "value": round(q * portfolio.price(s), 2),
            }
            for s, q in portfolio.positions.items()
        }
        fills = [
            {k: f[k] for k in ("ts", "symbol", "side", "quantity", "price", "fee")}
            for f in self._storage.recent_fills(5)
        ]
        return MarketView(
            timestamp=now,
            quote_currency=portfolio.quote_currency,
            stake=self._stake,
            equity=round(portfolio.equity, 2),
            cash=round(portfolio.cash, 2),
            positions=positions,
            recent_fills=fills,
            limits=self._guardrails.describe_limits(portfolio, day, tier),
            risk_tier=tier,
            candle_timeframe=self._cfg.market.timeframe,
            market=self._market_summary(now),
        )

    # -- un cycle --------------------------------------------------------
    def run_cycle(self) -> CycleResult:
        if self._ks.active:
            return CycleResult("stopped", f"{self._ks.status}: {self._ks.reason}")

        try:
            portfolio = self.snapshot()
        except ExchangeError as exc:
            return self._on_error("snapshot", exc)

        now = self._clock()
        equity = portfolio.equity
        net = equity - self._rent()          # ce qui reste une fois le loyer payé : c'est lui que le kill switch juge
        day = self._roll_day(equity, now)    # la perte du jour, elle, reste celle du trading
        peak = self._update_peak(net)
        self._storage.record_equity(now, equity, portfolio.cash)
        # Derniers prix connus : l'interface web en a besoin pour valoriser les positions (lecture seule).
        self._storage.set("last_quotes", {s: portfolio.price(s) for s in self._cfg.symbols})

        reason = self._ks.check_financial(net, peak)
        if reason:
            self._die(reason, now)
            return CycleResult("dead", reason)

        tier = self._guardrails.tier_for(net, peak)
        self._track_tier(tier, now, net, peak)

        view = self._market_view(portfolio, day, now, tier)
        try:
            decision = self._agent.decide(view)
            if not isinstance(decision, Decision):
                raise TypeError(f"l'agent a renvoyé {type(decision).__name__}, pas une Decision")
        except Exception as exc:  # LLM, réseau, JSON invalide, bug : tout vaut « ne rien faire »
            return self._on_error("agent", exc)

        if decision.skipped:
            # Ni décision ni erreur : ne touche pas au compteur d'erreurs consécutives.
            log.debug("agent en pause : %s", decision.reasoning)
            return CycleResult("skip", decision.reasoning, decision)

        if decision.action == HOLD:
            self._storage.record_decision(now, self._agent.name, decision, None, "hold")
            self._ks.note_success()
            return CycleResult("hold", decision.reasoning, decision)

        verdict = self._guardrails.check(decision, portfolio, day, now, tier)
        self._storage.record_decision(now, self._agent.name, decision, verdict.approved, verdict.reason)
        if not verdict.approved or verdict.order is None:
            self._ks.note_success()
            log.info("ordre refusé : %s", verdict.reason)
            return CycleResult("rejected", verdict.reason, decision, verdict)

        order = verdict.order
        try:
            fill = self._exchange.market_order(order.symbol, order.side, order.quantity)
        except ExchangeError as exc:
            return self._on_error("order", exc)

        self._storage.record_fill(fill, source=self._agent.name)
        day.trades += 1
        if fill.side == BUY:
            day.buys += 1
        self._storage.set("day", asdict(day))
        self._ks.note_success()
        note = (
            f"{fill.side} {fill.quantity:g} {fill.symbol} @ {fill.price:.2f} "
            f"(frais {fill.fee:.3f}) | {verdict.reason}"
        )
        return CycleResult("filled", note, decision, verdict, fill)

    # -- mort, liquidation, erreurs -------------------------------------
    def _die(self, reason: str, now: float) -> None:
        self._ks.die(reason)
        self._storage.record_event(now, "critical", f"MORT : {reason}")
        log.error("MORT : %s", reason)
        if self._cfg.killswitch.liquidate_on_death:
            self._liquidate(now)

    def _liquidate(self, now: float) -> None:
        """Vend tout au marché. Action du moteur, pas de l'agent : elle ne passe pas par les garde-fous."""
        try:
            balances = self._exchange.get_balances()
        except ExchangeError as exc:
            self._storage.record_event(now, "error", f"liquidation impossible : {exc}")
            return
        for symbol in self._cfg.symbols:
            quantity = balances.get(base_currency(symbol), 0.0)
            if quantity <= 0:
                continue
            try:
                fill = self._exchange.market_order(symbol, SELL, quantity)
            except ExchangeError as exc:
                self._storage.record_event(now, "error", f"liquidation de {symbol} échouée : {exc}")
                continue
            self._storage.record_fill(fill, source="killswitch")
            log.info("liquidation : vendu %g %s @ %.2f", fill.quantity, symbol, fill.price)

    def _on_error(self, where: str, exc: BaseException) -> CycleResult:
        message = f"{where} : {exc}"
        now = self._clock()
        log.warning("erreur %s", message)
        self._storage.record_event(now, "error", message)
        if self._ks.note_error(message):
            self._storage.record_event(now, "critical", f"ARRÊT : {self._ks.reason}")
            return CycleResult("halted", self._ks.reason)
        return CycleResult("error", message)

    # -- boucle ----------------------------------------------------------
    def run_forever(self, max_cycles: int | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    should_stop: Callable[[], bool] = lambda: False) -> int:
        """`should_stop` n'est consulté qu'entre deux cycles : un cycle commencé va toujours à son terme."""
        n = 0
        while (max_cycles is None or n < max_cycles) and not should_stop():
            n += 1
            try:
                result = self.run_cycle()
            except Exception as exc:  # une exception inattendue ne doit pas tuer la boucle en silence
                log.exception("exception inattendue dans le cycle")
                result = self._on_error("inattendu", exc)
            level = logging.DEBUG if result.status == "skip" else logging.INFO
            log.log(level, "cycle %d | %s | %s", n, result.status, result.note)
            if self._ks.active:
                log.warning("agent arrêté (%s) : %s", self._ks.status, self._ks.reason)
                break
            if (max_cycles is None or n < max_cycles) and not should_stop():
                sleep(self._cfg.cycle_seconds)
        return n
