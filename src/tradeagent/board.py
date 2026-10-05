"""Le board : plusieurs sous-agents, un seul portefeuille.

Un sous-agent ne passe pas d'ordre. Il tient ce qu'il VEUT détenir, en quantité par symbole (une quantité, pas un
pourcentage de l'equity : une cible en pourcentage bouge avec le prix et ferait retailler la position à chaque
cycle, ce qui se paie en frais). Sortir, pour lui, c'est remettre sa cible à zéro.

Le board additionne les cibles, les compare au portefeuille réel et rend une `Decision` par cycle, les ventes
d'abord. C'est un agent comme les autres : il ne voit qu'une `MarketView`, et tout ce qu'il demande passe par les
garde-fous.

Les cibles ne dépassent jamais longtemps la réalité : au début de chaque cycle, ce qui a été demandé mais pas
obtenu (ordre réduit, refusé, raté, ou passé après un autre) est retiré aux sous-agents. Celui dont l'achat n'est
pas parti redevient libre et rejuge son entrée avec les prix et les marges du moment. Une vente possible qui
n'est pas partie, elle, reste due : elle est redemandée à chaque cycle, et tant qu'il en reste une aucun
sous-agent n'ouvre de position (une sortie décidée ne s'annule pas par une entrée arrivée entre-temps).

Une position de moins que l'ordre minimal ne peut pas être vendue (les garde-fous la refusent) : ce n'est pas une
vente due. Si les sous-agents la réclament et qu'elle leur a été servie (à `FILL_TOLERANCE` près : frais,
glissement, arrondi de quantité), elle reste la leur, avec son niveau de sortie. Si personne ne la réclame, elle
est laissée là : elle sera vendue le jour où elle repasse le minimum, ou comptée dans une nouvelle entrée.

Les seuils se jugent sur la valeur exacte (quantité fois prix), comme le font les garde-fous, et non sur la valeur
arrondie de la vue : sinon une position à un demi-centime du minimum serait redemandée et refusée à chaque cycle.

État : un dictionnaire par sous-agent. En backtest il reste en mémoire ; en `run`, `stored_board` le relit de la
base au démarrage (clé `board_state`, effacée par `reset`) et l'y réécrit à chaque changement. Il est écrit au
moment de la décision, avant l'ordre : si le bot s'arrête entre les deux, le cycle suivant constate que la
position n'est pas là et rend la cible au sous-agent.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Protocol

from .agents import MarketView
from .models import BUY, HOLD, SELL, Decision
from .strategies import buy_room

FILL_TOLERANCE = 0.95   # détenir au moins 95 % de la cible, c'est l'avoir obtenue (frais, glissement, arrondi)
STATE_KEY = "board_state"   # {sous-agent: {qty, stops, notes}} : dans LIFE_KEYS (`app.py`), une vie = un état
SLEEVE_NAMES = ("trend",)
MAX_NOTE_CHARS = 200


def exact_value(position: dict[str, float]) -> float:
    return position["quantity"] * position["price"]


class Sleeve(Protocol):
    """Un sous-agent du board."""

    name: str

    def wanted(self) -> dict[str, float]:
        """Quantité voulue par symbole (absent ou 0 = rien)."""
        ...

    def rescale(self, symbol: str, factor: float) -> None:
        """Le portefeuille détient moins que voulu : la cible est multipliée par `factor` (0 = position perdue)."""
        ...

    def update(self, view: MarketView, may_enter: bool) -> None:
        """Lit le marché : sorties, puis entrées si `may_enter` (faux tant qu'une vente reste due)."""
        ...

    def note(self, symbol: str) -> str:
        """La raison du dernier changement de cible sur ce symbole."""
        ...


class TrendSleeve:
    """Suivi de tendance : la logique du témoin `quant` (`strategies.py`), exprimée en cibles.

    Entre sur un symbole dont le régime de tendance est à la hausse, à la taille que donne le modèle de risque ;
    sort quand le régime passe à la baisse ou quand le prix touche son niveau de sortie, qui suit le prix à la
    hausse. Pas d'entrée le cycle d'une sortie : la vente passe seule, comme chez le témoin. Deux entrées le même
    cycle sont possibles : le board n'en achète qu'une, l'autre est rendue au cycle suivant et rejugée avec les
    marges du moment.
    """

    name = "trend"

    def __init__(self, state: dict | None = None) -> None:
        state = state if state is not None else {}
        self._qty: dict[str, float] = state.setdefault("qty", {})
        self._stops: dict[str, float] = state.setdefault("stops", {})
        self._notes: dict[str, str] = state.setdefault("notes", {})

    def wanted(self) -> dict[str, float]:
        return dict(self._qty)

    def note(self, symbol: str) -> str:
        return self._notes.get(symbol, "")

    def _drop(self, symbol: str) -> None:
        self._qty.pop(symbol, None)
        self._stops.pop(symbol, None)

    def rescale(self, symbol: str, factor: float) -> None:
        if factor <= 0:
            if symbol in self._qty:
                self._notes[symbol] = "position non obtenue ou perdue"
            self._drop(symbol)
        elif symbol in self._qty:
            self._qty[symbol] *= factor

    def update(self, view: MarketView, may_enter: bool) -> None:
        leaving = False
        for symbol in sorted(self._qty):                # sortir d'abord
            position = view.positions.get(symbol)
            if position is None:                        # symbole retiré de la config : plus rien à y vouloir
                self._drop(symbol)
                continue
            price = position["price"]
            models = view.market.get(symbol, {}).get("models") or {}
            exit_pct = (models.get("risk") or {}).get("exit_pct")
            if exit_pct:                                # le niveau de sortie suit le prix à la hausse, jamais à la baisse
                self._stops[symbol] = max(self._stops.get(symbol, 0.0), price * (1 - exit_pct / 100))
            stop = self._stops.get(symbol)
            if (models.get("trend") or {}).get("regime") == "down":
                self._drop(symbol)
                self._notes[symbol] = "tendance à la baisse, on sort"
            elif stop and price <= stop:
                self._drop(symbol)
                self._notes[symbol] = f"niveau de sortie touché ({stop:.5g})"
            else:
                continue
            leaving = True
        if leaving or not may_enter:
            return
        minimum = view.limits.get("min_order_quote", 0.0)
        for symbol in sorted(view.positions):
            if symbol in self._qty:
                continue
            models = view.market.get(symbol, {}).get("models") or {}
            risk = models.get("risk") or {}
            price = view.positions[symbol]["price"]
            if (models.get("trend") or {}).get("regime") != "up" or not risk or price <= 0:
                continue
            amount = min(buy_room(view, symbol), round(view.equity * risk["size_pct"] / 100, 2))
            if amount >= minimum and amount > 0:
                self._qty[symbol] = amount / price
                self._stops[symbol] = price * (1 - risk["exit_pct"] / 100)
                self._notes[symbol] = f"tendance à la hausse, sortie à -{risk['exit_pct']:g} %"


class BoardAgent:
    """Additionne les cibles des sous-agents et trade l'écart avec le portefeuille réel, un ordre par cycle."""

    name = "board"

    def __init__(self, sleeves: list[Sleeve], state: dict | None = None,
                 save: Callable[[dict], None] | None = None) -> None:
        """`state` et `save` : l'état que se partagent les sous-agents, et de quoi le garder quand il change."""
        if not sleeves:
            raise ValueError("un board sans sous-agent ne peut rien décider")
        self._sleeves = list(sleeves)
        self._state = state
        self._save = save
        self._saved = json.dumps(state, sort_keys=True)     # ce que la base contient, pas ce qu'on a voulu y écrire

    def _wanted(self, symbol: str) -> float:
        return sum(max(0.0, sleeve.wanted().get(symbol, 0.0)) for sleeve in self._sleeves)

    def _why(self, symbol: str, leaving: bool) -> str:
        """Les raisons des sous-agents concernés : ceux qui sortent pour une vente, ceux qui détiennent pour un achat."""
        notes = [f"{sleeve.name}: {sleeve.note(symbol)}" for sleeve in self._sleeves
                 if (sleeve.wanted().get(symbol, 0.0) <= 0) == leaving and sleeve.note(symbol)]
        return "board/" + (" ; ".join(notes) or "écart avec les cibles")

    def _sell(self, view: MarketView, floor: float) -> Decision | None:
        """La première vente due : ce que le portefeuille détient au-delà des cibles."""
        for symbol in sorted(view.positions):
            position = view.positions[symbol]
            held = exact_value(position)
            if held < floor:
                continue
            everything = round(held, 2)
            wanted = self._wanted(symbol)
            if wanted <= 0:
                return Decision(SELL, symbol, everything, self._why(symbol, leaving=True))
            excess = round((position["quantity"] - wanted) * position["price"], 2)
            if excess >= floor:
                # Un reste trop petit pour être revendu un jour ne se garde pas : la sortie l'emporte.
                amount = excess if held - excess >= floor else everything
                return Decision(SELL, symbol, amount, self._why(symbol, leaving=True))
        return None

    def decide(self, view: MarketView) -> Decision:
        if self._save is None:
            return self._decide(view)
        try:
            return self._decide(view)
        finally:
            # Comparé à la dernière écriture RÉUSSIE : une écriture ratée (ou une erreur en route) est retentée au
            # cycle suivant, sinon un redémarrage relirait un état plus ancien, avec un niveau de sortie plus bas.
            current = json.dumps(self._state, sort_keys=True)
            if current != self._saved:
                self._save(self._state)
                self._saved = current

    def _decide(self, view: MarketView) -> Decision:
        minimum = view.limits.get("min_order_quote", 0.0)
        floor = max(minimum, 0.01)
        for symbol, position in view.positions.items():     # ce qui a été demandé mais pas obtenu est rendu
            wanted = self._wanted(symbol)
            if position["quantity"] >= wanted:
                continue
            served = position["quantity"] >= wanted * FILL_TOLERANCE
            factor = position["quantity"] / wanted if served or exact_value(position) >= floor else 0.0
            for sleeve in self._sleeves:
                sleeve.rescale(symbol, factor)
        may_enter = self._sell(view, floor) is None         # une vente encore due : personne n'entre
        for sleeve in self._sleeves:
            sleeve.update(view, may_enter)
        sell = self._sell(view, floor)                      # sortir d'abord : une vente n'est jamais bloquée
        if sell is not None:
            return sell
        for symbol in sorted(view.positions):
            position = view.positions[symbol]
            missing = round((self._wanted(symbol) - position["quantity"]) * position["price"], 2)
            amount = min(buy_room(view, symbol), missing)
            if amount >= minimum and amount > 0:
                return Decision(BUY, symbol, amount, self._why(symbol, leaving=False))
        return Decision(HOLD, reasoning="board: pas d'écart à traiter")


def _positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:       # un entier démesuré dans le JSON
        return None
    return number if math.isfinite(number) and number > 0 else None


def clean_state(stored: Any) -> dict[str, dict[str, dict]]:
    """L'état relu de la base : tout ce qui n'a pas la forme attendue est écarté, jamais une erreur.

    Une quantité qui n'est pas un nombre fini positif disparaît, avec son niveau de sortie : la position, si elle
    existe, n'est alors plus réclamée par personne, et le board la vend.
    """
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, dict[str, dict]] = {}
    for name in SLEEVE_NAMES:
        part = stored.get(name) if isinstance(stored.get(name), dict) else {}
        numbers = {key: {s: n for s, v in part[key].items() if isinstance(s, str) and (n := _positive(v)) is not None}
                   if isinstance(part.get(key), dict) else {} for key in ("qty", "stops")}
        notes = part.get("notes") if isinstance(part.get("notes"), dict) else {}
        out[name] = {
            "qty": numbers["qty"],
            "stops": {s: v for s, v in numbers["stops"].items() if s in numbers["qty"]},
            "notes": {s: v[:MAX_NOTE_CHARS] for s, v in notes.items() if isinstance(s, str) and isinstance(v, str)},
        }
    return out


def stored_board(storage: Any) -> BoardAgent:
    """Le board de `tradeagent run` : son état vit dans la base du profil."""
    state = clean_state(storage.get(STATE_KEY))
    return BoardAgent([TrendSleeve(state["trend"])], state, lambda current: storage.set(STATE_KEY, current))
