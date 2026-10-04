"""Conseil à l'utilisateur quand l'agent est à l'arrêt.

Un bot qui ne peut plus passer aucun ordre n'est plus appelé (il ne paierait que pour constater). Il ne meurt
pas pour autant : c'est l'utilisateur qui décide de son sort (décision de Pierre, 2026-10-04), au vu de ce que
la vie a rapporté et consommé. Le code ne fait que dresser le bilan et conseiller ; il n'agit pas.

Fonction pure : le texte est fabriqué ici, à partir de nombres. Aucun texte de l'agent n'y entre.
"""
from __future__ import annotations

from typing import Any

DAILY, DEFENSIVE, CASH = "daily", "defensive", "cash"       # causes d'arrêt, posées par l'agent (llm_agent.py)

WHY = {
    DEFENSIVE: "Son equity nette du loyer a trop reculé depuis son plus haut : les achats sont bloqués et il n'a rien "
               "à vendre. Il n'est plus appelé et ne paie plus de loyer, mais il ne s'en relèvera pas seul.",
    CASH: "Il n'a plus assez de cash pour passer l'ordre minimum, et rien à vendre. Il n'est plus appelé et ne paie "
          "plus de loyer, mais il ne s'en relèvera pas seul.",
}


def idle_advice(cause: str, stake: float, equity: float, rent: float, currency: str) -> dict[str, Any]:
    """Ce que l'interface, `status` et les notifications disent d'un agent à l'arrêt.

    `decision` vaut True quand l'utilisateur doit trancher (arrêt sans issue), False quand il suffit d'attendre.
    """
    trading = equity - stake
    net = trading - rent
    if cause == DAILY:
        return {
            "cause": cause, "decision": False, "title": "Agent en pause jusqu'à demain",
            "text": "Les achats sont bloqués pour aujourd'hui (plafond d'achats ou perte du jour atteints) et il n'y a rien "
                    "à vendre. Rien à faire : ils reprennent demain (UTC).",
            "trading": trading, "rent": rent, "net": net,
        }
    balance = (f"Bilan de cette vie : {trading:+.2f} {currency} en trading, {rent:.2f} {currency} d'API consommés, "
               f"soit {net:+.2f} {currency} net.")
    if net < 0:
        advice = ("Conseil : mettre fin à cette vie (tradeagent reset). Elle n'a pas couvert son loyer ; relancée à "
                  "l'identique elle referait la même chose, change d'abord la variante (prompt, cadence).")
    else:
        advice = ("Conseil : repartir d'une nouvelle vie (tradeagent reset). Celle-ci a couvert son loyer, mais elle "
                  "ne peut plus agir.")
    return {
        "cause": cause, "decision": True, "title": "Agent à l'arrêt : à toi de décider",
        "text": f"{WHY.get(cause, WHY[DEFENSIVE])} {balance} {advice}",
        "trading": trading, "rent": rent, "net": net,
    }
