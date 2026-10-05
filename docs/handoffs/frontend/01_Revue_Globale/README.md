# Handoff : tradeagent, interface « Le conseil »

## Vue d'ensemble
Refonte de l'interface web en lecture seule de tradeagent (dépôt `pmourret/TradeAgent`, dossier `src/tradeagent/static/`). L'objectif est une interface « mode agent » : on voit des sous-agents quantitatifs qui réclament des positions, un portefeuille unique qui exécute, et la référence `hold` à battre. Le mouvement montre seulement deux choses vraies : le temps qui passe vers le prochain cycle (toutes les 15 min) et l'arrivée d'une décision. Argent fictif partout.

Écrans livrés : connexion, création du compte administrateur, accueil (liste des profils), page d'un profil, et 5 états particuliers (mort, arrêté, bot muet, connexion perdue, aucune donnée). Bureau, tablette, téléphone et fenêtre Electron de toute taille. **Thème sombre uniquement.**

## À propos des fichiers
Les fichiers de `maquettes/` sont des **références de design en HTML**. Ils montrent l'apparence et le comportement voulus, ils ne sont pas du code à copier. Ils utilisent un moteur de maquette (`support.js`, styles en ligne, `{{ }}`) **interdit dans le projet**. La tâche est de **recréer ces maquettes dans le code existant** : `static/index.html`, `app.css`, `app.js`, `auth.css`, `login.html`, `setup.html`, `hub.html`, en HTML, CSS et JavaScript simples.

Pour les ouvrir : servir le dossier `maquettes/` en local (`python -m http.server`) et ouvrir chaque `.dc.html`. Les fichiers `Le conseil - Ecrans.dc.html` et `Le conseil - Systeme.dc.html` sont les planches de référence. `Profil Conseil.dc.html` est la page d'un profil, pilotable par ses réglages (état, palier, contexte, largeur).

## Fidélité
**Haute fidélité.** Les couleurs, la typographie, les espacements, les textes et les animations sont définitifs. Les valeurs exactes sont dans `tokens.css` (prêt à fusionner dans `app.css`) et ci-dessous.

## Contraintes non négociables (vérifiées par `tests/test_web.py`)
- HTML, CSS et JS simples, sans étape de construction, sans bibliothèque ni framework.
- **Aucune ressource externe.** Inter doit être servie par le site (`static/fonts/Inter-{Regular,Medium,SemiBold}.woff2`, licence OFL), avec system-ui en repli. Ne copie pas `maquettes/_ds/.../styles.css` : son `@import` vers Google Fonts est interdit.
- **CSP stricte** : pas de script en ligne, pas d'attribut `style`, pas de `setAttribute("style")`. Le dynamique passe par des classes, par `element.style.setProperty('--x', v)` et par `element.style.width = …`, comme dans l'`app.js` actuel.
- Tout texte venu des données passe par `textContent`, jamais par `innerHTML`.
- **Lecture seule** : aucun bouton, lien ou formulaire qui agit sur un agent. Les seuls formulaires sont `/login`, `/setup` et `/logout`. La navigation entre profils est permise.
- « Paper trading, argent fictif » reste visible sur chaque écran. Aucun tiret cadratin dans les textes.
- La couleur ne porte jamais seule une information : un état a toujours un glyphe et un mot, un résultat a toujours un signe écrit.
- `prefers-reduced-motion` : chaque animation a une version calme (voir la table du mouvement).

## Écrans

### En-tête (toutes les pages de données)
Flex, retour à la ligne permis, `gap: 8px 18px`, `padding: 14px clamp(16px, 2.4cqi, 28px)`.
- Marque « tradeagent » en 15/600, interlettrage 0.01em.
- Fil d'Ariane « Profils / board » en 13 px : lien `--color-accent-300`, séparateur `--color-neutral-500`, profil courant `--color-text`.
- Liens vers les autres profils, en pastilles 12 px (`padding: 4px 10px`, rayon 6). Le profil courant porte `aria-current="page"`, texte `--color-accent-300` et `box-shadow: inset 0 0 0 1px --color-accent-700`. Au survol : fond `color-mix(text 7%)`. Les pastilles sont masquées en mode compact.
- Badge « Paper trading, argent fictif » à droite (`margin-left: auto`) : 12 px, bordure 1 px `--color-neutral-700`, rayon 6, texte `--color-neutral-300`.
- Fraîcheur « Actualisé il y a 6 s » en 12 px `--color-neutral-400`, précédée d'un point de 7 px. Le point est accent plein et émet un ping à chaque snapshot reçu. Connexion perdue : point creux à bordure 1,5 px `--tone-orange`, texte « Connexion perdue · il y a 2 min 10 s ».
- « Se déconnecter » : `.btn.btn-secondary` dans un `<form method="post" action="/logout">`, hauteur minimale 44 px en compact et 32 px ailleurs.
- **Contexte Electron** (page locale `tradeagent web`, sans session) : masquer le fil d'Ariane, les pastilles de profils et le bouton de déconnexion. Afficher seulement le nom du profil en 13 px `--color-neutral-300`, car les onglets de l'application font la navigation. Détection proposée : `serve` (multi-profils derrière Traefik) ajoute `data-context="web"` au `<body>` du gabarit, et `web` (local) ajoute `data-context="electron"`.

### Barre de cycle (tout en haut, 3 px)
Fond `--color-neutral-900`, remplissage accent `width: calc(var(--cycle) * 100%)`, avec `--cycle = clamp(0, (now - money.last_update) / cycle_seconds, 1)` recalculé chaque seconde.
- Bot muet : pointillé `--tone-warn` sur toute la largeur.
- Connexion perdue : remplissage `--color-neutral-600`, figé.

### Page d'un profil
Conteneur principal `max-width: 1680px`, aligné à gauche (pas centré), `padding: 0 clamp(16px, 2.4cqi, 28px)`, écart vertical `clamp(18px, 2.2cqi, 28px)`.

1. **Bandeau d'état** (si mort, arrêté, muet, hors ligne ou défensif) : `role="status"`, surface, rayon 8, `--shadow-sm`, `padding: 14px 16px`. Glyphe de 20 px, titre en 15/500, texte en 13 px `--color-neutral-300`. Textes exacts : voir `bannerTitle` / `bannerText` dans `Profil Conseil.dc.html`, méthode `renderVals`.
2. **Ligne de cadence** en 13 px : « Prochain conseil dans **07:42** · cycle n° 1 168 · toutes les 15 min ». Dans les 30 dernières secondes, ajouter « Le board délibère » en `--color-accent-300`, qui clignote. Variantes : mort « Plus aucun cycle depuis ven. 14:15 » ; arrêté « Cycles suspendus depuis … » ; muet « Cycle attendu à 18:00, en retard de 32 min » ; hors ligne « Dernier état connu : prochain conseil vers 18:45 ».
3. **Scène du conseil** (`.stage`) : flex avec retour à la ligne, `gap: 14px`, `align-items: center`.
   - **Sous-agents** (`flex: 1 1 250px`) : surtitre « Sous-agents · N », puis une carte par sous-agent (surface, rayon 14, `--shadow-sm`, padding 16).
     - Présence : socle rond de 44 px `--color-accent-900` portant un point de 16 px. Cinq états : au repos (contour 1,5 px `--color-neutral-500`) ; réclame (point accent plus halo animé) ; sort (point `--color-accent-300` plus chevron qui glisse) ; en suspens (glyphe pause `--color-neutral-400`) ; sans nouvelles (contour tireté `--tone-warn`).
     - Nom en 16/500, posture en 12 px `--color-neutral-400` (« Réclame 2 positions », « Sort de ETH/EUR », « En suspens, cibles gelées », « Pas de nouvelles depuis 47 min »).
     - Une ligne par position réclamée : symbole et valeur, en 13 px avec chiffres tabulaires. Sans position : « Aucune position réclamée. »
   - **Liaison** (`flex: 0 0 96px`, `aria-hidden`) : trait tireté 1 px `--color-neutral-700` et légende « cibles » en 11 px. En mode compact, la liaison devient verticale (hauteur 36 px, toute la largeur).
   - **Portefeuille unique** (`flex: 2 1 340px`) : surface, rayon 14, `--shadow-md`, `padding: clamp(16px, 2cqi, 24px)`.
     - Surtitre « Portefeuille unique », avec à droite l'état (glyphe et « En vie · palier normal »).
     - Equity en `clamp(34px, 3.8cqi, 48px)`, poids 500, interlettrage −0.03em, chiffres tabulaires.
     - Ligne « Résultat net **+12,40 €** · Drawdown **3,1 %** · 12 j 4 h de vie ».
     - Barre de répartition de 8 px (gap 2, rayon 4) : 1er actif accent, 2e `--color-accent-300`, cash `--color-neutral-700`.
     - Lignes des positions en grille `1.4fr 1fr 44px` : nom et quantité, valeur, part. Les filets s'estompent sur 48 px à chaque bout.
     - Pied : « Aujourd'hui **−4,10 € (−0,40 %)** », puis le quota d'achats en 6 pastilles de 8 px (utilisées : accent plein ; libres : contour `--color-neutral-600`) et « 1 sur 6 ».
     - Zone d'annonce : « Ordre reçu : … ». Si l'agent est mort : « Vérifie ci-dessus qu'il ne reste aucune position : la liquidation peut avoir échoué. »
   - **Référence hold** (`flex: 1 1 220px`) : un lien vers le profil hold, bordure tiretée 1 px `--color-neutral-700`, rayon 14. Contenu : « Référence · hold », « Ne fait rien. À battre. », equity en 26 px, « Écart du board **+12,40 €** devant/derrière ».
4. **Corps** (`.body`) : grille `repeat(auto-fit, minmax(min(420px, 100%), 1fr))`, `gap: clamp(20px, 2.4cqi, 32px)`.
   - **Marges** :
     - Rail « Mort de l'agent » de 22 px, rayon 6, fond découpé en 3 zones (0 à 37,5 % neutre ; 37,5 à 62,5 % teinté `--tone-warn` à 14 % ; au-delà teinté `--tone-orange` à 18 %). Remplissage accent à 32 % avec un bord droit de 2 px accent, `width: calc(var(--death) * 100%)`. Repères « prudent 15 % · défensif 25 % · mort 40 % » en 10 px. Texte : « 3,1 % sur 40 % · encore 36,9 pts ».
     - Rails « Sortie BTC/EUR à 55 300 € » : remplissage depuis la droite, sur une échelle de 0 à 8 % de marge (au-delà de 8 %, plafonné), soit `width = 100 − (1 − min(marge/8, 1)) × 94` %. Mot : large (≥ 4 %), attentif (1,5 à 4 %), tendu (< 1,5 %). Une marge tendue passe en ton `--tone-warn` et frémit.
   - **Equity depuis la mise** : SVG construit par le script (`viewBox 0 0 300 100`, `preserveAspectRatio none`, trait accent de 2 px en `vector-effect: non-scaling-stroke`), sur 170 px de haut. Lignes tiretées pour la mise et les paliers présents dans l'échelle, légendées à droite en 11 px. Point final de 10 px avec un anneau de la couleur du fond. `aria-label` descriptif, comme dans l'`app.js` actuel.
   - **Ce qui a été décidé** : une carte par entrée (surface, rayon 8, `padding: 12px 14px`).
     - Ligne 1 : heure, « trend → portefeuille », verdict aligné à droite.
     - Ligne 2 : « + Achat / − Vente / · Attente » en 14/500, puis « BTC/EUR · 40,00 € » en `--color-neutral-400`.
     - Ligne 3 : la raison entre guillemets français, en 13 px `--color-neutral-300`, puis le détail du verdict en `--color-neutral-500`.
     - Les attentes consécutives sont regroupées, comme aujourd'hui (« 7 fois depuis 17:15 »). On affiche 4 entrées, ou 3 en compact.
   - **Évènements** : niveau (info en `.tag-neutral` ; alerte en contour `--tone-warn` ; critique en contour `--tone-dead`), heure avec regroupement « ×3 depuis … », puis le message.
5. **Pied de page** en 12 px `--color-neutral-500` : « Lecture seule · actualisation toutes les 15 s · heures dans ton fuseau. » et « Reprendre un bot arrêté ou repartir d'une nouvelle vie reste en ligne de commande. »

### États particuliers
- **Mort** (`status.state = "dead"`) :
  - voile rouge en haut de page, scène et corps désaturés (`saturate(.6)`), toutes les animations arrêtées ;
  - présence au repos, aucune position, cash à 100 % ;
  - journal : les ventes de liquidation viennent de « kill switch → portefeuille » ;
  - évènement critique.
- **Arrêté** (`halted`) : hachures orangées discrètes sur toute la page, glyphe pause, cibles gelées mais affichées, animations arrêtées.
- **Bot muet** (`alive` et `now − last_update > 2,5 × cycle_seconds + 60`, la règle actuelle) : barre de cycle pointillée ambre, présence « sans nouvelles », bandeau « Le bot ne donne plus de nouvelles ».
- **Connexion perdue** (échec du fetch) : `.page.is-frozen` (opacité .62, `saturate(.4)`), compte à rebours suspendu, point de fraîcheur creux, bandeau « Connexion au serveur perdue ». On continue d'essayer toutes les 15 s.
- **Aucune donnée** (`has_data = false`) : carte « Aucune donnée pour l'instant » avec la commande `tradeagent run --profile board` dans un `<pre>` (fond `--color-neutral-900`, monospace système), puis trois blocs tiretés qui esquissent Sous-agents, Portefeuille unique et Référence hold.
- **Palier prudent / défensif** : voile ambre ou orangé en haut de page (fondu croisé de 1,2 s) et glyphe triangle ou bouclier. Le palier défensif ajoute le bandeau « Palier défensif : achats bloqués ».

### Accueil (`hub.html`)
Même en-tête, sans pastilles de profils.
- Titre « Profils » en 26/500, sous-titre « 4 profils · actualisé il y a 6 s · lecture seule ».
- Carte « Le board face à la référence » : une ligne par profil, en grille `72px 1fr 92px`.
  - Barre de 18 px avec un trait central, la mise.
  - Un gain part à droite, en `--tone-gain`. Une perte part à gauche, en rouge hachuré `--tone-loss`.
  - Montant coloré avec son signe, puis la légende de lecture.
- Grille de cartes-liens (`auto-fill, minmax(min(270px, 100%), 1fr)`, gap 14). Chaque carte :
  - nom en 16/500, type d'agent en 12 px, état avec glyphe ;
  - equity en 24/500, « Net » coloré ;
  - courbe miniature en SVG de 96 × 32 ;
  - pied en 12 px (prochain cycle, cause de la mort ou de l'arrêt).
- Pied : « Démarrer ou arrêter un bot reste hors de cette page. »

### Connexion et création du compte (`login.html`, `setup.html`, `auth.css`)
- Fond `--color-bg` avec un halo accent à 8 % en haut à gauche.
- Colonne alignée à gauche, `max-width: 400px`, `margin: clamp(40px, 8vw, 120px) 20px 0 clamp(20px, 12vw, 160px)`, écart 22 px.
- Contenu dans l'ordre :
  - marque, puis le badge « Paper trading, argent fictif » ;
  - titre en 24/500 (« Connexion » ou « Créer le compte administrateur »). Pour la création, le texte d'explication actuel suit, en 13 px `--color-neutral-400` ;
  - `<p role="alert">` en `--tone-warn-ink`, hauteur minimale 20 px (le `{{message}}` du serveur) ;
  - champs `.field` + `.input`, hauteur minimale 44 px ;
  - `.btn.btn-primary.btn-block`, hauteur minimale 44 px : contour accent, **jamais rempli** ;
  - mention « Interface en lecture seule : rien ici n'agit sur un agent. »
- Les noms, `autocomplete` et `minlength` des champs restent ceux des fichiers actuels.

## Adaptatif (Electron)
La page s'adapte à **sa propre largeur**, pas à celle de l'écran : la fenêtre Electron se redimensionne librement.
- `app.js` observe `.page` avec un `ResizeObserver` et pose `data-mode` : `compact` sous 600 px, `moyen` de 600 à 1023, `large` de 1024 à 1499, `tres-large` à partir de 1500.
- Le CSS sélectionne `[data-mode="compact"]` pour la liaison verticale, les pastilles de profils masquées, 3 entrées de journal et des cibles de 44 px.
- `.page` porte `container-type: inline-size` : les `clamp(…cqi…)` gèrent les tailles intermédiaires sans saut.
- Contenu plafonné à 1 680 px et aligné à gauche. Fenêtre minimale conseillée : 360 × 560.
- Aucune largeur fixe sur un bloc qui contient du texte. Les montants restent sur une ligne (`white-space: nowrap` seulement sur la quantité).

## Mouvement
Déclenché seulement par des données réelles. `app.js` garde le snapshot précédent et compare :
- nouvelle décision = `journal[0].id` plus grand que le dernier vu ;
- chiffre changé = `money.equity` différent ;
- palier changé = `risk_tier` différent.

Pour rejouer une animation : retirer la classe, forcer la mise en page (`void el.offsetWidth`), remettre la classe.

| Animation | Déclencheur | Durée | Courbe / CSS | Mouvement réduit |
|---|---|---|---|---|
| Barre de cycle | chaque seconde, `--cycle` | 1 s continu | `width` linéaire | pas de 1 min, sans transition |
| Délibération | 30 dernières s avant le cycle | 1 s, boucle | opacité 1 → .35 | texte fixe |
| Halo de présence | sous-agent avec au moins une position | 3,2 s, boucle | scale 1 → 2,4, opacité .7 → 0, `cubic-bezier(.2,.6,.3,1)` | anneau fixe, scale 1,6, opacité .35 |
| Sortie | nouvelle décision « sell » | 0,9 s × 4 | chevron translateX 0 → 6 px, ease-in | chevron fixe et libellé |
| Ordre qui part | nouvelle décision buy ou sell | 1,6 s (1,4 s en vertical) | `left` 0 → 100 %, `cubic-bezier(.45,0,.2,1)` | pas de point |
| Ordre reçu | même évènement | 0,5 s, délai 0,9 s | translateY −8 px → 0, `cubic-bezier(.2,.7,.3,1)` | sans délai ni déplacement |
| Chiffre qui change | `money.equity` change | 2,2 s | halo accent 18 % → 0, ease-out | halo fixe jusqu'au snapshot suivant |
| Entrée du journal | nouvelle ligne ou compteur d'attente qui augmente | 2,6 s | trait de 2 px, opacité .95 → 0 | mention « nouveau » |
| Marge qui bouge | `stop_margin_pct` ou `drawdown_pct` change | 0,8 s | `width`, `cubic-bezier(.2,.7,.3,1)` | saut direct |
| Marge tendue | marge < 1,5 % et agent en vie | 0,4 s, boucle | translateY 0 / −1 px, linéaire | barre fixe ambre, mot « tendu » |
| Fraîcheur | chaque snapshot reçu | 1,6 s, une fois | scale 1 → 2,6, opacité .8 → 0 | point fixe |
| Ambiance de palier | `risk_tier` ou état change | 1,2 s | fondu croisé du voile `::before` | instantané |
| Figer | fetch en échec, mort, arrêté | 0,6 s | opacité .62, `saturate(.4)`, `animation-play-state: paused` | sans transition |

Les keyframes et les classes sont dans `tokens.css`.

## Données (`dashboard.build_snapshot`)
Tout ce qui est affiché vient du snapshot actuel, sauf les points ci-dessous. Chacun a un repli si on ne l'ajoute pas.

1. `journal[].outcome` (executed | reduced | refused | none), pour distinguer le tag « Réduit ». Repli : chercher « réduit » dans `verdict`.
2. `journal[].sleeve`, le sous-agent à l'origine de l'ordre. Repli : « board → portefeuille ».
3. `board[].sleeve_label`, le nom lisible fixé par le code. Repli : table de correspondance dans app.js (`trend` → « Suivi de tendance »).
4. `/api/profiles` : `[{name, agent, state, risk_tier, equity, net_result, last_update, series_7j}]`, pour les cartes de l'accueil. Repli : la liste actuelle, avec des liens sans chiffres.
5. `snapshot.reference` : `{profile, equity, net_result}`, pour la carte hold de la page d'un profil. Repli : masquer la carte.
6. `snapshot.cycles_life`, pour le numéro de cycle. Repli : `counts.decisions`.
7. `snapshot.risk_tier_since`. Repli : l'évènement de changement de palier, s'il est dans les 20 derniers.
8. Facultatif : `positions[].cost_basis` et `unrealized_pct`. Ce n'est pas affiché dans les maquettes.

## Jetons de design
Tous les jetons sont dans `tokens.css`. Les essentiels :
- **Couleurs** : fond `#161826`, surface `#232532`, texte `#e9e9ed`, accent `#9184d9`, accent texte `#d2cefd` (accent-300).
- **Neutres** : 300 `#cfd3e5`, 400 `#b2b6ca`, 500 `#9397ab`, 600 `#75798c`, 700 `#595d6c`, 800 `#3f424d`, 900 `#292b31`.
- **Tons d'état** : prudent `oklch(0.82 0.11 85)`, défensif `oklch(0.74 0.13 52)`, mort `oklch(0.70 0.14 22)`.
- **Résultats** : gain `oklch(0.80 0.14 155)`, perte `oklch(0.72 0.16 25)`.
- **Règles d'usage** : l'accent se porte en traits et en points, jamais en aplat. Pour un texte d'accent, prendre accent-300. Les filets isolés s'estompent sur 48 px à chaque bout :
  `linear-gradient(to right, transparent, var(--color-divider) 48px, var(--color-divider) calc(100% - 48px), transparent)`.
- **Typographie** : Inter 400, 500 et 600, poids maximal 500 sauf la marque. Chiffres toujours en `font-variant-numeric: tabular-nums`. Échelle : 48, 26, 24, 16, 15, 14, 13, 12, 11, 10. Surtitres en 13/500, 0.08em, capitales.
- **Espacements** : 2.8 / 5.6 / 8.4 / 11.2 / 16.8 / 22.4 px, gouttière 28 px.
- **Rayons** : 4 (pastilles), 6 (tags, rails), 8 (champs, entrées), 14 (cartes).
- **Ombres** : `--shadow-sm` `0 0 0 1px #3f424d` ; `--shadow-md` `0 0 0 1px #595d6c, 0 6px 18px rgba(0,0,0,.55)`.
- **États interactifs** : survol par teinte `color-mix(accent 12%)` (primaire) ou `color-mix(text 7%)` (secondaire) ; pressé à 22 % ou 14 % ; focus en contour 2 px accent, décalé de 2 px ; désactivé à opacité .45.

## Pilotage à venir (ne rien dessiner)
Des emplacements vides et nommés sont réservés :
- `header .slot-account`, avant le bouton de déconnexion ;
- `.portfolio .slot-controls`, en pied de la carte ;
- `.sleeve .slot-actions`, sous les positions de chaque sous-agent.

Aucun bouton ni lien n'y est placé aujourd'hui.

## Icônes
Les glyphes d'état (rond, triangle, bouclier, pause, croix) sont repris de la fonction `icon()` de l'`app.js` actuel, en SVG construit par le script. Aucune police d'icônes et aucun fichier externe.

## Captures de référence (`captures/`)
Les captures sont la cible visuelle. Le rendu de Claude Code doit leur être identique à la même largeur, avec les mêmes données (`fixtures/`). Elles ont été prises animations au repos : le point d'ordre en vol et les halos sont décrits dans la table du mouvement et visibles en direct dans `Le conseil - Ecrans.dc.html`.

| Fichier | Écran | Largeur | Données |
|---|---|---|---|
| 01-profil-bureau | Profil en vie, palier normal | 1280 | snapshot-vie-normal |
| 02-profil-tablette | idem | 834 | snapshot-vie-normal |
| 03-profil-telephone | idem | 390 (×2) | snapshot-vie-normal |
| 04 / 05 -decision-arrive | « Ordre reçu » juste après une décision | 1280 / 390 | vie-normal, avec une nouvelle ligne d'achat BTC/EUR 40,00 € réduit à 25,30 € |
| 06-palier-prudent-bureau | Palier prudent | 1280 | snapshot-vie-prudent |
| 07 / 08-palier-defensif | Palier défensif | 1280 / 390 | snapshot-vie-defensif |
| 09 / 10-etat-mort | Mort | 1280 / 390 | snapshot-mort |
| 11 / 12-etat-arrete | Arrêté | 1280 / 390 | snapshot-arrete |
| 13 / 14-etat-muet | Bot muet | 1280 / 390 | snapshot-muet |
| 15 / 16-etat-horsligne | Connexion perdue | 1280 / 390 | vie-normal, puis le serveur coupé (le fetch échoue) |
| 17 / 18-etat-vide | Aucune donnée | 1280 / 390 | snapshot-vide |
| 19 à 22-electron | Fenêtre Electron (sans fil d'Ariane ni déconnexion) | 480, 720, 1024, 1600 | vie-normal, vie-prudent, vie-normal, vie-defensif |
| 23 / 24-accueil | Accueil | 1280 / 390 | 4 profils : board +12,40 €, hold ±0,00 €, llm mort −376,40 €, demo arrêté +3,20 € |
| 25 à 28 | Connexion, création du compte (et erreur au téléphone) | 1280 / 390 | message « Identifiant ou mot de passe incorrect. » |
| 29-systeme-de-design | Planche du système | 1120 | - |

Pour régénérer les captures depuis les maquettes : ouvrir `maquettes/Captures.dc.html`. Chaque cadre porte un id (`profil-bureau`, `etat-mort-telephone`…) à la largeur exacte.

## Données de test (`fixtures/`)
Un `snapshot-*.json` par état, au format de `build_snapshot`, avec exactement les valeurs des maquettes (equity 1 012,40 €, BTC 0,0021 à 58 420 €, sortie 55 300 € à +5,6 %, etc.).
- Heure de référence : lundi 5 octobre 2026, 18:37:21, heure de Paris. La page doit afficher « Prochain conseil dans 07:39 » au chargement.
- Les colonnes des lignes `journal` (id, ts, agent, action, symbol, amount_quote, approved, verdict, reasoning) sont celles qu'utilise `app.js`. Vérifie-les contre `storage.py` et ajuste la fixture si une colonne diffère.
- Pour comparer : servir une fixture à la place de `/api/snapshot` (petit serveur de test, ou option de développement de `web.py` hors production), avec le navigateur réglé sur le fuseau Europe/Paris et l'horloge figée à `generated_at`.

## Fichiers
- `README.md` : ce document.
- `PROMPT.md` : le prompt à donner à Claude Code.
- `tokens.css` : jetons, keyframes et classes d'animation, à fusionner dans `static/app.css`.
- `captures/` : 29 captures de référence (voir le tableau ci-dessus).
- `fixtures/` : 7 snapshots JSON pour reproduire chaque capture.
- `maquettes/Captures.dc.html` : les cadres d'où viennent les captures.
- `maquettes/Le conseil - Ecrans.dc.html` : planche de tous les écrans et états, y compris les fenêtres Electron (une fenêtre se redimensionne à la main).
- `maquettes/Le conseil - Systeme.dc.html` : système de design, table du mouvement, données manquantes.
- `maquettes/Profil Conseil.dc.html` : la page d'un profil. Les textes exacts et la logique d'affichage sont dans `renderVals()`.
- `maquettes/Profil actuel (reference).dc.html` : recréation de la page actuelle, pour comparer.
- `maquettes/support.js` et `maquettes/_ds/` : servent seulement à ouvrir les maquettes. Ne pas les intégrer au projet.
