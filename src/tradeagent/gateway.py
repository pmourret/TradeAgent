"""L'interface « derrière un proxy » : la même page en lecture seule, pour plusieurs profils, avec un compte.

C'est le seul mode où l'interface peut écouter ailleurs que sur la boucle locale (`web.py`, lui, ne change pas), et
seulement si on le demande (`--bind 0.0.0.0`, que le compose passe dans le conteneur). Il est fait pour un conteneur
sans port publié, joint par le réseau Docker du proxy (Traefik), qui porte le HTTPS. Les en-têtes Host et
X-Forwarded-Proto ne prouvent pas qu'une requête vient du proxy (un voisin du même réseau Docker peut les écrire) :
ils évitent les erreurs de branchement, et c'est la session qui protège les données.

Choix de sécurité :
- le nom de domaine attendu est déclaré au démarrage ; l'en-tête Host doit être exactement ce nom ;
- le proxy doit annoncer du HTTPS (`X-Forwarded-Proto: https`) : un mot de passe ne voyage pas en clair ;
- toute page et toute donnée d'un bot exigent une session ouverte ; sans session, une page renvoie vers la
  connexion et une donnée répond 401, que le chemin existe ou non. Seuls sont publics : la page de connexion, celle
  de création du compte tant qu'il n'existe pas, leur feuille de style, et `/healthz` qui répond « ok » ;
- les seules requêtes qui ne sont pas GET ou HEAD sont la création du premier compte, la connexion et la
  déconnexion ; elles doivent venir de la page elle-même (en-tête Origin) ;
- aucune route n'agit sur un bot ; les bases sont lues en lecture seule (`dashboard.py`) ;
- le profil vient d'une liste fixe, jamais d'un chemin construit à partir de la requête ;
- rien de ce que saisit la personne n'est journalisé ni renvoyé dans une page.
"""
from __future__ import annotations

import html
import json
import logging
import re
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .auth import (LOCK_SECONDS, MIN_PASSWORD_CHARS, MIN_SETUP_CODE_CHARS, SESSION_SECONDS, SETUP_CODE_ENV, Account, AuthError,
                   Throttle, WrongSetupCode)
from .config import Config, ConfigError
from .dashboard import DashboardError, build_snapshot
from .web import SECURITY_HEADERS, STATIC_DIR

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080
COOKIE = "__Host-tradeagent"        # préfixe __Host- : le navigateur exige Secure, Path=/ et aucun Domain
MAX_BODY_BYTES = 4096
SOCKET_TIMEOUT_SECONDS = 10         # une requête incomplète ne garde pas un fil du serveur indéfiniment
HOST_PATTERN = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
PROFILE_ASSETS = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
}
HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": SECURITY_HEADERS["Content-Security-Policy"].replace("form-action 'none'", "form-action 'self'"),
    "Strict-Transport-Security": "max-age=31536000",
}
HTML_TYPE = "text/html; charset=utf-8"
BAD_LOGIN = "Identifiant ou mot de passe incorrect."
LOCKED = f"Trop d'essais : la connexion est bloquée pendant {LOCK_SECONDS // 60} minutes."


def _page(name: str, **values: str) -> bytes:
    """Une page du dossier statique, où `{{clé}}` est remplacé par un texte fabriqué par le code, échappé."""
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


def make_gateway_handler(profiles: dict[str, Config], account: Account, throttle: Throttle, public_host: str,
                         setup_code: str | None):
    origin = f"https://{public_host}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "tradeagent"
        sys_version = ""
        timeout = SOCKET_TIMEOUT_SECONDS

        # -- réponses -------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, head_only: bool = False,
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _json(self, status: int, payload: object, head_only: bool = False) -> None:
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except ValueError:
                status, body = 500, b'{"error": "valeur non finie dans la base"}'
            self._send(status, body, "application/json; charset=utf-8", head_only)

        def _redirect(self, location: str, head_only: bool = False, cookie: str | None = None) -> None:
            extra = {"Location": location}
            if cookie is not None:
                extra["Set-Cookie"] = cookie
            self._send(303, b"", "text/plain; charset=utf-8", head_only, extra)

        def _form(self, name: str, status: int, message: str = "", head_only: bool = False) -> None:
            note = html.escape(message)
            self._send(status, _page(name, message=note, min_password=str(MIN_PASSWORD_CHARS)), HTML_TYPE, head_only)

        # -- contrôles ------------------------------------------------------
        def _transport_ok(self, head_only: bool, health: bool = False) -> bool:
            if self.headers.get("Host", "").lower() != public_host:
                self._json(403, {"error": "hôte non autorisé"}, head_only)
                return False
            if not health and self.headers.get("X-Forwarded-Proto", "").lower() != "https":
                self._json(403, {"error": "HTTPS requis : cette interface se sert derrière un proxy"}, head_only)
                return False
            return True

        def _session(self) -> bool:
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except CookieError:
                return False
            morsel = jar.get(COOKIE)
            return account.session_valid(morsel.value if morsel else None)

        def _has_account(self) -> bool:
            return account.exists()

        # -- lecture --------------------------------------------------------
        def _serve(self, head_only: bool) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/healthz":                      # pour le contrôle de santé du conteneur : ne dit rien du bot
                if self._transport_ok(head_only, health=True):
                    self._send(200, b"ok\n", "text/plain; charset=utf-8", head_only)
                return
            if not self._transport_ok(head_only):
                return
            try:
                self._route(path, head_only)
            except AuthError as exc:                    # fichier du compte abîmé : dit quoi faire, sans détail technique
                log.error("compte illisible : %s", exc)
                self._json(503, {"error": str(exc)}, head_only)

        def _route(self, path: str, head_only: bool) -> None:
            if path == "/auth.css":
                self._send(200, (STATIC_DIR / "auth.css").read_bytes(), "text/css; charset=utf-8", head_only)
                return
            has_account = self._has_account()
            if path == "/setup":
                if has_account:
                    self._redirect("/login", head_only)
                else:
                    self._form("setup.html", 200, head_only=head_only)
                return
            if not has_account:
                self._redirect("/setup", head_only)
                return
            logged_in = self._session()
            if path == "/login":
                if logged_in:
                    self._redirect("/", head_only)
                else:
                    self._form("login.html", 200, head_only=head_only)
                return
            if not logged_in:
                if path.startswith("/api/") or path.endswith("/api/snapshot"):
                    self._json(401, {"error": "session expirée : reconnecte-toi"}, head_only)
                else:
                    self._redirect("/login", head_only)
                return
            if path == "/":
                links = "\n".join(f'<li><a href="/p/{html.escape(name)}/">{html.escape(name)}</a></li>' for name in profiles)
                self._send(200, _page("hub.html", profiles=links), HTML_TYPE, head_only)
                return
            if path == "/api/profiles":
                self._json(200, [{"name": name, "path": f"/p/{name}/"} for name in profiles], head_only)
                return
            parts = path.split("/")                     # /p/<profil>/<ressource>
            cfg = profiles.get(parts[2]) if len(parts) >= 4 and parts[1] == "p" else None
            if cfg is None:
                self._json(404, {"error": "introuvable"}, head_only)
                return
            rest = "/".join(parts[3:])
            if rest == "api/snapshot":
                try:
                    self._json(200, build_snapshot(cfg), head_only)
                except DashboardError as exc:
                    log.warning("instantané indisponible : %s", exc)
                    self._json(503, {"error": "base momentanément illisible, nouvelle tentative au prochain rafraîchissement"},
                               head_only)
                return
            asset = PROFILE_ASSETS.get(rest)
            if asset is None:
                self._json(404, {"error": "introuvable"}, head_only)
                return
            self._send(200, (STATIC_DIR / asset[0]).read_bytes(), asset[1], head_only)

        def do_GET(self) -> None:  # noqa: N802 (nom imposé par http.server)
            self._serve(head_only=False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(head_only=True)

        # -- les trois seules écritures : créer le compte, se connecter, se déconnecter --
        def _fields(self) -> dict[str, str] | None:
            """Les champs du formulaire, ou None si la requête n'a pas la forme attendue."""
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return None
            if not 0 <= length <= MAX_BODY_BYTES:
                return None
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
                return None
            try:
                raw = self.rfile.read(length).decode("utf-8")
                parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=8)
            except ValueError:
                return None
            return {key: values[0] for key, values in parsed.items()}

        def do_POST(self) -> None:  # noqa: N802
            if not self._transport_ok(False):
                return
            path = self.path.split("?", 1)[0]
            if path not in ("/setup", "/login", "/logout"):
                self._refuse()
                return
            if self.headers.get("Origin", "") != origin:     # la requête doit venir de la page elle-même
                self._json(403, {"error": "origine non autorisée"})
                return
            fields = self._fields()
            if fields is None:
                self._json(400, {"error": "requête invalide"})
                return
            try:
                if path == "/logout":
                    if self._session():
                        account.close_sessions()
                    self._redirect("/login", cookie=f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
                elif path == "/setup":
                    self._setup(fields)
                else:
                    self._login(fields)
            except AuthError as exc:
                log.error("compte illisible : %s", exc)
                self._json(503, {"error": str(exc)})

        def _setup(self, fields: dict[str, str]) -> None:
            if self._has_account():
                self._redirect("/login")
                return
            with throttle.gate:                         # un essai à la fois : voir `Throttle.gate`
                if throttle.locked():
                    self._form("setup.html", 429, LOCKED)
                    return
                try:
                    account.create(fields.get("user", ""), fields.get("password", ""), fields.get("again", ""),
                                   fields.get("code", ""), setup_code)
                except AuthError as exc:
                    if isinstance(exc, WrongSetupCode):     # une faute de saisie, elle, ne bloque personne
                        throttle.failed()
                    log.warning("création du compte refusée")
                    self._form("setup.html", 400, str(exc)[:1].upper() + str(exc)[1:] + ".")
                    return
                throttle.succeeded()
            log.info("compte administrateur créé")
            self._redirect("/login")

        def _login(self, fields: dict[str, str]) -> None:
            if not self._has_account():
                self._redirect("/setup")
                return
            with throttle.gate:                         # un essai à la fois : voir `Throttle.gate`
                if throttle.locked():
                    self._form("login.html", 429, LOCKED)
                    return
                if not account.verify(fields.get("user", ""), fields.get("password", "")):
                    throttle.failed()
                    log.warning("échec de connexion")
                    self._form("login.html", 401, BAD_LOGIN)
                    return
                throttle.succeeded()
            cookie = f"{COOKIE}={account.open_session()}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; Secure; SameSite=Strict"
            self._redirect("/", cookie=cookie)

        def _refuse(self) -> None:
            self._send(405, b'{"error": "lecture seule"}', "application/json; charset=utf-8", extra={"Allow": "GET, HEAD"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            log.debug("%s - %s", self.address_string(), format % args)

    return Handler


def make_gateway_server(profiles: dict[str, Config], account: Account, public_host: str, setup_code: str | None,
                        bind: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Refuse de démarrer tant que la configuration laisserait l'interface ouverte ou inutilisable."""
    public_host = (public_host or "").strip().lower()
    if not HOST_PATTERN.fullmatch(public_host):
        raise ConfigError(
            "l'interface derrière un proxy a besoin du nom de domaine sous lequel elle est servie "
            "(--public-host trade.exemple.org, ou TRADEAGENT_PUBLIC_HOST) : un nom complet, pas une adresse IP."
        )
    if not profiles:
        raise ConfigError("aucun profil à servir (--profiles hold,board)")
    try:
        has_account = account.exists()
    except AuthError as exc:
        raise ConfigError(str(exc)) from None
    if not has_account and (not setup_code or len(setup_code) < MIN_SETUP_CODE_CHARS):
        raise ConfigError(
            f"aucun compte n'existe encore : définis un code d'installation sur le serveur ({SETUP_CODE_ENV}, "
            f"{MIN_SETUP_CODE_CHARS} caractères au moins, dans le fichier d'environnement du conteneur). Il sera demandé "
            "une seule fois, pour créer le compte administrateur."
        )
    handler = make_gateway_handler(profiles, account, Throttle(), public_host, setup_code)
    try:
        server = ThreadingHTTPServer((bind, port), handler)
    except OSError as exc:
        raise ConfigError(f"impossible d'ouvrir le port {port} ({exc.strerror or exc}).") from exc
    server.daemon_threads = True
    return server
