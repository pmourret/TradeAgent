# Feuille de route : optimisation des outils financiers et de l'architecture

Écrite le 2026-10-06 à la demande de Pierre, après une relecture complète du dépôt (modèles, témoin, board, garde-fous, backtest, moteur, stockage). Elle est destinée à **Claude Code (Opus 5.5)**, qui l'exécute phase par phase. Elle complète `roadmap.md` (qui garde l'historique et les axes A à G) et ne remplace aucune décision de `decisions.md`. Les points qui touchent un invariant ou de l'argent sont marqués **[décision Pierre]** : on s'arrête et on demande avant de coder.

## 0. Comment lire et exécuter cette feuille de route

### 0.1 Le diagnostic en quatre lignes

1. Les outils d'analyse sont sains mais minces : une seule logique décide (suivi de tendance 7/30 jours, stop à deux écarts-types, taille à 1 % de risque). RSI, ATR, fourchettes et volume sont calculés mais ne servent qu'au LLM, sorti de la décision.
2. Le frein à la rentabilité n'est pas le manque d'indicateurs. Ce sont les frais à 50 € de mise et l'absence de données hors échantillon : les treize mois disponibles ont tous été vus, tout réglage de plus sur eux est du surapprentissage.
3. Le témoin `quant` est gagnant **brut** sur le marché à −40 % du contrôle (+0,82 € de trading) et finit sous `hold` à cause des frais (1,25 €). Sur les sept mois de hausse, les frais mangent 28 % du brut (3,02 € sur 10,97 €). Le premier euro à gagner est un euro de frais.
4. L'architecture est bonne (protocoles, moteur unique, état en base, backtest sur le vrai moteur). Ce qui manque pour optimiser sans se tromper : un registre d'expériences, des métriques par ordre, un rejeu fidèle à la boucle réelle (prix toutes les 15 minutes, bougies horaires), et des variantes déclarées plutôt que des constantes modifiées à la main.

### 0.2 Ordre d'exécution et règle d'arrêt

Phases dans l'ordre : **P0 (mesure) → P1 (frais) → P2 (sortie et horizon) → P3 (board) → P4 (disponibilité et architecture) → P5 (LLM hors décision)**. P4 peut avancer en parallèle de P1 à P3 car elle ne touche pas aux modèles. Chaque étape est livrable seule ; on ne commence pas l'étape suivante tant que la précédente n'est pas « finie » au sens de `CLAUDE.md` (code, tests, doc, deux suites au vert, mutation et relecture des invariants pour le code de sécurité).

Rien ne passe en direct, même en paper, sans être passé par le protocole d'expérimentation ci-dessous.

### 0.3 Protocole d'expérimentation (obligatoire pour tout changement de modèle)

C'est la règle qui protège contre le surapprentissage. Elle vaut pour chaque variante de P1, P2 et P3.

1. **Déclarer avant de lancer** : nom de la variante, paramètres, hypothèse (« réduit les rachats après stop, donc les frais »), métriques regardées, critère de réussite et d'échec écrits dans le registre (P0.1) **avant** le premier backtest sur la période de contrôle.
2. **Trois jeux de périodes** (bornes à confirmer en P0.3 d'après la profondeur de l'historique de Bitvavo) :
   - **développement** : du début de l'historique disponible à `2024-06-30`. On y regarde tout ce qu'on veut.
   - **contrôle** : `2024-07-01` à `2025-09-08`. Une variante n'y est rejouée **qu'une fois**, après déclaration. Pas de réglage dessus, jamais.
   - **déjà vu** : `2025-09-09` à `2026-10-04` (les treize mois déjà regardés). Ils servent de contrôle de cohérence, pas de preuve.
   - **hors-échantillon réel** : le paper en direct sur le serveur. C'est le seul juge final.
3. **Une variante à la fois**, un seul paramètre changé par rapport à la variante de référence. Pas de grille de paramètres.
4. **Le témoin ne bouge pas** : `QuantAgent` (`strategies.py`) et `trend-v1` (le `TrendSleeve` actuel, figé sous ce nom en P1.1) restent la référence à battre, avec `hold` et `buyhold`.
5. **Critère de promotion** d'une variante : sur développement **et** contrôle, net supérieur à la référence, frais par euro de brut inférieurs ou égaux, pire mois pas plus profond de plus de 1 point, nombre d'ordres pas supérieur de plus de 20 %. Une variante promue devient un profil paper en direct (`profiles.py`) à côté du board actuel, jamais à sa place, pendant au moins 30 jours.
6. **Tout résultat est enregistré** dans le registre, même mauvais, avec le commit, la config, la période et le jeu (développement, contrôle, déjà vu).

### 0.4 Définition de « fini » pour une étape

Celle de `CLAUDE.md`, plus : la ligne du registre d'expériences quand il y a eu un backtest, la mise à jour de `etat.md` (ce qui a tourné pour de vrai, ce qui n'a pas été vérifié) et une entrée dans `decisions.md` si Pierre a tranché quelque chose. Le rapport de fin d'étape à Pierre tient en dix lignes : ce qui a changé, le tableau de mesure, ce qui n'a pas été vérifié.

---

## P0. Socle de mesure (avant de toucher un modèle)

But : pouvoir dire, chiffres à l'appui et sans se mentir, si un changement rapporte. Tout est gratuit, hors réseau sauf le téléchargement d'historique.

### P0.1 Registre d'expériences

- Nouveau module `src/tradeagent/experiments.py` : `record_run(path, entry)` ajoute une ligne JSON à `data/experiments.jsonl` ; `ExperimentEntry` (dataclass gelée) : date, commit git (`git rev-parse HEAD`, « inconnu » hors dépôt), empreinte de `config.yaml`, agents, variantes et leurs paramètres, périodes, jeu (`dev`, `control`, `seen`, `custom`), `--tag` libre, résultats (une ligne par agent et par fenêtre, reprise de `BacktestResult`), et le texte de l'hypothèse quand `--hypothesis` est donné.
- `tradeagent backtest` : options `--tag`, `--hypothesis`, `--set dev|control|seen` ; sans `--set`, le jeu est déduit des dates (bornes de 0.3 codées dans `experiments.py`, pas dans la config). Un backtest sur `control` sans `--hypothesis` est **refusé** : c'est la règle 1 du protocole, appliquée par le code.
- `tradeagent experiments` : liste les lignes, filtrable par tag et par jeu ; un tableau par variante avec net, frais, pire mois, ordres sur chaque jeu.
- Tests : écriture et relecture, refus sans hypothèse, déduction du jeu. Pas de réseau.
- Doc : README (section backtest), `architecture.md` (nouvelle ligne de table), `config.yaml` inchangé.

### P0.2 Métriques par ordre et cumul avec réinvestissement

- Nouveau module `src/tradeagent/trades.py` : `round_trips(fills) -> list[RoundTrip]`, appariement FIFO par symbole des exécutions (`storage.fills_since(0)`), chaque aller-retour avec entrée, sortie, quantité, brut, frais, net, durée, et la raison de sortie lue dans `decisions` (stop, régime, orpheline, mort). Fonction pure, testée sur des listes d'exécutions fabriquées.
- `BacktestResult` reçoit : `gross_pnl` (net + frais + loyer), `round_trips`, `win_rate`, `avg_win`, `avg_loss`, `expectancy` (gain moyen par aller-retour, net de frais), `exposure_pct` (part du temps avec une position, d'après `equity` et `cash`), `fees_per_gross` (frais sur brut, en %), `reentries_24h` (achats dans les 24 h qui suivent une vente du même symbole : c'est la fuite visée par P1.2).
- `format_summary` ajoute trois colonnes : brut, frais/brut, allers-retours, et une ligne par agent « gagnants / perdants, gain moyen / perte moyenne, espérance par ordre ».
- Option `--reinvest` : les fenêtres de `--months` s'enchaînent **sans** repartir de la mise (une seule base par agent sur toute la période, soldes et plus-haut conservés). Montre la croissance composée, le drawdown réel et la mort. Le tableau dit lequel des deux modes est affiché. La règle de mort de `decisions.md` (déficitaire à 90 jours contre `buyhold`) n'est pas codée ici : elle reste un chantier à part (invariant 2, voir P4.1).
- Tests : chaque métrique sur un cas construit à la main ; `--reinvest` contre un enchaînement manuel de deux fenêtres.

### P0.3 Historique sur plusieurs années

- `replay.load_history` et `cli` : option `--start AAAA-MM-JJ` (en plus de `--days`/`--end`), et `--years N`. Vérifier d'abord, par le sous-agent `doc-externe` puis par un vrai téléchargement sur le PC de Pierre, **jusqu'où remonte l'historique horaire BTC/EUR et ETH/EUR de Bitvavo** et la taille maximale d'une page `fetch_ohlcv` (à vérifier, jamais relevé). Fixer alors les bornes de 0.3 du protocole et les écrire dans `experiments.py` et dans ce fichier.
- Cache `data/history/` : un fichier JSON par paire et par timeframe suffit pour cinq ans de bougies horaires (ordre de grandeur : 45 000 lignes). Pas de nouvelle dépendance. `check_coverage` garde sa tolérance de six bougies ; un trou plus long dans un historique ancien est signalé avec sa date, et la période est à choisir autour.
- Lancer **une fois** la référence (`hold,buyhold,quant,board`) sur tout le jeu de développement, par fenêtres de 30 jours et en `--reinvest`, l'enregistrer dans le registre avec le tag `baseline`. C'est le repère de toutes les phases suivantes. Ne pas lancer le contrôle à ce stade.

### P0.4 Rejeu fidèle à la boucle réelle : prix toutes les 15 minutes, bougies horaires

En direct, le moteur lit un prix toutes les 15 minutes (`cycle_seconds: 900`) et des bougies horaires ; le stop est donc vu quatre fois par heure. En rejeu, le prix est la clôture horaire : le stop n'est vu qu'une fois par heure. Les deux bots ne font pas la même chose, et le backtest sous-estime les sorties.

- `ReplayPriceFeed` accepte un second historique, plus fin, pour les cotations : `ReplayPriceFeed(history_1h, timeframe="1h", clock, quotes=history_15m)`. `get_quote` lit la dernière clôture **terminée** du flux fin ; `get_candles` reste sur l'horaire. Même règle qu'aujourd'hui : jamais une bougie en cours.
- `load_history` télécharge les deux timeframes (vérifier que Bitvavo sert du 15 minutes par ccxt, à vérifier) ; `--quotes-timeframe 15m` par défaut, `1h` pour retrouver l'ancien comportement et comparer.
- Mesure : rejouer `baseline` avec et sans cotations fines ; enregistrer les deux. L'écart est la part du résultat qui tenait à la cadence du rejeu.
- Le glissement reste fixe (5 points de base). P0.5 dit s'il est réaliste.

### P0.5 Mesure du spread réel (une fois, sur le PC de Pierre)

- `tools/spread_probe.py` (hors du paquet, comme `tools/frontcheck/`) : lit `fetch_order_book` des deux paires toutes les minutes pendant une heure, écrit le spread relatif médian et au 95e centile, et la profondeur au meilleur prix. Sans clé.
- Si le demi-spread médian dépasse 5 points de base, `slippage_bps` est relevé dans `config.yaml` avec la source et la date en commentaire, et `baseline` est rejoué. Un paper plus pessimiste que le réel vaut mieux que l'inverse.

---

## P1. Les fuites de frais

But : moins d'ordres pour le même signal. Les frais sont le seul poste que l'on maîtrise à coup sûr.

### P1.1 Variantes déclarées : paramètres figés dans le code, choisies par leur nom

Aujourd'hui les réglages sont des constantes de module dans `signals.py` et la logique du témoin est dupliquée entre `QuantAgent` et `TrendSleeve`. Pour tester des variantes sans toucher au témoin ni multiplier les clés de config :

- `signals.py` : `TrendParams` (dataclass gelée : `fast_days`, `slow_days`, `deadband_pct`, `confirm_candles`, `exit_sigmas`, `exit_min_pct`, `exit_max_pct`, `risk_per_trade_pct`, `reentry_cooldown_hours`, `exit_mode`) ; `trend_regime`, `volatility_forecast`, `risk_sizing` reçoivent `params` avec, par défaut, les valeurs actuelles. `compute_models(candles, timeframe, params=DEFAULT)`.
- `board.py` : `TrendSleeve(state, params)` ; `VARIANTS: dict[str, TrendParams]` dans un nouveau module `variants.py` ; `trend-v1` = les valeurs d'aujourd'hui, à l'identique. Un test vérifie que `board` avec `trend-v1` rend, décision par décision, ce que rend le board actuel sur l'historique synthétique (même graine).
- Config : nouvelle section `board:` avec `sleeves: [trend-v1]` (liste de noms, validée strictement contre `VARIANTS`, erreur de démarrage sinon), commentée dans `config.yaml` et documentée dans le README. Le profil `board` lit cette liste. Backtest : `--agents board:trend-v2` choisit une variante sans toucher à la config ; `--agents board` lit la config.
- `MarketView.market[symbol]["models"]` reste calculé avec `DEFAULT` pour tous les agents (le LLM et le témoin voient ce qu'ils voyaient) ; un sleeve avec d'autres paramètres recalcule ses signaux depuis les bougies. Pour cela, `MarketView` porte les bougies terminées (`candles` par symbole) : ajout d'un champ, lecture seule, rien de nouveau n'est exposé à l'agent (il les recevait déjà sous forme de résumé). **[décision Pierre]** : exposer les bougies brutes à l'agent LLM ferait grossir le prompt ; le `LLMAgent` les retire avant d'écrire le prompt (comme `REDUNDANT_WITH_MODELS`), test à l'appui.
- `QuantAgent` : inchangé (témoin). Ses paramètres d'exposition hérités du superviseur restent, documentés comme inutilisés.

### P1.2 Rachat après stop

Constat dans `TrendSleeve.update` : après une vente sur stop, `leaving` empêche l'entrée ce cycle-ci seulement ; au cycle suivant, 15 minutes plus tard, si le régime est encore « up », le sleeve rachète et paie un aller-retour complet pour rien.

- Variante `trend-v2` : `reentry_cooldown_hours = 24` ; après une sortie sur stop, pas de nouvelle entrée sur ce symbole avant ce délai **et** tant que le prix n'a pas repassé le niveau du stop touché. L'état du sleeve garde `{symbol: {exited_at, exit_level}}` (nettoyé par `clean_state`). Une sortie sur régime « down » n'a pas de délai : la réentrée exige déjà un régime « up ».
- Hypothèse écrite d'avance : `reentries_24h` baisse d'au moins moitié, frais/brut baissent, net ne baisse pas. Mesure sur développement, puis une fois sur contrôle.

### P1.3 Hystérésis du régime

La bande morte de 0,1 % dans `_vote` est du bruit à l'échelle de BTC ; le score peut osciller entre 0,25 et 0,5 et faire alterner « up » et « range ».

- Variante `trend-v3` (à partir de la meilleure de v1/v2) : `confirm_candles = 2`, un changement de régime n'est pris en compte qu'après deux clôtures horaires de suite dans le nouveau régime ; `deadband_pct = 0.5`. Deux paramètres, donc deux variantes (`v3a`, `v3b`), pas une grille.
- Hypothèse : moins d'ordres, net égal ou supérieur.

### P1.4 Ordres limites : mesurer le gain possible avant de concevoir

Les frais maker de Bitvavo sont inférieurs aux frais taker (grille à vérifier par `doc-externe`, source officielle et date). À 10 à 20 € de notionnel sur BTC/EUR, un ordre limite au meilleur prix s'exécute presque toujours.

- Mesure gratuite d'abord : rejouer `baseline` et la meilleure variante de P1 avec `costs.fee_rate` au taux maker. L'écart borne le gain d'un passage aux ordres limites. Enregistrer.
- Si l'écart dépasse 10 % du net, ouvrir la conception **[décision Pierre]** : `OrderRequest.kind` (`market`, `limit`), ordre limite posté au dernier prix avec expiration d'un cycle, repli en ordre au marché pour une **vente** (sortir reste toujours possible, invariant 4) et abandon pour un achat ; en paper, exécution si la bougie suivante traverse le prix, sinon annulation. Cela touche `engine.py` (un ordre en attente entre deux cycles) et la décision « ordres au marché » de `decisions.md` : rien ne se code avant l'accord de Pierre, et la quarantaine de l'étape 1 de `roadmap.md` devra le prendre en compte.

---

## P2. La sortie et l'horizon

But : garder les positions gagnantes plus longtemps et payer moins d'allers-retours, sans aggraver le pire mois.

### P2.1 Sortie « chandelier »

Aujourd'hui la distance de sortie est recalculée à chaque cycle sur la volatilité du moment : quand le marché se calme, le stop remonte à 2 % sous le prix et une simple respiration sort la position (le contrôle de `quant` rate le rebond ; le prompt version 6 sortait sur des replis).

- `signals.py` : `atr(candles, period)` déjà présent dans `market.py` sous forme de pourcentage ; le déplacer dans `signals.py` et le réutiliser. `exit_mode = "chandelier"` : niveau de sortie = plus-haut des clôtures depuis l'entrée moins `k × ATR` sur 22 bougies journalières (`k = 3`, valeur classique de LeBeau, pas réglée). Le niveau ne descend jamais. Les bougies journalières viennent d'un rééchantillonnage des horaires (P2.2).
- Variante `trend-v4` = meilleure de P1 + `exit_mode = chandelier`. Hypothèse : durée moyenne des allers-retours gagnants en hausse, `win_rate` en baisse acceptée, espérance par ordre en hausse, pire mois pas plus profond de plus de 1 point.

### P2.2 Rééchantillonnage et décision journalière

- `models.py` ou `signals.py` : `resample(candles, "1h" -> "1d")`, fonction pure (jour UTC, ouverture de la première, plus-haut, plus-bas, clôture de la dernière, volumes additionnés ; un jour incomplet n'est pas une bougie terminée). Testée sur des cas construits. Aucun téléchargement de plus.
- Variante `trend-daily-v1` : mêmes règles que `trend-v1` mais régime et taille calculés sur bougies journalières (moyennes 7 et 30 **jours**, donc 7 et 30 bougies), décision d'entrée et de sortie sur régime uniquement à la clôture journalière ; le stop reste surveillé à chaque cycle de 15 minutes. Hypothèse : moitié moins d'ordres, net comparable, pire mois comparable.
- Prérequis : P1.1 (les bougies dans `MarketView`).

### P2.3 Exposition : risque par ordre et renfort

Desserrer les tailles a déjà rapporté (+16,6 % contre +9,9 % en somme sur les sept mois) pour un pire mois à peine plus profond. `quant` capte la moitié des gains de `buyhold` avec des positions d'environ 12 € sur 50 €.

- Variante `trend-v5` : `risk_per_trade_pct = 2.0`. Variante `trend-v6` : un **seul** renfort, de la taille initiale, quand la position gagne plus d'une distance de sortie et que le régime tient ; le stop de l'ensemble passe au niveau du renfort. Deux variantes séparées.
- Critère : net en hausse, pire mois et drawdown max sous les seuils de `cautious` (15 %) sur toute la période de développement en `--reinvest` ; sinon la variante est écartée, quel que soit le net.
- Rappel honnête à écrire dans le rapport : au comptant, aucune de ces variantes ne gagne dans une baisse ; elles ne jouent qu'en hausse.

---

## P3. Le board à plusieurs sièges

But : des sources de gain qui ne perdent pas en même temps. L'ordre décidé par Pierre (`decisions.md`) tient : rien, puis force relative BTC/ETH, puis univers élargi. Cette feuille de route y ajoute, **avant** la force relative, un siège qui diversifie l'horizon plutôt que l'actif, car c'est là que la littérature trouve quelque chose sur BTC et ETH (momentum à horizon de une à quatre semaines ; citations à faire vérifier par `doc-externe` avant de l'écrire dans `decisions.md`). **[décision Pierre]** sur cet ordre.

### P3.1 Siège momentum à horizon long

- `board.py` : `MomentumSleeve(state, params)` sur bougies journalières : entrée si le rendement sur `lookback_days = 90` est positif **et** le prix au-dessus de sa moyenne 90 jours ; sortie à l'inverse ; stop chandelier `k = 3` ; taille par le même modèle de risque. Un seul jeu de paramètres, déclaré (`momentum-v1`).
- `SLEEVE_NAMES` et `clean_state` deviennent génériques : un sleeve inconnu dans l'état est ignoré et journalisé, jamais une erreur de démarrage.
- Config : `board.sleeves: [trend-v1, momentum-v1]`. Backtest : `--agents board:trend-v1+momentum-v1`.
- Mesure du levier « répartition » (étape 3 de `roadmap.md`) : rejouer `trend` seul, `momentum` seul, les deux ; comparer la somme au mélange, et le mélange à la meilleure répartition après coup (coefficients 0,25 / 0,5 / 0,75). Si le mélange à parts égales capte moins de 70 % de la meilleure répartition après coup, un répartiteur a de la valeur ; sinon on reste à parts égales et la question du LLM au sommet est close pour longtemps.

### P3.2 Plafond de volatilité du portefeuille

- `BoardAgent` : coefficient par sleeve (prévu, pas codé) et un facteur global `min(1, target_daily_vol / realized_daily_vol)` appliqué à la somme des cibles, où la volatilité réalisée vient de `volatility_forecast` pondérée par les positions. `target_daily_vol` fixé à 2 % (valeur classique, pas réglée), dans `variants.py`, pas dans la config. Le facteur ne fait **que réduire** ; il n'augmente jamais une cible au-dessus de ce que demandent les sleeves. Les retailles ne partent que si l'écart vaut un ordre, et au plus une fois par jour, pour ne pas payer de frais sur du bruit.
- Hypothèse : pire mois moins profond pour un net proche. Mesure avec et sans.

### P3.3 Force relative BTC/ETH, puis univers élargi

Comme décidé. Une seule variante déclarée chacune. Pour l'univers élargi : vérifier l'existence des paires en EUR et leurs minimums sur Bitvavo (à vérifier), et garder `max_total_exposure_pct` tel quel : plus de symboles ne veut pas dire plus de risque total.

---

## P4. Disponibilité 24 h sur 24 et architecture logicielle

But : que le bot du serveur ne s'arrête jamais en silence, et que le code reste sûr et vérifiable pendant les phases précédentes. P4 ne touche pas aux modèles ; elle peut avancer en parallèle.

### P4.1 Une panne de flux ne doit pas arrêter le bot pour de bon **[décision Pierre, invariant 2]**

Constat : une erreur de `snapshot` (prix indisponible) compte comme une erreur de cycle ; dix d'affilée, soit 2 h 30 à 15 minutes par cycle, mettent le bot en `halted`, et seul `tradeagent resume` en ligne de commande le relance. Sur un serveur, une coupure réseau d'une nuit arrête le bot jusqu'à l'intervention de Pierre, à l'opposé du cap 24 h sur 24.

- Ce qui ne touche pas l'invariant, à faire d'abord : dans `CcxtPriceFeed`, trois tentatives avec attente croissante (injectée, pas de `sleep` réel en test) avant de lever `FeedError` ; dans `Engine._market_summary`, garder les dernières bougies terminées en mémoire et ne redemander que ce qui manque (moins d'appels, moins d'occasions d'erreur).
- Ce qui touche l'invariant, à proposer à Pierre, pas à coder sans lui : distinguer les erreurs **sans action possible** (flux de prix, ni ordre ni agent en cause) des erreurs **d'exécution** (ordre, agent). Les premières seraient comptées par durée (par exemple : `halted` seulement après 6 h sans aucun prix) et journalisées comme avertissements ; les secondes garderaient le compteur à 10. Un `halted` resterait réversible uniquement par `resume`. À relire par `relecteur-invariants` et à tester par mutation.
- Notifications sortantes (D1 de `roadmap.md`) : obligatoires pour un bot 24 h sur 24. `ntfy` par simple requête HTTP, adresse dans l'environnement du serveur, jamais dans la config ni la base ; sortantes uniquement ; événements : mort, `halted`, palier, aucun cycle depuis plus de 1 h, erreur répétée, résumé quotidien. Fonction pure pour décider quoi envoyer (comme `notifier.js`), transport minimal, tests sans réseau.

### P4.2 Intégration continue et linter (D5, D6)

- `.github/workflows/ci.yml` : pytest sur Ubuntu et Windows, Python 3.10 et 3.12 ; `node --test` dans `desktop/` ; `ruff check` avec une configuration minimale dans `pyproject.toml` (erreurs et imports seulement, pas de style qui ferait réécrire le dépôt). Un test échoue si U+2014 revient dans `src/` ou `desktop/` (E2 de `roadmap.md`).
- Aucun secret dans la CI ; les tests n'ont pas de réseau (invariant 12).

### P4.3 Sauvegarde des bases et santé

- `tradeagent backup --profile X` : copie cohérente par l'API de sauvegarde de SQLite dans `data/backups/`, rotation à 14 fichiers ; service `backup` dans `compose.yaml` lancé une fois par jour. `tradeagent health` vérifie aussi l'âge du dernier cycle (`equity` la plus récente) et répond en erreur au-delà de deux cycles.

### P4.4 Montants en `Decimal` avant le réel (étape 1.c de `roadmap.md`)

- Introduire `Decimal` d'abord dans `guardrails.py` et `paper.py` (quantités et montants), derrière les mêmes tests, sans changer les résultats en paper à 1e-6 près. Préparer le terrain du vrai adaptateur, qui ne doit pas faire ce travail en même temps que le réseau et l'idempotence.

### P4.5 Dette identifiée, à régler au fil des phases

- `QuantAgent.trade` porte des paramètres du superviseur retiré : les documenter comme figés avec le témoin, ne pas les supprimer (toute retouche du témoin invalide les comparaisons passées).
- `market.py` et `signals.py` calculent deux fois la tendance (contre moyennes mobiles, et régime) : après P1.1, `market.indicators` lit le régime de `signals` au lieu de recalculer, pour que le LLM et les sleeves voient exactement la même chose.
- `max_price_age_s` repose sur l'horodatage du ticker ; vérifier sur le PC de Pierre que Bitvavo en renvoie un (sinon l'âge est toujours nul et le garde-fou ne joue pas). À noter dans `pieges.md`.
- Le registre d'expériences (P0.1) rend inutile le report à la main des résultats dans `etat.md` : `etat.md` ne garde que la synthèse et le tag du registre.

### P4.6 Fiscalité et net réel (information, pas un conseil)

Un net « réel » passe par l'impôt : en France, les cessions de crypto contre euros sont imposables au-delà d'un seuil annuel de cessions (montant et règles à vérifier sur une source officielle ; le projet ne donne pas de conseil fiscal). À 100 ordres de 12 € par an, le seuil est dépassé. L'export CSV des exécutions (D3 de `roadmap.md`) devient un prérequis du réel, à coder avec `trades.py` de P0.2.

---

## P5. Le LLM hors de la boucle de décision

Deux essais sur sept mois ont montré qu'un LLM n'apporte rien par-dessus les modèles et coûte un loyer. Il garde deux usages, à coût borné :

- **Compte rendu hebdomadaire** (`tradeagent report --profile X`, puis service `report` dans `compose.yaml`) : le code fabrique un bilan chiffré (allers-retours de la semaine, frais, drawdown, décisions des sleeves) et le LLM le met en français lisible, une fois par semaine, sous `daily_budget_eur`. Le texte est écrit dans `events` et envoyé par la notification du P4.1. Aucune décision, aucun ordre. Le profil `llm` reste disponible pour une expérience déclarée, jamais par défaut.
- **Répartiteur au sommet du board** : seulement si la mesure de P3.1 montre que la répartition vaut quelque chose, et alors comme décidé : règle codée d'abord, LLM local ensuite, jugé par le même protocole.

---

## Points qui demandent une décision de Pierre avant tout code

| Point | Phase | Ce qui est en jeu |
|---|---|---|
| Bornes des périodes de développement et de contrôle | P0.3 | Fixées une fois, jamais rouvertes |
| Bougies brutes dans `MarketView` (retirées du prompt du LLM) | P1.1 | Taille du prompt, invariant 9 inchangé |
| Ordres limites et repli au marché pour les ventes | P1.4 | Décision « ordres au marché », `engine.py`, quarantaine future |
| Siège momentum avant la force relative BTC/ETH | P3.1 | Ordre décidé le 2026-10-05 |
| Erreurs de flux comptées par durée, pas par cycle | P4.1 | Invariant 2 |
| Relever la mise en paper à 200 € sur un profil d'essai | transverse | Dit ce qui tient au minimum de 5 € par ordre et ce qui est structurel ; la mise réelle maximale de 50 € n'est pas rouverte |

## Ce que cette feuille de route ne promet pas

Un backtest, même sur cinq ans, reste une trajectoire passée, exécutée de façon optimiste. Avec 50 € et 0,6 % l'aller-retour, le meilleur résultat observé est +8 € sur sept mois de hausse ; aucun modèle ne change cet ordre de grandeur, seuls les frais, l'exposition et le nombre d'actifs le font bouger. Rien de ce qui est mesuré ici n'est une promesse de gain, et rien ne passe en réel sans l'étape 1 de `roadmap.md` (quarantaine, adaptateur réel, `Decimal`, filets).
