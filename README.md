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
| `board` | réels (publics, sans clé) | le board : des sous-agents sans LLM (aujourd'hui le seul suivi de tendance) sur un seul portefeuille | aucun | 8768 |

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
tradeagent reset --profile llm --yes  # nouvelle vie : repart de la mise de départ (le journal est gardé) ; refusé tant que le bot du profil tourne
```

`tradeagent run --stop-on-stdin` sert aux programmes qui lancent le bot (l'application de bureau) : le bot s'arrête proprement, à la fin de son cycle, quand son entrée standard reçoit `stop` ou se ferme. C'est l'équivalent de `Ctrl+C` là où il n'y a pas de terminal, notamment sous Windows. Rien d'autre n'est lu sur l'entrée standard.

Sans `--profile`, les commandes utilisent la base de `config.yaml` (`data/agent.db`), comme avant. `--agent` et `--feed` restent prioritaires sur le profil.

Agents : `hold` (ne fait rien, **la référence à battre**), `chaos` (aléatoire, demande aussi des choses absurdes, pour tester la plomberie), `llm-fake` (sorties simulées, coût simulé, pour tester le circuit LLM hors ligne), `llm` (Anthropic : le LLM choisit chaque ordre) et `board` (les sous-agents du code tradent, sans LLM : c'est l'agent du profil `board`).

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

Elle **prévient par une notification de bureau** quand l'état d'un profil change. Elle relit pour cela l'instantané en lecture seule de chaque profil toutes les 15 secondes ; un clic sur la notification ouvre la fenêtre sur le profil concerné, rien de plus :

| Notification | Quand |
|---|---|
| Le bot est mort | Le kill switch s'est déclenché (mise perdue ou drawdown maximal atteint). La notification donne la raison et demande de vérifier sur la page qu'il ne reste aucune position : elle n'affirme pas que tout est vendu, la liquidation pouvant être désactivée ou avoir échoué |
| Le bot est suspendu | Passage en `halted` : trop d'erreurs d'affilée, rien n'est vendu. Le détail de l'erreur reste sur la page du profil |
**Mode distant.** Quand les bots tournent sur un serveur (voir « Sur un serveur »), l'application peut afficher son interface au lieu de lancer la sienne : menu Serveur, « Se connecter à un serveur… », puis l'adresse HTTPS. Elle vérifie qu'un serveur tradeagent y répond, retient l'adresse dans `data/desktop.json` et redémarre. Ensuite : la page de connexion du serveur, puis un onglet par profil. Dans ce mode elle ne fait que montrer : le menu Bots n'offre aucune action, rien n'est lancé sur le poste (pas besoin de Python), la fenêtre ne navigue que vers ce serveur, et les notifications de bureau lisent le serveur. La connexion reste ouverte d'un lancement à l'autre (sept jours au plus). Retour au mode local : menu Serveur. En version portable, le premier lancement commence par ce choix. **Vu pour de vrai jusqu'à la page de connexion seulement** ; la suite (onglets après connexion, notifications, session expirée, fenêtre de choix) n'a été vue par personne.

| Changement de palier | Passage à prudent, défensif, ou retour à normal (le palier dépend du drawdown, c'est-à-dire du recul du portefeuille depuis son plus haut) |
| Budget API épuisé | Le plafond du jour (UTC) ou le plafond total vient d'être atteint : l'agent est en pause, les garde-fous et le kill switch continuent |
| Le bot ne donne plus de nouvelles | Un bot démarré par l'application n'a réussi aucun cycle depuis 3 cycles et 2 minutes (bot figé, ou flux de prix en panne : un cycle en erreur n'écrit rien) |

Limites : seules les **transitions** sont notifiées (un bot déjà mort au lancement de l'application ne notifie pas, la page le montre) ; rien n'arrive si l'application est quittée ou le PC éteint ; le silence n'est surveillé que pour les bots démarrés par l'application (les autres notifications marchent aussi pour un bot lancé ailleurs, tant que l'application est ouverte) ; si l'interface web d'un profil s'arrête ou ne répond plus, ce profil n'est plus surveillé (son onglet affiche l'erreur) ; la mort d'un bot démarré par l'application donne deux notifications, celle-ci et celle de l'arrêt du processus. Les notifications ne contiennent jamais le texte écrit par l'agent.

#### Version portable

Un dossier à poser où l'on veut (disque, clé USB), sans installateur et sans rien écrire ailleurs :

```bash
cd desktop
npm run pack         # construit desktop/dist/win-unpacked/ (le dossier de l'application)
npm run dist         # la même chose, en archive zip dans desktop/dist/
```

Ces commandes se lancent depuis le dépôt, après `scripts/setup` (elles utilisent le `.venv` pour construire le paquet Python de `tradeagent`). Elles ont besoin du réseau.

Au **premier lancement** de `tradeagent.exe`, l'application prépare son environnement, dans son propre dossier :

1. elle cherche Python 3.10 ou plus récent sur le PC (`py -3`, `python`, `python3`). **Elle ne télécharge pas Python** : s'il est absent ou trop ancien, elle affiche la version requise et un bouton vers https://www.python.org/downloads/, puis propose de réessayer ;
2. elle crée `.venv` et y installe `tradeagent` (livré avec l'application) et ses dépendances, téléchargées sur PyPI. **Une connexion Internet est nécessaire à ce premier lancement**, qui dure quelques minutes au plus ;
3. elle crée `config.yaml` et `.env` s'ils n'existent pas. Ils ne sont jamais écrasés ensuite : mets ta clé dans `.env` pour le profil `llm`.

Tout vit dans le dossier de l'application : `config.yaml`, `.env`, `data/` (bases, journaux, cache de la fenêtre) et `.venv`. Rien n'est écrit dans le dossier utilisateur. Le dossier doit être inscriptible (pas `Program Files`). Aux lancements suivants, rien n'est réinstallé ; quand la version de l'application change, le `.venv` est refait, sans toucher à `config.yaml`, `.env` ni `data/`.

Les dépendances sont installées aux versions du `.venv` qui a servi à construire l'application (fichier `constraints.txt` livré), pas « à la dernière version ». pip tourne isolé : il ignore la configuration pip du PC (seul PyPI est utilisé), n'installe que des paquets déjà construits (aucun code de construction n'est exécuté) et ne reçoit pas les variables d'environnement du PC, donc aucune clé. Quitter pendant l'installation l'interrompt ; elle reprend de zéro au lancement suivant.

Limites : les versions ne sont pas vérifiées par empreinte (hachage) ; le fichier de contraintes vient d'un Python 3.12 sous Windows, donc avec une autre version de Python une dépendance qui lui est propre serait prise à sa dernière version ; `config.yaml` livré est celui du dépôt au moment de la construction ; deux copies de l'application lancées en même temps se partagent les mêmes ports, la seconde affiche alors les pages de la première ; l'exécutable n'est pas signé (Windows peut afficher un avertissement) et garde l'icône d'Electron, et seule la construction pour Windows existe.

Les protections de l'interface web restent en place, et la fenêtre en ajoute : chaque page tourne en bac à sable, sans accès à Node ni au processus principal ; elle ne peut ni naviguer ni charger quoi que ce soit hors de son serveur local, ni ouvrir de fenêtre, ni obtenir de permission ; un port n'est affiché que si c'est bien tradeagent qui y répond.

## Sur un serveur (Docker)

Pour que les bots tournent 24 h sur 24 sans dépendre d'un PC allumé : une image (`Dockerfile`), un conteneur par bot (`compose.yaml`), relancé tout seul après un plantage ou un redémarrage du serveur. L'état est en base, dans un dossier du serveur monté dans les conteneurs : une relance ne perd rien.

```bash
sudo mkdir -p /var/lib/docker/hiatus/tradeagent/data && sudo chown 1000:1000 /var/lib/docker/hiatus/tradeagent/data
docker compose build
docker compose up -d                 # démarre hold (la référence) et board
docker compose ps                    # « healthy » = un cycle récent, pas seulement un processus en vie
docker compose logs -f board
docker compose stop board            # arrêt comme Ctrl+C
docker compose run --rm board status --all
```

`tradeagent health --profile board` (code 0 ou 1) est ce que Docker interroge : le bot est en bonne santé s'il a enregistré un cycle depuis moins de trois intervalles. Un bot mort ou `halted` ne fait plus de cycle, il est donc signalé lui aussi.

Les bots n'ont besoin d'aucun secret (argent fictif, prix publics) et n'ouvrent aucun port.

### L'interface depuis un autre poste

Le service `web` du `compose.yaml` lance `tradeagent serve` : la même page en lecture seule, pour plusieurs profils, sous une seule adresse (`https://trade.sternum-lab.duckdns.org/`), atteinte uniquement par Traefik. L'interface locale (`tradeagent web`) ne change pas et reste sur la boucle locale.

```bash
sudo mkdir -p /var/lib/docker/hiatus/tradeagent/auth && sudo chown 1000:1000 /var/lib/docker/hiatus/tradeagent/auth
nano .env.web          # une ligne : TRADEAGENT_SETUP_CODE=un-code-que-tu-choisis   (12 caractères au moins)
chmod 600 .env.web
docker compose up -d web
```

À la première visite, la page propose de **créer le compte administrateur** : il demande le code d'installation ci-dessus (sans lui, le premier visiteur venu deviendrait administrateur), un identifiant et un mot de passe de 16 caractères au moins. Il n'y a qu'un compte et la création se ferme dès qu'il existe ; vide ensuite `.env.web` et relance `docker compose up -d web`, qui recrée le conteneur sans le code. Ne mets rien d'autre dans ce fichier. Puis : connexion, liste des profils, page de chaque profil.

Ce que fait le code, et que les tests vérifient :

- le serveur refuse de démarrer sans nom de domaine, et sans code d'installation tant qu'aucun compte n'existe ; hors du conteneur il n'écoute que sur la boucle locale (`--bind 0.0.0.0` est passé par le compose) ;
- l'en-tête `Host` doit être exactement le nom déclaré, et le proxy doit annoncer du HTTPS ;
- sans session, une page renvoie à la connexion et une donnée répond 401, que le chemin existe ou non ;
- le mot de passe n'est gardé que sous forme d'empreinte (scrypt), dans `auth/auth.json`, avec l'identifiant et la clé qui signe les sessions : ce fichier est sensible, ne le copie pas ailleurs. Il est hors des bases des bots, que l'interface monte en lecture seule ;
- la session est un cookie signé (`HttpOnly`, `Secure`, `SameSite=Strict`), valable sept jours ; se déconnecter annule toutes les sessions ;
- cinq échecs en un quart d'heure bloquent la connexion pour tout le monde pendant ce quart d'heure (quelqu'un du réseau peut donc te bloquer, pas deviner ton mot de passe à la chaîne) ; les essais passent un par un, même envoyés en même temps ;
- les seules requêtes qui écrivent sont la création du compte, la connexion et la déconnexion ; aucune n'agit sur un bot.

Mot de passe oublié : supprime `auth/auth.json` sur le serveur, remets un code dans `.env.web`, relance `web`, recrée le compte. Double authentification et codes de récupération : prévus, pas codés.

**Jamais construit ni lancé sur un serveur** : l'image n'a pas été construite, le projet n'a jamais tourné sous Linux, et l'interface n'a jamais été servie derrière un vrai Traefik ; les autres conteneurs du réseau Docker `proxy` peuvent la joindre sans passer par lui (ils n'y verraient rien sans session) (`serve` a tourné une fois en local, avec un compte jetable et les en-têtes du proxy imités). Les pages de connexion n'ont pas été vues dans un navigateur. L'application de bureau ne sait pas encore se brancher sur cette adresse : pour l'instant, un navigateur.

## Backtest : rejouer une période passée

Avant de comparer des agents sur des semaines de paper trading, on peut les faire tourner sur le passé. Par défaut c'est gratuit (prix publics, aucune clé, aucun appel au LLM) et ça prend quelques secondes :

```bash
tradeagent backtest                      # 30 derniers jours, bougies de l'exchange de config.yaml
tradeagent backtest --days 90 --end 2026-09-01
tradeagent backtest --agents hold,buyhold,quant,board
tradeagent backtest --days 30 --months 7     # 7 périodes de 30 jours à la suite, avec le cumul par agent
tradeagent backtest --synthetic          # sans réseau : historique fabriqué, pour essayer la commande
```

Le backtest fait tourner **le vrai moteur** : mêmes garde-fous, même kill switch, mêmes frais et glissement que `tradeagent run`. Seuls changent l'horloge (simulée) et les prix (rejoués). Chaque agent tourne dans une base en mémoire : un backtest n'écrit dans aucune base de profil et ne gêne pas un bot en marche. L'historique téléchargé est gardé dans `data/history/` et seules les bougies manquantes sont retéléchargées (`--refresh` pour tout reprendre).

| Agent | Ce qu'il fait |
|---|---|
| `hold` | Rien. La référence : finit toujours à la mise |
| `buyhold` | Achat-conservation : achète autant de chaque symbole que les garde-fous le permettent, à parts égales, puis ne touche plus à rien |
| `quant` | Le témoin : trade sur les signaux des modèles mathématiques, sans LLM donc sans loyer. Achète une tendance haussière à la taille du modèle de risque, sort quand la tendance se retourne ou que son niveau de sortie (qui suit le prix à la hausse) est touché |
| `board` | Le board : des sous-agents tiennent chacun ce qu'ils veulent détenir, un seul portefeuille additionne ces cibles et trade l'écart, un ordre par cycle, les ventes d'abord. Pour l'instant un seul sous-agent, le suivi de tendance de `quant` : les deux lignes doivent donc se ressembler. C'est aussi l'agent du profil `board`, qui tourne en direct : son état (ce que chaque sous-agent réclame, ses niveaux de sortie) est gardé en base, survit à un redémarrage, est effacé par `reset`, et s'affiche sous le portefeuille dans l'interface. Une position que plus aucun sous-agent ne réclame (état effacé ou abîmé) est vendue dès qu'elle vaut au moins l'ordre minimal ; en dessous elle est invendable et reste là |
| `chaos` | Aléatoire : montre ce que coûtent les frais quand on trade sans raison |
| `llm-fake` | Faux LLM (décisions aléatoires) : vérifie que les coûts d'API entrent bien dans le résultat net |

Avec `--months N`, la commande rejoue N périodes consécutives et termine par un tableau de cumul : net total, nombre de mois positifs, pire mois. C'est lui qu'il faut regarder : un mois isolé ne dit presque rien. Chaque période repart de la mise, les gains ne sont pas réinvestis d'un mois sur l'autre.

Colonnes : `net` = equity finale − mise − coûts d'API ; `drawdown` = la plus forte baisse depuis un plus-haut pendant la période ; `ordres` et `frais` = ce qui a été exécuté et payé. La dernière ligne donne le **marché** : ce qu'aurait rapporté un achat à parts égales de toute la mise au début, sans garde-fou. Ce n'est pas une stratégie jouable ici (les garde-fous gardent au moins 20 % en cash), c'est un repère.

**À lire avec prudence.** Un backtest est optimiste par construction :

- l'ordre est exécuté au prix que l'agent vient de voir (la clôture de la dernière bougie terminée), plus le glissement fixe de la config. Un vrai ordre part au prix d'après ;
- le prix ne bouge qu'à chaque clôture de bougie : en bougies d'une heure, le backtest ne voit rien de ce qui se passe dans l'heure ;
- le kill switch et le drawdown ne voient donc que les clôtures : une chute en cours de bougie, qui tuerait le bot en vrai, passe inaperçue. Morts et drawdown sont sous-estimés ;
- le contrôle « prix périmé » des garde-fous ne joue pas en backtest ;
- 30 jours de hausse ne disent rien des 30 suivants. Une stratégie qui gagne sur une période choisie après coup ne prouve rien.

Garanties du code, testées : à un instant simulé, un agent ne voit jamais une bougie encore en cours (pas de vue sur le futur) ; un historique incomplet (début ou fin manquants, trou de plus de 6 bougies) est refusé avec la date du trou, au lieu de donner un backtest plus court que la période affichée ; un agent arrêté en route (`dead`, `halted`) est signalé sous le tableau avec sa raison, sa ligne ne se compare pas aux autres.

### Le vrai LLM en backtest (payant)

```bash
tradeagent backtest --agents hold,buyhold,llm --days 7 --max-api-eur 0.50
```

Ici chaque appel au LLM coûte de l'**argent réel**, et le budget de `config.yaml` ne suffit pas à protéger : un backtest part d'une base vide, ses compteurs repartent donc de zéro à chaque lancement. D'où des protections propres au backtest, appliquées par le code :

- `--max-api-eur` est **obligatoire** avec l'agent `llm` : c'est la dépense réelle maximale de ce backtest. Il ne peut pas dépasser `llm.total_budget_eur` ;
- avant de lancer, une répétition à blanc (sans aucun appel) affiche le nombre d'appels prévus, ce qui est déjà en cache et le coût estimé ; si l'estimation dépasse le plafond, le backtest refuse de démarrer ;
- il faut ensuite taper `oui` (ou passer `--yes`). Sans terminal, c'est un refus ;
- avant **chaque** appel, le code vérifie deux plafonds avec une estimation pessimiste du coût de l'appel : celui du backtest en cours, et celui de **tous les backtests cumulés** (`llm.total_budget_eur`, d'après ce qui a déjà été payé). Plafond atteint : l'agent `llm` s'arrête (`halted`), c'est signalé sous le tableau ; la ligne « vrai LLM » compte les appels refusés (le résultat est alors incomplet) ;
- le coût pessimiste de chaque appel est **réservé** dans le fichier de cache avant l'appel, puis remplacé par le coût réel à la réponse. Un appel raté, un arrêt brutal ou un disque plein laissent donc la réservation comptée (on suppose l'appel facturé) : la comptabilité se trompe toujours du côté prudent. Si le fichier ne peut pas être écrit, aucun appel payant ne part ;
- trois appels ratés d'affilée, et le backtest n'essaie plus ;
- un seul backtest payant à la fois (verrou) : deux en parallèle liraient le même cumul et dépasseraient le plafond.

Les réponses payées sont gardées dans `data/llm-cache/backtest.jsonl` : relancer le même backtest ne coûte rien, ne demande ni clé ni confirmation, et donne le même résultat (le LLM n'est pas déterministe, le cache si). Le coût d'API d'une réponse relue du cache reste compté dans le résultat net, comme s'il avait été payé : le but est de comparer net de l'API. Le fichier ne contient ni prompt ni clé. **Ne le supprime pas** : c'est aussi le registre de ce que les backtests ont réellement dépensé ; l'effacer fait repayer les appels et remet le cumul à zéro.

Dans le tableau, la colonne `API` est le coût d'API de la période simulée ; la dépense réelle de ce lancement est sur la ligne « vrai LLM » en dessous. L'estimation n'est pas un devis : le nombre d'appels et leur taille changent avec les décisions du LLM. Seuls les plafonds sont des garanties.

Limites à connaître :

- **les backtests ont leur propre enveloppe**, égale à `llm.total_budget_eur`, comptée à part de celle du bot. La dépense réelle totale possible est donc de deux fois ce montant (bot + backtests) ;
- il n'y a pas de plafond par jour réel en backtest : `daily_budget_eur` ne joue qu'en temps simulé. `--max-api-eur` peut être dépensé en quelques minutes ;
- les plafonds sont calculés aux prix écrits dans `config.yaml` (`price_*_per_mtok_usd`, `usd_to_eur`) : s'ils sont faux, la dépense réelle l'est dans le même rapport ;
- le cache suit une trajectoire : si un appel échoue en route, les prompts suivants changent (le budget restant y figure) et la relance repaie la suite. « Ne pas payer deux fois » vaut pour un backtest allé au bout ;
- les appels s'enchaînent sans attente : sur une longue période, l'API peut refuser pour excès de débit, ce qui arrête l'agent après trois échecs.

`llm-fake` reste là pour vérifier la chaîne sans rien dépenser.

## Comment l'agent est tenu

Flux d'un cycle : `prix + bougies → agent → garde-fous → exchange → journal`, avec le kill switch qui surveille à chaque cycle, que l'agent soit appelé ou non.

L'agent reçoit un état compact (~1 500 tokens) : equity, positions, derniers ordres, résumé des bougies (variation 1 h/6 h/24 h, plus haut/plus bas 24 h, volatilité), ses limites actuelles, son palier de risque et son budget API restant. Il renvoie un unique JSON `{action, symbol, amount_quote, reasoning}`. Il n'a accès ni à l'exchange, ni aux clés, ni à la configuration des garde-fous. Le prompt dit explicitement que ne rien faire est une action valide.

| Garde-fou (`guardrails.py`) | Effet |
|---|---|
| Liste de symboles | Tout symbole hors `symbols` est refusé |
| Spot uniquement, ordres au marché | Pas de levier, pas de vente à découvert (on ne vend pas plus qu'on détient) |
| `max_order_pct` / `max_position_pct` / `max_total_exposure_pct` (40 % / 60 % / 80 % de l'equity) | L'ordre est **réduit** à la plus petite limite, la raison est journalisée |
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
| `dead` | equity nette du loyer ≤ mise − `max_total_loss_pct`, ou drawdown ≥ `max_drawdown_pct` | Tout est vendu (`liquidate_on_death`), plus aucun appel à l'agent, **définitif** |
| `halted` | `max_consecutive_errors` erreurs d'affilée (agent, LLM, flux de prix, exchange) | Arrêt sans liquidation ; `tradeagent resume` après vérification |

**Le loyer sort de la mise.** Le kill switch, le plus-haut et les paliers de risque ne jugent pas l'equity seule, mais l'**equity nette du loyer** : l'equity moins ce que l'inférence a coûté depuis le début de la vie. Un bot qui reste en cash et appelle le LLM toutes les heures ne perd rien en trading, mais son loyer le fait descendre de palier, puis le tue. C'est la règle du jeu : survivre, c'est gagner plus qu'on ne dépense en API. La perte du jour, la taille des ordres et l'equity montrée à l'agent restent calculées sur l'equity réelle ; l'interface affiche la courbe, le plus-haut et le drawdown nets du loyer. Pour `hold` et `demo`, qui n'appellent pas le LLM, rien ne change.

L'état est écrit en base SQLite : relancer le script ne ressuscite pas un bot mort et ne remet pas le solde simulé à zéro. Changer `stake` sans `reset` est refusé. Une pause de l'agent (cadence, budget épuisé, pas de bougies) n'est ni une erreur ni une réussite : elle ne remet pas à zéro le compteur d'erreurs, donc un LLM durablement en panne finit bien par arrêter le bot.

### Budget d'inférence (le « loyer »)

- Chaque appel est valorisé : `tokens × prix (config) × usd_to_eur`, et écrit dans la table `llm_calls`. Le cache de prompt est compté (lecture ×0,1, écriture ×1,25).
- Au plus un appel toutes les `call_every_seconds` (1 h par défaut) ; l'horodatage est écrit **avant** l'appel, pour qu'une panne ne déclenche jamais une rafale de nouvelles tentatives payantes. La cadence survit à un redémarrage.
- Plafond par jour (UTC) et plafond total. Budget épuisé : l'agent ne fait plus rien (« skip ») ; le kill switch continue de tourner. **Les positions déjà ouvertes ne sont alors gérées que par le kill switch**, pas par l'agent.
- Un appel dont la réponse est inutilisable (JSON cassé) est quand même payé et compté.
- **Appels sur évènement.** L'agent n'est pas appelé à l'horloge : après chaque réponse, il n'est rappelé qu'au bout de `quiet_call_interval_seconds` de calme (12 h), ou plus tôt si un prix a bougé de `wake_move_pct` (2 %) depuis son dernier appel. C'est le code qui l'impose, pas la bonne volonté du modèle. Sur un mois de bougies réelles, cela donne environ 70 appels au lieu de 720.
- **Réponse au format imposé.** L'API contraint la réponse à un schéma JSON (sorties structurées) : plus de réponse en prose payée pour rien.
- **L'agent peut ajuster son réveil.** `call_every_seconds` est un intervalle *minimal*. Dans sa réponse, l'agent peut demander à dormir plus longtemps (`next_check_minutes`) et à être réveillé plus tôt si un prix bouge d'un certain pourcentage (`wake_if_move_pct`). Le code borne tout : jamais plus tôt que l'intervalle minimal, jamais plus tard que `max_call_interval_seconds` (24 h par défaut), seuil de mouvement entre 0,5 % et 50 %. Un changement de palier le réveille toujours. **S'il dort en détenant une position, le réveil sur mouvement de prix est obligatoire** : le code impose `position_wake_move_pct` (3 % par défaut) ; l'agent peut demander un seuil plus serré, jamais plus large. Pendant qu'il dort, le kill switch et les garde-fous tournent à chaque cycle, comme d'habitude.
- **Palier « économie ».** Quand l'equity nette du loyer recule, l'intervalle minimal s'allonge : multiplié par `economy_call_factor` (2 par défaut) en palier prudent, par son carré (4) en palier défensif.
- **Aucun appel quand aucun ordre n'est possible** (achats bloqués ou cash insuffisant, et rien à vendre) : l'agent ne paie pas pour constater. Conséquence à connaître : un bot en cash que son loyer a mené au palier défensif n'est plus appelé. Il ne paie plus, ne peut plus rien faire, et ne meurt pas : **c'est toi qui décides de son sort**. Le code dresse le bilan de la vie (gagné en trading, consommé en API, net) et donne un conseil, dans l'interface, dans `tradeagent status` et par une notification de bureau ; il n'agit pas à ta place. Un arrêt seulement temporaire (plafond d'achats du jour, perte du jour) est signalé comme une pause, sans rien te demander. Cet arrêt est écrit une fois au journal des évènements et affiché dans l'interface ; un `reset` efface aussi le sommeil demandé par l'agent, mais pas la cadence des appels payants.
- **Le code calcule, le LLM interprète.** Pour chaque symbole, l'agent reçoit des indicateurs tout faits, calculés sur 31 jours de bougies (`market.candles`) : variations sur 1 h à 30 jours, écart du prix à ses moyennes sur 24 h, 7 jours et 30 jours (la tendance), RSI horaire et journalier (de 0 à 100 : au-dessus de 70 le prix a beaucoup monté, sous 30 beaucoup baissé), amplitude moyenne d'une bougie (pour distinguer un mouvement du bruit), et position du prix dans sa fourchette de 7 et 30 jours. Pour chaque position ouverte : prix de revient, gain ou perte latente, âge.
- **Des modèles mathématiques dans le code** (`signals.py`), qui tournent à chaque cycle, gratuitement, et alimentent tous les agents avec les mêmes signaux : **régime de tendance** (hausse, baisse ou sans direction, d'après les moyennes de 7 et 30 jours), **prévision de volatilité** (l'ampleur type du mouvement sur 24 h), **risque et taille** (une distance de sortie hors du bruit, et la mise qui ne perd qu'environ 1 % de l'equity si cette sortie est touchée). Leurs réglages sont des valeurs classiques fixées à l'avance, pas ajustées sur les mois de backtest. L'agent est réveillé quand un régime passe à la hausse, ou à la baisse sur un symbole qu'il détient. Le témoin `quant` trade sur ces mêmes signaux sans LLM : c'est la référence à battre, car un agent LLM qui ne fait pas mieux que lui n'apporte rien par-dessus les modèles.
- **Un plan de sortie à chaque achat.** En achetant, l'agent donne le prix sous lequel sa raison d'acheter ne tient plus (`exit_below`). Le code le borne (sous le prix, au-delà du bruit d'une bougie, à 25 % au plus ; s'il n'en donne pas, le code en pose un), le garde avec la position, le lui remontre à chaque appel avec sa raison d'achat, et **le réveille quand le prix franchit ce niveau**. Le code ne vend pas à sa place : c'est l'agent qui décide, une fois réveillé. Il peut déplacer le niveau en répondant « hold » sur ce symbole. Un `reset` efface les plans.
- L'agent reçoit dans ses données les vrais frais et glissement de la config, son loyer cumulé et son equity nette. Le prompt est versionné (`PROMPT_VERSION` dans `llm_agent.py`, version en cours : 9) ; la version utilisée est écrite en base.
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
