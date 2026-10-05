"""Profils de lancement : un nom court = un agent, un flux de prix, une base de données et un port.

Source unique de vérité : les scripts de lancement, la CLI et l'interface web s'appuient tous sur ce fichier.
`paper llm` et `ui llm` parlent donc forcément de la même base, et deux profils ne partagent jamais
ni leur argent fictif, ni leur kill switch, ni leur journal : on peut comparer `hold` et `llm` côte à côte.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .config import Config, ConfigError

DEFAULT_PROFILE = "hold"
LIVE = "live"

LIVE_REFUSAL = (
    "le mode réel n'existe pas encore dans ce projet, volontairement : le code ne sait pas passer d'ordre sur un vrai compte.\n"
    "Avant de l'écrire, il faut :\n"
    "  1. une à deux semaines de paper trading où l'agent `llm` est comparé à la référence `hold` ;\n"
    "  2. la confirmation manuelle des ordres (file d'approbation) ;\n"
    "  3. un adaptateur d'exchange authentifié, testé contre un faux exchange local, avec un sous-compte dédié et des clés\n"
    "     SANS droit de retrait.\n"
    "En attendant, le profil `llm` fait tout le travail avec de l'argent fictif (mais l'API, elle, est facturée)."
)


@dataclass(frozen=True)
class Profile:
    name: str
    agent: str
    feed: str
    port: int                      # un port par profil : on peut regarder plusieurs bots en même temps
    description: str
    cycle_seconds: float | None = None   # None = valeur de config.yaml
    costs_money: bool = False      # vrai si le profil consomme de l'API facturée


PROFILES: dict[str, Profile] = {
    "hold": Profile("hold", agent="hold", feed="ccxt", port=8765,
                    description="Référence : vrais prix, l'agent ne fait rien. Aucun coût."),
    "llm": Profile("llm", agent="llm", feed="ccxt", port=8766, costs_money=True,
                   description="Vrais prix + vrai LLM Anthropic. Argent fictif, mais l'API est facturée."),
    "demo": Profile("demo", agent="chaos", feed="synthetic", port=8767, cycle_seconds=2.0,
                    description="Hors ligne : prix simulés, agent aléatoire, un cycle toutes les 2 s. Pour voir l'interface s'animer."),
    "board": Profile("board", agent="board", feed="ccxt", port=8768,
                     description="Vrais prix, le board trade sur les modèles du code, sans LLM. Aucun coût."),
}


def get_profile(name: str) -> Profile:
    if name == LIVE:
        raise ConfigError(LIVE_REFUSAL)
    try:
        return PROFILES[name]
    except KeyError:
        raise ConfigError(f"profil inconnu : {name!r} (choix : {', '.join(PROFILES)})") from None


def database_for(cfg: Config, profile: Profile) -> str:
    """`data/agent.db` devient `data/paper-<profil>.db` : un profil = une vie, un journal."""
    return str(Path(cfg.database).with_name(f"paper-{profile.name}.db"))


def apply_profile(cfg: Config, profile: Profile) -> Config:
    changes: dict = {"database": database_for(cfg, profile)}
    if profile.cycle_seconds is not None:
        changes["cycle_seconds"] = profile.cycle_seconds
    return replace(cfg, **changes)
