# tradeagent

Agent de trading crypto piloté par un LLM, en **paper trading** (argent fictif) pour l'instant.
Principe : **l'IA propose, le code dispose**. L'agent ne parle jamais à l'exchange : chaque décision traverse des garde-fous en dur, et un kill switch indépendant le tue quand la mise est perdue. L'agent paie aussi son « loyer » : chaque appel au LLM coûte de l'argent réel, compté dans son résultat net.

> Projet d'expérimentation, pas un conseil financier. Rien ici ne prouve qu'un agent IA batte le marché une fois les frais **et** l'API payés. Avec une mise de 50 €, l'API seule peut représenter plusieurs euros par mois. Ne mets que de l'argent que tu acceptes de perdre à 100 %, et seulement après plusieurs semaines de paper trading.

## Installation

```bash
scripts/setup.sh                   # Windows : scripts\setup.bat
```

Crée l'environnement `.venv`, installe les dépendances, copie `.env.example` en `.env`, puis lance les tests (~450 tests, ~8 s, aucun accès réseau) pour vérifier que tout marche. Il faut Python 3.10 ou plus. À la main : `python -m venv .venv`, activer, `pip install -e ".[dev]"`, `pytest`.

## Lancer

Tout ce qui suit est du **paper trading** : l'argent est fictif. Le seul coût réel est l'API Anthropic du profil `llm`.

| Script (Windows : `.bat`) | Ce qu'il fait |
|---|---|
| `scripts/start.sh [profil…]` | **Le plus pratique** : bot(s) + interface web dans un seul terminal, `Ctrl+C` arrête tout. `start.sh hold llm` lance les deux côte à côte pour les comparer |
| `scripts/paper.sh [profil]` | Le bot seul, au premier plan |
| `scripts/ui.sh [profil]` | L'interface web seule (pour regarder un bot lancé ailleurs) |
| `scripts/status.sh [profil]` | L'état de tous les profils qui ont déjà tourné : equity, **résultat net après coût de l'API**, soldes |
| `scripts/live.sh` | **Mode réel : refuse**, voir plus bas |
| `scripts/setup.sh` | Installation, une fois |

| Profil | Prix | Agent | Coût | Port de l'interface |
|---|---|---|---|---|
| `hold` (défaut) | réels (publics, sans clé) | ne fait rien : **la référence à battre** | aucun | 8765 |
| `llm` | réels | Claude (Anthropic) | l'API, plafonnée par le code (0,25 €/jour, 10 € au total par défaut) | 8766 |
| `demo` | simulés, hors ligne | aléatoire, un cycle toutes les 2 s | aucun | 8767 |

Chaque profil a sa propre vie : sa base (`data/paper-<profil>.db`), son argent fictif, son kill switch, son journal. Ils ne se mélangent jamais, ce qui permet de lancer `hold` et `llm` en même temps. Un seul bot peut tourner par profil : un second est refusé avec un message clair.

Pour essayer tout de suite, sans clé ni réseau : `scripts/start.sh demo`. Pour le vrai LLM, mets ta clé dans `.env` (voir plus bas) puis `scripts/start.sh hold llm`.

### Le mode réel n'existe pas encore

`scripts/live.sh` (ou `tradeagent run --profile live`) refuse, volontairement : le code ne sait pas passer d'ordre sur un vrai compte, et `config.yaml` rejette `mode: live`. Ce n'est pas un oubli, c'est la dernière étape du plan (voir « Chemin vers l'argent réel ») : confirmation manuelle des ordres, adaptateur d'exchange authentifié testé contre un faux exchange local, sous-compte dédié, clés sans droit de retrait.

## Utilisation

Les scripts ci-dessus appellent la commande `tradeagent`, que tu peux utiliser directement (`python -m tradeagent` marche aussi) :

```bash
# 1. Démo hors ligne, sans clé ni réseau : agent LLM simulé + prix synthétiques
tradeagent run --agent llm-fake --feed synthetic --seed 3 --cycle-seconds 0 --max-cycles 20

# 2. Valider le flux de prix réel (prix publics, aucune clé) ; l'agent "hold" ne fait rien
tradeagent run --profile hold --max-cycles 1

# 3. Paper trading avec le vrai LLM (clé API nécessaire, voir plus bas)
tradeagent run --profile llm

tradeagent up hold llm       # bots + interfaces ensemble (ce que fait scripts/start.sh)
tradeagent web --profile llm --open   # interface web (lecture seule) seule
tradeagent status --all      # état de tous les profils ; ou --profile llm
tradeagent resume --profile llm       # relance un bot "halted" après avoir regardé pourquoi
tradeagent reset --profile llm --yes  # nouvelle vie : repart de la mise de départ (le journal est gardé)
```

`tradeagent run --stop-on-stdin` sert aux programmes qui lancent le bot (l'application de bureau) : le bot s'arrête proprement, à la fin de son cycle, quand son entrée standard reçoit `stop` ou se ferme. C'est l'équivalent de `Ctrl+C` là où il n'y a pas de terminal, notamment sous Windows. Rien d'autre n'est lu sur l'entrée standard.

Sans `--profile`, les commandes utilisent la base de `config.yaml` (`data/agent.db`), comme avant. `--agent` et `--feed` restent prioritaires sur le profil.

Agents : `hold` (ne fait rien, **la référence à battre**), `chaos` (aléatoire, demande aussi des choses absurdes, pour tester la plomberie), `llm-fake` (sorties simulées, coût simulé, pour tester le circuit LLM hors ligne) et `llm` (Anthropic).

### Clé API

`scripts/setup.sh` a déjà copié `.env.example` en `.env` (sinon fais-le toi-même) : décommente la ligne `ANTHROPIC_API_KEY=` et colles-y ta clé. Elle n'est nécessaire que pour le profil `llm` ; les scripts la vérifient avant de lancer quoi que ce soit. Le fichier est ignoré par git. Une variable déjà définie dans l'environnement n'est jamais écrasée. La clé ne figure ni dans les prompts, ni dans les logs, ni dans la base.

## Interface web

```bash
scripts/start.sh llm                       # bot + interface : http://localhost:8766
# ou, dans deux terminaux :
tradeagent run --profile llm               # terminal 1 : le bot
tradeagent web --profile llm --open        # terminal 2 : l'interface du même profil
```

Un tableau de bord pour voir le bot travailler : état (en vie, prudent, défensif, mort, arrêté), equity et courbe avec les zones de paliers, **résultat net après coût de l'API**, distance à la mort, portefeuille, dépense d'inférence (jour, total, 7 derniers jours), journal des décisions avec le verdict du code, évènements. Il suit le thème clair/sombre de ton système, se rafraîchit toutes les 15 s, s'adapte au téléphone et prévient si le bot n'a pas fait de cycle depuis longtemps. Les « attentes » consécutives du LLM sont regroupées sur une ligne pour ne pas noyer les ordres.

C'est volontairement **de la lecture seule** : pas de bouton qui achète, vend, reprend ou réinitialise. Ce qui engage l'argent ou relance un bot arrêté reste en ligne de commande.

| Protection | Détail |
|---|---|
| Boucle locale uniquement | Le serveur refuse d'écouter ailleurs que sur `127.0.0.1` (pas d'option pour l'ouvrir au réseau) |
| En-tête `Host` vérifié | Une page web ne peut pas lire le bot via ton navigateur (« DNS rebinding ») |
| GET / HEAD seulement | Tout le reste reçoit `405` |
| Base ouverte en lecture seule | Même un bug ne peut rien écrire ; lire ne crée jamais la base |
| Texte du LLM = texte | Les raisons données par l'agent sont affichées via `textContent`, jamais comme du HTML ; vérifié dans un vrai navigateur avec une charge `<img onerror>` |
| CSP stricte | Ni script ni style en ligne, aucune ressource externe, aucun cadre ; les polices sont celles de ton système |
| Rien de secret | L'API ne renvoie ni clé, ni variable d'environnement, ni chemin de fichier ; le détail des erreurs internes reste côté serveur |

La maquette a été conçue dans Claude Design avant d'être codée.

### Application de bureau (Electron)

```bash
cd desktop
npm install          # une fois : télécharge Electron
npm start            # une fenêtre, un onglet par profil (Ctrl+1, Ctrl+2, Ctrl+3)
npm test             # tests Node, hors réseau
```

La même interface, dans une fenêtre au lieu du navigateur. L'application lance `tradeagent web --profile X` pour chaque profil avec le Python du `.venv` (lance `scripts/setup` d'abord) et affiche chaque page dans un onglet. Si une interface tourne déjà (`scripts/start`), elle est réutilisée.

Elle sert aussi à **démarrer et arrêter les bots** :

- menu **Bots** (ou clic droit sur l'icône de la zone de notification) : *Démarrer* / *Arrêter* pour chaque profil, *Tout arrêter*. Le profil `llm` demande une confirmation, car son API est facturée. La pastille de l'onglet se remplit quand le bot tourne ;
- **fermer la fenêtre n'arrête rien** : l'application se range dans la zone de notification et les bots continuent. **Quitter** (menu Fichier ou icône) arrête les bots proprement, après confirmation : chacun finit son cycle en cours, affiche son état, garde ses positions ;
- si l'application plante ou est tuée, les bots qu'elle a démarrés s'arrêtent d'eux-mêmes à la fin de leur cycle : aucun bot ne tourne sans surveillance ;
- un bot qui s'arrête sans qu'on l'ait demandé (clé API absente, base déjà utilisée par un autre bot, mort, plantage) est signalé avec ses dernières lignes ;
- la sortie de chaque bot est écrite dans `data/logs/<profil>.log` (1 Mo, 3 fichiers d'historique) ; *Bots → Ouvrir le dossier des journaux*.

Ce que l'application ne fait pas, volontairement : reprendre un bot `halted` (`tradeagent resume`) ou repartir d'une nouvelle vie (`tradeagent reset`) restent en ligne de commande, et aucune page ne peut agir sur un bot (les actions sont dans les menus natifs, pas dans l'interface web). Elle ne voit pas non plus un bot lancé ailleurs (`scripts/start`, un terminal) : sa pastille reste vide, et vouloir le démarrer une seconde fois est refusé par le verrou de la base.

Pas encore faits : notifications de bureau sur les changements d'état (mort, palier), version portable.

Les protections de l'interface web restent en place, et la fenêtre en ajoute : chaque page tourne en bac à sable, sans accès à Node ni au processus principal ; elle ne peut ni naviguer ni charger quoi que ce soit hors de son serveur local, ni ouvrir de fenêtre, ni obtenir de permission ; un port n'est affiché que si c'est bien tradeagent qui y répond.

## Comment l'agent est tenu

Flux d'un cycle : `prix + bougies → agent → garde-fous → exchange → journal`, avec le kill switch qui surveille à chaque cycle, que l'agent soit appelé ou non.

L'agent reçoit un état compact (~1 500 tokens) : equity, positions, derniers ordres, résumé des bougies (variation 1 h/6 h/24 h, plus haut/plus bas 24 h, volatilité), ses limites actuelles, son palier de risque et son budget API restant. Il renvoie un unique JSON `{action, symbol, amount_quote, reasoning}`. Il n'a accès ni à l'exchange, ni aux clés, ni à la configuration des garde-fous. Le prompt dit explicitement que ne rien faire est une action valide.

| Garde-fou (`guardrails.py`) | Effet |
|---|---|
| Liste de symboles | Tout symbole hors `symbols` est refusé |
| Spot uniquement, ordres au marché | Pas de levier, pas de vente à découvert (on ne vend pas plus qu'on détient) |
| `max_order_pct` / `max_position_pct` / `max_total_exposure_pct` | L'ordre est **réduit** à la plus petite limite, la raison est journalisée |
| `min_order_quote` | Pas d'ordres minuscules ; une vente qui laisserait de la poussière sort de la position en entier |
| `max_buys_per_day`, `max_daily_loss_pct` | Bloquent les **achats** seulement. Sortir d'une position reste toujours possible |
| `max_price_age_s` | Prix périmé : ordre refusé |
| Marge frais + glissement | Un achat ne peut pas dépasser le cash disponible une fois les coûts comptés |

### Paliers de risque

On réduit la voilure **avant** la mort, d'après le drawdown depuis le plus haut de l'equity :

| Palier | Déclencheur (défaut) | Effet |
|---|---|---|
| `normal` | — | Limites de `config.yaml` |
| `cautious` | drawdown ≥ 15 % | Plafonds d'ordre et de position × 0,5 |
| `defensive` | drawdown ≥ 25 % | Plus aucun achat, ventes seulement |
| `dead` | drawdown ≥ 40 % ou perte totale ≥ 50 % (kill switch) | Liquidation, arrêt définitif |

Le palier se recalcule à chaque cycle et redescend si l'equity remonte. Il complète la perte journalière (5 %), qui réagit aux chutes brutales d'une journée, là où les paliers réagissent aux pertes qui s'étalent sur plusieurs jours.

**Attention avec une petite mise.** Avec 50 € et un ordre minimum de 5 €, le palier `cautious` plafonne les ordres à `equity × 20 % × 0,5` : sous ~42,5 € d'equity, ce plafond passe sous 5 € et les achats sont refusés. En pratique, à cette échelle, `cautious` se comporte comme `defensive`. C'est sûr, mais sache-le.

### Kill switch

| État (`killswitch.py`) | Déclencheur | Conséquence |
|---|---|---|
| `dead` | equity ≤ mise − `max_total_loss_pct`, ou drawdown ≥ `max_drawdown_pct` | Tout est vendu (`liquidate_on_death`), plus aucun appel à l'agent, **définitif** |
| `halted` | `max_consecutive_errors` erreurs d'affilée (agent, LLM, flux de prix, exchange) | Arrêt sans liquidation ; `tradeagent resume` après vérification |

L'état est écrit en base SQLite : relancer le script ne ressuscite pas un bot mort et ne remet pas le solde simulé à zéro. Changer `stake` sans `reset` est refusé. Une pause de l'agent (cadence, budget épuisé, pas de bougies) n'est ni une erreur ni une réussite : elle ne remet pas à zéro le compteur d'erreurs, donc un LLM durablement en panne finit bien par arrêter le bot.

### Budget d'inférence (le « loyer »)

- Chaque appel est valorisé : `tokens × prix (config) × usd_to_eur`, et écrit dans la table `llm_calls`. Le cache de prompt est compté (lecture ×0,1, écriture ×1,25).
- Au plus un appel toutes les `call_every_seconds` (1 h par défaut) ; l'horodatage est écrit **avant** l'appel, pour qu'une panne ne déclenche jamais une rafale de nouvelles tentatives payantes. La cadence survit à un redémarrage.
- Plafond par jour (UTC) et plafond total. Budget épuisé : l'agent ne fait plus rien (« skip ») ; le kill switch continue de tourner. **Les positions déjà ouvertes ne sont alors gérées que par le kill switch**, pas par l'agent.
- Un appel dont la réponse est inutilisable (JSON cassé) est quand même payé et compté.
- `tradeagent status` affiche : `résultat net = equity − mise − coûts API de cette vie`.

Ordre de grandeur avec le prompt actuel et Haiku 4.5 (1 $/5 $ par million de tokens) : environ 0,002 € par appel, soit ~0,05 €/jour et ~1,5 €/mois à un appel par heure. C'est une estimation à vérifier avec `tradeagent status` pendant la phase paper ; sur une mise de 50 €, ce coût compte dans le résultat.

## Configuration

Tout est dans `config.yaml`, commenté. Les clés inconnues sont refusées au démarrage (une faute de frappe ne doit pas désactiver un garde-fou en silence), les valeurs incohérentes aussi (ex. palier défensif au-dessus du seuil de mort, budget du jour supérieur au budget total). Le mode `live` est volontairement refusé pour l'instant (voir « Le mode réel n'existe pas encore »). Les prix des tokens et `usd_to_eur` sont dans la config : c'est à toi de les tenir à jour.

## Choisir l'exchange

`exchange` dans `config.yaml` est un identifiant [ccxt](https://github.com/ccxt/ccxt) : le paper trading y lit les prix publics, sans clé ni compte. Prends celui où tu comptes trader réellement, pour que prix, paires et frais du paper ressemblent au réel. N'importe quel identifiant ccxt est accepté ; si une paire de `symbols` n'y existe pas, le bot le dit et liste les paires disponibles dans la même devise.

Avant d'ouvrir un compte quelque part, vérifie que tu as le droit d'y être servi. Dans l'Union européenne, le règlement MiCA impose aux plateformes un agrément de prestataire de services sur crypto-actifs ; sa période transitoire a pris fin le 1er juillet 2026. Pour un résident français :

- cherche **l'entité juridique qui te sert**, pas seulement la marque : un même nom commercial recouvre plusieurs sociétés, et seule celle qui a l'agrément peut te servir ;
- contrôle-la dans le registre de l'ESMA (prestataires agréés MiCA) et sur la liste blanche de l'AMF ;
- relève les frais, le montant minimum d'ordre et les permissions des clés API sur les pages officielles de la plateforme, avec la date : ils changent.

Ce projet ne recommande aucune plateforme et ne donne aucun conseil financier ou juridique. La valeur fournie dans `config.yaml` est le choix de son auteur, à refaire pour ta situation.

## Limites connues

- `CcxtPriceFeed` (vrais prix) n'a tourné pour de vrai que sur un cycle (profil `hold`, prix publics de Bitvavo, le 04/10/2026) : rien sur la durée. `AnthropicClient` (vrai appel API) n'a été testé qu'avec le vrai SDK et un transport HTTP simulé, jamais contre le réseau. Valide-le avec `scripts/paper.sh llm --max-cycles 1` (un appel API, ~0,002 €).
- Les scripts `.sh` sont testés sous Linux. Côté Windows, seul `paper.bat` a été lancé pour de vrai ; pour les autres `.bat`, la forme est vérifiée (CRLF, ASCII, commandes appelées), pas le comportement. Si l'un échoue, `python -m tradeagent <commande>` fait exactement la même chose.
- Le paper trading est **optimiste** : exécution au dernier prix ± glissement fixe, sans profondeur de carnet ni écart achat/vente réel. L'écart avec le réel grandit avec la taille de l'ordre et sur les paires peu échangées.
- Les frais simulés (`costs.fee_rate`) ne valent que s'ils correspondent à l'exchange visé et à ton palier : c'est le taux taker, puisque le bot ne passe que des ordres au marché. La valeur fournie est celle relevée sur la page officielle à la date indiquée dans `config.yaml` ; si tu changes `exchange`, change aussi `fee_rate`, sinon le résultat du paper ne veut plus rien dire.
- Calculs en `float` : suffisant en simulation, à revoir (`Decimal`) avant tout compte réel.
- Un seul actif de cotation (EUR par défaut), ordres au marché uniquement.
- Le LLM ne voit que des bougies et son propre état : ni actualité, ni carnet d'ordres. Rien n'indique qu'il en tire un avantage.

## Chemin vers l'argent réel (non construit)

1. **Paper sur ta machine, ~1 semaine** : `scripts/start.sh hold llm` lance la référence et le LLM côte à côte (bases séparées), `scripts/status.sh` les compare. Mesurer le vrai coût API.
2. **Quarantaine** : chaque ordre réel attend ta confirmation (file d'approbation en base, asynchrone pour ne pas bloquer les contrôles du kill switch).
3. **Adaptateur exchange réel** : l'exchange visé n'offre pas d'environnement de test au comptant, donc l'adaptateur est d'abord testé contre un faux exchange local (timeouts, exécutions partielles, rejets) ; puis sous-compte dédié, clés API limitées à la lecture et au trading, **sans droit de retrait ni de transfert**, restreintes à ton IP ; premiers ordres au minimum (5 €).
4. **Mise réelle** : 50 € maximum, uniquement de l'argent que tu acceptes de perdre en totalité.
