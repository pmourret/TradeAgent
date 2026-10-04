# Architecture

Le cycle du moteur et le rôle de chaque module.

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
| `stopper.py` | `StdinStop` : arrêt propre demandé par le processus parent (`run --stop-on-stdin` : `stop` ou fermeture de l'entrée standard → fin du cycle en cours puis sortie). Pas un canal de commande : un seul mot compris |
| `desktop/` (Node/Electron, hors du paquet Python) | Application de bureau : `main.js` (fenêtre, onglets, lancement des `tradeagent web`, verrouillage des pages, menu Bots, zone de notification, sortie), `lib/backend.js` (lien avec Python) et `lib/supervisor.js` (démarrage/arrêt des bots, journaux avec rotation dans `data/logs/`), `lib/notifier.js` (notifications de bureau : détection des transitions entre deux instantanés `/api/snapshot`, fonctions pures), tous sans Electron et testés par `node --test`, `shell.*` + `preload.js` (barre d'onglets), `start.js` (`npm start`). Les profils sont lus dans `profiles.py`, jamais recopiés. `test/readonly.test.js` vérifie qu'aucun message IPC ne permet à une page d'agir sur un bot |
| `scripts/` | Fines enveloppes `.sh` / `.bat` : `setup paper ui start status live` (`live` refuse, volontairement) |

**Points d'extension** : écrire une classe qui satisfait un protocole (`Exchange`, `PriceFeed`, `Agent`, `LLMClient`) et la brancher dans `app.py`. Ne pas modifier le moteur pour ça.
