"""Serveur web local, en LECTURE SEULE, pour regarder le bot travailler.

Choix de sécurité (le bot manipule de l'argent, même fictif pour l'instant) :
- il n'écoute que sur la boucle locale (127.0.0.1 / ::1) : aucune option pour l'exposer au réseau ;
- l'en-tête Host doit être celui de la boucle locale (protège contre le « DNS rebinding » depuis un site web) ;
- seulement GET et HEAD ; aucune route n'écrit quoi que ce soit, aucune ne lance ni n'arrête le bot ;
- la base est ouverte en lecture seule (voir `dashboard.py`) ;
- fichiers statiques servis depuis une liste blanche fixe (pas de chemin construit à partir de la requête) ;
- CSP stricte : aucun script ni style en ligne, aucune ressource externe (la police est servie par ce serveur).
"""
from __future__ import annotations

import html
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config, ConfigError
from .dashboard import REFERENCE_PROFILE, DashboardError, add_reference, build_snapshot

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HTML_TYPE = "text/html; charset=utf-8"
# Inter (licence OFL, `static/fonts/OFL.txt`) : trois fichiers fixes, jamais un chemin construit d'après la requête.
FONTS = {f"fonts/Inter-{weight}.woff2": (f"fonts/Inter-{weight}.woff2", "font/woff2")
         for weight in ("Regular", "Medium", "SemiBold")}
FONT_CACHE = {"Cache-Control": "max-age=86400"}    # fichiers publics et immuables : inutile de les relire toutes les 15 s
ASSETS = {
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    **{"/" + path: asset for path, asset in FONTS.items()},
}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORT = 8765

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; font-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def render_page(name: str, **values: str) -> bytes:
    """Une page du dossier statique, où `{{clé}}` est remplacé par un texte fabriqué par le code, déjà échappé."""
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


def profile_page(context: str, profile: str, nav: str, account: str = "") -> bytes:
    """La page d'un profil. `context` dit qui la sert : « electron » pour la page locale (les onglets de l'application
    font la navigation, il n'y a pas de session), « web » derrière un proxy (fil d'Ariane, autres profils, déconnexion)."""
    return render_page("index.html", context=context, profile=html.escape(profile), nav=nav, account=account)


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def make_handler(cfg: Config, profile: str | None = None, reference: Config | None = None):
    """`reference` : la config du profil `hold`, dont le résultat s'affiche à côté de celui de ce bot (lecture seule)."""
    name = profile or ""
    nav = f'<span class="profile-name">{html.escape(name)}</span>' if name else ""

    class Handler(BaseHTTPRequestHandler):
        server_version = "tradeagent"
        sys_version = ""

        # -- réponses -------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, head_only: bool = False,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _json(self, status: int, payload: dict, head_only: bool = False) -> None:
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except ValueError:  # un NaN/inf en base : réponse propre plutôt qu'une connexion coupée
                status, body = 500, b'{"error": "valeur non finie dans la base"}'
            self._send(status, body, "application/json; charset=utf-8", head_only)

        # -- routage --------------------------------------------------------
        def _serve(self, head_only: bool) -> None:
            if self.headers.get("Host", "") not in allowed_hosts(self.server.server_address[1]):
                self._json(403, {"error": "hôte non autorisé"}, head_only)
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot":
                try:
                    snap = build_snapshot(cfg)
                    if reference is not None:
                        add_reference(snap, reference, REFERENCE_PROFILE)
                    self._json(200, snap, head_only)
                except DashboardError as exc:
                    log.warning("instantané indisponible : %s", exc)
                    self._json(503, {"error": "base momentanément illisible, nouvelle tentative au prochain rafraîchissement"},
                               head_only)
                return
            if path == "/":
                self._send(200, profile_page("electron", name, nav), HTML_TYPE, head_only)
                return
            asset = ASSETS.get(path)
            if asset is None:
                self._json(404, {"error": "introuvable"}, head_only)
                return
            file, content_type = asset
            self._send(200, (STATIC_DIR / file).read_bytes(), content_type, head_only,
                       extra=FONT_CACHE if content_type == "font/woff2" else None)

        def do_GET(self) -> None:  # noqa: N802 (nom imposé par http.server)
            self._serve(head_only=False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(head_only=True)

        def _refuse(self) -> None:
            self._send(405, b'{"error": "lecture seule"}', "application/json; charset=utf-8",
                       extra={"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            log.debug("%s - %s", self.address_string(), format % args)

    return Handler


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                profile: str | None = None, reference: Config | None = None) -> ThreadingHTTPServer:
    if host not in LOOPBACK:
        raise ConfigError(
            f"l'interface web n'écoute que sur la boucle locale (127.0.0.1), pas sur {host!r} : "
            "elle montre l'état du bot et ne doit pas être exposée au réseau."
        )
    server_class = ThreadingHTTPServer
    if ":" in host:  # IPv6
        import socket

        class server_class(ThreadingHTTPServer):  # type: ignore[no-redef]
            address_family = socket.AF_INET6

    try:
        server = server_class((host, port), make_handler(cfg, profile, reference))
    except OSError as exc:
        raise ConfigError(
            f"impossible d'ouvrir le port {port} ({exc.strerror or exc}) : une interface est sans doute déjà lancée. "
            "Ouvre-la dans ton navigateur, ou choisis un autre port avec --port."
        ) from exc
    server.daemon_threads = True
    return server
