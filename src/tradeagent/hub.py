"""L'accueil de l'interface derrière un proxy : une carte par profil, rendue par le serveur, sans script.

Tout ce qui est écrit ici est fabriqué par le code à partir des résumés de `dashboard.summarize` (des nombres et
l'état d'un bot) et du nom du profil, qui vient de la liste fixe des profils. Chaque valeur est échappée. Aucun texte
de la base (raison d'une mort, journal) n'y passe, aucun attribut `style` (la CSP l'interdit) : les longueurs
des barres sont des attributs SVG.
"""
from __future__ import annotations

import html
from typing import Any

NBSP, THIN = " ", " "
KINDS = {"board": "sous-agents", "hold": "référence", "llm": "LLM", "llm-fake": "LLM simulé", "chaos": "aléatoire",
         "quant": "modèles", "buyhold": "achat puis conservation"}
GLYPHS = {
    "alive": '<svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true"><circle cx="4" cy="4" r="4"/></svg>',
    "dead": ('<svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true" class="stroke">'
             '<circle cx="8" cy="8" r="6"/><path d="m5.5 5.5 5 5m0-5-5 5"/></svg>'),
    "halted": ('<svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true">'
               '<rect x="3.5" y="3" width="3" height="10" rx="0.8"/><rect x="9.5" y="3" width="3" height="10" rx="0.8"/></svg>'),
    "mute": ('<svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true" class="stroke">'
             '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2 1.4"/></svg>'),
    "none": '<svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true" class="stroke"><circle cx="4" cy="4" r="3.2"/></svg>',
}


def number(value: float, decimals: int = 2) -> str:
    """Format français : espace fine insécable entre les milliers, virgule décimale (comme Intl fr-FR)."""
    text = f"{abs(value):,.{decimals}f}".replace(",", THIN).replace(".", ",")
    return ("−" if value < 0 else "") + text


def money(value: float) -> str:
    return number(value) + NBSP + "€"


def sign_of(value: float) -> str:
    return "pos" if value >= 0.005 else "neg" if value <= -0.005 else "zero"


def signed_money(value: float) -> str:
    return {"pos": "+", "neg": "−", "zero": "±"}[sign_of(value)] + money(abs(value))


def duration(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 172_800:
        h, m = divmod(round(s / 60), 60)
        return f"{h} h {m:02d}" if m else f"{h} h"
    return f"{round(s / 86_400)} j"


def state_of(summary: dict[str, Any] | None, now: float) -> tuple[str, str]:
    """(glyphe, mot) : un état a toujours un glyphe et un mot, jamais la couleur seule."""
    if summary is None:
        return "mute", "Illisible"
    if not summary["has_data"]:
        return "none", "Aucune donnée"
    if summary["state"] == "dead":
        return "dead", "Mort"
    if summary["state"] == "halted":
        return "halted", "Arrêté"
    if now - summary["last_update"] > 2.5 * summary["cycle_seconds"] + 60:
        return "mute", "Muet"
    return "alive", "En vie"


def _foot(name: str, summary: dict[str, Any] | None, glyph: str, now: float) -> str:
    if summary is None:
        return "Base momentanément illisible, nouvel essai au prochain chargement."
    if not summary["has_data"]:
        return f"Pas encore lancé : tradeagent run --profile {name}"
    tier = {"normal": "palier normal", "cautious": "palier prudent", "defensive": "palier défensif"}.get(
        summary["risk_tier"], "palier " + summary["risk_tier"])
    since = summary["since"]
    if glyph == "dead":
        return (f"Mort il y a {duration(now - since)} · " if since is not None else "Mort · ") + \
            f"drawdown {number(summary['drawdown_pct'], 1)} %"
    if glyph == "halted":
        return (f"Arrêté il y a {duration(now - since)} · " if since is not None else "Arrêté · ") + "reprise manuelle"
    if glyph == "mute":
        return f"Aucun cycle depuis {duration(now - summary['last_update'])}"
    if summary["agent"] == "hold":
        return "Ne fait rien. À battre."
    return f"Dernier cycle il y a {duration(now - summary['last_update'])} · {tier}"


def _spark(points: list[list[float]]) -> str:
    values = [p[1] for p in points]
    if len(values) < 2 or max(values) == min(values):
        path = "M0,15 L100,15"
    else:
        lo, hi = min(values), max(values)
        path = "M" + " L".join(f"{i / (len(values) - 1) * 100:.1f},{28 - (v - lo) / (hi - lo) * 26:.1f}"
                                for i, v in enumerate(values))
    return ('<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none" width="96" height="32" aria-hidden="true">'
            f'<path d="{path}" vector-effect="non-scaling-stroke"/></svg>')


def cards(entries: list[tuple[str, dict[str, Any] | None]], now: float) -> str:
    """Une carte-lien par profil. `None` = base illisible."""
    out = []
    for name, summary in entries:
        safe = html.escape(name)
        glyph, word = state_of(summary, now)
        kind = KINDS.get(summary.get("agent") or "", summary.get("agent") or "") if summary else ""
        body = ""
        if summary and summary["has_data"]:
            sign = sign_of(summary["net_result"])
            body = ('<span class="row"><span class="figures">'
                    f'<span class="equity">{money(summary["equity"])}</span>'
                    f'<span class="net">Net <span class="sign-{sign}">{signed_money(summary["net_result"])}</span></span>'
                    f'</span>{_spark(summary["series_7d"])}</span>')
        out.append(
            f'<li><a class="profile" href="/p/{safe}/">'
            f'<span class="head"><span class="name">{safe}</span><span class="kind">{html.escape(kind)}</span>'
            f'<span class="state is-{glyph}">{GLYPHS[glyph]}{word}</span></span>'
            f'{body}<span class="foot">{html.escape(_foot(name, summary, glyph, now))}</span></a></li>')
    return "\n".join(out)


def comparison(entries: list[tuple[str, dict[str, Any] | None]]) -> str:
    """Le résultat net de chaque profil sur une même échelle : un gain part à droite, une perte à gauche, hachurée."""
    rows = [(name, s["net_result"]) for name, s in entries if s and s["has_data"]]
    if not rows:
        return ""
    scale = max(abs(net) for _, net in rows) or 1.0
    lines = []
    for name, net in rows:
        sign, width = sign_of(net), abs(net) / scale * 96
        if sign == "pos":
            bar = f'<rect class="gain" x="100" y="3" width="{width:.2f}" height="12"/>'
        elif sign == "neg":
            bar = f'<rect class="loss" fill="url(#hatch)" x="{100 - width:.2f}" y="3" width="{width:.2f}" height="12"/>'
        else:
            bar = '<rect class="flat" x="99" y="3" width="2" height="12"/>'
        lines.append(
            f'<span class="cmp-name">{html.escape(name)}</span>'
            '<svg class="cmp-bar" viewBox="0 0 200 18" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect class="track" x="0" y="0" width="200" height="18"/>{bar}'
            '<line x1="100" y1="-4" x2="100" y2="22"/></svg>'
            f'<span class="cmp-value sign-{sign}">{signed_money(net)}</span>')
    return ('<section class="compare"><h2>Face à la référence</h2><div class="cmp-grid">' + "".join(lines) + "</div>"
            '<p class="fine">Résultat net depuis la mise, même échelle. À gauche du trait : sous la mise. '
            "Vert et à droite : gain. Rouge, hachuré et à gauche : perte.</p></section>")
