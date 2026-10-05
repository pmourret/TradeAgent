import hashlib
import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from helpers import FakeClock, ScriptedAgent, default_cfg, make_engine
from tradeagent.config import ConfigError
from tradeagent.models import Decision
from tradeagent.storage import Storage
from tradeagent.web import STATIC_DIR, make_server

HTML_PAYLOAD = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'


@pytest.fixture
def served(tmp_path):
    """Un vrai serveur sur un port libre, devant une vraie base produite par le moteur."""
    path = tmp_path / "agent.db"
    cfg = default_cfg(database=str(path))
    storage = Storage(str(path))
    agent = ScriptedAgent([Decision("buy", "BTC/EUR", 20.0, HTML_PAYLOAD)])
    engine, _, clock, storage = make_engine(cfg, agent, clock=FakeClock(), storage=storage)
    for _ in range(3):
        engine.run_cycle()
        clock.advance(900)
    storage.close()

    server = make_server(cfg, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield {"port": port, "cfg": cfg, "db": path}
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def request(served, method, path, host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", served["port"], timeout=5)
    headers = {"Host": host if host is not None else f"127.0.0.1:{served['port']}"}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    headers = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return response.status, headers, data


# -- routes ------------------------------------------------------------------------------

def test_index_and_assets_are_served_with_strict_headers(served):
    for path, content_type in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")):
        status, headers, body = request(served, "GET", path)
        assert status == 200 and headers["content-type"].startswith(content_type) and body
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" not in csp
        assert "frame-ancestors 'none'" in csp


FONT_PATHS = ["/fonts/Inter-Regular.woff2", "/fonts/Inter-Medium.woff2", "/fonts/Inter-SemiBold.woff2"]


@pytest.mark.parametrize("path", FONT_PATHS)
def test_the_font_is_served_by_this_server_and_allowed_by_the_csp(served, path):
    # Aucune ressource externe : la police vient d'ici, et la CSP n'autorise qu'elle (font-src 'self').
    status, headers, body = request(served, "GET", path)
    assert status == 200 and headers["content-type"] == "font/woff2" and body[:4] == b"wOF2"
    assert headers["x-content-type-options"] == "nosniff"
    assert "font-src 'self'" in headers["content-security-policy"]
    assert path[1:] in CSS


@pytest.mark.parametrize("path", ["/fonts/", "/fonts/OFL.txt", "/fonts/Inter-Bold.woff2", "/fonts/../app.js",
                                  "/fonts/Inter-Regular.woff2/x", "/fonts"])
def test_only_the_three_font_files_are_served(served, path):
    assert request(served, "GET", path)[0] == 404


def test_the_local_page_declares_its_context_and_has_no_form(served):
    # La page locale est celle de l'application de bureau : ni session, ni fil d'Ariane, ni formulaire.
    body = request(served, "GET", "/")[2].decode("utf-8")
    assert '<body data-context="electron" data-profile="">' in body
    assert "{{" not in body and "<form" not in body and "<button" not in body and "/logout" not in body


def test_the_profile_name_is_escaped_in_the_page(tmp_path):
    from tradeagent.web import make_handler, profile_page
    assert make_handler(default_cfg(database=str(tmp_path / "x.db")), "board")
    page = profile_page("electron", '"><script>x</script>', "").decode("utf-8")
    assert "<script>x" not in page and "&quot;&gt;&lt;script&gt;" in page


def test_the_profile_name_shown_in_the_header_is_escaped_too(tmp_path):
    # Le nom vient aujourd'hui d'une liste fixe, mais le serveur ne doit pas compter dessus.
    server = make_server(default_cfg(database=str(tmp_path / "x.db")), "127.0.0.1", 0, profile='"><script>x</script>')
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        body = request({"port": server.server_address[1]}, "GET", "/")[2].decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert "<script>x" not in body and body.count("&quot;&gt;&lt;script&gt;x&lt;/script&gt;") == 2    # attribut et en-tête
    assert '<span class="profile-name">' in body

def test_snapshot_endpoint_returns_json(served):
    status, headers, body = request(served, "GET", "/api/snapshot")
    snap = json.loads(body)
    assert status == 200 and headers["content-type"].startswith("application/json")
    assert snap["has_data"] and snap["quote_currency"] == "EUR"


def test_localhost_host_header_is_accepted(served):
    assert request(served, "GET", "/api/snapshot", host=f"localhost:{served['port']}")[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1", "127.0.0.1:9", "192.168.1.20:8765", ""])
def test_foreign_host_headers_are_refused(served, host):
    # Protège contre le « DNS rebinding » : une page web ne doit pas pouvoir lire le bot via le navigateur.
    status, _, body = request(served, "GET", "/api/snapshot", host=host)
    assert status == 403 and b"has_data" not in body


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_everything_but_get_and_head_is_refused(served, method):
    status, headers, _ = request(served, method, "/api/snapshot", body=b"{}")
    assert status == 405 and headers["allow"] == "GET, HEAD"


def test_head_has_headers_but_no_body(served):
    status, headers, body = request(served, "HEAD", "/")
    assert status == 200 and body == b"" and int(headers["content-length"]) > 0


@pytest.mark.parametrize("path", ["/nope", "/../config.yaml", "/..%2fconfig.yaml", "/app.js/..", "/static/app.js",
                                  "/index.html", "//etc/passwd", "/api", "/api/snapshot/extra"])
def test_unknown_paths_are_404_and_nothing_is_read_from_the_request_path(served, path):
    assert request(served, "GET", path)[0] == 404


def test_query_string_is_ignored(served):
    assert request(served, "GET", "/api/snapshot?x=../../etc/passwd")[0] == 200


def test_serving_never_modifies_the_database(served):
    before = hashlib.sha256(served["db"].read_bytes()).hexdigest()
    for path in ("/", "/api/snapshot", "/api/snapshot", "/app.js"):
        request(served, "GET", path)
    request(served, "POST", "/api/snapshot", body=b"{}")
    assert hashlib.sha256(served["db"].read_bytes()).hexdigest() == before


def test_unreadable_database_gives_503(served):
    served["db"].write_bytes(b"corrompu" * 200)
    status, _, body = request(served, "GET", "/api/snapshot")
    assert status == 503 and "error" in json.loads(body)


def test_error_details_never_reach_the_browser(served, monkeypatch):
    # Les erreurs internes peuvent contenir un chemin de fichier : le navigateur n'en voit jamais le détail.
    from tradeagent.dashboard import DashboardError

    def boom(cfg, now=None):
        raise DashboardError("lecture impossible : /home/pierre/secret/agent.db verrouillée")

    monkeypatch.setattr("tradeagent.web.build_snapshot", boom)
    status, _, body = request(served, "GET", "/api/snapshot")
    assert status == 503
    assert b"/home/pierre" not in body and b"agent.db" not in body


def test_non_finite_values_give_a_clean_500_not_a_dropped_connection(served, monkeypatch):
    monkeypatch.setattr("tradeagent.web.build_snapshot", lambda cfg, now=None: {"money": {"equity": float("nan")}})
    status, _, body = request(served, "GET", "/api/snapshot")
    assert status == 500 and b"NaN" not in body


def test_the_llm_text_travels_as_data_not_markup(served):
    # Le texte du LLM est du JSON échappé ; c'est app.js qui doit l'afficher comme texte (voir plus bas).
    body = request(served, "GET", "/api/snapshot")[2]
    snap = json.loads(body)
    assert any(HTML_PAYLOAD in (row["reasoning"] or "") for row in snap["journal"])


# -- ne s'expose jamais au réseau ---------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "", "192.168.1.20", "example.com", "::"])
def test_server_refuses_to_listen_outside_loopback(host):
    with pytest.raises(ConfigError, match="boucle locale"):
        make_server(default_cfg(), host, 0)


# -- invariants du front (cohérents avec la CSP) -------------------------------------------------

def strip_js_comments(source: str) -> str:
    """Ôte les commentaires (qui citent justement les API interdites) pour n'analyser que le code."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))


JS = strip_js_comments((STATIC_DIR / "app.js").read_text())
HTML = (STATIC_DIR / "index.html").read_text()
CSS = (STATIC_DIR / "app.css").read_text()


def test_comment_stripper_removes_comments_but_keeps_code():
    code = strip_js_comments('/* innerHTML */\n// eval(x)\nconst a = "b"; // trailing\nel.textContent = a;')
    assert "innerHTML" not in code and "eval(" not in code and "el.textContent = a;" in code


@pytest.mark.parametrize("forbidden", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                                       "new Function", 'setAttribute("style"', "setAttribute('style'", "localStorage",
                                       "XMLHttpRequest", "WebSocket", "sendBeacon"])
def test_front_never_uses_dangerous_apis(forbidden):
    assert forbidden not in JS


def test_front_only_talks_to_its_own_server_and_never_acts_on_the_bot():
    fetches = re.findall(r"fetch\(([^)]*)\)", JS)
    assert len(fetches) == 1 and '"api/snapshot"' in fetches[0]
    assert "method:" not in fetches[0] and "POST" not in JS
    assert "<button" not in HTML and "<form" not in HTML and "<input" not in HTML


def test_html_is_compatible_with_the_csp():
    assert not re.search(r"\sstyle=", HTML)                         # pas de style en ligne
    assert not re.search(r"\son[a-z]+=", HTML)                      # pas de gestionnaire en ligne
    assert "<script>" not in HTML                                   # seul <script src> est autorisé
    assert not re.findall(r'(?:src|href)="https?://', HTML + CSS)   # aucune ressource externe
    assert "@import" not in CSS and "url(http" not in CSS
    assert set(re.findall(r"url\(([^)]*)\)", CSS)) == {f'"{path[1:]}"' for path in FONT_PATHS}   # rien d'autre que la police
    assert "{{context}}" in HTML and "{{nav}}" in HTML and "{{account}}" in HTML                # remplis par le serveur


def test_the_page_is_dark_only_and_keeps_a_calm_version_of_every_animation():
    assert "prefers-color-scheme" not in CSS
    reduced = CSS.split("@media (prefers-reduced-motion: reduce)")[1]
    assert "animation: none !important" in reduced and "transition: none !important" in reduced
    assert "Paper trading, argent fictif" in HTML


# -- port déjà pris ----------------------------------------------------------------------------------------------------

def test_port_already_in_use_gives_a_clear_message_not_a_traceback():
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        port = blocker.getsockname()[1]
        with pytest.raises(ConfigError, match=f"port {port}.*déjà lancée"):
            make_server(default_cfg(), "127.0.0.1", port)
    finally:
        blocker.close()


def test_the_local_snapshot_carries_the_reference_when_one_is_given(served, tmp_path):
    ref = default_cfg(database=str(served["db"]))          # une base qui a des données sert de référence
    server = make_server(served["cfg"], "127.0.0.1", 0, profile="board", reference=ref)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        snap = json.loads(request({"port": server.server_address[1]}, "GET", "/api/snapshot")[2])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert snap["reference"]["profile"] == "hold"
    assert "reference" not in json.loads(request(served, "GET", "/api/snapshot")[2])     # pas de référence donnée
