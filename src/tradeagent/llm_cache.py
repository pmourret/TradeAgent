"""Le vrai LLM en backtest : ne pas payer deux fois le même appel, ne jamais dépasser un plafond.

Rejouer une période avec le vrai LLM coûte de l'argent RÉEL, et le budget de `config.yaml` ne protège pas ici :
chaque backtest part d'une base vide, donc ses compteurs repartent de zéro. D'où deux protections en code :

- un cache des réponses par empreinte du prompt (fichier JSONL) : un backtest relancé à l'identique ne coûte
  rien, et donne le même résultat (le LLM n'est pas déterministe, le cache si) ;
- deux plafonds de dépense RÉELLE, vérifiés AVANT chaque appel avec une estimation pessimiste de son coût :
  celui du backtest en cours (obligatoire, donné par l'utilisateur) et celui de tous les backtests cumulés
  (d'après ce fichier).

La comptabilité penche toujours du côté prudent : le coût pessimiste d'un appel est RÉSERVÉ dans le fichier
avant l'appel, puis remplacé par le coût réel à la réponse. Un appel raté, un arrêt brutal ou une écriture
impossible laissent donc la réservation comptée (on suppose l'appel facturé), jamais oubliée.

Une réponse relue du cache rend aussi son décompte de tokens : le coût d'API est compté dans le résultat net
du backtest comme s'il avait été payé, ce qui est le but (comparer net de l'API).

Le cache ne contient ni prompt ni clé : seulement l'empreinte, la réponse, les tokens et le coût.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Callable

from .llm import LLMClient, LLMError, LLMReply, LLMUsage

CHARS_PER_TOKEN_WORST = 2.0     # estimation pessimiste AVANT l'appel : au plus un token pour 2 caractères
CHARS_PER_TOKEN_TYPICAL = 3.0
TYPICAL_OUTPUT_TOKENS = 120     # une décision JSON courte
MAX_FAILURES = 3                # appels réels ratés d'affilée avant d'arrêter d'essayer (pas de rafale payante)


class SpendCapReached(LLMError):
    """Un plafond de dépense réelle interdit cet appel."""


def prompt_key(model: str, system: str, user: str, max_output_tokens: int) -> str:
    payload = json.dumps([model, system, user, max_output_tokens], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def worst_case_usage(system: str, user: str, max_output_tokens: int) -> LLMUsage:
    return LLMUsage(input_tokens=math.ceil((len(system) + len(user)) / CHARS_PER_TOKEN_WORST),
                    output_tokens=max_output_tokens)


def _cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


class ReplyCache:
    """Réponses déjà payées et dépense réelle cumulée, dans un fichier JSONL.

    Deux sortes de lignes : une RÉSERVATION (`reserved_eur`, écrite avant l'appel) et une RÉPONSE (`text`,
    `usage`, `cost_eur`, écrite après). La dépense cumulée additionne, par numéro d'appel, le coût réel s'il est
    connu, sinon la réservation.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._replies: dict[str, LLMReply] = {}
        self._costs: dict[str, float] = {}      # numéro d'appel -> coût retenu (réel, sinon réservé)
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    call, key = str(row["call"]), str(row["key"])
                    if "reserved_eur" in row:
                        reserved = _cost(row["reserved_eur"])
                        if reserved is not None:
                            self._costs.setdefault(call, reserved)
                        continue
                    reply = LLMReply(text=str(row["text"]), model=str(row["model"]), usage=LLMUsage(**row["usage"]))
                    cost = _cost(row["cost_eur"])
                except (ValueError, KeyError, TypeError):
                    continue            # ligne abîmée : sa réservation, écrite avant, reste comptée
                if cost is None:
                    continue
                self._replies[key] = reply
                self._costs[call] = cost

    @property
    def spent_lifetime(self) -> float:
        """Tout ce que les backtests ont réellement payé (ou réservé sans réponse), d'après ce fichier."""
        return sum(self._costs.values())

    def __contains__(self, key: str) -> bool:
        return key in self._replies

    def get(self, key: str) -> LLMReply | None:
        return self._replies.get(key)

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def reserve(self, key: str, worst_eur: float, ts: float) -> str:
        """Inscrit le coût pessimiste d'un appel AVANT de le lancer. Lève OSError si l'écriture est impossible."""
        call = f"{ts:.6f}-{len(self._costs)}"
        self._append({"call": call, "key": key, "ts": ts, "reserved_eur": worst_eur})
        self._costs[call] = worst_eur
        return call

    def add(self, call: str, key: str, reply: LLMReply, cost_eur: float, ts: float) -> None:
        """Remplace la réservation par le coût réel. La mémoire est mise à jour même si l'écriture échoue."""
        self._replies[key] = reply
        self._costs[call] = cost_eur
        usage = reply.usage
        self._append({"call": call, "key": key, "ts": ts, "model": reply.model, "text": reply.text, "cost_eur": cost_eur,
                      "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                                "cache_read_tokens": usage.cache_read_tokens,
                                "cache_write_tokens": usage.cache_write_tokens}})


class CachingLLMClient:
    """`LLMClient` qui relit le cache, et ne paie un appel réel que sous les deux plafonds.

    `inner_factory` construit le vrai client au premier appel réel seulement : un backtest entièrement en
    cache n'a besoin ni de clé ni de réseau.
    """

    def __init__(self, inner_factory: Callable[[], LLMClient], cache: ReplyCache, model: str,
                 cost_of: Callable[[LLMUsage], float], run_cap_eur: float, total_cap_eur: float,
                 clock: Callable[[], float] = time.time) -> None:
        self._inner_factory = inner_factory
        self._inner: LLMClient | None = None
        self._cache = cache
        self._model = model
        self._cost_of = cost_of
        self._run_cap = run_cap_eur
        self._total_cap = total_cap_eur
        self._clock = clock
        self._failures = 0
        self._broken = ""           # non vide : plus aucun appel payant (le fichier de cache n'est plus fiable)
        self.hits = 0
        self.paid_calls = 0
        self.failed_calls = 0
        self.spent_run = 0.0        # coûts réels des appels réussis + réservations des appels ratés

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMReply:
        key = prompt_key(self._model, system, user, max_output_tokens)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached

        worst = self._cost_of(worst_case_usage(system, user, max_output_tokens))
        if self._broken:
            raise LLMError(self._broken)
        if self.spent_run + worst > self._run_cap:
            raise SpendCapReached(f"plafond de dépense réelle de ce backtest atteint "
                                  f"({self.spent_run:.4f} € comptés, plafond {self._run_cap:.2f} €)")
        if self._cache.spent_lifetime + worst > self._total_cap:
            raise SpendCapReached(f"plafond de dépense réelle de tous les backtests atteint "
                                  f"({self._cache.spent_lifetime:.4f} € comptés, plafond {self._total_cap:.2f} €)")
        if self._failures >= MAX_FAILURES:
            raise LLMError(f"{MAX_FAILURES} appels réels ratés d'affilée : le backtest n'essaie plus")

        try:
            call = self._cache.reserve(key, worst, self._clock())
        except OSError as exc:      # rien n'a été payé : on refuse d'appeler sans pouvoir tenir les comptes
            self._broken = f"impossible d'écrire le cache des réponses ({exc}) : aucun appel payant sans comptabilité"
            raise LLMError(self._broken) from exc
        self.spent_run += worst
        if self._inner is None:
            self._inner = self._inner_factory()
        try:
            reply = self._inner.complete(system, user, max_output_tokens)
        except Exception:
            # Raté, mais peut-être facturé (délai dépassé après génération) : la réservation reste comptée.
            self._failures += 1
            self.failed_calls += 1
            raise
        self._failures = 0
        cost = self._cost_of(reply.usage)
        self.spent_run += cost - worst
        self.paid_calls += 1
        try:
            self._cache.add(call, key, reply, cost, self._clock())
        except OSError as exc:
            # La réponse est payée et rendue (son coût entre dans le résultat), mais on ne peut plus l'écrire :
            # la réservation reste dans le fichier, et on arrête les appels payants.
            self._broken = f"impossible d'écrire le cache des réponses ({exc}) : arrêt des appels payants"
        return reply


class EstimatingClient:
    """Répétition à blanc : ne contacte personne, répond « hold » hors cache, et compte ce qu'un vrai backtest
    aurait à payer (appels absents du cache). Les prompts d'un vrai backtest différeront dès la première
    décision qui n'est pas un hold : c'est une estimation, pas un devis."""

    def __init__(self, cache: ReplyCache, model: str, cost_of: Callable[[LLMUsage], float]) -> None:
        self._cache = cache
        self._model = model
        self._cost_of = cost_of
        self.calls = 0
        self.cached = 0
        self.typical_cost = 0.0
        self.worst_cost = 0.0

    def complete(self, system: str, user: str, max_output_tokens: int) -> LLMReply:
        self.calls += 1
        typical = LLMUsage(input_tokens=math.ceil((len(system) + len(user)) / CHARS_PER_TOKEN_TYPICAL),
                           output_tokens=min(max_output_tokens, TYPICAL_OUTPUT_TOKENS))
        cached = self._cache.get(prompt_key(self._model, system, user, max_output_tokens))
        if cached is not None:
            # On rejoue la vraie réponse : tant que le cache suit, la répétition suit la trajectoire du vrai
            # backtest, et un backtest déjà entièrement payé est reconnu comme gratuit.
            self.cached += 1
            return cached
        self.typical_cost += self._cost_of(typical)
        self.worst_cost += self._cost_of(worst_case_usage(system, user, max_output_tokens))
        return LLMReply(text='{"action": "hold", "reasoning": "estimation"}', usage=typical, model=self._model)
