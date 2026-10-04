"""Serveur web local, en LECTURE SEULE, pour regarder le bot travailler.

Choix de sécurité (le bot manipule de l'argent, même fictif pour l'instant) :
- il n'écoute que sur la boucle locale (127.0.0.1 / ::1) : aucune option pour l'exposer au réseau ;
- l'en-tête Host doit être celui de la boucle locale (protège contre le « DNS rebinding » depuis un site web) ;
- seulement GET et HEAD ; aucune route n'écrit quoi que ce soit, aucune ne lance ni n'arrête le bot ;
- la base est ouverte en lecture seule (voir `dashboard.py`) ;
- fichiers statiques servis depuis une liste blanche fixe (pas de chemin construit à partir de la requête) ;
- CSP stricte : aucun script ni style en ligne, aucune ressource externe.
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config, ConfigError
from .dashboard import DashboardError, build_snapshot

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
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
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def make_handler(cfg: Config):
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
                    self._json(200, build_snapshot(cfg), head_only)
                except DashboardError as exc:
                    log.warning("instantané indisponible : %s", exc)
                    self._json(503, {"error": "base momentanément illisible, nouvelle tentative au prochain rafraîchissement"},
                               head_only)
                return
            asset = ASSETS.get(path)
            if asset is None:
                self._json(404, {"error": "introuvable"}, head_only)
                return
            name, content_type = asset
            self._send(200, (STATIC_DIR / name).read_bytes(), content_type, head_only)

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


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
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
        server = server_class((host, port), make_handler(cfg))
    except OSError as exc:
        raise ConfigError(
            f"impossible d'ouvrir le port {port} ({exc.strerror or exc}) : une interface est sans doute déjà lancée. "
            "Ouvre-la dans ton navigateur, ou choisis un autre port avec --port."
        ) from exc
    server.daemon_threads = True
    return server
