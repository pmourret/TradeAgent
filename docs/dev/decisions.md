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
| Paliers : 15 % / 25 % / mort à 40 % de drawdown ou 50 % de perte totale | On réduit la voilure avant la mort |
| Quarantaine **asynchrone** (future) | Attendre la confirmation ne doit pas bloquer les contrôles du kill switch |
| Mise réelle maximale : 50 € | Uniquement de l'argent que Pierre accepte de perdre à 100 % |

## Hors périmètre (sauf décision explicite de Pierre)

Levier, marge, futures, vente à découvert · retrait ou transfert de fonds par le code · agent qui modifie son code, ses garde-fous ou sa config · agent qui peut ajouter de l'argent ou relever sa mise · UI exposée au réseau, ou page web avec des boutons d'action (le démarrage/arrêt par le menu natif de l'app de bureau est, lui, décidé : axe F) · mise réelle au-delà de 50 € · promesse de rendement ou conseil de placement présenté comme fiable.
