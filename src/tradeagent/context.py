"""Flux d'information : ce que les prix ne disent pas, lu par le code et donné au superviseur.

Trois flux publics, sans clé :
- l'indice « Fear & Greed » d'alternative.me (humeur du marché crypto, de 0 à 100, une valeur par jour) ;
- le taux de financement des contrats perpétuels (Kraken Futures, via ccxt) : ce que paient les positions
  acheteuses à effet de levier. Élevé et positif = beaucoup d'acheteurs à crédit, donc un marché fragile ;
- le calendrier macro américain : décisions de la Réserve fédérale et publications de l'inflation (CPI). Des
  dates officielles, écrites ici.

Règles :
- rien que des nombres : aucun texte venu de l'extérieur n'entre dans un prompt ;
- une panne ou une valeur trop vieille = flux absent, jamais une erreur de cycle ;
- rejouable sans biais d'anticipation : à l'instant `now`, une valeur n'est visible qu'une fois publiée. La même
  fonction sert au backtest et au direct.

Ce module ne lit aucune clé et ne parle à aucun compte : ce sont des données publiques, en lecture seule.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .feeds import FeedError

log = logging.getLogger("tradeagent")

DAY = 86_400
HOUR = 3_600

FNG_URL = "https://api.alternative.me/fng/?format=json&limit="
FNG_MAX_BYTES = 2_000_000
# La valeur datée de minuit UTC du jour J est tenue pour publiée une heure après. Observé le 2026-10-04 (à 20:57 UTC
# la valeur du jour existait, prochaine mise à jour annoncée à minuit) ; l'heure n'est écrite nulle part : à vérifier.
FNG_DELAY = HOUR
FNG_MAX_AGE = 3 * DAY           # plus vieille que ça, elle ne dit plus rien du jour
FNG_BANDS = ((25, "extreme_fear"), (45, "fear"), (55, "neutral"), (75, "greed"))   # bornes du code, pas celles du site

FUNDING_EXCHANGE = "krakenfutures"
# Un taux par heure, relatif (fraction du montant de la position), d'après les données réelles du 2026-10-04 ; la
# doc de Kraken ne donne ni la période ni l'unité : à vérifier avant de s'en servir pour autre chose qu'un ordre de grandeur.
FUNDING_MIN_POINTS = 12         # sur 24 h : en dessous, la moyenne ne veut rien dire
FUNDING_MAX_ABS = 0.01          # par heure : au-delà (87 par an), c'est une valeur abîmée

# Calendrier macro, relevé le 2026-10-04 sur les sites officiels. Hors de l'année couverte, le flux est absent
# plutôt que faux : à compléter chaque année. Limite en backtest : si une date passée avait été déplacée, le rejeu
# connaît la date réelle avant qu'elle ait été annoncée (biais faible).
# Jour de la décision de la Fed (second jour de réunion) : federalreserve.gov/monetarypolicy/fomccalendars.htm
FED_DECISIONS = ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
                 "2026-12-09")
# Publication de l'inflation américaine (CPI) : bls.gov/schedule/news_release/cpi.htm
US_CPI_RELEASES = ("2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14",
                   "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10")
MACRO_EVENTS = {"fed_decision_in_days": FED_DECISIONS, "us_cpi_in_days": US_CPI_RELEASES}

Points = list[tuple[float, float]]      # (horodatage en secondes, valeur), du plus ancien au plus récent


class ContextSource(Protocol):
    def context(self, now: float) -> dict[str, Any]: ...


def _day(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _visible(points: Points, limit: float) -> Points:
    """Les points dont l'horodatage est au plus `limit`."""
    return points[:bisect.bisect_right(points, (limit, math.inf))]


def fear_greed_at(points: Points, now: float) -> dict[str, Any] | None:
    seen = _visible(points, now - FNG_DELAY)
    if not seen or now - seen[-1][0] > FNG_MAX_AGE:
        return None
    ts, value = seen[-1]
    label = next((name for limit, name in FNG_BANDS if value < limit), "extreme_greed")
    out: dict[str, Any] = {"value": round(value), "label": label}
    before = _visible(seen, ts - 7 * DAY)
    if before and ts - 7 * DAY - before[-1][0] <= 2 * DAY:
        out["change_7d"] = round(value - before[-1][1])
    return out


def funding_at(points: Points, now: float) -> dict[str, Any] | None:
    """Taux de financement annualisé, en % : moyenne des 24 dernières heures et des 7 derniers jours. Un taux
    horaire n'est visible qu'une fois son heure écoulée."""
    seen = _visible(points, now - HOUR)
    last_day = [rate for ts, rate in seen if ts > now - HOUR - DAY]
    if len(last_day) < FUNDING_MIN_POINTS:
        return None
    week = [rate for ts, rate in seen if ts > now - HOUR - 7 * DAY]
    annual = lambda rates: round(sum(rates) / len(rates) * 24 * 365 * 100, 1)  # noqa: E731
    return {"annual_pct_24h": annual(last_day), "annual_pct_7d": annual(week)}


def macro_at(now: float, events: dict[str, tuple[str, ...]] | None = None) -> dict[str, Any] | None:
    """Nombre de jours avant le prochain évènement de chaque sorte (0 = aujourd'hui, en UTC)."""
    today = now // DAY * DAY
    out = {}
    for name, dates in (MACRO_EVENTS if events is None else events).items():
        days = sorted(_day(d) for d in dates)
        if not days or today < _day(dates[0][:4] + "-01-01") or today > days[-1]:
            continue                             # hors de l'année couverte : on ne sait pas, donc on ne dit rien
        out[name] = round((next(d for d in days if d >= today) - today) / DAY)
    return out or None


@dataclass(frozen=True)
class ContextData:
    """Les séries des flux. `context(now)` rend ce qui était connu à `now`, et rien d'autre."""

    fear_greed: Points = field(default_factory=list)
    funding: dict[str, Points] = field(default_factory=dict)     # par devise de base (BTC, ETH)
    macro: bool = True

    def context(self, now: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        mood = fear_greed_at(self.fear_greed, now)
        if mood:
            out["fear_greed"] = mood
        funding = {base: rate for base, points in sorted(self.funding.items()) if (rate := funding_at(points, now))}
        if funding:
            out["funding"] = funding
        macro = macro_at(now) if self.macro else None
        if macro:
            out["macro"] = macro
        return out


# -- lecture des sources (réseau) ------------------------------------------------------------------------------
def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_fear_greed(raw: Any) -> Points:
    """Les lignes de l'API en points triés. Une ligne abîmée est ignorée ; aucune ligne valable = erreur."""
    rows = raw.get("data") if isinstance(raw, dict) else None
    points = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        ts, value = _finite(row.get("timestamp")), _finite(row.get("value"))
        if ts is not None and value is not None and ts > 0 and 0 <= value <= 100:
            points[ts] = value
    if not points:
        raise FeedError("indice Fear & Greed : réponse sans valeur exploitable")
    return sorted(points.items())


def fetch_fear_greed(limit: int = 0, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 15.0) -> Points:
    """`limit` dernières valeurs (0 = tout l'historique, depuis 2018)."""
    request = urllib.request.Request(FNG_URL + str(int(limit)), headers={"User-Agent": "tradeagent"})
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(FNG_MAX_BYTES + 1)
        if len(body) > FNG_MAX_BYTES:
            raise FeedError("indice Fear & Greed : réponse trop grosse")
        return parse_fear_greed(json.loads(body))
    except FeedError:
        raise
    except Exception as exc:  # réseau, délai, JSON
        raise FeedError(f"indice Fear & Greed indisponible : {type(exc).__name__}: {exc}") from exc


def funding_symbol(base: str) -> str:
    return f"{base}/USD:USD"


def fetch_funding(client: Any, base: str) -> Points:
    """Taux horaires du contrat perpétuel de `base` (Kraken Futures rend tout son historique en un appel)."""
    try:
        rows = client.fetch_funding_rate_history(funding_symbol(base))
    except Exception as exc:  # famille d'exceptions ccxt
        raise FeedError(f"taux de financement de {base} indisponible : {type(exc).__name__}: {exc}") from exc
    points = {}
    for row in rows or []:
        ts, rate = _finite((row or {}).get("timestamp")), _finite((row or {}).get("fundingRate"))
        if ts is not None and rate is not None and ts > 0 and abs(rate) <= FUNDING_MAX_ABS:
            points[ts / 1000] = rate
    if not points:
        raise FeedError(f"taux de financement de {base} : réponse sans valeur exploitable")
    return sorted(points.items())


def funding_client() -> Any:
    from .replay import public_client

    return public_client(FUNDING_EXCHANGE)


class LiveContext:
    """Les flux en direct : relus au plus une fois par heure, gardés en mémoire. Une source en panne garde ses
    dernières valeurs, qui disparaissent d'elles-mêmes quand elles sont trop vieilles."""

    def __init__(self, bases: tuple[str, ...] | list[str], refresh_seconds: float = HOUR,
                 fear_greed: Callable[[], Points] | None = None,
                 funding: Callable[[str], Points] | None = None) -> None:
        self._bases = tuple(bases)
        self._refresh = refresh_seconds
        self._fear_greed = fear_greed or (lambda: fetch_fear_greed(limit=30))
        self._funding = funding
        self._client: Any = None
        self._data = ContextData()
        self._read_at: float | None = None

    def _read_funding(self, base: str) -> Points:
        if self._funding is not None:
            return self._funding(base)
        if self._client is None:
            self._client = funding_client()
        return fetch_funding(self._client, base)

    def context(self, now: float) -> dict[str, Any]:
        if self._read_at is None or now - self._read_at >= self._refresh:
            self._read_at = now                  # avant la lecture : une panne ne relance pas la lecture à chaque cycle
            mood, funding = self._data.fear_greed, dict(self._data.funding)
            try:
                mood = self._fear_greed()
            except Exception as exc:
                log.warning("flux d'information : %s", exc)
            for base in self._bases:
                try:
                    funding[base] = self._read_funding(base)
                except Exception as exc:
                    log.warning("flux d'information : %s", exc)
            self._data = ContextData(mood, funding)
        return self._data.context(now)


# -- historique pour le backtest -------------------------------------------------------------------------------
def _read_points(path: Path) -> Points | None:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
        return sorted((float(ts), float(value)) for ts, value in rows)
    except (OSError, ValueError, KeyError, TypeError):
        return None     # cache absent ou abîmé : on retélécharge


def _cached(path: Path, end: float, slack: float, refresh: bool, fetch: Callable[[], Points]) -> Points:
    """La série du cache si elle va jusqu'à `end` (à `slack` près), sinon retéléchargée en entier."""
    points = None if refresh else _read_points(path)
    if points and points[-1][0] >= end - slack:
        return points
    points = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": time.time(), "rows": points}), encoding="utf-8")
    return points


def load_context_history(cache_dir: str | Path, bases: tuple[str, ...] | list[str], start: float, end: float,
                         refresh: bool = False, fear_greed: Callable[[], Points] = fetch_fear_greed,
                         funding: Callable[[str], Points] | None = None) -> tuple[ContextData, list[str]]:
    """Les flux pour rejouer [start, end), et une ligne par flux qui dit ce qu'il couvre. Un flux qu'on ne peut pas
    lire est absent du backtest, et c'est écrit."""
    cache_dir = Path(cache_dir)
    day = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")  # noqa: E731
    notes: list[str] = []
    mood: Points = []
    try:
        mood = _cached(cache_dir / "context-fear-greed.json", end, 2 * DAY, refresh, fear_greed)
        notes.append(f"indice Fear & Greed (source : alternative.me), du {day(mood[0][0])} au {day(mood[-1][0])}")
    except (FeedError, OSError) as exc:
        notes.append(f"indice Fear & Greed ABSENT : {exc}")
    rates: dict[str, Points] = {}
    client: Any = None
    for base in bases:
        def read(base: str = base) -> Points:
            nonlocal client
            if funding is not None:
                return funding(base)
            if client is None:
                client = funding_client()
            return fetch_funding(client, base)
        try:
            rates[base] = _cached(cache_dir / f"context-funding-{FUNDING_EXCHANGE}-{base}.json", end, 3 * HOUR, refresh, read)
            first = rates[base][0][0]
            notes.append(f"taux de financement {base} ({FUNDING_EXCHANGE}), du {day(first)} au {day(rates[base][-1][0])}"
                         + (" : ne couvre pas le début de la période" if first > start else ""))
        except (FeedError, OSError) as exc:
            notes.append(f"taux de financement {base} ABSENT : {exc}")
    covered = macro_at(start) is not None and macro_at(end) is not None
    notes.append("calendrier macro (Fed et CPI, dates de 2026)" + ("" if covered else " : ne couvre pas toute la période"))
    return ContextData(mood, rates), notes
