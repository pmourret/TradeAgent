# CLAUDE.md — tradeagent

Ce fichier est chargé à chaque session : il ne contient que ce qui sert tout le temps. Le reste est dans `docs/dev/` et `.claude/rules/`, à lire **seulement quand la tâche le demande** (table en bas).

## Le projet

Un agent de trading crypto piloté par un LLM, en Python (≥ 3.10), sur le PC Windows de Pierre. **L'IA propose, le code dispose** : l'agent ne parle jamais à l'exchange ; chaque décision traverse des garde-fous codés en dur, et un kill switch indépendant le « tue » quand la mise est perdue. Chaque appel au LLM coûte de l'argent réel, compté dans le résultat (`résultat net = equity − mise − coûts API de la vie`).

- **Mode actuel : paper trading uniquement** (argent fictif, prix publics de Bitvavo via ccxt). Le mode réel n'existe pas ; mise réelle visée : 50 € au plus.
- C'est une expérience, pas un produit : rien n'indique qu'un LLM batte le marché une fois frais et API payés. `hold` est la référence à battre.
- Une application de bureau Electron (`desktop/`) affiche l'interface web locale et démarre/arrête les bots.
- **Cap fixé par Pierre (2026-10-05) : l'application doit tourner 24 h sur 24 et être la plus rentable possible.** Toute proposition se juge à cette double aune : la disponibilité (redémarrage sans perte d'état, aucun arrêt silencieux, positions surveillées en continu) et le résultat net de frais, mesuré contre `hold` et `buyhold`. Jamais au prix d'un invariant, et jamais en présentant un résultat de paper ou de backtest comme une promesse.
- **Ce code manipulera de l'argent : la justesse passe avant la vitesse et avant l'économie de tokens.**

## Invariants — ne jamais casser, ne jamais contourner

Ce sont des propriétés de sécurité, protégées par des tests. Si une demande semble en exiger la violation, **arrête-toi et demande à Pierre**.

1. **L'agent n'agit pas.** `Agent.decide(MarketView) -> Decision` reçoit un état en lecture seule. Il n'a accès ni à l'exchange, ni aux clés, ni à la config des garde-fous. Tout ordre passe par `Guardrails.check` avant `Exchange.market_order`.
2. **Le kill switch est indépendant de l'agent.** Évalué à chaque cycle *avant* l'appel à l'agent, même si l'agent est en pause. `dead` est définitif jusqu'à `tradeagent reset --yes` ; `halted` (trop d'erreurs d'affilée, sans liquidation) ne repart que par `resume`. L'état est en base : un redémarrage ne ressuscite rien.
3. **La liquidation à la mort est une action du moteur**, qui contourne volontairement les garde-fous et n'est jamais exposée à l'agent.
4. **Sortir d'une position est toujours possible.** Perte journalière, plafond d'achats et palier défensif ne bloquent que les *achats*.
5. **Les garde-fous réduisent avant de refuser**, et journalisent la raison (« réduit de 14.40 à 10.00 EUR par max_order_pct »).
6. **Config stricte.** Clé inconnue ou valeur incohérente = refus au démarrage (une faute de frappe ne doit jamais désactiver un garde-fou). `mode: live` est refusé (`config.py`) tant que la quarantaine et l'adaptateur réel n'existent pas.
7. **Budget d'inférence appliqué en code** : chaque appel est valorisé et écrit dans `llm_calls` ; `llm_last_call` est écrit *avant* l'appel (une panne ne déclenche jamais une rafale d'appels payants) ; plafonds par jour (UTC) et au total. Une pause (`Decision.skipped`) n'est ni un hold ni une erreur : elle ne remet pas à zéro le compteur d'erreurs.
8. **Une vie = une mise.** Changer `stake` sans `reset` est refusé. `reset` efface l'état de la vie (`LIFE_KEYS` dans `app.py`), garde le journal et le suivi des coûts API ; il est refusé tant qu'un `run` tient la base (le bot vivant réécrirait l'ancienne vie par-dessus la nouvelle).
9. **Aucun secret** dans un prompt, un log, la base, le snapshot web ou un message d'erreur. Aucune fonction de retrait ou de transfert de fonds dans le code, jamais. Clés d'exchange futures : sous-compte dédié, **sans droit de retrait**. Ne lis jamais `.env` (la lecture est d'ailleurs refusée par `.claude/settings.json`).
10. **Interface web en lecture seule**, boucle locale uniquement (pas d'option pour l'ouvrir au réseau), base ouverte en `mode=ro`, GET/HEAD seulement, en-tête `Host` vérifié, CSP stricte, texte de l'agent affiché via `textContent`. Pas de bouton qui agit sur le bot. Ce sont des tests (`test_web.py`), pas des conventions. **L'application de bureau ne change rien à cela** : elle affiche cette même page dans une fenêtre verrouillée ; démarrer ou arrêter un bot se fait par le menu natif et la zone de notification, jamais depuis une page ni par une route HTTP. `resume` et `reset` restent en ligne de commande.
11. **Un seul `run` par base** (verrou fichier de l'OS, `lock.py`) ; chaque profil a sa propre base.
12. **Les tests n'accèdent jamais au réseau ni à l'API payante.**

## Commandes (Windows, PowerShell)

```powershell
.\.venv\Scripts\python.exe -m pytest -q             # suite Python, ~10 s, hors ligne
.\.venv\Scripts\python.exe -m tradeagent run --profile hold --max-cycles 1   # un cycle sur prix réels, gratuit
.\.venv\Scripts\python.exe -m tradeagent status --all
cd desktop; node --test                              # tests de l'application de bureau
cd desktop; npm start                                # l'application de bureau
```

Profils : `hold` (référence, gratuit), `board` (les modèles du code, sans LLM, gratuit), `llm` (API facturée), `demo` (hors ligne). Scripts `scripts\*.bat` et `scripts/*.sh` : fines enveloppes des mêmes commandes.

## Conventions

- **Langue** : docs, commentaires, messages utilisateur et logs en **français** ; identifiants et noms de fichiers en anglais ; le prompt système du LLM est en anglais.
- **Python** : `from __future__ import annotations`, types annotés, dataclasses gelées quand c'est possible. Pas de nouvelle dépendance sans en parler (aujourd'hui : `ccxt`, `pyyaml`, `anthropic` ; Electron, cantonné à `desktop/`).
- **Erreurs** : `ConfigError` (démarrage, code de sortie 2), `LLMError`, `ExchangeError`/`FeedError` (erreur de cycle, comptée par le kill switch). Un message d'erreur dit quoi faire.
- **Déterminisme** : l'horloge (`clock`) et l'aléa (`seed`) sont injectés ; pas d'attente réelle dans le code testé.
- **Points d'extension** : une classe qui satisfait un protocole (`Exchange`, `PriceFeed`, `Agent`, `LLMClient`), branchée dans `app.py`. Ne pas modifier le moteur pour ça.
- **Toute nouvelle limite ou clé de config** : commentée dans `config.yaml`, documentée dans le README, testée.
- **Commits** : un par sujet, message en français écrit dans un fichier puis `git commit -F` (les guillemets cassent `-m` sous PowerShell 5.1).

## Définition de « fini »

Code + tests + doc (`config.yaml`, README, et le fichier de `docs/dev/` concerné si l'état, une décision ou la feuille de route change), **les deux suites au vert** (Python et Node), et pour tout changement qui touche à la sécurité : le test par mutation et la relecture des invariants (voir ci-dessous). Dis toujours ce qui n'a pas été vérifié pour de vrai (réseau, vrai LLM, éléments visuels).

## Travailler à moindre coût sans rien céder sur la qualité

Le raisonnement, la conception et tout code qui touche à l'argent restent dans la session principale (Opus, effort élevé). Ce qui est mécanique ou volumineux part dans un sous-agent, pour que seul le résultat entre dans le contexte :

| Sous-agent | Quand l'utiliser |
|---|---|
| `tests` | Après un changement : lance les deux suites et ne rapporte que le bilan et les échecs |
| `verif-desktop` | Après un changement dans `desktop/` : lance l'application en mode de vérification automatique et décrit ce qu'elle affiche |
| `mutation` | Après un changement de logique de sécurité : mutants dans une copie temporaire, tableau tués/survivants |
| `relecteur-invariants` | Avant de proposer un commit qui touche `src/tradeagent/` ou `desktop/` : relit le diff contre les 12 invariants |
| `doc-externe` | Pour un fait extérieur (frais, API d'un exchange, réglementation, doc d'une bibliothèque) : citations et liens, rien d'inventé |
| `Explore` (intégré) | Pour localiser du code quand tu ne sais pas où chercher |

Règles de sobriété, valables partout :
- Ne lis que ce que la tâche demande : `Grep` avant `Read`, `Read` avec `offset`/`limit` sur les gros fichiers, jamais `Glob "**/*"` (il remonte `.venv/` et `node_modules/`).
- Coupe la sortie des commandes (`| Select-Object -Last 15`) ; une suite verte tient en une ligne.
- Ne recopie pas dans ta réponse ce que Pierre a déjà sous les yeux.
- Un sous-agent ne décide rien sur les invariants, le mode réel ou l'argent : il rapporte, la session principale tranche avec Pierre.

## Où trouver le reste (à lire à la demande, pas par défaut)

| Fichier | Contenu | Quand le lire |
|---|---|---|
| `docs/dev/etat.md` | Ce qui est fait, ce qui n'a jamais tourné pour de vrai, ce qui manque | Avant d'affirmer qu'une chose marche ; à mettre à jour quand l'état change |
| `docs/dev/architecture.md` | Cycle du moteur, rôle de chaque module | Avant de toucher un module que tu ne connais pas |
| `docs/dev/roadmap.md` | Axes d'évolution et ordre conseillé | Pour choisir ou préparer la tâche suivante |
| `docs/dev/decisions.md` | Décisions prises, hors périmètre | Avant de proposer un changement de conception |
| `docs/dev/pieges.md` | Pièges connus | Quand quelque chose se comporte bizarrement |
| `.claude/rules/*.md` | Règles propres à un dossier (application de bureau, interface web, code de sécurité, scripts) | Chargées toutes seules quand tu ouvres un fichier concerné |
| `README.md` | Documentation utilisateur | Quand un comportement visible change |

## Collaborer avec Pierre

- Réponds en **français, en tutoyant**. Ton direct, honnête, structuré ; priorise ; découpe en petites étapes livrables.
- Pierre est développeur confirmé, débutant en trading crypto : explique les notions financières quand elles comptent (drawdown, glissement, maker/taker), pas Python ou SQL.
- Tu n'es pas conseiller financier : à l'approche du réel, rappelle le risque de perte totale. Ne présente jamais un résultat de paper comme une promesse.
- **Demande avant** de toucher aux invariants, au mode réel, ou à tout ce qui peut engager de l'argent.
- N'écris aucun fait extérieur (frais, minimums, agréments) que tu n'as pas vérifié : écris « à vérifier ».
