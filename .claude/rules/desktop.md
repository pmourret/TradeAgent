---
paths:
  - "desktop/**"
---

# Application de bureau (Electron)

Coquille mince autour de l'interface web locale. `web.py` reste la seule interface ; elle doit toujours marcher dans un navigateur.

## Règles de sécurité (invariant 10)

- La page d'un profil tourne en bac à sable, sans Node, **sans preload** : elle n'a aucun moyen de parler au processus principal.
- Démarrer et arrêter un bot : menu natif et zone de notification uniquement. **N'ajoute aucun message IPC, aucun bouton dans une page, aucune route HTTP qui agisse sur un bot.** `test/readonly.test.js` vérifie que les seuls messages sont `select-tab`, `shell-ready` et `tabs`.
- Le superviseur démarre et arrête, rien d'autre : jamais `reset`, `resume`, `--agent`, `--feed`, `live`.
- Dans les pages (`shell.js`) : `textContent` et classes CSS seulement ; ni `innerHTML`, ni style en ligne, ni ressource externe.
- Les profils (noms, ports) se lisent dans `profiles.py` via Python, jamais recopiés côté Node.
- Les notifications (`lib/notifier.js`) sont sortantes uniquement : elles lisent `/api/snapshot` en GET sur la boucle locale, notifient une transition, et un clic ne fait qu'ouvrir la fenêtre. Jamais le texte de l'agent dedans : la raison d'une mort (écrite par le kill switch seul) est reprise, celle d'un `halted` non (elle cite la dernière erreur, donc parfois un bout de réponse du LLM). Le conseil d'un agent à l'arrêt (`advice` de l'instantané) est repris : il est fabriqué par le code à partir de nombres. Un texte de notification n'affirme que ce que l'instantané prouve (pas de « positions liquidées »).
- Fermer la fenêtre range l'application et les bots continuent ; seul « Quitter » les arrête, par `stop` sur leur entrée standard (`run --stop-on-stdin`). L'arrêt sec n'est qu'un dernier recours après délai.

## Organisation

- `main.js` : tout ce qui dépend d'Electron (fenêtre, onglets, menus, zone de notification, sortie).
- `lib/setup.js` : première installation de la version portable (trouver Python, créer le venv, installer la roue livrée). `build-payload.js` prépare `payload/` (roue, `constraints.txt`, modèles de config). Ne jamais livrer `.env` ni `data/` ; ne jamais écraser `config.yaml` ni `.env` ; ne jamais télécharger Python.
- `lib/backend.js`, `lib/supervisor.js`, `lib/notifier.js`, `lib/setup.js` : logique sans Electron, testée par `node --test`. Toute nouvelle logique testable va dans `lib/`, pas dans `main.js`.
- Tests Node hors réseau ; les faux bots sont de petits scripts Node, jamais le vrai Python.

## Pièges

- `ELECTRON_RUN_AS_NODE=1` (hérité des terminaux lancés par VSCode) fait démarrer Electron comme un simple Node, sans fenêtre (`app` vaut `undefined`) : passer par `npm start` (`start.js` retire la variable). Sous PowerShell, `npm start -- --profile=demo` perd l'argument : utiliser `node start.js --profile=demo`.
- Vérifier l'application sans la regarder : `TRADEAGENT_DESKTOP_SMOKE=<dossier>` fait capturer la barre d'onglets et la page active (`shell.png`, `view.png`, `state.json`), puis quitter. Avec `TRADEAGENT_DESKTOP_SMOKE_BOT=demo` en plus : démarre ce bot, range la fenêtre, l'arrête et écrit son état. Délègue cette vérification au sous-agent `verif-desktop`.
- electron-builder est épinglé à 26.0.12 : les versions suivantes demandent Node ≥ 22.12 (`ERR_REQUIRE_ESM` sous 22.11). `signAndEditExecutable: false` évite le téléchargement de winCodeSign, qui échoue sans droit de créer des liens symboliques ; conséquence : icône et métadonnées d'Electron sur l'exécutable.
- Version portable : `npm run pack`, puis copier `dist/win-unpacked` ailleurs et lancer `tradeagent.exe` avec `TRADEAGENT_DESKTOP_SMOKE` pour vérifier un premier lancement (réseau nécessaire). Dans un heredoc de Git Bash, `\\` devient `\` : écrire les scripts avec l'outil d'écriture.
- `node --test test/` échoue sous Node 22 (le dossier est pris pour un module) : `node --test` tout court.
- Les boîtes de dialogue, le menu de la zone de notification et les notifications ne sont vus par aucun test : dis-le quand tu y touches, Pierre doit les regarder.
- Jamais lancée sous Linux ni macOS.
