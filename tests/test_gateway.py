"""L'interface derrière un proxy : nom de domaine imposé, HTTPS annoncé, session exigée partout, trois écritures."""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import threading

import pytest

from helpers import FakeClock, ScriptedAgent, default_cfg, make_engine
from tradeagent import auth, cli
from tradeagent.auth import MAX_FAILURES, Account
from tradeagent.config import ConfigError
from tradeagent.gateway import COOKIE, make_gateway_server
from tradeagent.models import Decision
from tradeagent.storage import Storage
from tradeagent.web import STATIC_DIR

HOST = "trade.exemple.org"
ORIGIN = f"https://{HOST}"
CODE = "code-d-installation"
USER, PASSWORD = "pierre", "un mot de passe très long"
FORM = "application/x-www-form-urlencoded"


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(auth, "SCRYPT", {"n": 2 ** 4, "r": 8, "p": 1})


def _serve(tmp_path, with_account=True, setup_code=CODE):
    path = tmp_path / "data" / "paper-board.db"
    path.parent.mkdir()
    cfg = default_cfg(database=str(path))
    storage = Storage(str(path))
    engine, _, clock, storage = make_engine(cfg, ScriptedAgent([Decision("buy", "BTC/EUR", 20.0, "test")]),
                                            clock=FakeClock(), storage=storage)
    engine.run_cycle()
    storage.close()
    account = Account(tmp_path / "auth" / "auth.json")
    if with_account:
        account.create(USER, PASSWORD, PASSWORD, CODE, CODE)
    profiles = {"hold": default_cfg(database=str(tmp_path / "data" / "paper-hold.db")), "board": cfg}
    server = make_gateway_server(profiles, account, HOST, setup_code, bind="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    return {"port": server.server_address[1], "db": path, "account": account, "server": server, "thread": thread}


def _stop(served):
    served["server"].shutdown()
    served["server"].server_close()
    served["thread"].join(timeout=5)


@pytest.fixture
def served(tmp_path):
    served = _serve(tmp_path)
    yield served
    _stop(served)


@pytest.fixture
def fresh(tmp_path):
    """Un serveur tout neuf : aucun compte encore."""
    served = _serve(tmp_path, with_account=False)
    yield served
    _stop(served)


def request(served, method, path, body=None, cookie=None, **overrides):
    headers = {"Host": HOST, "X-Forwarded-Proto": "https"}
    if method == "POST":
        headers.update({"Origin": ORIGIN, "Content-Type": FORM})
    if cookie:
        headers["Cookie"] = cookie
    for key, value in overrides.items():
        name = key.replace("_", "-")
        if value is None:
            headers.pop(name, None)
            headers.pop(name.title(), None)
            headers = {k: v for k, v in headers.items() if k.lower() != name.lower()}
        else:
            headers = {k: v for k, v in headers.items() if k.lower() != name.lower()}
            headers[name] = value
    conn = http.client.HTTPConnection("127.0.0.1", served["port"], timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    out = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return response.status, out, data.decode("utf-8")


def form(**fields):
    from urllib.parse import urlencode
    return urlencode(fields)


def login(served, user=USER, password=PASSWORD):
    status, headers, body = request(served, "POST", "/login", form(user=user, password=password))
    return status, headers, body


def session(served):
    status, headers, _ = login(served)
    assert status == 303
    return headers["set-cookie"].split(";")[0]


# -- démarrage ---------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["", "localhost", "127.0.0.1", "192.168.1.20", "trade", "trade.exemple.org:443",
                                  "https://trade.exemple.org", "trade..org", "-x.exemple.org", "trade.exemple.org/x"])
def test_le_serveur_refuse_de_demarrer_sans_un_vrai_nom_de_domaine(tmp_path, host):
    with pytest.raises(ConfigError, match="nom de domaine"):
        make_gateway_server({"board": default_cfg()}, Account(tmp_path / "auth.json"), host, CODE, bind="127.0.0.1", port=0)


@pytest.mark.parametrize("code", [None, "", "trop-court"])
def test_sans_compte_ni_code_d_installation_le_serveur_ne_demarre_pas(tmp_path, code):
    with pytest.raises(ConfigError, match="TRADEAGENT_SETUP_CODE"):
        make_gateway_server({"board": default_cfg()}, Account(tmp_path / "auth.json"), HOST, code, bind="127.0.0.1", port=0)


def test_avec_un_compte_le_code_d_installation_n_est_plus_necessaire_et_il_faut_au_moins_un_profil(tmp_path):
    account = Account(tmp_path / "auth.json")
    account.create(USER, PASSWORD, PASSWORD, CODE, CODE)
    make_gateway_server({"board": default_cfg()}, account, "Trade.Exemple.ORG", None, bind="127.0.0.1", port=0).server_close()
    with pytest.raises(ConfigError, match="aucun profil"):
        make_gateway_server({}, account, HOST, None, bind="127.0.0.1", port=0)


def test_un_fichier_de_compte_abime_empeche_de_demarrer_avec_un_message_clair(tmp_path):
    (tmp_path / "auth.json").write_text("abîmé", encoding="utf-8")
    with pytest.raises(ConfigError, match="illisible"):
        make_gateway_server({"board": default_cfg()}, Account(tmp_path / "auth.json"), HOST, CODE, bind="127.0.0.1", port=0)


def test_la_commande_serve_exige_le_nom_de_domaine_et_ne_prend_pas_le_code_en_argument(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\nstake: 50\n", encoding="utf-8")
    for name in ("TRADEAGENT_PUBLIC_HOST", "TRADEAGENT_SETUP_CODE", "TRADEAGENT_AUTH_FILE"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["serve"]) == 2 and "nom de domaine" in capsys.readouterr().err
    assert cli.main(["serve", "--public-host", HOST]) == 2 and "TRADEAGENT_SETUP_CODE" in capsys.readouterr().err
    assert cli.main(["serve", "--public-host", HOST, "--profiles", "live"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["serve", "--public-host", HOST, "--setup-code", CODE])
    assert not (tmp_path / "data").exists()                                     # un refus ne laisse rien derrière lui


# -- transport ---------------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", f"{HOST}:8080", f"x.{HOST}", "", f"{HOST}.evil.example"])
def test_un_autre_nom_d_hote_est_refuse(served, host):
    for path in ("/", "/login", "/healthz", "/p/board/api/snapshot"):
        status, _, body = request(served, "GET", path, Host=host)
        assert status == 403 and "hôte" in body
    assert request(served, "POST", "/login", form(user=USER, password=PASSWORD), Host=host)[0] == 403


@pytest.mark.parametrize("proto", [None, "http", "", "wss"])
def test_sans_https_annonce_par_le_proxy_rien_n_est_servi(served, proto):
    for path in ("/", "/login", "/p/board/"):
        assert request(served, "GET", path, X_Forwarded_Proto=proto)[0] == 403
    status, headers, _ = request(served, "POST", "/login", form(user=USER, password=PASSWORD), X_Forwarded_Proto=proto)
    assert status == 403 and "set-cookie" not in headers


def test_le_controle_de_sante_ne_dit_rien_et_ne_demande_pas_de_session(served):
    status, _, body = request(served, "GET", "/healthz", X_Forwarded_Proto=None)
    assert (status, body) == (200, "ok\n")


def test_les_en_tetes_de_securite_sont_la_avec_les_formulaires_limites_au_site(served):
    _, headers, _ = request(served, "GET", "/login")
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "form-action 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe" not in csp and "script-src 'self'" in csp
    assert headers["cache-control"] == "no-store" and headers["x-frame-options"] == "DENY"
    assert headers["strict-transport-security"].startswith("max-age=")
    assert "python" not in headers.get("server", "").lower()


# -- premier compte ----------------------------------------------------------------------------
def test_sans_compte_tout_mene_a_la_creation(fresh):
    for path in ("/", "/login", "/p/board/", "/p/board/api/snapshot", "/api/profiles", "/nope"):
        status, headers, body = request(fresh, "GET", path)
        assert (status, headers["location"]) == (303, "/setup") and "equity" not in body
    status, _, body = request(fresh, "GET", "/setup")
    assert status == 200 and 'action="/setup"' in body and "{{" not in body and 'minlength="16"' in body
    assert request(fresh, "POST", "/login", form(user=USER, password=PASSWORD))[1]["location"] == "/setup"


def test_la_creation_exige_le_code_puis_se_ferme(fresh):
    fields = dict(user=USER, password=PASSWORD, again=PASSWORD)
    status, _, body = request(fresh, "POST", "/setup", form(code="mauvais-code-là", **fields))
    assert status == 400 and "Code d&#x27;installation incorrect." in body and not fresh["account"].exists()
    status, _, body = request(fresh, "POST", "/setup", form(code=CODE, user=USER, password=PASSWORD, again="autre"))
    assert status == 400 and "pas identiques" in body and not fresh["account"].exists()
    status, headers, _ = request(fresh, "POST", "/setup", form(code=CODE, **fields))
    assert (status, headers["location"]) == (303, "/login") and "set-cookie" not in headers
    assert fresh["account"].verify(USER, PASSWORD)
    record = fresh["account"].path.read_text(encoding="utf-8")
    assert request(fresh, "GET", "/setup")[1]["location"] == "/login"            # fermée
    status, headers, _ = request(fresh, "POST", "/setup", form(code=CODE, user="intrus", password=PASSWORD, again=PASSWORD))
    assert (status, headers["location"]) == (303, "/login")
    assert fresh["account"].path.read_text(encoding="utf-8") == record           # le compte n'a pas bougé
    assert login(fresh)[0] == 303


def test_une_faute_de_saisie_avec_le_bon_code_ne_bloque_pas_la_creation(fresh):
    for _ in range(MAX_FAILURES + 1):
        status, _, _ = request(fresh, "POST", "/setup", form(code=CODE, user=USER, password=PASSWORD, again="autre"))
        assert status == 400
    assert request(fresh, "POST", "/setup", form(code=CODE, user=USER, password=PASSWORD, again=PASSWORD))[0] == 303


def test_la_creation_est_freinee_comme_la_connexion(fresh):
    for _ in range(MAX_FAILURES):
        request(fresh, "POST", "/setup", form(code="mauvais-code-là", user=USER, password=PASSWORD, again=PASSWORD))
    status, _, body = request(fresh, "POST", "/setup", form(code=CODE, user=USER, password=PASSWORD, again=PASSWORD))
    assert status == 429 and "Trop d&#x27;essais" in body and not fresh["account"].exists()


# -- connexion ---------------------------------------------------------------------------------
def test_la_connexion_ouvre_une_session_par_un_cookie_verrouille(served):
    status, headers, body = login(served)
    assert (status, headers["location"], body) == (303, "/", "")
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and COOKIE.startswith("__Host-")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=604800"):
        assert flag in cookie
    assert "Domain" not in cookie
    assert request(served, "GET", "/login", cookie=cookie.split(";")[0])[1]["location"] == "/"     # déjà connecté


@pytest.mark.parametrize("user,password", [(USER, "faux"), ("autre", PASSWORD), ("", ""), (USER, PASSWORD + "x")])
def test_une_mauvaise_connexion_n_ouvre_rien_et_ne_dit_pas_ce_qui_est_faux(served, user, password):
    status, headers, body = login(served, user, password)
    assert status == 401 and "set-cookie" not in headers
    assert "Identifiant ou mot de passe incorrect." in body and 'action="/login"' in body


def test_rien_de_ce_qui_est_saisi_ne_revient_dans_la_page_ni_dans_le_journal(served, caplog):
    caplog.set_level(logging.DEBUG)
    secret, name = "MotDePasseTrèsSecret!", "<script>alert(1)</script>"
    _, _, body = login(served, name, secret)
    assert secret not in body and "alert(1)" not in body and "script>" not in body.split("</head>")[1]
    login(served)
    assert secret not in caplog.text and PASSWORD not in caplog.text and "alert(1)" not in caplog.text
    assert "échec de connexion" in caplog.text


def test_apres_cinq_echecs_meme_le_bon_mot_de_passe_est_refuse(served):
    for _ in range(MAX_FAILURES):
        assert login(served, USER, "faux")[0] == 401
    status, headers, body = login(served)
    assert status == 429 and "set-cookie" not in headers and "Trop d&#x27;essais" in body


def test_des_essais_simultanes_ne_passent_pas_avant_le_premier_decompte(served):
    results = []
    threads = [threading.Thread(target=lambda: results.append(login(served, USER, "faux")[0])) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(results) == [401] * MAX_FAILURES + [429] * (20 - MAX_FAILURES)     # cinq essais réels, pas un de plus


def test_une_requete_incomplete_ne_garde_pas_le_serveur(served, monkeypatch):
    import socket
    from tradeagent import gateway
    assert gateway.SOCKET_TIMEOUT_SECONDS <= 30
    handler = served["server"].RequestHandlerClass
    monkeypatch.setattr(handler, "timeout", 0.3)
    sock = socket.create_connection(("127.0.0.1", served["port"]), timeout=5)
    sock.sendall(f"POST /login HTTP/1.1\r\nHost: {HOST}\r\nX-Forwarded-Proto: https\r\nOrigin: {ORIGIN}\r\n"
                 f"Content-Type: {FORM}\r\nContent-Length: 4000\r\n\r\nuser=x".encode())
    assert sock.recv(1024) == b""                                               # le serveur a coupé, sans attendre la suite
    sock.close()
    assert login(served)[0] == 303                                              # et il répond toujours


def test_une_reussite_efface_les_echecs_precedents(served):
    for _ in range(MAX_FAILURES - 1):
        login(served, USER, "faux")
    assert login(served)[0] == 303
    assert login(served, USER, "faux")[0] == 401 and login(served)[0] == 303


# -- ce que voit une session ouverte -------------------------------------------------------------
def test_une_session_ouverte_voit_les_profils_et_leurs_donnees(served):
    cookie = session(served)
    status, _, body = request(served, "GET", "/", cookie=cookie)
    assert status == 200 and 'href="/p/hold/"' in body and 'href="/p/board/"' in body and 'action="/logout"' in body
    status, _, body = request(served, "GET", "/api/profiles", cookie=cookie)
    assert json.loads(body) == [{"name": "hold", "path": "/p/hold/"}, {"name": "board", "path": "/p/board/"}]
    status, headers, body = request(served, "GET", "/p/board/", cookie=cookie)
    assert status == 200 and headers["content-type"].startswith("text/html") and 'src="app.js"' in body
    assert request(served, "GET", "/p/board/app.js", cookie=cookie)[1]["content-type"].startswith("text/javascript")
    assert request(served, "GET", "/p/board/app.css", cookie=cookie)[1]["content-type"].startswith("text/css")
    status, _, body = request(served, "GET", "/p/board/api/snapshot", cookie=cookie)
    snap = json.loads(body)
    assert status == 200 and snap["has_data"] and snap["agent"] == "scripted"
    status, _, body = request(served, "GET", "/p/hold/api/snapshot", cookie=cookie)
    assert status == 200 and json.loads(body)["has_data"] is False               # chaque profil lit sa propre base
    status, headers, body = request(served, "HEAD", "/p/board/api/snapshot", cookie=cookie)
    assert status == 200 and body == "" and int(headers["content-length"]) > 0


@pytest.mark.parametrize("path", ["/p/demo/", "/p/board", "/p/board/nope", "/p/board/../hold/", "/p/../config.yaml",
                                  "/p/board/index.html", "/p//", "/p/board/api/snapshot/x", "/app.js", "/api/snapshot",
                                  "/p/board/auth.json", "/auth.json", "/nope", "/x/board/", "/q/board/api/snapshot",
                                  "/board/", "//board/"])
def test_les_chemins_inconnus_sont_404_et_rien_n_est_lu_d_apres_la_requete(served, path):
    status, _, body = request(served, "GET", path, cookie=session(served))
    assert status == 404 and "introuvable" in body


def test_sans_session_les_pages_renvoient_a_la_connexion_et_les_donnees_repondent_401(served):
    for path in ("/", "/p/board/", "/p/board/app.js", "/p/demo/", "/nope", "/auth.json"):
        status, headers, body = request(served, "GET", path)
        assert (status, headers["location"], body) == (303, "/login", "")         # que le chemin existe ou non
    for path in ("/p/board/api/snapshot", "/api/profiles", "/p/demo/api/snapshot"):
        status, _, body = request(served, "GET", path)
        assert status == 401 and "equity" not in body and "hold" not in body
    for cookie in (f"{COOKIE}=1.2.3", f"{COOKIE}=", "autre=1", f"{COOKIE}=" + "9" * 300, "\x00;;;=="):
        assert request(served, "GET", "/p/board/api/snapshot", cookie=cookie)[0] == 401


def test_la_page_de_connexion_et_sa_feuille_de_style_sont_publiques(served):
    status, _, body = request(served, "GET", "/login")
    assert status == 200 and 'action="/login"' in body and "{{" not in body
    status, headers, _ = request(served, "GET", "/auth.css")
    assert status == 200 and headers["content-type"].startswith("text/css")


def test_se_deconnecter_annule_la_session(served):
    cookie = session(served)
    status, headers, _ = request(served, "POST", "/logout", "", cookie=cookie)
    assert (status, headers["location"]) == (303, "/login")
    assert "Max-Age=0" in headers["set-cookie"] and headers["set-cookie"].startswith(f"{COOKIE}=;")
    assert request(served, "GET", "/p/board/api/snapshot", cookie=cookie)[0] == 401      # l'ancien cookie ne vaut plus rien
    again = session(served)
    request(served, "POST", "/logout", "")                                       # sans session : ne déconnecte personne
    assert request(served, "GET", "/p/board/api/snapshot", cookie=again)[0] == 200


# -- les trois seules écritures ------------------------------------------------------------------
@pytest.mark.parametrize("origin", [None, "", "http://trade.exemple.org", "https://evil.example", "null",
                                    "https://trade.exemple.org.evil.example", "https://trade.exemple.org:8443"])
def test_une_ecriture_qui_ne_vient_pas_de_la_page_est_refusee(served, origin):
    status, headers, body = request(served, "POST", "/login", form(user=USER, password=PASSWORD), Origin=origin)
    assert status == 403 and "origine" in body and "set-cookie" not in headers
    cookie = session(served)
    assert request(served, "POST", "/logout", "", cookie=cookie, Origin=origin)[0] == 403
    assert request(served, "GET", "/p/board/api/snapshot", cookie=cookie)[0] == 200      # personne n'a été déconnecté


def test_une_requete_mal_formee_est_refusee(served):
    good = form(user=USER, password=PASSWORD)
    assert request(served, "POST", "/login", good, Content_Type="application/json")[0] == 400
    assert request(served, "POST", "/login", json.dumps({"user": USER, "password": PASSWORD}),
                   Content_Type="application/json")[0] == 400
    assert request(served, "POST", "/login", good + "&pad=" + "x" * 4096)[0] == 400         # trop gros, même bien formé
    assert request(served, "POST", "/login", good + "&pad=" + "x" * (4096 - len(good) - 5))[0] == 303      # pile à la limite
    assert request(served, "POST", "/login", "&".join(f"a{i}=1" for i in range(9)))[0] == 400     # trop de champs
    assert request(served, "POST", "/login", "pas un formulaire")[0] == 400
    assert request(served, "POST", "/login", "x" * 4096)[0] in (400, 401)       # à la limite : lu, puis refusé
    assert request(served, "POST", "/login", good + "&user=intrus")[0] == 303     # champ répété : le premier compte


@pytest.mark.parametrize("method,path", [("POST", "/"), ("POST", "/p/board/api/snapshot"), ("POST", "/api/profiles"),
                                         ("POST", "/p/board/"), ("PUT", "/login"), ("DELETE", "/p/board/"),
                                         ("PATCH", "/"), ("OPTIONS", "/login"), ("PUT", "/setup")])
def test_tout_le_reste_est_en_lecture_seule(served, method, path):
    status, headers, body = request(served, method, path, "x=1", cookie=session(served),
                                    Origin=ORIGIN, Content_Type=FORM)
    assert status == 405 and "lecture seule" in body and headers["allow"] == "GET, HEAD"


def test_servir_ne_modifie_jamais_la_base_d_un_bot(served):
    before = hashlib.sha256(served["db"].read_bytes()).hexdigest()
    cookie = session(served)
    for path in ("/", "/p/board/", "/p/board/api/snapshot", "/p/hold/api/snapshot"):
        request(served, "GET", path, cookie=cookie)
    request(served, "POST", "/logout", "", cookie=cookie)
    assert hashlib.sha256(served["db"].read_bytes()).hexdigest() == before
    assert sorted(p.name for p in served["db"].parent.iterdir()) == ["paper-board.db"]   # ni compte ni fichier en plus


def test_un_compte_abime_en_route_donne_une_reponse_propre(served):
    served["account"].path.write_text("abîmé", encoding="utf-8")
    status, _, body = request(served, "GET", "/")
    assert status == 503 and "illisible" in body
    assert request(served, "POST", "/login", form(user=USER, password=PASSWORD))[0] == 503


# -- les pages ---------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["login.html", "setup.html", "hub.html"])
def test_les_pages_du_compte_respectent_la_csp(name):
    page = (STATIC_DIR / name).read_text(encoding="utf-8")
    assert not re.search(r"\sstyle=", page) and not re.search(r"\son[a-z]+=", page)
    assert "<script" not in page and not re.findall(r'(?:src|href|action)="https?://', page)
    assert 'method="post"' in page and re.findall(r'action="([^"]+)"', page) in (["/login"], ["/setup"], ["/logout"])
    css = (STATIC_DIR / "auth.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css


def test_le_compose_ne_publie_aucun_port_et_l_interface_ne_peut_pas_ecrire_dans_les_bases():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert set(services) == {"hold", "board", "web"}
    for name, service in services.items():
        assert "ports" not in service and service.get("network_mode") != "host", name
    web = services["web"]
    data = [v for v in web["volumes"] if v.split(":")[1] == "/app/data"]
    assert len(data) == 1 and data[0].endswith(":ro")                           # les bases : lecture seule
    assert web["command"][0] == "serve" and web["command"][web["command"].index("--bind") + 1] == "0.0.0.0"
    assert "TRADEAGENT_SETUP_CODE" not in json.dumps(web.get("environment", {}))  # jamais écrit dans le compose
    assert "TRADEAGENT_SETUP_CODE=" not in (root / "Dockerfile").read_text(encoding="utf-8")
    for name in ("hold", "board"):
        assert "labels" not in services[name] and services[name]["networks"] == ["backend"]     # les bots : injoignables
    ignored = (root / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and ".env.*" in ignored


def test_par_defaut_serve_n_ecoute_que_sur_la_boucle_locale():
    parser = cli._parser()
    assert parser.parse_args(["serve"]).bind == "127.0.0.1"
    import inspect
    assert inspect.signature(make_gateway_server).parameters["bind"].default == "127.0.0.1"


def test_la_page_du_bot_reste_sans_formulaire_et_se_sert_en_relatif():
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<form" not in page and "<button" not in page and "<input" not in page
    assert 'href="app.css"' in page and 'src="app.js"' in page
