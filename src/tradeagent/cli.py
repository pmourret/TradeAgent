"""Ligne de commande : run, up, status, resume, reset, web, backtest."""
from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .advice import idle_advice
from .app import AGENT_KINDS, PAID_KINDS, build_agent, build_engine, build_feed, reset_life
from .backtest import (BACKTEST_AGENTS, DEFAULT_AGENTS, PAID_AGENTS, compare, format_summary, format_table, market_return_pct,
                       run_backtest, warmup_seconds)
from .budget import InferenceBudget, day_start_ts
from .config import ConfigError, load_config
from .context import load_context_history
from .envfile import load_env_file
from .exchange import ExchangeError
from .models import TIMEFRAME_SECONDS
from .replay import load_history, public_client, synthetic_history
from .killswitch import KillSwitch, KillSwitchError
from .launcher import build_jobs, describe, preflight, resolve_profiles, run_jobs
from .llm import AnthropicClient, LLMError, require_api_key
from .llm_agent import DECISION_SCHEMA
from .llm_cache import CachingLLMClient, EstimatingClient, ReplyCache
from .lock import InstanceLock
from .portfolio import base_currency
from .profiles import LIVE, PROFILES, apply_profile, get_profile
from .stopper import StdinStop
from .storage import Storage
from .supervisor import DIRECTIVE_SCHEMA, ESTIMATE_REPLY
from .web import DEFAULT_PORT, make_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradeagent", description="Agent de trading crypto (paper trading).")
    sub = parser.add_subparsers(dest="command", required=True)
    with_config = argparse.ArgumentParser(add_help=False)
    with_config.add_argument("--config", default="config.yaml", help="fichier de config (défaut : config.yaml)")
    common = argparse.ArgumentParser(add_help=False, parents=[with_config])
    common.add_argument("--profile", choices=[*PROFILES, LIVE], default=None,
                        help="profil de lancement : " + " ; ".join(f"{p.name} = {p.description}" for p in PROFILES.values())
                             + " ; live = mode réel (pas encore disponible). Un profil a sa propre base de données.")

    run = sub.add_parser("run", parents=[common], help="lance la boucle de trading")
    run.add_argument("--agent", choices=AGENT_KINDS, default=None,
                     help="hold = ne fait rien (défaut) ; chaos = aléatoire, pour tester les garde-fous ; "
                          "llm = agent Anthropic (coûte de l'argent, clé requise) ; "
                          "supervisor = les modèles du code tradent, le LLM règle le niveau de risque "
                          "(coûte de l'argent, clé requise) ; "
                          "llm-fake = faux LLM hors ligne, mêmes comptes de tokens. Prime sur le profil.")
    run.add_argument("--feed", choices=["ccxt", "synthetic"], default=None,
                     help="ccxt = vrais prix publics (défaut), synthetic = marche aléatoire hors ligne. Prime sur le profil.")
    run.add_argument("--max-cycles", type=int, default=None, help="s'arrête après N cycles")
    run.add_argument("--cycle-seconds", type=float, default=None, help="remplace cycle_seconds de la config")
    run.add_argument("--seed", type=int, default=None, help="graine pour agent chaos / llm-fake / flux synthetic")
    run.add_argument("--stop-on-stdin", action="store_true",
                     help="s'arrête proprement, en fin de cycle, quand l'entrée standard reçoit `stop` ou se ferme "
                          "(pour un programme parent, comme l'application de bureau, qui ne peut pas envoyer Ctrl+C)")

    status = sub.add_parser("status", parents=[common], help="affiche l'état (lecture seule)")
    status.add_argument("--all", action="store_true", help="tous les profils qui ont déjà tourné, à la suite")
    up = sub.add_parser("up", parents=[with_config],
                        help="lance bot(s) + interface(s) web ensemble dans ce terminal ; Ctrl+C arrête tout")
    up.add_argument("profiles", nargs="*", metavar="profil",
                    help="un ou plusieurs profils (défaut : hold). Ex. `up hold llm` compare la référence au LLM")
    up.add_argument("--no-ui", action="store_true", help="bots seulement, sans interface web")
    up.add_argument("--no-open", action="store_true", help="n'ouvre pas le navigateur")
    sub.add_parser("resume", parents=[common], help="relance un bot arrêté (halted) après vérification")
    reset = sub.add_parser("reset", parents=[common], help="nouvelle vie : repart de la mise de départ")
    reset.add_argument("--yes", action="store_true", help="confirmation obligatoire")
    web = sub.add_parser("web", parents=[common],
                         help="interface web locale en lecture seule (à lancer à côté de `run`, dans un autre terminal)")
    web.add_argument("--host", default="127.0.0.1", help="boucle locale uniquement (défaut : 127.0.0.1)")
    web.add_argument("--port", type=int, default=None,
                     help=f"port (défaut : celui du profil, sinon {DEFAULT_PORT})")
    web.add_argument("--open", action="store_true", help="ouvre la page dans le navigateur")

    backtest = sub.add_parser("backtest", parents=[with_config],
                              help="rejoue une période passée avec le vrai moteur et compare des stratégies (gratuit)")
    backtest.add_argument("--days", type=int, default=30, help="durée de la période rejouée, en jours (défaut : 30)")
    backtest.add_argument("--months", type=int, default=1, metavar="N",
                          help="rejoue N périodes de --days jours à la suite, la dernière finissant à --end, "
                               "et affiche le cumul (défaut : 1)")
    backtest.add_argument("--end", default=None, metavar="AAAA-MM-JJ",
                          help="fin de la période, à minuit UTC (défaut : maintenant)")
    backtest.add_argument("--agents", default=",".join(DEFAULT_AGENTS),
                          help=f"agents à comparer, séparés par des virgules (choix : {', '.join(BACKTEST_AGENTS)})")
    backtest.add_argument("--seed", type=int, default=1, help="graine des agents aléatoires et de l'historique synthétique")
    backtest.add_argument("--synthetic", action="store_true",
                          help="historique fabriqué (marche aléatoire), sans réseau : pour essayer la commande")
    backtest.add_argument("--max-api-eur", type=float, default=None, metavar="EUR",
                          help="obligatoire avec l'agent llm ou supervisor : dépense RÉELLE maximale d'API pour ce backtest")
    backtest.add_argument("--yes", action="store_true", help="avec l'agent llm ou supervisor : ne demande pas de confirmation")
    backtest.add_argument("--refresh", action="store_true", help="retélécharge l'historique au lieu d'utiliser le cache")
    return parser


def _load(args: argparse.Namespace):
    """Config + profil éventuel. Un profil impose sa base de données (et parfois la cadence)."""
    cfg = load_config(args.config)
    profile = get_profile(args.profile) if args.profile else None
    if profile is not None:
        cfg = apply_profile(cfg, profile)
    return cfg, profile


def cmd_run(args: argparse.Namespace) -> int:
    cfg, profile = _load(args)
    agent_kind = args.agent or (profile.agent if profile else "hold")
    feed_kind = args.feed or (profile.feed if profile else "ccxt")
    if args.cycle_seconds is not None:
        cfg = replace(cfg, cycle_seconds=args.cycle_seconds)
    if agent_kind in PAID_KINDS:
        require_api_key()                         # avant toute écriture : un lancement refusé ne laisse rien derrière lui
    lock = InstanceLock(cfg.database).acquire()   # gardé jusqu'à la fin du processus
    storage = Storage(cfg.database)
    agent = build_agent(agent_kind, cfg, storage, seed=args.seed)
    feed = build_feed(feed_kind, cfg, seed=args.seed)
    engine = build_engine(cfg, agent, feed, storage)

    label = f"profil={profile.name} " if profile else ""
    print(f"{label}mode={cfg.mode} agent={agent.name} flux={feed_kind} mise={cfg.stake:g} {cfg.quote_currency} "
          f"symboles={','.join(cfg.symbols)}")
    if profile:
        print(f"base : {cfg.database}  (interface web : tradeagent web --profile {profile.name})")
    if agent_kind in PAID_KINDS:
        budget = InferenceBudget(cfg.llm, storage)
        print(f"LLM {cfg.llm.model} : un appel max toutes les {cfg.llm.call_every_seconds / 60:g} min, "
              f"budget restant {budget.left()['today']:.2f} € aujourd'hui / {budget.left()['total']:.2f} € au total")
    try:
        if args.stop_on_stdin:
            stop = StdinStop().start()
            engine.run_forever(max_cycles=args.max_cycles, sleep=stop.wait, should_stop=stop.requested)
            if stop.requested():
                print(f"\n{stop.reason} : le bot s'arrête proprement (les positions éventuelles sont conservées)")
        else:
            engine.run_forever(max_cycles=args.max_cycles, sleep=time.sleep)
    except KeyboardInterrupt:
        print("\ninterrompu (les positions éventuelles sont conservées)")
    finally:
        lock.release()
    return _print_status(cfg)


def _open_storage(cfg) -> Storage | None:
    if not Path(cfg.database).exists():
        print(f"aucune base à {cfg.database} : rien n'a encore tourné.")
        return None
    return Storage(cfg.database)


def _print_status(cfg) -> int:
    storage = _open_storage(cfg)
    if storage is None:
        return 0
    life = storage.get("life") or {"stake": cfg.stake, "started": 0.0}
    ks = KillSwitch(cfg.killswitch, storage, life["stake"])
    tier = storage.get("risk_tier", "normal")
    print(f"\nétat : {ks.status.upper()}" + (f" — {ks.reason}" if ks.reason else "")
          + (f" | palier de risque : {tier}" if ks.status == "alive" else ""))

    ccy = cfg.quote_currency
    spent_life = storage.llm_spend_since(life.get("started", 0.0))
    last = storage.last_equity()
    if last:
        change = (last["equity"] / life["stake"] - 1) * 100
        print(f"equity : {last['equity']:.2f} {ccy} (mise {life['stake']:.2f}, {change:+.2f} %)")
        net = last["equity"] - life["stake"] - spent_life
        digits = 4 if 0 < abs(net) < 0.01 else 2   # évite l'affichage trompeur « -0.00 »
        print(f"résultat net : {net:+.{digits}f} {ccy} (equity - mise - coûts API de cette vie)")
        if spent_life:
            peak = storage.get("peak_equity", life["stake"])
            floor = max(life["stake"] * (1 - cfg.killswitch.max_total_loss_pct / 100),     # les deux seuils de mort
                        peak * (1 - cfg.killswitch.max_drawdown_pct / 100))
            print(f"equity nette du loyer : {last['equity'] - spent_life:.2f} {ccy} (loyer {spent_life:.4f} ; "
                  f"mort sous {floor:.2f})")
    idle = storage.get("llm_idle")
    if last and isinstance(idle, dict) and idle.get("cause") and ks.status == "alive":
        advice = idle_advice(str(idle["cause"]), life["stake"], last["equity"], spent_life, ccy)
        print(f"{advice['title'].upper()}\n  {advice['text']}")
    balances = storage.get("paper_balances") or {}
    held = {k: v for k, v in balances.items() if v}
    print("soldes :", ", ".join(f"{k} {v:.6g}" for k, v in held.items()) or "—")

    calls = storage.count("llm_calls")
    if calls:
        today = storage.llm_spend_since(day_start_ts(time.time()))
        print(f"API LLM : {calls} appels, {storage.llm_spend_since(0.0):.4f} € au total "
              f"({today:.4f} € aujourd'hui, budget {cfg.llm.daily_budget_eur:.2f} €/jour "
              f"et {cfg.llm.total_budget_eur:.2f} € au total)")
    print(f"journal : {storage.count('decisions')} décisions, {storage.count('fills')} exécutions, "
          f"{storage.count('events')} évènements")
    for event in reversed(storage.recent_events(3)):
        print(f"  [{event['level']}] {event['message']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.all:
        if args.profile:
            raise ConfigError("--all et --profile s'excluent")
        base = load_config(args.config)
        shown = 0
        for profile in PROFILES.values():
            cfg = apply_profile(base, profile)
            if Path(cfg.database).exists():
                print(f"=== profil {profile.name} ({profile.agent}) ===")
                _print_status(cfg)
                print()
                shown += 1
        if not shown:
            print("aucun profil n'a encore tourné.")
        return 0
    return _print_status(_load(args)[0])


def cmd_up(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    profiles = resolve_profiles(args.profiles)
    preflight(profiles, bots=True)
    jobs = build_jobs(profiles, args.config, ui=not args.no_ui, open_browser=not args.no_open)
    print("Lancement (argent fictif) :")
    print("\n".join(describe(profiles, cfg, bots=True, ui=not args.no_ui)))
    print("Ctrl+C arrête tout proprement.\n", flush=True)
    return run_jobs(jobs)


def cmd_resume(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    storage = _open_storage(cfg)
    if storage is None:
        return 1
    life = storage.get("life") or {"stake": cfg.stake}
    ks = KillSwitch(cfg.killswitch, storage, life["stake"])
    try:
        ks.resume()
    except KillSwitchError as exc:
        print(f"refusé : {exc}", file=sys.stderr)
        return 1
    print("bot relancé (si le bot était déjà actif, rien n'a changé).")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    cfg, _ = _load(args)
    if not args.yes:
        print("Cela efface l'état de la vie en cours (soldes simulés, kill switch, plus-haut).\n"
              "Le journal et le suivi des coûts API sont conservés. Relance avec --yes pour confirmer.",
              file=sys.stderr)
        return 1
    storage = Storage(cfg.database)
    reset_life(storage, time.time())
    print(f"nouvelle vie : prochaine mise {cfg.stake:g} {cfg.quote_currency}.")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    cfg, profile = _load(args)
    port = args.port if args.port is not None else (profile.port if profile else DEFAULT_PORT)
    server = make_server(cfg, args.host, port)
    host, port = server.server_address[:2]
    url = f"http://{'localhost' if host == '127.0.0.1' else host}:{port}/"
    print(f"interface web en lecture seule : {url}  (Ctrl+C pour arrêter ; le bot, lui, tourne dans `tradeagent run`)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\narrêt de l'interface web")
    finally:
        server.server_close()
    return 0


def _llm_cache_path(cfg) -> Path:
    return Path(cfg.database).parent / "llm-cache" / "backtest.jsonl"


def _lock_llm_cache(cfg) -> InstanceLock:
    """Un seul backtest payant à la fois : deux lancements liraient le même cumul et dépasseraient le plafond."""
    try:
        return InstanceLock(_llm_cache_path(cfg)).acquire()
    except ConfigError:
        raise ConfigError("un autre backtest avec le vrai LLM est déjà en cours : attends qu'il finisse "
                          "(deux à la fois dépasseraient le plafond de dépense).") from None


def _backtest_llm_client(cfg, args: argparse.Namespace, history, windows: list[tuple[float, float]],
                         kind: str = "llm", context=None) -> CachingLLMClient | None:
    """Prépare le vrai LLM pour un backtest : estimation à blanc, puis confirmation. None = refusé, rien dépensé."""
    cost_of = InferenceBudget(cfg.llm, Storage(":memory:")).cost_eur
    cache = ReplyCache(_llm_cache_path(cfg))
    schema, placeholder = ((DIRECTIVE_SCHEMA, ESTIMATE_REPLY) if kind == "supervisor"
                           else (DECISION_SCHEMA, '{"action": "hold", "reasoning": "estimation"}'))
    estimate = EstimatingClient(cache, cfg.llm.model, cost_of, placeholder)
    for start, end in windows:
        run_backtest(cfg, kind, history, start, end, seed=args.seed, llm_client=estimate, context=context)

    to_pay = estimate.calls - estimate.cached
    print(f"vrai LLM ({cfg.llm.model}) : environ {estimate.calls} appels sur la période, dont {estimate.cached} déjà en cache.")
    print(f"  à payer pour de vrai : environ {estimate.typical_cost:.2f} € (au pire {estimate.worst_cost:.2f} €) ; "
          f"plafond de ce backtest : {args.max_api_eur:.2f} €")
    print(f"  déjà payé par les backtests précédents : {cache.spent_lifetime:.2f} € sur {cfg.llm.total_budget_eur:.2f} € "
          "(llm.total_budget_eur)")
    print("  C'est une estimation : le nombre d'appels et leur taille changent avec les décisions du LLM. "
          "Le plafond, lui, est appliqué par le code avant chaque appel.")
    if estimate.typical_cost > args.max_api_eur:
        raise ConfigError("l'estimation dépasse le plafond : le backtest s'arrêterait en route. "
                          "Raccourcis la période (--days) ou relève --max-api-eur.")
    if cache.spent_lifetime + estimate.typical_cost > cfg.llm.total_budget_eur:
        raise ConfigError("l'estimation dépasse ce qu'il reste du plafond cumulé des backtests (llm.total_budget_eur) : "
                          "le backtest s'arrêterait en route. Raccourcis la période (--days).")
    if to_pay > 0:
        require_api_key()
        if not args.yes:
            try:
                answer = input("Tape « oui » pour lancer ce backtest payant : ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "oui":
                print("abandonné : rien n'a été dépensé.")
                return None
    return CachingLLMClient(lambda: AnthropicClient(cfg.llm.model, output_schema=schema), cache,
                            cfg.llm.model, cost_of,
                            run_cap_eur=args.max_api_eur, total_cap_eur=cfg.llm.total_budget_eur)


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    paid = [a for a in agents if a in PAID_AGENTS]
    real_llm = bool(paid)
    if len(paid) > 1:
        raise ConfigError(f"un seul agent payant par backtest ({' ou '.join(PAID_AGENTS)}) : chacun a son format de "
                          "réponse et son estimation. Lance-les l'un après l'autre : ce qui est déjà payé est relu du cache.")
    if real_llm:
        if args.max_api_eur is None:
            raise ConfigError("le vrai LLM en backtest coûte de l'argent réel : donne un plafond de dépense, "
                              "par exemple --max-api-eur 0.50. Sans dépense : --agents llm-fake.")
        if not 0 < args.max_api_eur <= cfg.llm.total_budget_eur:
            raise ConfigError(f"--max-api-eur doit être supérieur à 0 et au plus égal à llm.total_budget_eur "
                              f"({cfg.llm.total_budget_eur:g} €)")
    unknown = [a for a in agents if a not in BACKTEST_AGENTS]
    if unknown or not agents:
        raise ConfigError(f"agents de backtest inconnus : {', '.join(unknown) or '(aucun)'} "
                          f"(choix : {', '.join(BACKTEST_AGENTS)})")
    if args.days < 1:
        raise ConfigError("--days doit valoir au moins 1")
    if not 1 <= args.months <= 24:
        raise ConfigError("--months doit être compris entre 1 et 24")
    llm_client = None
    step = TIMEFRAME_SECONDS[cfg.market.timeframe]
    if args.end:
        try:
            end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError as exc:
            raise ConfigError(f"--end doit être une date AAAA-MM-JJ, reçu {args.end!r}") from exc
    else:
        end = time.time()
    end = end // step * step                     # on s'arrête sur une bougie terminée
    if end > time.time():
        raise ConfigError("--end est dans le futur")
    span = args.days * 86_400
    windows = [(end - (i + 1) * span, end - i * span) for i in reversed(range(args.months))]     # de la plus ancienne à la plus récente
    start = windows[0][0]
    history_start = start - warmup_seconds(cfg)

    try:
        if args.synthetic:
            history = synthetic_history(cfg.symbols, cfg.market.timeframe, history_start, end, seed=args.seed)
            source = f"historique synthétique (graine {args.seed})"
        else:
            cache_dir = Path(cfg.database).parent / "history"
            history = load_history(public_client(cfg.exchange), cfg.exchange, cfg.symbols, cfg.market.timeframe,
                                   history_start, end, cache_dir, refresh=args.refresh)
            source = f"{cfg.exchange}, bougies {cfg.market.timeframe} (cache : {cache_dir})"
        context, flows = None, []
        if "supervisor" in agents and not args.synthetic:
            # Les flux d'information du superviseur, rejoués comme les prix : données publiques, sans clé.
            context, flows = load_context_history(Path(cfg.database).parent / "history",
                                                  [base_currency(s) for s in cfg.symbols], start, end, refresh=args.refresh)
        logger = logging.getLogger("tradeagent")
        previous_level = logger.level
        logger.setLevel(logging.CRITICAL)        # des milliers de cycles : pas de journal par cycle
        lock = _lock_llm_cache(cfg) if real_llm else None     # gardé de l'estimation à la fin du backtest
        try:
            llm_client = _backtest_llm_client(cfg, args, history, windows, paid[0], context) if real_llm else None
            if real_llm and llm_client is None:
                return 1                         # refusé à la confirmation : rien n'a été dépensé
            per_window = [compare(cfg, agents, history, s, e, seed=args.seed, llm_client=llm_client, context=context)
                          for s, e in windows]
        finally:
            logger.setLevel(previous_level)
            if lock is not None:
                lock.release()
        markets = [market_return_pct(cfg, history, s, e) for s, e in windows]
    except ExchangeError as exc:
        print(f"erreur de données : {exc}", file=sys.stderr)
        return 2

    day = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    periods = f"{args.months} périodes de {args.days} j" if args.months > 1 else f"{args.days} j"
    print(f"backtest du {day(start)} au {day(end)} UTC ({periods}), {source}")
    print(f"mise {cfg.stake:g} {cfg.quote_currency}, un cycle toutes les {cfg.cycle_seconds / 60:g} min, "
          f"frais {cfg.costs.fee_rate * 100:g} % + glissement {cfg.costs.slippage_bps:g} pb par ordre")
    for flow in flows:
        print(f"flux du superviseur : {flow}")
    print()
    for (s, e), results, market_pct in zip(windows, per_window, markets):
        if args.months > 1:
            print(f"-- du {day(s)} au {day(e)} UTC")
        print(format_table(results, cfg, market_pct))
        if args.months > 1:
            print()
    if args.months > 1:
        print(f"== cumul des {args.months} périodes")
        print(format_summary(per_window, cfg))
    if llm_client is not None:
        failed = (f", {llm_client.failed_calls} appels ratés comptés au pire coût" if llm_client.failed_calls else "")
        if llm_client.refused_calls:
            failed += f", {llm_client.refused_calls} appels refusés par un plafond (résultat incomplet : relève --max-api-eur)"
        print(f"vrai LLM : {llm_client.paid_calls} appels payés ({llm_client.spent_run:.4f} € réels, plafond "
              f"{args.max_api_eur:.2f} €), {llm_client.hits} réponses relues du cache{failed}")
    print("\nÀ lire avec prudence : les ordres sont exécutés au dernier prix de clôture (optimiste), le prix ne bouge pas "
          "entre deux bougies (le kill switch et le drawdown ne voient donc que les clôtures), et une période passée ne dit rien de la suivante. Ce n'est pas un conseil de placement.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    load_env_file(".env")
    args = _parser().parse_args(argv)
    handlers = {"run": cmd_run, "up": cmd_up, "status": cmd_status, "resume": cmd_resume, "reset": cmd_reset, "web": cmd_web,
                "backtest": cmd_backtest}
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"erreur de configuration : {exc}", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"erreur LLM : {exc}", file=sys.stderr)
        return 2
