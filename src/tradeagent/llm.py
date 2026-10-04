"""Clients LLM. L'agent ne connaît que l'interface `LLMClient` : passer en local plus tard
revient à écrire une seule classe de plus.
"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from typing import Any, Protocol


class LLMError(Exception):
    """Appel au LLM impossible ou refusé (réseau, clé, quota, etc.)."""


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMReply:
    text: str
    usage: LLMUsage
    model: str


class LLMClient(Protocol):
    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMReply: ...


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def require_api_key(environ=None) -> str:
    """La clé Anthropic, ou LLMError. Vérifiée AVANT d'ouvrir la moindre base : un lancement refusé ne laisse aucune trace."""
    key = (os.environ if environ is None else environ).get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("ANTHROPIC_API_KEY absente : l'agent llm appelle l'API Anthropic (voir .env.example).")
    return key


class AnthropicClient:
    """API Messages d'Anthropic. La clé vient de ANTHROPIC_API_KEY (jamais dans un prompt ni un log)."""

    def __init__(self, model: str, api_key: str | None = None, client: Any = None,
                 timeout: float = 45.0, max_retries: int = 2) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise LLMError("package 'anthropic' manquant : pip install anthropic") from exc
            key = api_key or require_api_key()
            client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)
        self._client = client
        self._model = model

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMReply:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # famille anthropic.APIError, réseau, timeout
            raise LLMError(f"appel Anthropic échoué : {type(exc).__name__}: {exc}") from exc

        text = "".join(
            getattr(block, "text", "") for block in response.content
            if getattr(block, "type", None) == "text"
        )
        usage = response.usage
        return LLMReply(
            text=text,
            model=self._model,
            usage=LLMUsage(
                input_tokens=_count(getattr(usage, "input_tokens", 0)),
                output_tokens=_count(getattr(usage, "output_tokens", 0)),
                cache_read_tokens=_count(getattr(usage, "cache_read_input_tokens", 0)),
                cache_write_tokens=_count(getattr(usage, "cache_creation_input_tokens", 0)),
            ),
        )


class FakeLLMClient:
    """Faux LLM pour tester toute la chaîne hors ligne : décisions aléatoires, tokens estimés.

    Les jetons sont facturés au budget comme de vrais appels, ce qui permet de vérifier la
    comptabilité sans dépenser un centime.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMReply:
        try:
            symbols = list(json.loads(user.split("\n", 1)[1])["positions"])
        except (IndexError, KeyError, ValueError):
            symbols = []
        roll = self._rng.random()
        if not symbols or roll < 0.7:
            body = {"action": "hold", "reasoning": "fake LLM: nothing compelling"}
        elif roll < 0.9:
            body = {"action": "buy", "symbol": self._rng.choice(symbols),
                    "amount_quote": round(self._rng.uniform(5, 15), 2), "reasoning": "fake LLM: random buy"}
        else:
            body = {"action": "sell", "symbol": self._rng.choice(symbols),
                    "amount_quote": round(self._rng.uniform(5, 15), 2), "reasoning": "fake LLM: random sell"}
        text = json.dumps(body)
        usage = LLMUsage(input_tokens=(len(system) + len(user)) // 4, output_tokens=len(text) // 3)
        return LLMReply(text=text, usage=usage, model="fake")
