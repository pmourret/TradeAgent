"""Assemblage des composants."""
from __future__ import annotations

import time
from typing import Callable

from .agents import Agent, ChaosAgent, HoldAgent
from .budget import InferenceBudget
from .config import Config, ConfigError
from .engine import Engine
from .feeds import CcxtPriceFeed, PriceFeed, SyntheticPriceFeed
from .guardrails import Guardrails
from .killswitch import KillSwitch
from .llm import AnthropicClient, FakeLLMClient
from .llm_agent import DECISION_SCHEMA, LLMAgent
from .paper import PaperExchange
from .storage import Storage

# `llm_wake` et `llm_idle` : le sommeil et l'arrêt de l'agent appartiennent à la vie qui les a décidés.
# `llm_last_call` n'y est pas, exprès : la cadence des appels payants survit à un reset.
LIFE_KEYS = ("life", "paper_balances", "killswitch", "peak_equity", "day", "risk_tier", "llm_wake", "llm_idle")
AGENT_KINDS = ("hold", "chaos", "llm", "llm-fake")


def ensure_life(storage: Storage, cfg: Config, now: float) -> float:
    """Une « vie » = une mise de départ. Retourne la mise de la vie en cours.

    Si la config change de mise sans `reset`, on refuse de démarrer : le kill switch
    compare l'equity à la mise de la vie en cours, pas à celle écrite dans le fichier.
    """
    life = storage.get("life")
    if life is None:
        storage.set("life", {"stake": cfg.stake, "started": now})
        storage.record_event(now, "info", f"nouvelle vie, mise {cfg.stake:.2f} {cfg.quote_currency}")
        return cfg.stake
    if abs(life["stake"] - cfg.stake) > 1e-9:
        raise ConfigError(
            f"la mise de la config ({cfg.stake}) diffère de celle de la vie en cours ({life['stake']}). "
            "Remets l'ancienne valeur, ou lance `tradeagent reset --yes` pour une nouvelle vie."
        )
    return life["stake"]


def reset_life(storage: Storage, now: float) -> None:
    for key in LIFE_KEYS:
        storage.delete(key)
    storage.record_event(now, "info", "reset : nouvelle vie")


def build_feed(kind: str, cfg: Config, seed: int | None = None,
               clock: Callable[[], float] = time.time) -> PriceFeed:
    if kind == "synthetic":
        return SyntheticPriceFeed(cfg.symbols, seed=seed, clock=clock)
    if kind == "ccxt":
        return CcxtPriceFeed(cfg.exchange)
    raise ConfigError(f"flux de prix inconnu : {kind!r}")


def build_engine(cfg: Config, agent: Agent, feed: PriceFeed, storage: Storage | None = None,
                 clock: Callable[[], float] = time.time) -> Engine:
    storage = storage or Storage(cfg.database)
    stake = ensure_life(storage, cfg, clock())
    exchange = PaperExchange(cfg, feed, storage, clock)
    guardrails = Guardrails(cfg.guardrails, cfg.costs, cfg.symbols, cfg.quote_currency, cfg.risk_tiers)
    killswitch = KillSwitch(cfg.killswitch, storage, stake, clock)
    return Engine(cfg, exchange, agent, storage, guardrails, killswitch, stake, clock)


def build_agent(kind: str, cfg: Config, storage: Storage, seed: int | None = None,
                clock: Callable[[], float] = time.time) -> Agent:
    if kind == "hold":
        return HoldAgent()
    if kind == "chaos":
        return ChaosAgent(seed=seed)
    if kind in ("llm", "llm-fake"):
        client = (AnthropicClient(cfg.llm.model, output_schema=DECISION_SCHEMA) if kind == "llm"
                  else FakeLLMClient(seed=seed))
        return LLMAgent(cfg, client, InferenceBudget(cfg.llm, storage, clock), storage, clock)
    raise ConfigError(f"agent inconnu : {kind!r} (choix : {', '.join(AGENT_KINDS)})")
