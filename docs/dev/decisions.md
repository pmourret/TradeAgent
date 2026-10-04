# Décisions déjà prises et hors périmètre

Ne pas rouvrir une décision sans raison. Toute nouvelle décision de Pierre s'ajoute ici, datée.

| Décision | Pourquoi |
|---|---|
| Spot, ordres au marché, pas de levier ni de short | Perte bornée à la mise, simplicité |
| Exchange cible : `bitvavo`, choisi le 2026-10-04 | Raison : à compléter par Pierre. Éléments relevés ce jour-là : taker du premier palier 0,25 % (page officielle, contre 0,80 % chez Kraken Pro), marchés en EUR, minimum d'ordre de 5 € (confirmé par Pierre), agrément MiCA via l'AFM néerlandaise selon des sites tiers (à vérifier sur le registre ESMA) |
| `hold` est la référence à battre | Sans elle, un résultat positif ne prouve rien (le marché a pu monter seul) |
| API d'abord, LLM local ensuite | Décision de Pierre ; l'interface `LLMClient` est prête |
| SQLite + `kv` JSON | Un fichier, transactionnel, lisible en lecture seule par l'UI |
| Un profil = une base | Comparer `hold` et `llm` sans contamination |
| UI en lecture seule, stdlib, sans build | Aucune surface d'attaque ajoutée, rien à compiler |
| Application de bureau Electron = coquille mince autour de l'UI web existante (2026-10-04) | Décision de Pierre. `web.py` et ses protections restent la seule UI ; elle marche toujours dans un navigateur. Electron/Node est une dépendance acceptée, cantonnée à `desktop/` |
| L'app de bureau est visionneuse **et** superviseur (démarrer/arrêter les bots) | Décision de Pierre. Actions dans le menu natif et la zone de notification seulement (voir invariant 10) |
| Fermer la fenêtre n'arrête pas les bots ; notifications de bureau si nécessaire | Décision de Pierre. L'app reste dans la zone de notification ; seul « Quitter » arrête les bots, proprement |
| Distribution : version portable + semi-installeur (venv créé et dépendances téléchargées au premier lancement) | Décision de Pierre. Pas de Python embarqué par PyInstaller. Réalisé le 2026-10-04 (F4) sous forme d'un dossier ou d'un zip, pas d'un exécutable unique auto-extrait : ce dernier s'extrait dans le dossier temporaire à chaque lancement, ce qui contredit « rien dans le dossier utilisateur » (choix de Claude, à confirmer par Pierre) |
| But de l'application : rapporter de l'argent à l'utilisateur ; le bot doit survivre, se cloner, viser une croissance composée (2026-10-04) | Décision de Pierre. Sa mort signifie qu'il ne gagne pas plus qu'il ne dépense en API. Ce n'est pas une promesse de gain : tout se juge en paper et par backtest |
| Le loyer d'API sort de la mise : kill switch, plus-haut et paliers jugent l'equity nette du loyer (2026-10-04) | Décision de Pierre. Sans cela un bot en cash qui appelle le LLM ne mourait jamais |
| Clonage par variantes, inspiré d'automaton : le parent transmet une note de stratégie, une graine aléatoire fait varier les paramètres ; l'enfant est financé par les gains du parent, jamais par de l'argent neuf (2026-10-04) | Décision de Pierre. L'agent propose, le code dispose ; pas d'auto-modification du code |
| Caisse de l'utilisateur : part des gains mise de côté, configurable, 50/50 pour commencer (2026-10-04) | Décision de Pierre. Écriture comptable, aucun retrait ni transfert dans le code |
| Un bot à l'arrêt sans issue ne meurt pas tout seul : il est signalé, le code dresse le bilan et conseille, l'utilisateur décide (2026-10-04) | Décision de Pierre. Il juge au vu de ce que l'agent a rapporté et consommé |
| Réveil sur mouvement de prix obligatoire quand l'agent dort en détenant une position (2026-10-04) | Décision de Pierre. Sinon la position n'est surveillée que par le kill switch pendant le sommeil |
| L'application reste en lecture seule pour l'instant ; réglage de la température de l'agent à terme (2026-10-04) | Décision de Pierre |
| Paliers : 15 % / 25 % / mort à 40 % de drawdown ou 50 % de perte totale | On réduit la voilure avant la mort |
| Quarantaine **asynchrone** (future) | Attendre la confirmation ne doit pas bloquer les contrôles du kill switch |
| Mise réelle maximale : 50 € | Uniquement de l'argent que Pierre accepte de perdre à 100 % |

## Hors périmètre (sauf décision explicite de Pierre)

Levier, marge, futures, vente à découvert · retrait ou transfert de fonds par le code · agent qui modifie son code, ses garde-fous ou sa config · agent qui peut ajouter de l'argent neuf ou relever sa mise (le clonage décidé le 2026-10-04 ne finance un enfant qu'avec les gains du parent) · UI exposée au réseau, ou page web avec des boutons d'action (le démarrage/arrêt par le menu natif de l'app de bureau est, lui, décidé : axe F) · mise réelle au-delà de 50 € · promesse de rendement ou conseil de placement présenté comme fiable.
