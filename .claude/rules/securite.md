---
paths:
  - "src/tradeagent/{engine,guardrails,killswitch,budget,paper,exchange,config,lock,stopper,llm_agent,app}.py"
---

# Code de sécurité : moteur, garde-fous, kill switch, budget

Tu touches à ce qui protège l'argent. Avant de modifier :

1. Relis les invariants de `CLAUDE.md`. Si le changement en frôle un, **arrête-toi et demande à Pierre**.
2. `engine.py` est le seul endroit qui appelle l'exchange. Un nouveau besoin se règle par un protocole branché dans `app.py`, pas en modifiant le moteur.
3. Écris le test **avant** de considérer le changement fait : un fichier de test par module (`tests/test_<module>.py`), outils dans `tests/helpers.py` (`FakeClock`, `ScriptedFeed`, `ScriptedAgent`, `default_cfg(**overrides)`, `make_engine(...)`).

Après la modification, deux vérifications obligatoires, à déléguer :

- **Mutation** (sous-agent `mutation`) : dans une copie temporaire du projet, remplacer *un* motif par sa version cassée, lancer les tests concernés, attendre un échec. Un mutant qui survit = test manquant, ou mutant équivalent (à documenter). Derniers bilans : 29/29 sur le web et le proxy (refonte « Le conseil », 2026-10-05 : police, contexte de la page, navigation, échappement des noms de profils), 14/14 sur le web avant, 38/39 sur le lanceur (le survivant est équivalent : le ramasse-miettes de CPython ferme le fichier de toute façon).
- **Relecture** (sous-agent `relecteur-invariants`) : le diff contre les 12 invariants.

Rappels qui ont déjà servi :
- Montants en `float` (suffisant en simulation) ; passage à `Decimal` obligatoire avant le réel.
- Une pause (`Decision.skipped`) n'est ni un hold ni une erreur.
- `InstanceLock(db).acquire()` sans variable libère le verrou aussitôt.
- `--stop-on-stdin` doit rester une option : `tradeagent up` lance ses enfants avec une entrée standard vide, qui serait lue comme « fermée ».
