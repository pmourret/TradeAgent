"""Capture l'interface à la largeur de chaque capture de référence. OUTIL DE DÉVELOPPEMENT.

À lancer avec un Python qui a Playwright (il n'est volontairement pas dans le venv du projet) :

    D:\\SDKs\\Pyhton310-6\\python.exe tools/frontcheck/capture.py --out <dossier> [numéros...]

Il démarre `preview.py` sur la boucle locale, ouvre chaque écran dans Chromium sans interface (fuseau Europe/Paris,
langue fr-FR, mouvement réduit, horloge figée à l'heure de l'instantané), et écrit un PNG par ligne du tableau
« Captures de référence » du handoff, sous le même nom.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
NOW = 1791218241            # lundi 5 octobre 2026, 18:37:21 à Paris : `generated_at` des instantanés
PHONE = (390, 2)            # largeur, densité

# numéro, nom, chemin, largeur, densité, scénario
SHOTS = [
    (1, "01-profil-bureau", "/web/vie-normal/", 1280, 1, ""),
    (2, "02-profil-tablette", "/web/vie-normal/", 834, 1, ""),
    (3, "03-profil-telephone", "/web/vie-normal/", *PHONE, ""),
    (4, "04-profil-bureau-decision-arrive", "/web/vie-normal/", 1280, 1, "arrive"),
    (5, "05-profil-telephone-decision-arrive", "/web/vie-normal/", *PHONE, "arrive"),
    (6, "06-palier-prudent-bureau", "/web/vie-prudent/", 1280, 1, ""),
    (7, "07-palier-defensif-bureau", "/web/vie-defensif/", 1280, 1, ""),
    (8, "08-palier-defensif-telephone", "/web/vie-defensif/", *PHONE, ""),
    (9, "09-etat-mort-bureau", "/web/mort/", 1280, 1, ""),
    (10, "10-etat-mort-telephone", "/web/mort/", *PHONE, ""),
    (11, "11-etat-arrete-bureau", "/web/arrete/", 1280, 1, ""),
    (12, "12-etat-arrete-telephone", "/web/arrete/", *PHONE, ""),
    (13, "13-etat-muet-bureau", "/web/muet/", 1280, 1, ""),
    (14, "14-etat-muet-telephone", "/web/muet/", *PHONE, ""),
    (15, "15-etat-horsligne-bureau", "/web/vie-normal/", 1280, 1, "down"),
    (16, "16-etat-horsligne-telephone", "/web/vie-normal/", *PHONE, "down"),
    (17, "17-etat-vide-bureau", "/web/vide/", 1280, 1, ""),
    (18, "18-etat-vide-telephone", "/web/vide/", *PHONE, ""),
    (19, "19-electron-480", "/electron/vie-normal/", 480, 1, ""),
    (20, "20-electron-720", "/electron/vie-prudent/", 720, 1, ""),
    (21, "21-electron-1024", "/electron/vie-normal/", 1024, 1, ""),
    (22, "22-electron-1600", "/electron/vie-defensif/", 1600, 1, ""),
    (23, "23-accueil-bureau", "/hub", 1280, 1, ""),
    (24, "24-accueil-telephone", "/hub", *PHONE, ""),
    (25, "25-connexion-bureau", "/login", 1280, 1, ""),
    (26, "26-creation-compte-bureau", "/setup", 1280, 1, ""),
    (27, "27-connexion-telephone-erreur", "/login-erreur", *PHONE, ""),
    (28, "28-creation-compte-telephone", "/setup", *PHONE, ""),
]


def control(query: str) -> None:
    urllib.request.urlopen(f"{BASE}/__set?{query}", timeout=5).read()


def shoot(browser, shot, out: Path, motion: str) -> None:
    number, name, path, width, scale, scenario = shot
    control("snapshot=&down=0")
    context = browser.new_context(viewport={"width": width, "height": 400}, device_scale_factor=scale,
                                  locale="fr-FR", timezone_id="Europe/Paris", reduced_motion=motion)
    page = context.new_page()
    now = NOW + (159 if "muet" in path else 0)       # l'instantané « muet » est daté de 18:40
    page.clock.set_fixed_time(now)                    # en secondes
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    dynamic = path.startswith(("/web/", "/electron/"))
    if scenario == "arrive":
        control("snapshot=arrive")
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_selector(".order-received")
    if scenario == "down":
        control("down=1")
        page.clock.set_fixed_time(now + 130)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_selector(".page.is-frozen")
    elif dynamic:
        page.clock.set_fixed_time(now + 6)      # « Actualisé il y a 6 s », comme sur les captures
    if dynamic:
        page.wait_for_timeout(1300)                       # le temps d'un battement de l'horloge de la page
    page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--motion", default="reduce", choices=["reduce", "no-preference"])
    parser.add_argument("numbers", nargs="*", type=int)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    venv = ROOT / ".venv" / "Scripts" / "python.exe"      # l'aperçu importe tradeagent : il lui faut le venv du projet
    server = subprocess.Popen([str(venv) if venv.exists() else sys.executable, str(HERE / "preview.py"), "--port", str(PORT)], cwd=str(ROOT))
    try:
        for _ in range(50):
            try:
                control("down=0")
                break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for shot in SHOTS:
                if not args.numbers or shot[0] in args.numbers:
                    shoot(browser, shot, out, args.motion)
                    print("capture", shot[1])
            browser.close()
    finally:
        server.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
