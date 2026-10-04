"""Backtest : faire tourner le VRAI moteur sur des bougies passées, et comparer des stratégies.

Rien n'est simulé à part : même moteur, mêmes garde-fous, même kill switch, même exchange papier (frais et
glissement de la config) que `tradeagent run`. Seuls changent l'horloge (simulée) et le flux de prix (rejeu).
Chaque stratégie tourne dans sa propre base en mémoire : un backtest n'écrit jamais dans une base de profil.

Limites à garder en tête en lisant un résultat :
- l'ordre est exécuté au prix que l'agent vient de voir (clôture de la dernière bougie), plus le glissement
  fixe de la config : c'est optimiste, un vrai ordre part au prix d'après ;
- entre deux clôtures le prix ne bouge pas : un backtest en bougies d'une heure ne voit pas ce qui se passe
  dans l'heure (mèches, pics de volatilité) ;
- le kill switch et le drawdown ne voient que les clôtures : une chute en cours de bougie, qui tuerait le bot
  en vrai, passe inaperçue. Morts et drawdown sont donc sous-estimés ;
- le contrôle « prix périmé » des garde-fous ne joue pas : le prix rejoué est toujours daté de l'instant simulé ;
- une période passée n'annonce rien de la suivante.

Le vrai LLM n'est pas disponible ici : le rejouer coûterait de l'argent réel à chaque backtest.
"""
from __future__ import annotations

from dataclasses import dataclass

from .agents import Agent, ChaosAgent, HoldAgent
from .app import build_engine
from .budget import InferenceBudget
from .config import Config, ConfigError
from .exchange import ExchangeError
from .llm import FakeLLMClient
from .llm_agent import LLMAgent
from .models import TIMEFRAME_SECONDS, Candle
from .portfolio import base_currency
from .replay import ReplayPriceFeed, SimClock
from .storage import Storage
from .strategies import STRATEGIES

BACKTEST_AGENTS = ("hold", "buyhold", "dca", "momentum", "chaos", "llm-fake")
DEFAULT_AGENTS = ("hold", "buyhold", "dca", "momentum", "chaos")
STOPPED = ("dead", "halted", "stopped")


@dataclass(frozen=True)
class BacktestResult:
    agent: str
    status: str             # alive | dead | halted
    cycles: int
    final_equity: float
    api_cost: float
    net_result: float       # equity finale - mise - coûts API
    return_pct: float       # net_result en % de la mise
    max_drawdown_pct: float
    orders: int
    fees: float
    reason: str = ""       # pourquoi le bot s'est arrêté avant la fin (mort ou halted)


def max_drawdown_pct(series: list[float]) -> float:
    """Plus forte baisse depuis un plus-haut, en % (drawdown maximal)."""
    worst, peak = 0.0, 0.0
    for equity in series:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (1 - equity / peak) * 100)
    return worst


def warmup_seconds(cfg: Config) -> float:
    """Historique nécessaire AVANT le début du backtest pour que l'agent ait ses bougies dès le premier cycle."""
    return (cfg.market.candles + 1) * TIMEFRAME_SECONDS[cfg.market.timeframe]


def _build_agent(kind: str, cfg: Config, storage: Storage, clock: SimClock, seed: int | None) -> Agent:
    if kind == "hold":
        return HoldAgent()
    if kind == "chaos":
        return ChaosAgent(seed=seed)
    if kind in STRATEGIES:
        return STRATEGIES[kind]()
    if kind == "llm-fake":
        return LLMAgent(cfg, FakeLLMClient(seed=seed), InferenceBudget(cfg.llm, storage, clock), storage, clock)
    if kind == "llm":
        raise ConfigError("le vrai LLM n'est pas disponible en backtest : chaque rejeu coûterait de l'argent réel. "
                          "Utilise llm-fake pour vérifier la chaîne, sans dépense.")
    raise ConfigError(f"agent de backtest inconnu : {kind!r} (choix : {', '.join(BACKTEST_AGENTS)})")


def run_backtest(cfg: Config, agent_kind: str, history: dict[str, list[Candle]], start: float, end: float,
                 seed: int | None = None) -> BacktestResult:
    """Un agent, une période. `history` doit commencer `warmup_seconds(cfg)` avant `start`."""
    if end <= start:
        raise ConfigError("la période du backtest est vide")
    clock = SimClock(start)
    storage = Storage(":memory:")
    try:
        feed = ReplayPriceFeed(history, cfg.market.timeframe, clock)
        agent = _build_agent(agent_kind, cfg, storage, clock, seed)
        engine = build_engine(cfg, agent, feed, storage, clock)

        cycles, t = 0, float(start)
        while t < end:
            clock.set(t)
            result = engine.run_cycle()
            cycles += 1
            if result.status in STOPPED:
                break
            t += cfg.cycle_seconds

        clock.set(min(t, end))
        # Valeur finale au dernier prix connu, d'après les soldes courants (après une mort, tout est vendu).
        try:
            final_equity = engine.snapshot().equity
        except ExchangeError:       # trou dans l'historique à la toute fin : on garde la dernière valeur enregistrée
            last = storage.last_equity()
            final_equity = float(last["equity"]) if last else cfg.stake
        state = storage.get("killswitch") or {"status": "alive"}
        totals = storage.fills_totals()
        api_cost = storage.llm_spend_since(0.0)
        net = final_equity - cfg.stake - api_cost
        return BacktestResult(
            agent=agent_kind, status=state["status"], cycles=cycles, final_equity=final_equity,
            api_cost=api_cost, net_result=net, return_pct=net / cfg.stake * 100,
            max_drawdown_pct=max_drawdown_pct(storage.equity_series() + [final_equity]),
            orders=int(totals["orders"]), fees=totals["fees"], reason=str(state.get("reason", "")),
        )
    finally:
        storage.close()


def market_return_pct(cfg: Config, history: dict[str, list[Candle]], start: float, end: float) -> float:
    """Repère : tout investir à parts égales au début et ne plus bouger, SANS garde-fou (frais et glissement
    d'achat compris). Ce n'est pas une stratégie jouable ici (les garde-fous plafonnent l'exposition) : c'est ce
    qu'a fait le marché sur la période."""
    step = TIMEFRAME_SECONDS[cfg.market.timeframe]
    cost = (1 + cfg.costs.slippage_bps / 10_000) * (1 + cfg.costs.fee_rate)
    total = 0.0
    for symbol in cfg.symbols:
        done = [c for c in history.get(symbol, []) if c.timestamp + step <= end]
        before = [c for c in done if c.timestamp + step <= start]
        if not before:
            raise ConfigError(f"historique insuffisant pour {base_currency(symbol)} avant le début du backtest")
        total += done[-1].close / (before[-1].close * cost)
    return (total / len(cfg.symbols) - 1) * 100


def compare(cfg: Config, agents: list[str] | tuple[str, ...], history: dict[str, list[Candle]],
            start: float, end: float, seed: int | None = None) -> list[BacktestResult]:
    return [run_backtest(cfg, kind, history, start, end, seed) for kind in agents]


def format_table(results: list[BacktestResult], cfg: Config, market_pct: float) -> str:
    ccy = cfg.quote_currency
    lines = [f"{'agent':<10} {'état':<7} {'equity':>9} {'net':>9} {'net %':>8} {'drawdown':>9} {'ordres':>7} {'frais':>7} {'API':>7}"]
    for r in results:
        lines.append(f"{r.agent:<10} {r.status:<7} {r.final_equity:>9.2f} {r.net_result:>+9.2f} {r.return_pct:>+7.2f}% "
                     f"{r.max_drawdown_pct:>8.2f}% {r.orders:>7d} {r.fees:>7.3f} {r.api_cost:>7.3f}")
    for r in results:
        if r.status != "alive":
            # Un bot arrêté en route n'a pas vécu toute la période : sa ligne ne se compare pas aux autres.
            lines.append(f"  ! {r.agent} : {r.status} après {r.cycles} cycles, résultat non comparable. {r.reason}")
    lines.append(f"marché (parts égales, 100 % investi, hors garde-fous) : {market_pct:+.2f} %   |   montants en {ccy}")
    return "\n".join(lines)
