"""Le compte de l'interface distante : un seul administrateur, créé à la première visite.

Ce module ne sert qu'au serveur « derrière un proxy » (`gateway.py`). L'interface locale n'a pas de compte.

Ce qui est gardé, dans un fichier à part (jamais dans la base d'un bot, un journal, l'instantané ou le dépôt) :
l'identifiant, l'empreinte du mot de passe (scrypt, sel aléatoire), et la clé qui signe les sessions. Le mot de
passe lui-même n'est écrit nulle part.

- Création du compte : une seule fois, et seulement avec le code d'installation choisi par le propriétaire du
  serveur (variable d'environnement). Sans ce code, le premier visiteur venu deviendrait administrateur.
- Session : un jeton signé (HMAC-SHA256) qui porte sa date d'expiration. Rien n'est gardé côté serveur, sauf un
  compteur (`epoch`) : se déconnecter l'augmente, ce qui annule toutes les sessions d'un coup.
- Essais répétés : après `MAX_FAILURES` échecs en `LOCK_SECONDS`, tout est refusé jusqu'à la fin de la fenêtre.
  Le compteur est commun à tout le monde (derrière un proxy, l'adresse du client n'est pas fiable) : quelqu'un du
  réseau peut donc bloquer la connexion un quart d'heure, mais pas deviner le mot de passe à la chaîne.
  `Throttle.gate` fait passer les essais un par un : le contrôle, le calcul de l'empreinte et le décompte ne se
  chevauchent pas (sinon des requêtes simultanées passeraient toutes avant le premier décompte), et un seul calcul
  scrypt (32 Mio) tourne à la fois.

Changer `SCRYPT` rend le compte existant inutilisable (il faut le recréer) : `verify` calcule avec la constante.

Double authentification et codes de récupération : les champs `totp` et `recovery` sont réservés, rien n'est codé.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_CHARS = 16
MAX_PASSWORD_CHARS = 256
MIN_SETUP_CODE_CHARS = 12
USER_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,64}")
SESSION_SECONDS = 7 * 86_400
MAX_FAILURES = 5
LOCK_SECONDS = 900
SETUP_CODE_ENV = "TRADEAGENT_SETUP_CODE"


class AuthError(Exception):
    """Refus à montrer tel quel à la personne (le message ne contient jamais ce qu'elle a saisi)."""


class WrongSetupCode(AuthError):
    """Le seul refus de création qui compte comme un essai raté : les autres sont des fautes de saisie."""


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, maxmem=SCRYPT_MAXMEM, **SCRYPT)


class Throttle:
    """Le frein contre les essais répétés, en mémoire."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._failures: list[float] = []
        self._lock = threading.Lock()
        self.gate = threading.Lock()        # tenu pendant tout un essai : contrôle, empreinte, décompte

    def locked(self) -> bool:
        with self._lock:
            horizon = self._clock() - LOCK_SECONDS
            self._failures = [t for t in self._failures if t > horizon]
            return len(self._failures) >= MAX_FAILURES

    def failed(self) -> None:
        with self._lock:
            self._failures.append(self._clock())

    def succeeded(self) -> None:
        with self._lock:
            self._failures.clear()


class Account:
    """Le fichier du compte. Relu à chaque usage : il est minuscule, et un autre processus a pu le changer."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._write_lock = threading.Lock()

    # -- lecture -----------------------------------------------------------
    def _load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise AuthError("le fichier du compte est illisible : répare-le ou supprime-le sur le serveur") from exc
        wanted = {"user": str, "salt": str, "hash": str, "session_key": str, "epoch": int}
        if not isinstance(data, dict) or any(not isinstance(data.get(k), t) or isinstance(data.get(k), bool)
                                             for k, t in wanted.items()):
            raise AuthError("le fichier du compte est illisible : répare-le ou supprime-le sur le serveur")
        return data

    def exists(self) -> bool:
        return self._load() is not None

    # -- création ----------------------------------------------------------
    def create(self, user: str, password: str, again: str, code: str, expected_code: str | None) -> None:
        """Crée l'unique compte. Lève AuthError avec un message qui dit quoi corriger."""
        if not expected_code or len(expected_code) < MIN_SETUP_CODE_CHARS:
            raise AuthError(f"aucun code d'installation valable n'est défini sur le serveur ({SETUP_CODE_ENV}, "
                            f"{MIN_SETUP_CODE_CHARS} caractères au moins)")
        if not hmac.compare_digest(code.encode("utf-8"), expected_code.encode("utf-8")):
            raise WrongSetupCode("code d'installation incorrect")
        if not USER_PATTERN.fullmatch(user):
            raise AuthError("identifiant : 3 à 64 caractères parmi les lettres, les chiffres, le point, le tiret et le souligné")
        if not MIN_PASSWORD_CHARS <= len(password) <= MAX_PASSWORD_CHARS:
            raise AuthError(f"mot de passe : {MIN_PASSWORD_CHARS} caractères au moins ({MAX_PASSWORD_CHARS} au plus)")
        if password != again:
            raise AuthError("les deux mots de passe ne sont pas identiques")
        salt = secrets.token_bytes(16)
        record = {
            "version": 1, "user": user, "salt": salt.hex(), "hash": _hash(password, salt).hex(), "scrypt": SCRYPT,
            "session_key": secrets.token_hex(32), "epoch": 0, "created": self._clock(),
            "totp": None, "recovery": [],       # réservés : double authentification et codes de récupération
        }
        with self._write_lock:
            unwritable = AuthError("impossible d'écrire le fichier du compte : vérifie sur le serveur que le dossier du "
                                   "compte existe et appartient à l'utilisateur du conteneur")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise unwritable from None
            try:
                # Création exclusive : deux créations en même temps, une seule gagne.
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise AuthError("un compte existe déjà") from None
            except OSError:
                raise unwritable from None
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle)
            except OSError:
                self.path.unlink(missing_ok=True)       # pas de fichier à moitié écrit, qui bloquerait tout
                raise AuthError("impossible d'écrire le fichier du compte (disque plein ?)") from None

    # -- connexion ---------------------------------------------------------
    def verify(self, user: str, password: str) -> bool:
        """Vrai si l'identifiant et le mot de passe sont les bons. Le calcul coûte autant dans tous les cas."""
        data = self._load()
        if data is None or len(password) > MAX_PASSWORD_CHARS:
            return False
        try:
            salt, expected = bytes.fromhex(data["salt"]), bytes.fromhex(data["hash"])
        except ValueError:
            return False
        good_password = hmac.compare_digest(_hash(password, salt), expected)
        good_user = hmac.compare_digest(user.encode("utf-8"), data["user"].encode("utf-8"))
        return good_password and good_user

    # -- sessions ----------------------------------------------------------
    @staticmethod
    def _signature(data: dict[str, Any], expires: int, nonce: str) -> str:
        message = f"{data['user']}|{data['epoch']}|{expires}|{nonce}".encode("utf-8")
        return hmac.new(bytes.fromhex(data["session_key"]), message, hashlib.sha256).hexdigest()

    def open_session(self) -> str:
        data = self._load()
        if data is None:
            raise AuthError("aucun compte")
        expires, nonce = int(self._clock()) + SESSION_SECONDS, secrets.token_hex(8)
        return f"{expires}.{nonce}.{self._signature(data, expires, nonce)}"

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 3 or not (parts[0].isascii() and parts[0].isdigit()):     # « ² » est un chiffre, pas pour int()
            return False
        try:
            data = self._load()
        except AuthError:
            return False
        if data is None:
            return False
        expires = int(parts[0])
        try:
            expected = self._signature(data, expires, parts[1])
        except ValueError:
            return False
        return hmac.compare_digest(parts[2].encode("utf-8"), expected.encode("utf-8")) and expires > self._clock()

    def close_sessions(self) -> None:
        """Déconnexion : toutes les sessions ouvertes tombent."""
        with self._write_lock:
            data = self._load()
            if data is None:
                return
            data["epoch"] += 1
            temp = self.path.with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temp, self.path)
