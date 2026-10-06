"""Serveur d'aperçu de l'interface, pour la comparer aux captures de référence. OUTIL DE DÉVELOPPEMENT.

Il sert les fichiers de `src/tradeagent/static/` avec les mêmes gabarits que `web.py` et `gateway.py`, mais un
instantané figé (`fixtures/snapshot-*.json` du dossier de handoff) à la place de `/api/snapshot`. Il n'ouvre aucune
base, ne lance aucun bot, et ne fait pas partie du paquet `tradeagent` : rien ne permet de l'activer en production.

    python tools/frontcheck/preview.py --port 8799
    http://127.0.0.1:8799/web/vie-normal/        la page d'un profil, derrière un proxy
    http://127.0.0.1:8799/electron/mort/         la même, servie en local (application de bureau)
    http://127.0.0.1:8799/hub  /login  /login-erreur  /setup

Deux réglages, pour rejouer un changement d'état sans recharger la page :
    /__set?snapshot=arrive     tous les profils servent cet instantané (vide = retour à la normale)
    /__set?down=1              /api/snapshot répond 503, comme un serveur coupé (down=0 pour rétablir)
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from tradeagent import hub  # noqa: E402
from tradeagent.auth import MIN_PASSWORD_CHARS  # noqa: E402
from tradeagent.gateway import BAD_LOGIN, HEADERS, LOGOUT_FORM, _profile_nav  # noqa: E402
from tradeagent.web import FONTS, HTML_TYPE, SECURITY_HEADERS, STATIC_DIR, profile_page, render_page  # noqa: E402

FIXTURES = ROOT / "docs" / "handoffs" / "frontend" / "01_Revue_Globale" / "fixtures"
PROFILES = ["board", "hold", "llm"]
# Liste fixe : le nom d'un instantané ne sert jamais à construire un chemin à partir de la requête.
NAMES = ("vie-normal", "vie-prudent", "vie-defensif", "mort", "arrete", "muet", "vide", "arrive")
STATE = {"snapshot": "", "down": False}


def fixture(name: str) -> dict:
    """Un instantané de test, rapproché de ce que produit vraiment `build_snapshot` (voir le compte rendu)."""
    if name not in NAMES:
        raise KeyError(name)
    if name == "arrive":            # « vie-normal », un cycle plus tard : un achat réduit vient d'arriver
        snap = fixture("vie-normal")
        snap["journal"].insert(0, {
            "id": snap["journal"][0]["id"] + 1, "ts": snap["generated_at"], "agent": "board", "action": "buy",
            "symbol": "BTC/EUR", "amount_quote": 40.0, "approved": 1,
            "verdict": "réduit à 25,30 € : plafond de position", "reasoning": "trend : régime haussier confirmé sur BTC/EUR",
        })
        return snap
    snap = json.loads((FIXTURES / f"snapshot-{name}.json").read_text(encoding="utf-8"))
    if not snap.get("has_data"):
        return snap
    # La référence à battre n'est pas encore dans l'instantané (point 5 de la liste « Données ») : elle est ajoutée
    # ici pour que la carte puisse être comparée aux captures.
    snap.setdefault("reference", {"profile": "hold", "equity": snap["money"]["stake"], "net_result": 0.0})
    # De même pour la référence marché et la valeur de liquidation (ajoutées après le handoff) : des valeurs de
    # démonstration, dans la forme que leur donne `build_snapshot`.
    money, held = snap["money"], sum(p["value"] or 0.0 for p in snap["positions"])
    money.setdefault("exit_costs", held * 0.0029987)
    money.setdefault("liquidation_result", money["net_result"] - money["exit_costs"])
    snap.setdefault("market_reference", {
        "started": snap["life"]["started"], "late": False, "weights_pct": {s: 40.0 for s in snap["symbols"]},
        "equity": money["stake"] * 1.012, "net_result": money["stake"] * 0.012, "exit_costs": money["stake"] * 0.0024})
    # Le moteur n'écrit pas de décision pour une liquidation : il écrit une exécution de source « killswitch ».
    kept = []
    for row in snap["journal"]:
        if "kill switch" in (row.get("reasoning") or ""):
            snap.setdefault("fills", []).append({
                "id": row["id"], "ts": row["ts"], "source": "killswitch", "symbol": row["symbol"], "side": row["action"],
                "quantity": 0.0, "price": 0.0, "fee": 0.0, "notional": row["amount_quote"]})
        else:
            kept.append(row)
    snap["journal"] = kept
    return snap


def hub_entries() -> tuple[list, float]:
    """Les quatre profils de la capture d'accueil, résumés comme le ferait `dashboard.summarize`."""
    def summary(snap: dict, agent: str | None = None, **changes) -> dict:
        m = snap["money"]
        out = {"has_data": True, "agent": agent or snap["agent"], "state": snap["status"]["state"],
               "since": snap["status"]["since"], "risk_tier": snap["risk_tier"], "equity": m["equity"], "stake": m["stake"],
               "net_result": m["net_result"], "drawdown_pct": m["drawdown_pct"], "life_started": snap["life"]["started"],
               "last_update": m["last_update"], "cycle_seconds": snap["cycle_seconds"], "generated_at": snap["generated_at"],
               "series_7d": snap["equity_series"][-15:]}
        out.update(changes)
        return out
    normal = fixture("vie-normal")
    flat = [[p[0], 1000.0] for p in normal["equity_series"]]
    entries = [("board", summary(normal)),
               ("hold", summary(normal, "hold", equity=1000.0, net_result=0.0, series_7d=flat[-15:])),
               ("llm", summary(fixture("mort"), "llm")),
               ("demo", summary(fixture("arrete"), "chaos"))]
    return entries, normal["generated_at"]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # Comme `web.py` : seul un navigateur qui s'adresse à la boucle locale est servi (« DNS rebinding »).
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send(403, b"hote non autorise", "text/plain; charset=utf-8", {})
            return
        url = urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        query = parse_qs(url.query, keep_blank_values=True)
        if url.path == "/__set":
            if "snapshot" in query and query["snapshot"][0] in ("", *NAMES):
                STATE["snapshot"] = query["snapshot"][0]
            if "down" in query:
                STATE["down"] = query["down"][0] == "1"
            self._send(200, json.dumps(STATE).encode(), "application/json", {})
            return
        if url.path in ("/auth.css",) or url.path[1:] in FONTS:
            name = url.path[1:]
            kind = "font/woff2" if name in FONTS else "text/css; charset=utf-8"
            self._send(200, (STATIC_DIR / name).read_bytes(), kind, HEADERS)
            return
        if url.path == "/hub":
            entries, now = hub_entries()
            page = render_page("hub.html", profiles=hub.cards(entries, now), compare=hub.comparison(entries), count="4 profils")
            self._send(200, page, HTML_TYPE, HEADERS)
            return
        if url.path in ("/login", "/login-erreur", "/setup"):
            page = "setup.html" if url.path == "/setup" else "login.html"
            note = html.escape(BAD_LOGIN) if url.path == "/login-erreur" else ""
            self._send(200, render_page(page, message=note, min_password=str(MIN_PASSWORD_CHARS)), HTML_TYPE, HEADERS)
            return
        if len(parts) >= 2 and parts[0] in ("web", "electron") and parts[1] in NAMES:
            context, name, rest = parts[0], parts[1], "/".join(parts[2:])
            headers = HEADERS if context == "web" else SECURITY_HEADERS
            if rest == "":
                if context == "web":
                    page = profile_page("web", "board", _profile_nav("board", PROFILES), LOGOUT_FORM)
                else:
                    page = profile_page("electron", "board", '<span class="profile-name">board</span>')
                self._send(200, page, HTML_TYPE, headers)
                return
            if rest == "api/snapshot":
                if STATE["down"]:
                    self._send(503, b'{"error": "serveur coupe"}', "application/json", headers)
                    return
                snap = fixture(STATE["snapshot"] or name)
                self._send(200, json.dumps(snap, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", headers)
                return
            if rest in ("app.css", "app.js") or rest in FONTS:
                kind = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}.get(rest, "font/woff2")
                self._send(200, (STATIC_DIR / rest).read_bytes(), kind, headers)
                return
        self._send(404, b"introuvable", "text/plain; charset=utf-8", {})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)     # boucle locale seulement
    print(f"aperçu : http://127.0.0.1:{args.port}/web/vie-normal/  (Ctrl+C pour arrêter)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
