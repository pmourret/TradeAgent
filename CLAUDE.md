# CLAUDE.md — tradeagent

Lis ce fichier en entier avant de toucher au code. Le `README.md` est la doc utilisateur ; ce fichier est la doc de travail : à quoi sert le projet, ce qu'il ne faut jamais casser, où on en est, où on va.

## 1. Le projet en 30 secondes

Un agent de trading crypto piloté par un LLM. **L'IA propose, le code dispose** : l'agent ne parle jamais à l'exchange ; chaque décision traverse des garde-fous codés en dur, et un kill switch indépendant le « tue » quand la mise est perdue. L'agent paie aussi son « loyer » : chaque appel au LLM coûte de l'argent réel, compté dans son résultat net (`résultat net = equity − mise − coûts API de la vie`).

- **But de Pierre** : voir ce que donne une petite mise (50 € + coût de l'API) confiée à une IA, avec un objectif de revenu passif. Il est développeur confirmé, débutant en trading crypto. Aucun résultat n'est garanti, et rien n'indique qu'un LLM batte le marché une fois frais **et** API payés : le projet est une expérience, pas un produit.
- **Où ça tourne** : le PC perso de Pierre, en Python (≥ 3.10). IA via API Anthropic d'abord (Haiku 4.5 par défaut), en local plus tard.
- **Inspiration** : [Conway-Research/automaton](https://github.com/Conway-Research/automaton) (agent autonome qui doit créer de la valeur pour payer son propre calcul, sinon il meurt ; paliers de survie ; règles immuables). **Gardé** : mort économique, loyer d'inférence, paliers de risque, règles hors de portée de l'agent. **Écarté volontairement** : auto-modification du code, réplication, agent maître de son portefeuille.
- **Mode actuel : paper trading uniquement** (argent fictif). Le mode réel **n'existe pas** (voir §2 et §10).

## 2. Où on en est (état réel, à tenir à jour)

| État | Quoi |
|---|---|
| Fait, testé hors réseau (~455 tests) | Moteur, garde-fous, paliers de risque, kill switch, exchange papier, budget d'inférence, agent LLM (vrai SDK + transport HTTP simulé), profils, lanceur `up`, verrou anti-double-bot, interface web (vérifiée dans Chromium), scripts `.sh` (Linux) |
| Vérifié sur le PC de Pierre (Windows 11, Python 3.12, 2026-10-04) | Suite au vert : 437 passent, 17 ignorés (tests à signaux POSIX du lanceur, tests des `.sh` faute de `sh`). Donc **sous Windows, l'arrêt propre du lanceur et les scripts ne sont couverts par aucun test** |
| Fait, **jamais exécuté pour de vrai** | `CcxtPriceFeed` (prix réels : le réseau de l'environnement de dev bloque Binance), vrai appel à l'API Anthropic, scripts `.bat` (jamais lancés sous Windows), interface sur Firefox/Safari. Aucun bot n'a encore tourné sur le PC de Pierre (pas de `data/`) |
| Fait, vérifié sous Windows seulement | Application de bureau, phase visionneuse (F1) : 7 tests Node + lancement réel avec capture d'écran. Jamais lancée sous Linux/macOS ; le verrouillage des pages (navigation, requêtes externes, permissions) est codé mais **pas testé automatiquement** |
| **Pas fait** | Suite de l'application de bureau (F2 superviseur, F3 notifications, F4 portable), mode réel (adaptateur d'exchange authentifié + quarantaine), backtest/replay, notifications, export CSV, service/daemon, CI, historique des vies. Dépôt git initialisé (branche `main`), **aucun commit** pour l'instant |

Premier réflexe si quelque chose « ne marche pas » : `scripts/paper.sh hold --max-cycles 1` (prix réels, gratuit). Si ça échoue, c'est le flux ccxt, jamais testé en vrai.

## 3. Invariants — ne jamais casser, ne jamais contourner

Ce sont des propriétés de sécurité, protégées par des tests. Si une demande semble en exiger la violation, **arrête-toi et demande à Pierre**.

1. **L'agent n'agit pas.** `Agent.decide(MarketView) -> Decision` reçoit un état en lecture seule. Il n'a accès ni à l'exchange, ni aux clés, ni à la config des garde-fous. Tout ordre passe par `Guardrails.check` avant `Exchange.market_order`.
2. **Le kill switch est indépendant de l'agent.** Évalué à chaque cycle *avant* l'appel à l'agent, même si l'agent est en pause. `dead` est définitif jusqu'à `tradeagent reset --yes` ; `halted` (trop d'erreurs d'affilée, sans liquidation) ne repart que par `resume`. L'état est en base : un redémarrage ne ressuscite rien.
3. **La liquidation à la mort est une action du moteur**, qui contourne volontairement les garde-fous et n'est jamais exposée à l'agent.
4. **Sortir d'une position est toujours possible.** Perte journalière, plafond d'achats et palier défensif ne bloquent que les *achats*.
5. **Les garde-fous réduisent avant de refuser**, et journalisent la raison (« réduit de 14.40 à 10.00 EUR par max_order_pct »).
6. **Config stricte.** Clé inconnue ou valeur incohérente = refus au démarrage (une faute de frappe ne doit jamais désactiver un garde-fou). `mode: live` est refusé (`config.py`) tant que la quarantaine et l'adaptateur réel n'existent pas.
7. **Budget d'inférence appliqué en code** : chaque appel est valorisé et écrit dans `llm_calls` ; `llm_last_call` est écrit *avant* l'appel (une panne ne déclenche jamais une rafale d'appels payants) ; plafonds par jour (UTC) et au total. Une pause (`Decision.skipped`) n'est ni un hold ni une erreur : elle ne remet pas à zéro le compteur d'erreurs.
8. **Une vie = une mise.** Changer `stake` sans `reset` est refusé. `reset` efface l'état de la vie (`LIFE_KEYS` dans `app.py`), garde le journal et le suivi des coûts API.
9. **Aucun secret** dans un prompt, un log, la base, le snapshot web ou un message d'erreur. Aucune fonction de retrait ou de transfert de fonds dans le code, jamais. Clés d'exchange futures : sous-compte dédié, **sans droit de retrait**.
10. **Interface web en lecture seule**, boucle locale uniquement (pas d'option pour l'ouvrir au réseau), base ouverte en `mode=ro`, GET/HEAD seulement, en-tête `Host` vérifié, CSP stricte, texte de l'agent affiché via `textContent`. Pas de bouton qui agit sur le bot. Ce sont des tests (`test_web.py`), pas des conventions. **L'application de bureau (axe F) ne change rien à cela** : elle affiche cette même page dans une fenêtre verrouillée ; démarrer ou arrêter un bot se fait par le menu natif et la zone de notification (processus principal d'Electron, qui lance les mêmes commandes `tradeagent run` qu'à la main), jamais depuis la page ni par une route HTTP. `resume` et `reset` restent en ligne de commande.
11. **Un seul `run` par base** (verrou fichier de l'OS, `lock.py`) ; chaque profil a sa propre base.
12. **Les tests n'accèdent jamais au réseau ni à l'API payante.**

## 4. Architecture

```
prix + bougies → agent → garde-fous → exchange → journal      (kill switch qui surveille à chaque cycle)
```

Un cycle (`engine.py::Engine.run_cycle`) : kill switch actif ? → photo du portefeuille (erreur = erreur de cycle) → jour UTC, plus-haut, equity enregistrée → seuils de mort (liquidation) → palier de risque (`normal` / `cautious` ≥ 15 % de drawdown / `defensive` ≥ 25 %) → `MarketView` → `agent.decide` (toute exception = ne rien faire + erreur comptée) → `skipped` / `hold` / garde-fous → `market_order` → journal. Détails et valeurs : `README.md` et `config.yaml` (commenté).

| Module (`src/tradeagent/`) | Rôle |
|---|---|
| `models.py` | `Quote`, `Candle`, `Fill`, `OrderRequest`, `Decision` (buy/sell/hold, `from_json` strict, `Decision.skip`) |
| `config.py` | Chargement strict de `config.yaml` → dataclasses ; refuse clés inconnues, incohérences et `mode != paper` |
| `engine.py` | Boucle principale (ci-dessus) ; seul endroit qui appelle l'exchange |
| `guardrails.py` | `Guardrails.check` (valide ou réduit), `tier_for`, `describe_limits` (ce que voit l'agent) |
| `killswitch.py` | États `alive` / `halted` / `dead`, persistés ; `check_financial`, `note_error`, `resume` |
| `exchange.py`, `paper.py` | Protocole `Exchange` ; `PaperExchange` (soldes en base, frais + glissement fixes) |
| `feeds.py` | Protocole `PriceFeed` ; `CcxtPriceFeed` (prix publics), `SyntheticPriceFeed` (hors ligne) |
| `agents.py` | Protocole `Agent`, `MarketView`, `HoldAgent` (la référence), `ChaosAgent` (teste la plomberie) |
| `llm.py`, `llm_agent.py`, `budget.py`, `market.py` | `LLMClient` (`AnthropicClient`, `FakeLLMClient`), `LLMAgent` + `SYSTEM_PROMPT` (en anglais), `InferenceBudget`, résumé de bougies |
| `storage.py` | SQLite : table `kv` (JSON : `life`, `paper_balances`, `killswitch`, `peak_equity`, `day`, `risk_tier`, `last_quotes`, `llm_last_call`) + `decisions`, `fills`, `equity`, `events`, `llm_calls` |
| `app.py` | Composition (`build_agent/feed/engine`), `ensure_life`, `reset_life` |
| `profiles.py`, `launcher.py`, `lock.py`, `cli.py` | Profils `hold` / `llm` / `demo` (base `data/paper-<profil>.db`, ports 8765 / 8766 / 8767) ; `tradeagent up` (sous-processus préfixés, arrêt SIGINT → TERM → KILL) ; verrou ; sous-commandes `run up status resume reset web` |
| `dashboard.py`, `web.py`, `static/` | `build_snapshot` = **seul contrat** entre le bot et l'UI ; serveur stdlib ; front JS/CSS sans build |
| `desktop/` (Node/Electron, hors du paquet Python) | Application de bureau : `main.js` (fenêtre, onglets, lancement des `tradeagent web`, verrouillage des pages), `lib/backend.js` (lien avec Python, sans Electron, testé par `node --test`), `shell.*` + `preload.js` (barre d'onglets), `start.js` (`npm start`). Les profils sont lus dans `profiles.py`, jamais recopiés |
| `scripts/` | Fines enveloppes `.sh` / `.bat` : `setup paper ui start status live` (`live` refuse, volontairement) |

**Points d'extension** : écrire une classe qui satisfait un protocole (`Exchange`, `PriceFeed`, `Agent`, `LLMClient`) et la brancher dans `app.py`. Ne pas modifier le moteur pour ça.

## 5. Commandes

```bash
scripts/setup.sh                        # venv + dépendances + .env + tests (Windows : scripts\setup.bat)
.venv/bin/python -m pytest -q           # ~455 tests, ~10 s, hors ligne
scripts/start.sh demo                   # essai complet hors ligne : bot + interface (http://localhost:8767)
scripts/start.sh hold llm               # référence + LLM côte à côte (llm exige ANTHROPIC_API_KEY dans .env)
.venv/bin/tradeagent run --profile hold --max-cycles 1
.venv/bin/tradeagent status --all       # état + résultat net de chaque profil
.venv/bin/tradeagent web --profile llm --open
cd desktop && npm install && npm start  # application de bureau (visionneuse) ; `npm test` pour ses tests
```

`python -m tradeagent …` équivaut à `tradeagent …`. Sans `--profile`, les commandes utilisent la base de `config.yaml` (`data/agent.db`). Pour arrêter un bot : `Ctrl+C`. Pour trouver son PID : `data/<base>.db.lock` le contient.

## 6. Conventions

- **Langue** : docs, commentaires, messages utilisateur et logs en **français** ; identifiants et noms de fichiers en anglais ; le prompt système du LLM est en anglais.
- **Python** : `from __future__ import annotations`, annotations de types, dataclasses (gelées quand c'est possible). Pas de nouvelle dépendance sans en parler (aujourd'hui : `ccxt`, `pyyaml`, `anthropic`). Serveur web = stdlib ; front = JS/CSS vanilla.
- **Erreurs** : `ConfigError` (démarrage/config, code de sortie 2), `LLMError`, `ExchangeError`/`FeedError` (erreur de cycle, comptée par le kill switch). Un message d'erreur dit quoi faire.
- **Déterminisme** : l'horloge (`clock`) et l'aléa (`seed`) sont injectés ; pas d'attente réelle dans le code testé.
- **Montants en `float`** (suffisant en simulation) ; passage à `Decimal` obligatoire avant le réel (§10).
- **Front** : `textContent` et CSSOM uniquement ; ni `innerHTML`, ni style en ligne, ni ressource externe (CSP). `test_web.py` le vérifie.
- **Scripts** : `.sh` POSIX (`#!/usr/bin/env sh`, exécutable) **et** `.bat` (ASCII, CRLF), toujours par paires ; `test_scripts.py` vérifie qu'ils n'appellent que des sous-commandes existantes.
- **Toute nouvelle limite ou clé de config** : commentée dans `config.yaml`, documentée dans le README, testée.

## 7. Tests et méthode de vérification

`pytest` avec `pythonpath=["src"]`. Outils dans `tests/helpers.py` : `FakeClock`, `ScriptedFeed`, `ScriptedAgent`, `default_cfg(**overrides)`, `make_engine(...)`. Un fichier de test par module (`test_guardrails.py`, `test_killswitch.py`, `test_engine.py`, `test_risk_tiers.py`, `test_budget.py`, `test_llm*.py`, `test_dashboard.py`, `test_web.py`, `test_cli.py`, `test_profiles.py`, `test_launcher.py`, `test_lock.py`, `test_scripts.py`…).

**Définition de « fini »** pour tout changement : code + tests + doc (`config.yaml`, README, ce fichier si l'état ou une décision change) et la suite complète au vert.

**Mutation à la main pour la logique de sécurité** (garde-fous, kill switch, budget, lanceur, web) : copie le projet dans un dossier temporaire, remplace *un* motif par sa version cassée, lance les tests concernés, **attends un échec**. Un mutant qui survit = test manquant, ou mutant équivalent (à documenter). Dernier bilan : 14/14 mutants tués sur le web, 38/39 sur le lanceur (le survivant est équivalent : le ramasse-miettes de CPython ferme le fichier de toute façon).

## 8. Pièges connus

- `pkill -f motif` tue **ton propre shell** si le motif figure dans ta ligne de commande. Utilise le PID du fichier `.lock`, ou mets le motif dans un script.
- `InstanceLock` ne tient que tant que l'objet est référencé : `InstanceLock(db).acquire()` sans variable libère aussitôt (ou utilise `with`).
- Avec 50 € de mise et un ordre minimum de 5 €, `cautious` s'active vers 42,5 € d'equity (plus-haut à 50 €) ; son plafond d'ordre (equity × 20 % × 0,5 ≈ 4,25 €) est alors déjà sous le minimum : en pratique `cautious` se comporte comme `defensive`.
- Le paper est **optimiste** : exécution au dernier prix ± glissement fixe, sans profondeur de carnet.
- Devise de cotation EUR et paires `X/EUR` : changer d'exchange ou de devise impose de vérifier que les paires existent.
- Les prix des tokens du LLM et `usd_to_eur` sont dans `config.yaml` : à tenir à jour à la main.
- L'UI valorise les positions avec `last_quotes`, écrit par le moteur : ne pas le supprimer.
- Un profil n'a pas de vie tant qu'il n'a pas réellement démarré : les contrôles (clé API, profil `live`) se font *avant* d'ouvrir la base.
- `ELECTRON_RUN_AS_NODE=1` (hérité des terminaux lancés par VSCode) fait démarrer Electron comme un simple Node, sans fenêtre (`app` vaut `undefined`) : passer par `npm start` (`desktop/start.js` retire la variable). Sous PowerShell, `npm start -- --profile=demo` perd l'argument : utiliser `node start.js --profile=demo`.
- Vérifier l'app de bureau sans la regarder : `TRADEAGENT_DESKTOP_SMOKE=<dossier>` fait capturer la barre d'onglets et la page active (`shell.png`, `view.png`, `state.json`), puis quitter.
- `node --test test/` échoue sous Node 22 (le dossier est pris pour un module) : `node --test` tout court.
- Playwright n'est pas dans le venv : pour vérifier l'UI dans un vrai navigateur, utiliser le Python système.

## 9. Décisions déjà prises (ne pas rouvrir sans raison)

| Décision | Pourquoi |
|---|---|
| Spot, ordres au marché, pas de levier ni de short | Perte bornée à la mise, simplicité |
| `hold` est la référence à battre | Sans elle, un résultat positif ne prouve rien (le marché a pu monter seul) |
| API d'abord, LLM local ensuite | Décision de Pierre ; l'interface `LLMClient` est prête |
| SQLite + `kv` JSON | Un fichier, transactionnel, lisible en lecture seule par l'UI |
| Un profil = une base | Comparer `hold` et `llm` sans contamination |
| UI en lecture seule, stdlib, sans build | Aucune surface d'attaque ajoutée, rien à compiler |
| Application de bureau Electron = coquille mince autour de l'UI web existante (2026-10-04) | Décision de Pierre. `web.py` et ses protections restent la seule UI ; elle marche toujours dans un navigateur. Electron/Node est une dépendance acceptée, cantonnée à `desktop/` |
| L'app de bureau est visionneuse **et** superviseur (démarrer/arrêter les bots) | Décision de Pierre. Actions dans le menu natif et la zone de notification seulement (voir invariant 10) |
| Fermer la fenêtre n'arrête pas les bots ; notifications de bureau si nécessaire | Décision de Pierre. L'app reste dans la zone de notification ; seul « Quitter » arrête les bots, proprement |
| Distribution : version portable + semi-installeur (venv créé et dépendances téléchargées au premier lancement) | Décision de Pierre. Pas de Python embarqué par PyInstaller |
| Paliers : 15 % / 25 % / mort à 40 % de drawdown ou 50 % de perte totale | On réduit la voilure avant la mort |
| Quarantaine **asynchrone** (future) | Attendre la confirmation ne doit pas bloquer les contrôles du kill switch |
| Mise réelle maximale : 50 € | Uniquement de l'argent que Pierre accepte de perdre à 100 % |

## 10. Axes d'évolution

**Ordre conseillé** : axe F (F1 → F4, priorité de Pierre ; l'étape 0 se fait en parallèle, elle ne demande aucun code) → B1 → B2–B4 selon les résultats → D1/D2 → étape 1 (a → e) → réel à 50 €. Rien ne justifie de brancher l'argent réel avant d'avoir montré, en paper, que l'agent fait mieux que `hold` *net de l'API*. S'il n'y arrive pas, la valeur du projet est le cadre d'expérimentation lui-même.

### Axe F — Application de bureau Electron (priorité actuelle)
Code dans `desktop/` (Node/Electron), sans toucher au moteur. Chaque phase est livrable seule.
- **F0. Socle** — *fait le 2026-10-04* : `git init`, suite au vert sous Windows (PID du `.lock` lisible pendant que le bot tourne, `test_launcher.py` chargeable, chemins comparés en `Path`).
- **F1. Visionneuse** — *faite le 2026-10-04, vérifiée par capture d'écran sous Windows (Electron 41) ; reste le nom du profil dans l'en-tête de la page web (axe E), aujourd'hui seulement dans l'onglet et le titre de la fenêtre* : le processus principal lance `python -m tradeagent web --profile X` pour chaque profil et affiche `http://127.0.0.1:<port>` dans une fenêtre à onglets (un par profil). Fenêtre verrouillée : `sandbox`, `contextIsolation`, pas de `nodeIntegration`, pas de preload sur la page du bot, navigation et nouvelles fenêtres hors boucle locale refusées. Une seule instance de l'app. Nom du profil dans l'en-tête et le titre (axe E). En développement, l'app utilise le `.venv` du projet.
- **F2. Superviseur** : démarrer/arrêter chaque profil depuis le menu natif et la zone de notification ; confirmation avant `llm` (API facturée) ; erreurs de pré-lancement affichées (clé absente, base déjà verrouillée). Fermer la fenêtre = réduire dans la zone de notification, les bots continuent ; « Quitter » les arrête. **Prérequis côté Python : un arrêt propre sous Windows** (aujourd'hui `terminate()` = arrêt sec). Décidé avec Pierre (2026-10-04) : le bot s'arrête en fin de cycle quand son entrée standard se ferme ou reçoit `stop` — canal local parent → enfant, équivalent à Ctrl+C, donc si l'app meurt les bots s'arrêtent aussi. Sortie des bots dans des fichiers de logs avec rotation (`data/logs/`, recouvre une partie de D2).
- **F3. Notifications de bureau** : le processus principal interroge `/api/snapshot` de chaque profil et notifie sur transition : mort, `halted`, changement de palier, bot arrêté sans l'avoir demandé ou sans cycle depuis trop longtemps, budget API épuisé. Sortantes uniquement. Détection des transitions = fonction pure, testée. Ne remplace pas D1 (rien n'arrive si le PC est éteint ou l'app quittée).
- **F4. Portable + semi-installeur** : exécutable portable (electron-builder) ; au premier lancement, un écran d'installation trouve Python ≥ 3.10, crée le venv, installe `tradeagent` et ses dépendances, puis lance l'app ; le venv est refait si la version change. Décidé avec Pierre (2026-10-04) : `config.yaml`, `.env`, `data/` et le venv vivent **dans le dossier de l'application** (vraiment portable, rien dans le dossier utilisateur) ; si Python est absent ou trop ancien, **simple message avec lien de téléchargement et version requise**, pas de téléchargement automatique de Python.
- **F5. Qualité** : tests Node hors réseau (`node --test`) pour la supervision et les notifications ; à brancher sur D5.

### Étape 0 — Valider en vrai (Pierre, sur son PC ; aucun code, ~1 semaine)
1. `scripts/paper.sh hold --max-cycles 1` passe (prix réels). Sinon : corriger `CcxtPriceFeed`.
2. `scripts/paper.sh llm --max-cycles 1` passe ; mesurer le coût réel d'un appel (attendu ≈ 0,002 €).
3. `scripts/start.sh hold llm` pendant 7 jours sans `halted` inexpliqué ; comparer avec `status --all`.

### Étape 1 — Prérequis de l'argent réel (dans cet ordre, seulement après l'étape 0)
- **a. Quarantaine** : le moteur n'appelle plus `market_order` directement ; il enregistre l'ordre validé par les garde-fous (table `pending_orders` : id, ts, symbole, sens, quantité, prix de référence, expiration, statut). Confirmation en **CLI** (`tradeagent orders / approve / reject`, à créer) ; une approbation depuis l'UI serait une décision à prendre avec Pierre, pas par défaut. Expiration (~10 min) et refus si le prix a trop bougé ; l'approbation rejoue les garde-fous avec l'état du moment ; la liquidation de mort n'y passe pas.
- **b. `RealExchange`** (ccxt authentifié, protocole `Exchange`) : testnet d'abord ; clés par variables d'environnement ; `clientOrderId` pour l'idempotence ; après un timeout, **réconcilier avant tout renvoi** ; frais réels (y compris payés en autre monnaie), précisions et notionnel minimum de l'exchange, exécutions partielles ; **les soldes de l'exchange font foi** (écart avec l'état local au-delà d'un seuil → `halted`).
- **c. `Decimal`** pour montants et quantités en mode réel.
- **d. Profil `live`** : `mode: live` accepté seulement si (a) et (b) existent et que la quarantaine est active ; plafond dur `max_live_stake` ; confirmation tapée dans `scripts/live.*` ; retirer le refus de `profiles.py`.
- **e. Filets** : alertes (D1) obligatoires avant le réel ; watchdog séparé qui lit le solde et peut liquider si le bot meurt en silence.
- *Fin d'étape* : tests de timeouts, exécutions partielles, rejets, divergence de solde ; une semaine sur testnet.

### Axe B — Qualité de l'agent (là où se joue le résultat)
- **B1. Replay/backtest** : `ReplayPriceFeed` sur bougies historiques (`fetch_ohlcv`, cache fichier) + horloge simulée ; comparer `hold`, achat-conservation équipondéré, DCA, momentum simple, `chaos`, `llm` sur les mêmes périodes ; cache des réponses LLM par empreinte du prompt pour ne pas payer deux fois. Métriques : résultat net (frais **et** API), drawdown max, nombre d'ordres, frais payés.
- **B2. Prompt versionné** (version stockée dans `decisions`) et A/B entre deux profils.
- **B3. Appels sur événement** (volatilité, franchissement de seuil) plutôt que toutes les heures ; ou modèle léger en routine et plus gros modèle sur événement.
- **B4. « Loyer » indexé sur l'equity** : plafond d'API proportionnel au capital restant (fidèle à l'idée de survie : plus il perd, moins il peut s'offrir d'intelligence).
- **B5. Mémoire/leçons** réinjectées dans le prompt : à n'adopter que si B1 en montre le gain, coût en tokens compris.
- **B6. LLM local** (Ollama / llama.cpp) via une nouvelle classe `LLMClient`. Contraintes : GPU de Pierre (dernier état connu : 16 Go de VRAM, partagés avec ses autres projets IA) → modèles quantifiés 7–14 B ; fiabilité du JSON à mesurer (sortie contrainte) ; coût d'API nul mais latence et électricité ; à évaluer contre l'API avec B1.
- **B7. Plus d'entrées** (carnet d'ordres, autres échelles de temps, actualité) et plus de symboles : un seul ajout à la fois, chacun justifié par B1.

### Axe D — Exploitation
- **D1. Notifications sortantes** (ntfy / Telegram / e-mail) : mort, arrêt, changement de palier, erreurs répétées, résumé quotidien. Jamais de canal entrant qui commande le bot.
- **D2. Tourner en continu** : service (systemd ou Planificateur de tâches), redémarrage automatique (sûr : l'état est en base), logs fichier avec rotation (aujourd'hui stdout seulement), sauvegarde de la base.
- **D3. `tradeagent export`** (à créer) : CSV des exécutions (date, sens, quantité, prix, frais) pour la déclaration fiscale — à vérifier auprès des sources officielles, le projet ne donne pas de conseil fiscal.
- **D4. Table `lives`** (mise, début, fin, cause, résultat net, coût API) alimentée par `reset` et par la mort, et une page « cimetière » dans l'UI.
- **D5. CI** GitHub Actions : pytest sur Linux **et Windows** (enfin un vrai test des `.bat`), Python 3.10–3.13.
- **D6.** `git init` fait (F0) ; restent le premier commit et le linter (ruff).

### Axe E — Interface (reste en lecture seule)
Vue de comparaison `hold` vs `llm` sur une même page ; nom du profil dans l'en-tête et l'onglet ; page des vies passées (D4) ; vérification Firefox/Safari ; accessibilité.

## 11. Hors périmètre (sauf décision explicite de Pierre)

Levier, marge, futures, vente à découvert · retrait ou transfert de fonds par le code · agent qui modifie son code, ses garde-fous ou sa config · agent qui peut ajouter de l'argent ou relever sa mise · UI exposée au réseau, ou page web avec des boutons d'action (le démarrage/arrêt par le menu natif de l'app de bureau est, lui, décidé : axe F) · mise réelle au-delà de 50 € · promesse de rendement ou conseil de placement présenté comme fiable.

## 12. Collaborer avec Pierre

- Réponds en **français, en tutoyant**. Ton direct, honnête, structuré ; priorise ; découpe en petites étapes livrables.
- Dis toujours **ce qui n'est pas testé** (réseau, Windows, vrai LLM). Ne présente jamais un résultat de paper comme une promesse.
- Explique les notions financières quand elles comptent (drawdown, glissement, maker/taker), pas Python ou SQL.
- Tu n'es pas conseiller financier : à l'approche du réel, rappelle le risque de perte totale et ne mets que l'argent qu'il accepte de perdre.
- **Demande avant** de toucher aux invariants (§3), au mode réel, ou à tout ce qui peut engager de l'argent.
- Quand l'état du projet, une décision ou la feuille de route change, **mets à jour les §2, §9 et §10 de ce fichier**.
