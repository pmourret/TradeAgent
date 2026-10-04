"""Chargement et validation stricte de la configuration.

La config contient les garde-fous : elle doit échouer bruyamment.
Les clés inconnues (fautes de frappe) et les valeurs absurdes sont refusées.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .models import TIMEFRAME_SECONDS


class ConfigError(ValueError):
    """Configuration invalide."""


def _number(where: str, name: str, value: Any, *, gt: float | None = None,
            ge: float | None = None, le: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{where}.{name}: nombre fini attendu, reçu {value!r}")
    if gt is not None and not value > gt:
        raise ConfigError(f"{where}.{name}: doit être > {gt}, reçu {value}")
    if ge is not None and not value >= ge:
        raise ConfigError(f"{where}.{name}: doit être >= {ge}, reçu {value}")
    if le is not None and not value <= le:
        raise ConfigError(f"{where}.{name}: doit être <= {le}, reçu {value}")
    return float(value)


def _integer(where: str, name: str, value: Any, *, ge: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{name}: entier attendu, reçu {value!r}")
    if value < ge:
        raise ConfigError(f"{where}.{name}: doit être >= {ge}, reçu {value}")
    return value


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


@dataclass(frozen=True)
class CostsConfig:
    fee_rate: float = 0.001
    slippage_bps: float = 5.0

    def __post_init__(self) -> None:
        _set(self, "fee_rate", _number("costs", "fee_rate", self.fee_rate, ge=0, le=0.05))
        _set(self, "slippage_bps", _number("costs", "slippage_bps", self.slippage_bps, ge=0, le=500))


@dataclass(frozen=True)
class GuardrailsConfig:
    max_order_pct: float = 20.0
    max_position_pct: float = 40.0
    max_total_exposure_pct: float = 80.0
    min_order_quote: float = 5.0
    max_buys_per_day: int = 10
    max_daily_loss_pct: float = 5.0
    max_price_age_s: float = 120.0
    quantity_decimals: int = 6

    def __post_init__(self) -> None:
        w = "guardrails"
        for name in ("max_order_pct", "max_position_pct", "max_total_exposure_pct"):
            _set(self, name, _number(w, name, getattr(self, name), gt=0, le=100))
        _set(self, "min_order_quote", _number(w, "min_order_quote", self.min_order_quote, gt=0))
        _set(self, "max_buys_per_day", _integer(w, "max_buys_per_day", self.max_buys_per_day))
        _set(self, "max_daily_loss_pct",
             _number(w, "max_daily_loss_pct", self.max_daily_loss_pct, gt=0, le=100))
        _set(self, "max_price_age_s", _number(w, "max_price_age_s", self.max_price_age_s, gt=0))
        _set(self, "quantity_decimals", _integer(w, "quantity_decimals", self.quantity_decimals))


@dataclass(frozen=True)
class KillSwitchConfig:
    max_total_loss_pct: float = 50.0
    max_drawdown_pct: float = 40.0
    max_consecutive_errors: int = 10
    liquidate_on_death: bool = True

    def __post_init__(self) -> None:
        w = "killswitch"
        _set(self, "max_total_loss_pct",
             _number(w, "max_total_loss_pct", self.max_total_loss_pct, gt=0, le=100))
        _set(self, "max_drawdown_pct",
             _number(w, "max_drawdown_pct", self.max_drawdown_pct, gt=0, le=100))
        _set(self, "max_consecutive_errors",
             _integer(w, "max_consecutive_errors", self.max_consecutive_errors, ge=1))
        if not isinstance(self.liquidate_on_death, bool):
            raise ConfigError(f"{w}.liquidate_on_death: booléen attendu")


@dataclass(frozen=True)
class RiskTiersConfig:
    """Dégradation progressive avant la mort : on réduit la voilure puis on ferme les achats."""

    cautious_drawdown_pct: float = 15.0
    cautious_size_factor: float = 0.5
    defensive_drawdown_pct: float = 25.0

    def __post_init__(self) -> None:
        w = "risk_tiers"
        _set(self, "cautious_drawdown_pct",
             _number(w, "cautious_drawdown_pct", self.cautious_drawdown_pct, gt=0, le=100))
        _set(self, "cautious_size_factor",
             _number(w, "cautious_size_factor", self.cautious_size_factor, gt=0, le=1))
        _set(self, "defensive_drawdown_pct",
             _number(w, "defensive_drawdown_pct", self.defensive_drawdown_pct, gt=0, le=100))
        if self.cautious_drawdown_pct >= self.defensive_drawdown_pct:
            raise ConfigError("risk_tiers: cautious_drawdown_pct doit être < defensive_drawdown_pct")


@dataclass(frozen=True)
class MarketConfig:
    timeframe: str = "1h"
    candles: int = 48

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ConfigError(
                f"market.timeframe: {self.timeframe!r} inconnu, choix : {sorted(TIMEFRAME_SECONDS)}"
            )
        _set(self, "candles", _integer("market", "candles", self.candles, ge=24))
        if self.candles > 500:
            raise ConfigError("market.candles: 500 au maximum")


@dataclass(frozen=True)
class LLMConfig:
    """L'agent LLM et son « loyer » : chaque appel coûte de l'argent réel."""

    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    max_output_tokens: int = 400
    call_every_seconds: float = 3600.0
    max_call_interval_seconds: float = 86_400.0
    economy_call_factor: float = 2.0
    position_wake_move_pct: float = 3.0
    quiet_call_interval_seconds: float | None = None
    wake_move_pct: float | None = None
    price_input_per_mtok_usd: float = 1.0
    price_output_per_mtok_usd: float = 5.0
    usd_to_eur: float = 0.90
    daily_budget_eur: float = 0.25
    total_budget_eur: float = 10.0

    def __post_init__(self) -> None:
        w = "llm"
        if self.provider != "anthropic":
            raise ConfigError(f"llm.provider: {self.provider!r} non géré (seul 'anthropic' pour l'instant)")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigError("llm.model: texte non vide attendu")
        _set(self, "max_output_tokens", _integer(w, "max_output_tokens", self.max_output_tokens, ge=50))
        if self.max_output_tokens > 4096:
            raise ConfigError("llm.max_output_tokens: 4096 au maximum (une décision tient en quelques lignes)")
        _set(self, "call_every_seconds", _number(w, "call_every_seconds", self.call_every_seconds, ge=60))
        _set(self, "max_call_interval_seconds",
             _number(w, "max_call_interval_seconds", self.max_call_interval_seconds, ge=60, le=7 * 86_400))
        if self.max_call_interval_seconds < self.call_every_seconds:
            raise ConfigError("llm: max_call_interval_seconds ne peut pas être inférieur à call_every_seconds")
        _set(self, "economy_call_factor", _number(w, "economy_call_factor", self.economy_call_factor, ge=1, le=10))
        _set(self, "position_wake_move_pct",
             _number(w, "position_wake_move_pct", self.position_wake_move_pct, ge=0.5, le=50))
        # Appels sur évènement : None = comportement d'origine (un appel à chaque intervalle minimal).
        if self.quiet_call_interval_seconds is not None:
            _set(self, "quiet_call_interval_seconds",
                 _number(w, "quiet_call_interval_seconds", self.quiet_call_interval_seconds,
                         ge=self.call_every_seconds, le=self.max_call_interval_seconds))
        if self.wake_move_pct is not None:
            _set(self, "wake_move_pct", _number(w, "wake_move_pct", self.wake_move_pct, ge=0.5, le=50))
        _set(self, "price_input_per_mtok_usd",
             _number(w, "price_input_per_mtok_usd", self.price_input_per_mtok_usd, ge=0))
        _set(self, "price_output_per_mtok_usd",
             _number(w, "price_output_per_mtok_usd", self.price_output_per_mtok_usd, ge=0))
        _set(self, "usd_to_eur", _number(w, "usd_to_eur", self.usd_to_eur, gt=0, le=5))
        _set(self, "daily_budget_eur", _number(w, "daily_budget_eur", self.daily_budget_eur, gt=0))
        _set(self, "total_budget_eur", _number(w, "total_budget_eur", self.total_budget_eur, gt=0))
        if self.daily_budget_eur > self.total_budget_eur:
            raise ConfigError("llm: daily_budget_eur ne peut pas dépasser total_budget_eur")


@dataclass(frozen=True)
class Config:
    mode: str = "paper"
    exchange: str = "bitvavo"
    quote_currency: str = "EUR"
    symbols: tuple[str, ...] = ("BTC/EUR", "ETH/EUR")
    stake: float = 100.0
    cycle_seconds: float = 900.0
    database: str = "data/agent.db"
    costs: CostsConfig = field(default_factory=CostsConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    killswitch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    risk_tiers: RiskTiersConfig = field(default_factory=RiskTiersConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    def __post_init__(self) -> None:
        if self.mode != "paper":
            raise ConfigError(
                f"mode {self.mode!r} non disponible : seul 'paper' est implémenté. "
                "Le mode réel viendra après la phase de paper trading."
            )
        for name in ("exchange", "quote_currency", "database"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigError(f"{name}: texte non vide attendu")
        _set(self, "stake", _number("config", "stake", self.stake, gt=0))
        _set(self, "cycle_seconds", _number("config", "cycle_seconds", self.cycle_seconds, ge=0))

        if isinstance(self.symbols, str) or not isinstance(self.symbols, (list, tuple)):
            raise ConfigError("symbols: liste attendue")
        symbols = tuple(self.symbols)
        if not symbols:
            raise ConfigError("symbols: au moins un symbole")
        if len(set(symbols)) != len(symbols):
            raise ConfigError("symbols: doublons")
        for s in symbols:
            parts = s.split("/") if isinstance(s, str) else []
            if len(parts) != 2 or not all(parts):
                raise ConfigError(f"symbols: {s!r} n'est pas de la forme BASE/QUOTE")
            if parts[1] != self.quote_currency:
                raise ConfigError(
                    f"symbols: {s!r} n'est pas coté en {self.quote_currency} "
                    "(un seul actif de cotation est géré)"
                )
        _set(self, "symbols", symbols)

        biggest_order = self.stake * self.guardrails.max_order_pct / 100
        if biggest_order < self.guardrails.min_order_quote:
            raise ConfigError(
                f"avec stake={self.stake} et max_order_pct={self.guardrails.max_order_pct}, "
                f"l'ordre max ({biggest_order:.2f}) est sous min_order_quote "
                f"({self.guardrails.min_order_quote}) : aucun ordre ne pourrait passer"
            )
        if self.risk_tiers.defensive_drawdown_pct >= self.killswitch.max_drawdown_pct:
            raise ConfigError(
                f"risk_tiers.defensive_drawdown_pct ({self.risk_tiers.defensive_drawdown_pct:g}) doit rester "
                f"sous killswitch.max_drawdown_pct ({self.killswitch.max_drawdown_pct:g}) : "
                "le palier défensif précède la mort"
            )


_SECTIONS = {
    "costs": CostsConfig,
    "guardrails": GuardrailsConfig,
    "killswitch": KillSwitchConfig,
    "risk_tiers": RiskTiersConfig,
    "market": MarketConfig,
    "llm": LLMConfig,
}


def _build(cls: type, data: Any, where: str) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: un bloc clé/valeur est attendu")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"{where}: clé(s) inconnue(s) {sorted(unknown)}")
    return cls(**data)


def config_from_dict(raw: Any) -> Config:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("la configuration doit être un bloc clé/valeur")
    data = dict(raw)
    for name, cls in _SECTIONS.items():
        data[name] = _build(cls, raw.get(name), name)
    return _build(Config, data, "config")


def load_config(path: str | Path) -> Config:
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"fichier de configuration introuvable : {p}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {p} : {exc}") from exc
    return config_from_dict(raw)
