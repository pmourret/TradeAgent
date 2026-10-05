"""Le compte de l'interface distante : création unique, empreinte, sessions signées, frein aux essais."""
from __future__ import annotations

import json
import os

import pytest

from tradeagent import auth
from tradeagent.auth import LOCK_SECONDS, MAX_FAILURES, SESSION_SECONDS, Account, AuthError, Throttle

CODE = "code-d-installation"
PASSWORD = "un mot de passe très long"


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    """Le vrai coût de scrypt (un dixième de seconde par essai) n'est vérifié que par un test, plus bas."""
    monkeypatch.setattr(auth, "SCRYPT", {"n": 2 ** 4, "r": 8, "p": 1})


class Clock:
    def __init__(self, now=1_760_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def account(tmp_path, clock=None):
    return Account(tmp_path / "auth" / "auth.json", clock or Clock())


def created(tmp_path, clock=None):
    acc = account(tmp_path, clock)
    acc.create("pierre", PASSWORD, PASSWORD, CODE, CODE)
    return acc


# -- création ------------------------------------------------------------------------------
def test_le_compte_se_cree_une_seule_fois_et_le_mot_de_passe_n_est_ecrit_nulle_part(tmp_path):
    acc = account(tmp_path)
    assert not acc.exists()
    acc.create("pierre", PASSWORD, PASSWORD, CODE, CODE)
    assert acc.exists()
    record = json.loads(acc.path.read_text(encoding="utf-8"))
    flat = json.dumps(record, ensure_ascii=False)                               # « très » serait échappé sinon
    assert PASSWORD not in flat and CODE not in flat
    assert set(record) == {"version", "user", "salt", "hash", "scrypt", "session_key", "epoch", "created", "totp", "recovery"}
    assert record["user"] == "pierre" and len(record["hash"]) == 64 and len(record["salt"]) == 32
    assert len(record["session_key"]) == 64 and record["epoch"] == 0
    assert record["totp"] is None and record["recovery"] == []                  # réservés, rien de codé
    with pytest.raises(AuthError, match="existe déjà"):
        acc.create("autre", PASSWORD, PASSWORD, CODE, CODE)
    assert json.loads(acc.path.read_text(encoding="utf-8")) == record           # le premier compte n'a pas bougé


def test_deux_comptes_n_ont_ni_le_meme_sel_ni_la_meme_cle(tmp_path):
    a = json.loads(created(tmp_path / "a").path.read_text(encoding="utf-8"))
    b = json.loads(created(tmp_path / "b").path.read_text(encoding="utf-8"))
    assert a["salt"] != b["salt"] and a["hash"] != b["hash"] and a["session_key"] != b["session_key"]


@pytest.mark.skipif(os.name == "nt", reason="droits de fichier POSIX")
def test_le_fichier_du_compte_n_est_lisible_que_par_son_proprietaire(tmp_path):
    assert created(tmp_path).path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("expected", [None, "", "trop-court"])
def test_sans_code_d_installation_valable_sur_le_serveur_rien_ne_se_cree(tmp_path, expected):
    acc = account(tmp_path)
    with pytest.raises(AuthError, match="TRADEAGENT_SETUP_CODE"):
        acc.create("pierre", PASSWORD, PASSWORD, expected or "", expected)
    assert not acc.exists()


@pytest.mark.parametrize("user,password,again,code,message", [
    ("pierre", PASSWORD, PASSWORD, "mauvais-code-là", "code d'installation incorrect"),
    ("pierre", PASSWORD, PASSWORD, "", "code d'installation incorrect"),
    ("pi", PASSWORD, PASSWORD, CODE, "identifiant"),
    ("pierre mourret", PASSWORD, PASSWORD, CODE, "identifiant"),
    ("<script>", PASSWORD, PASSWORD, CODE, "identifiant"),
    ("p" * 65, PASSWORD, PASSWORD, CODE, "identifiant"),
    ("pierre", "x" * 15, "x" * 15, CODE, "16 caractères"),
    ("pierre", "x" * 257, "x" * 257, CODE, "16 caractères"),
    ("pierre", PASSWORD, PASSWORD + "!", CODE, "pas identiques"),
])
def test_une_creation_refusee_dit_quoi_corriger_et_ne_cree_rien(tmp_path, user, password, again, code, message):
    acc = account(tmp_path)
    with pytest.raises(AuthError, match=message) as error:
        acc.create(user, password, again, code, CODE)
    assert not acc.exists()
    assert password not in str(error.value) and (not code or code not in str(error.value))


def test_seul_un_mauvais_code_est_un_essai_rate(tmp_path):
    acc = account(tmp_path)
    with pytest.raises(auth.WrongSetupCode):
        acc.create("pierre", PASSWORD, PASSWORD, "mauvais-code-là", CODE)
    with pytest.raises(AuthError) as typo:
        acc.create("pierre", PASSWORD, "autre", CODE, CODE)
    assert not isinstance(typo.value, auth.WrongSetupCode)


def test_un_dossier_inaccessible_donne_un_message_qui_dit_quoi_faire(tmp_path):
    blocker = tmp_path / "auth"
    blocker.write_text("un fichier là où il faudrait un dossier", encoding="utf-8")
    acc = Account(blocker / "auth.json")
    with pytest.raises(AuthError, match="vérifie sur le serveur"):
        acc.create("pierre", PASSWORD, PASSWORD, CODE, CODE)


def test_les_bornes_du_mot_de_passe_et_de_l_identifiant_sont_incluses(tmp_path):
    account(tmp_path / "a").create("abc", "x" * 16, "x" * 16, CODE, CODE)
    account(tmp_path / "b").create("a" * 64, "x" * 256, "x" * 256, CODE, CODE)
    account(tmp_path / "c").create("p.m-_9", PASSWORD, PASSWORD, "x" * 12, "x" * 12)


# -- connexion -----------------------------------------------------------------------------
def test_seuls_le_bon_identifiant_et_le_bon_mot_de_passe_ouvrent(tmp_path):
    acc = created(tmp_path)
    assert acc.verify("pierre", PASSWORD)
    assert not acc.verify("pierre", PASSWORD + " ")
    assert not acc.verify("pierre", "")
    assert not acc.verify("Pierre", PASSWORD)
    assert not acc.verify("", PASSWORD)
    calls = []
    real = auth._hash
    auth._hash = lambda password, salt: calls.append(len(password)) or real(password, salt)
    try:
        assert not acc.verify("pierre", "x" * 257) and calls == []              # démesuré : refusé sans calcul
        assert not acc.verify("pierre", "x" * 256) and calls == [256]
    finally:
        auth._hash = real
    assert not account(tmp_path / "vide").verify("pierre", PASSWORD)            # aucun compte


def test_le_cout_reel_de_scrypt_est_celui_annonce(tmp_path, monkeypatch):
    monkeypatch.undo()
    assert auth.SCRYPT == {"n": 2 ** 15, "r": 8, "p": 1}
    acc = created(tmp_path)
    assert json.loads(acc.path.read_text(encoding="utf-8"))["scrypt"] == auth.SCRYPT
    assert acc.verify("pierre", PASSWORD) and not acc.verify("pierre", "faux")


# -- sessions ------------------------------------------------------------------------------
def test_une_session_vaut_jusqu_a_son_expiration(tmp_path):
    clock = Clock()
    acc = created(tmp_path, clock)
    token = acc.open_session()
    assert acc.session_valid(token)
    clock.now += SESSION_SECONDS - 1
    assert acc.session_valid(token)
    clock.now += 1
    assert not acc.session_valid(token)


def test_une_session_trafiquee_ou_mal_formee_est_refusee(tmp_path):
    clock = Clock()
    acc = created(tmp_path, clock)
    expires, nonce, signature = acc.open_session().split(".")
    later = str(int(expires) + 86_400)
    for forged in (f"{later}.{nonce}.{signature}", f"{expires}.{nonce}x.{signature}", f"{expires}.{nonce}.{'0' * 64}",
                   f"{expires}.{nonce}", f"{expires}.{nonce}.{signature}.x", f"x.{nonce}.{signature}", "", None,
                   "a.b.c", "9" * 300, f"-1.{nonce}.{signature}", f"²{expires}.{nonce}.{signature}", "².a.b",
                   f"{expires}.{nonce}.é"):
        assert not acc.session_valid(forged), forged
    other = created(tmp_path / "autre", clock)
    assert not acc.session_valid(other.open_session())                          # signée par une autre clé


def test_deux_sessions_ne_se_ressemblent_pas(tmp_path):
    acc = created(tmp_path)
    assert acc.open_session() != acc.open_session()


def test_se_deconnecter_annule_toutes_les_sessions(tmp_path):
    acc = created(tmp_path)
    first, second = acc.open_session(), acc.open_session()
    acc.close_sessions()
    assert not acc.session_valid(first) and not acc.session_valid(second)
    assert acc.session_valid(acc.open_session()) and acc.verify("pierre", PASSWORD)     # le compte, lui, est intact
    assert not acc.path.with_suffix(".tmp").exists()
    account(tmp_path / "vide").close_sessions()                                 # sans compte : rien, pas d'erreur


def test_pas_de_session_sans_compte(tmp_path):
    with pytest.raises(AuthError):
        account(tmp_path).open_session()
    assert not account(tmp_path).session_valid("1.2.3")


@pytest.mark.parametrize("content", ["pas du json", "[]", "{}", '{"user": "p"}',
                                     '{"user": "p", "salt": "00", "hash": "00", "session_key": "00", "epoch": true}',
                                     '{"user": 3, "salt": "00", "hash": "00", "session_key": "00", "epoch": 0}'])
def test_un_fichier_de_compte_abime_est_signale_et_n_ouvre_rien(tmp_path, content):
    acc = account(tmp_path)
    acc.path.parent.mkdir(parents=True)
    acc.path.write_text(content, encoding="utf-8")
    with pytest.raises(AuthError, match="illisible"):
        acc.exists()
    assert not acc.session_valid("1.2.3")


def test_un_sel_ou_une_cle_illisibles_n_ouvrent_rien(tmp_path):
    acc = created(tmp_path)
    token = acc.open_session()
    record = json.loads(acc.path.read_text(encoding="utf-8"))
    acc.path.write_text(json.dumps({**record, "salt": "zz"}), encoding="utf-8")
    assert not acc.verify("pierre", PASSWORD)
    acc.path.write_text(json.dumps({**record, "session_key": "zz"}), encoding="utf-8")
    assert not acc.session_valid(token)


# -- frein -----------------------------------------------------------------------------------
def test_apres_cinq_echecs_tout_est_bloque_un_quart_d_heure():
    clock = Clock()
    throttle = Throttle(clock)
    for _ in range(MAX_FAILURES - 1):
        throttle.failed()
    assert not throttle.locked()
    throttle.failed()
    assert throttle.locked()
    clock.now += LOCK_SECONDS - 1
    assert throttle.locked()
    clock.now += 1
    assert not throttle.locked()


def test_une_reussite_remet_le_compteur_a_zero():
    throttle = Throttle(Clock())
    for _ in range(MAX_FAILURES - 1):
        throttle.failed()
    throttle.succeeded()
    throttle.failed()
    assert not throttle.locked()
