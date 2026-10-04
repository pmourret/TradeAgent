# Pièges connus

Ceux de l'application de bureau sont dans `.claude/rules/desktop.md`.

- `pkill -f motif` tue **ton propre shell** si le motif figure dans ta ligne de commande. Utilise le PID du fichier `.lock`, ou mets le motif dans un script.
- `InstanceLock` ne tient que tant que l'objet est référencé : `InstanceLock(db).acquire()` sans variable libère aussitôt (ou utilise `with`).
- Avec 50 € de mise et un ordre minimum de 5 €, `cautious` s'active vers 42,5 € d'equity (plus-haut à 50 €) ; son plafond d'ordre (equity × 20 % × 0,5 ≈ 4,25 €) est alors déjà sous le minimum : en pratique `cautious` se comporte comme `defensive`.
- Le paper est **optimiste** : exécution au dernier prix ± glissement fixe, sans profondeur de carnet.
- Binance n'est plus utilisable par les résidents français depuis le 01/07/2026 (pas d'agrément MiCA ; information de Pierre, recoupée par la presse spécialisée, pas lue sur une source officielle). Son API publique de prix répond encore : ça ne prouve rien sur le droit d'y trader.
- `bitpanda` n'existe pas dans ccxt 4.5.85.
- Les frais et minimums que ccxt annonce (`markets[...]["taker"]`, `limits`) sont des valeurs embarquées, parfois périmées (Kraken : 0,26 % dans ccxt contre 0,80 % sur la page officielle le 04/10/2026). Ne jamais s'en servir comme source pour `fee_rate`.
- `costs.fee_rate` est lié à `exchange` : changer l'un sans l'autre fausse le paper.
- Devise de cotation EUR et paires `X/EUR` : changer d'exchange ou de devise impose de vérifier que les paires existent.
- Les prix des tokens du LLM et `usd_to_eur` sont dans `config.yaml` : à tenir à jour à la main.
- L'UI valorise les positions avec `last_quotes`, écrit par le moteur : ne pas le supprimer.
- Un profil n'a pas de vie tant qu'il n'a pas réellement démarré : les contrôles (clé API, profil `live`) se font *avant* d'ouvrir la base.
- `--stop-on-stdin` doit rester une option : `tradeagent up` lance ses enfants avec une entrée standard vide, qui serait lue comme « fermée » et arrêterait le bot aussitôt.
- Un message de commit contenant des guillemets doubles casse `git commit -m` sous PowerShell 5.1 : écrire le message dans un fichier et utiliser `git commit -F`.
- Playwright n'est pas dans le venv : pour vérifier l'UI dans un vrai navigateur, utiliser le Python système.
