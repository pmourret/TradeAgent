---
paths:
  - "src/tradeagent/{web,dashboard,gateway,auth}.py"
  - "src/tradeagent/static/**"
---

# Interface web locale (lecture seule)

- `build_snapshot` (`dashboard.py`) est le **seul contrat** entre le bot et l'interface ; la base y est ouverte en `mode=ro`, une connexion par appel.
- Serveur : stdlib uniquement, boucle locale uniquement, GET/HEAD seulement, en-tête `Host` vérifié, fichiers statiques servis depuis une liste blanche fixe, CSP stricte. Aucune route n'écrit, ne lance ni n'arrête quoi que ce soit.
- Front : JS/CSS vanilla sans build ; `textContent` et CSSOM uniquement ; ni `innerHTML`, ni style en ligne, ni ressource externe. Le texte écrit par le LLM est une donnée brute, jamais du HTML.
- Rien de secret dans le snapshot : ni clé, ni variable d'environnement, ni chemin de fichier.
- Ces règles sont des tests (`tests/test_web.py`, `tests/test_dashboard.py`), pas des conventions : un changement ici se vérifie par mutation (sous-agent `mutation`).
- L'interface valorise les positions avec `last_quotes`, écrit par le moteur : ne pas le supprimer.
- Playwright n'est pas dans le venv : pour vérifier dans un vrai navigateur, utiliser le Python système, ou l'application de bureau (sous-agent `verif-desktop`).
