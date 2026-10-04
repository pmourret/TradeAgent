"""Types de base.

Tout ce que produit l'IA est converti en `Decision` : types stricts, valeurs finies,
aucune confiance dans la sortie brute du modèle.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

BUY = "buy"
SELL = "sell"
HOLD = "hold"
ACTIONS = (BUY, SELL, HOLD)
MAX_REASONING_CHARS = 500


class InvalidDecision(ValueError):
    """La sortie de l'agent n'est pas une décision valide."""


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    timestamp: float  # secondes unix


TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3_600, "4h": 14_400, "1d": 86_400}


@dataclass(frozen=True)
class Candle:
    timestamp: float  # secondes unix, ouverture de la bougie
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    timestamp: float

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class OrderRequest:
    """Ordre validé par les garde-fous, prêt pour l'exchange (marché uniquement)."""

    symbol: str
    side: str
    quantity: float
    reference_price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.reference_price


@dataclass(frozen=True)
class Decision:
    """Ce que l'agent demande. Un montant en monnaie de cotation, jamais une quantité.

    - buy : dépenser `amount_quote` sur `symbol`
    - sell : vendre l'équivalent de `amount_quote` de `symbol`
    - hold : ne rien faire (action valide et encouragée)
    """

    action: str
    symbol: str | None = None
    amount_quote: float | None = None
    reasoning: str = ""
    # « Pas de décision ce cycle » (cadence, budget épuisé) : ni un hold, ni une erreur.
    # Jamais lu depuis la sortie d'un LLM : seul le code de l'agent peut le poser.
    skipped: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise InvalidDecision(f"action inconnue : {self.action!r}")
        if not isinstance(self.reasoning, str):
            raise InvalidDecision("reasoning doit être un texte")
        object.__setattr__(self, "reasoning", self.reasoning[:MAX_REASONING_CHARS])
        if self.action == HOLD:
            object.__setattr__(self, "symbol", None)
            object.__setattr__(self, "amount_quote", None)
            return
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise InvalidDecision("symbol manquant ou invalide")
        if not _is_number(self.amount_quote) or self.amount_quote <= 0:
            raise InvalidDecision(f"amount_quote doit être un nombre fini > 0, reçu {self.amount_quote!r}")
        object.__setattr__(self, "amount_quote", float(self.amount_quote))

    @classmethod
    def skip(cls, reason: str) -> "Decision":
        return cls(HOLD, reasoning=reason, skipped=True)

    @classmethod
    def from_dict(cls, raw: Any) -> "Decision":
        if not isinstance(raw, dict):
            raise InvalidDecision("la décision doit être un objet JSON")
        action = raw.get("action")
        if not isinstance(action, str):
            raise InvalidDecision("action manquante")
        return cls(
            action=action.strip().lower(),
            symbol=raw.get("symbol"),
            amount_quote=raw.get("amount_quote"),
            reasoning=raw.get("reasoning") or "",
        )

    @classmethod
    def from_json(cls, text: str) -> "Decision":
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())

        def reject_constant(name: str) -> Any:
            raise InvalidDecision(f"constante JSON interdite : {name}")

        try:
            raw = json.loads(cleaned, parse_constant=reject_constant)
        except json.JSONDecodeError as exc:
            raise InvalidDecision(f"JSON invalide : {exc}") from exc
        return cls.from_dict(raw)
