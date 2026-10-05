# Prompt pour Claude Code

Copie ce qui suit dans Claude Code, à la racine du dépôt TradeAgent, après avoir posé le dossier `design_handoff_tradeagent_conseil/` à la racine.

---

Tu vas réécrire l'interface web en lecture seule de tradeagent selon la maquette « Le conseil ». Le dossier `design_handoff_tradeagent_conseil/` contient tout : lis d'abord `README.md` en entier, puis `tokens.css`, puis ouvre les maquettes de `maquettes/` dans un navigateur (`cd design_handoff_tradeagent_conseil/maquettes && python -m http.server 8000`). Ces maquettes sont des références visuelles : leur code (styles en ligne, `{{ }}`, `support.js`) est interdit dans le projet. Ne le copie pas, recrée-le.

Avant d'écrire du code, lis :
- `README.md` du dépôt, section « Interface web » et le tableau des protections ;
- `src/tradeagent/static/` : index.html, app.js, app.css, auth.css, login.html, setup.html, hub.html ;
- `src/tradeagent/dashboard.py` (build_snapshot, la seule source de données) ;
- `src/tradeagent/web.py` et `tests/test_web.py`, pour savoir exactement ce que les tests vérifient (CSP, pas de style ni de script en ligne, `textContent`, aucune ressource externe, GET et HEAD seulement).

Ce qu'il faut faire, dans cet ordre, en lançant `pytest` après chaque étape :

1. **Polices.** Ajoute Inter 400, 500 et 600 en woff2 dans `static/fonts/` (licence OFL : joins le fichier de licence). Vérifie que le serveur les sert avec le bon type MIME et qu'elles passent la CSP (`font-src 'self'`). Si tu ne peux pas obtenir les fichiers, garde system-ui et signale-le.
2. **Jetons.** Fusionne `tokens.css` dans `static/app.css` en remplaçant l'ancien `:root`. Thème sombre uniquement : retire le bloc `prefers-color-scheme: light`.
3. **Page d'un profil** (`index.html`, `app.js`, `app.css`). Recrée la structure du README : barre de cycle, en-tête, bandeau, ligne de cadence, scène du conseil (sous-agents, liaison, portefeuille, référence hold), corps (marges, courbe, journal, évènements), pied.
   - Garde le modèle actuel de `app.js` : `el()`, `svg()`, `clear()` et `textContent` partout.
   - Pour le dynamique, utilise seulement des classes et `element.style.setProperty('--cycle', …)` / `element.style.width`.
   - Ajoute un `ResizeObserver` sur `.page`, qui pose `data-mode` (compact, moyen, large, tres-large ; seuils 600, 1024, 1500).
   - Garde le snapshot précédent pour déclencher les animations seulement sur un vrai changement : nouvelle décision, equity, palier.
   - Gère les six états : vie avec ses trois paliers, mort, arrêté, bot muet, connexion perdue, aucune donnée.
4. **Couleur des résultats.** Seuls le résultat net, l'écart à hold et la variation du jour prennent `.sign-pos` ou `.sign-neg`. Le signe + − ± reste toujours écrit.
5. **Contexte Electron.** Le gabarit servi par `tradeagent web` (local) doit indiquer `data-context="electron"`, celui de `tradeagent serve` `data-context="web"`, de la façon la plus simple compatible avec la CSP (par exemple deux gabarits ou une substitution côté serveur). En contexte Electron, masque le fil d'Ariane, les liens de profils et la déconnexion.
6. **Pages d'authentification et accueil** (`auth.css`, `login.html`, `setup.html`, `hub.html`). Applique les maquettes sans changer les noms de champs, les `action`, les `autocomplete` ni les `{{message}}` / `{{min_password}}` / `{{profiles}}` du serveur. Pour l'accueil, rends d'abord la liste avec les données disponibles. N'ajoute `/api/profiles` que si je le valide (point 4 de la liste « Données » du README).
7. **Mouvement réduit.** Vérifie que chaque animation de la table du README a sa version calme sous `prefers-reduced-motion: reduce`, et qu'un agent mort ou arrêté, ou une connexion perdue, arrête tout mouvement.
8. **Accessibilité.** Navigation complète au clavier, focus visible (contour accent de 2 px), contraste du texte d'au moins 4,5:1, cibles de 44 px en mode compact, `aria-current` sur le profil courant, `role="status"` sur le bandeau, `aria-label` descriptif sur la courbe.

Règles à ne jamais casser :
- Lecture seule : aucun bouton, lien ou formulaire qui agit sur un agent. Les seuls formulaires sont la connexion, la création du compte et la déconnexion.
- Aucune ressource externe, aucun CDN, aucune bibliothèque, aucune étape de construction.
- Aucun attribut `style`, aucun `<script>` en ligne, aucun `innerHTML` avec des données.
- « Paper trading, argent fictif » visible sur chaque écran. Textes en français, sans tiret cadratin. Aucun vocabulaire de casino ou de promesse de gain.
- La couleur ne porte jamais seule une information.

Pour les données qui n'existent pas encore (liste « Données » du README), utilise d'abord le repli indiqué. Propose-moi ensuite les changements de `dashboard.py`, avec leurs tests, dans un commit séparé, sans les appliquer d'office.

**Fidélité exigée : le résultat doit être identique aux captures de `captures/`.** Méthode :
1. Prépare un moyen de servir une fixture de `fixtures/` à la place de `/api/snapshot`. Ce doit être un script de test ou une option de développement, jamais activable en production. Fige l'horloge du navigateur à `generated_at` et règle le fuseau sur Europe/Paris.
2. Pour chaque ligne du tableau « Captures de référence » du README : ouvre la page à la largeur indiquée (avec un vrai navigateur sans interface si tu en as un, par exemple Playwright en dépendance de développement seulement), avec `prefers-reduced-motion: reduce` pour figer les animations. Fais une capture et compare-la à la référence, côte à côte.
3. Corrige jusqu'à ce que les textes, les positions, les couleurs, les tailles et les retours à la ligne correspondent. Un écart de rendu des polices de 1 ou 2 px est acceptable. Une différence de mise en page, de couleur, de texte ou d'élément ne l'est pas.
4. Les nombres affichés doivent sortir exactement des fixtures, avec les formats de l'`app.js` actuel (fr-FR, espace insécable avant €, signe − typographique).

Quand tu as fini : lance `pytest`, donne-moi le tableau des 29 captures avec, pour chacune, « identique » ou l'écart restant et sa cause, puis ouvre `tradeagent web --profile demo --open` pour un contrôle en direct.
