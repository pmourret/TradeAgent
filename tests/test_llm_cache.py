"""Vrai LLM en backtest : cache des réponses et plafonds de dépense réelle. Aucun réseau, aucun appel payant."""
from __future__ import annotations

import json

import pytest

from helpers import default_cfg
from tradeagent import cli
from tradeagent.backtest import run_backtest, warmup_seconds
from tradeagent.budget import InferenceBudget
from tradeagent.llm import LLMError, LLMReply, LLMUsage
from tradeagent.llm_cache import (MAX_FAILURES, CachingLLMClient, EstimatingClient, ReplyCache, SpendCapReached,
                                  prompt_key, worst_case_usage)
from tradeagent.replay import synthetic_history
from tradeagent.storage import Storage

T0 = 1_760_000_400.0
DAY = 86_400.0
HOLD = '{"action": "hold", "reasoning": "rien à faire"}'


def cost_one_euro_per_ktok(usage: LLMUsage) -> float:
    return (usage.input_tokens + usage.output_tokens) / 1000


class Inner:
    """Faux vrai client : compte les appels, répond `hold`, facture 100 tokens en entrée et 50 en sortie."""

    def __init__(self, fail: int = 0, fail_on: tuple[int, ...] = (), input_tokens: int = 100) -> None:
        self.calls = 0
        self.fail_on = set(fail_on) | set(range(1, fail + 1))
        self.input_tokens = input_tokens

    def complete(self, system, user, max_output_tokens):
        self.calls += 1
        if self.calls in self.fail_on:
            raise LLMError("appel Anthropic échoué (test)")
        return LLMReply(text=HOLD, usage=LLMUsage(input_tokens=self.input_tokens, output_tokens=50), model="m")


def paid(cache: ReplyCache, key: str, cost: float, reply: LLMReply | None = None) -> None:
    """Inscrit dans le cache un appel déjà payé (réservation puis réponse), comme le ferait un vrai backtest."""
    call = cache.reserve(key, cost, 1.0)
    cache.add(call, key, reply or LLMReply(HOLD, LLMUsage(1, 1), "m"), cost, 1.0)


def client(tmp_path, inner, run_cap=10.0, total_cap=100.0, cost_of=cost_one_euro_per_ktok):
    cache = ReplyCache(tmp_path / "cache" / "backtest.jsonl")
    return CachingLLMClient(lambda: inner, cache, "m", cost_of, run_cap, total_cap, clock=lambda: 123.0), cache


# -- cache ---------------------------------------------------------------------
def test_un_appel_identique_n_est_paye_qu_une_fois(tmp_path):
    inner = Inner()
    llm, cache = client(tmp_path, inner)
    first = llm.complete("sys", "user A", 400)
    again = llm.complete("sys", "user A", 400)
    assert first == again and inner.calls == 1
    assert (llm.paid_calls, llm.hits) == (1, 1)
    assert llm.spent_run == pytest.approx(0.15) and cache.spent_lifetime == pytest.approx(0.15)
    assert again.usage == LLMUsage(input_tokens=100, output_tokens=50)      # le coût reste compté dans le backtest
    llm.complete("sys", "user B", 400)
    assert inner.calls == 2


def test_le_cache_survit_d_un_backtest_a_l_autre_sans_garder_le_prompt(tmp_path):
    first, _ = client(tmp_path, Inner())
    first.complete("système secret", "état du portefeuille", 400)
    inner = Inner()
    later, cache = client(tmp_path, inner)
    assert later.complete("système secret", "état du portefeuille", 400).text == HOLD
    assert inner.calls == 0 and later.spent_run == 0.0 and later.hits == 1
    assert cache.spent_lifetime == pytest.approx(0.15)                      # ce qui a été payé reste compté
    stored = (tmp_path / "cache" / "backtest.jsonl").read_text(encoding="utf-8")
    assert "système secret" not in stored and "état du portefeuille" not in stored
    reserved, answered = (json.loads(line) for line in stored.splitlines())
    assert set(reserved) == {"call", "key", "ts", "reserved_eur"}              # écrite AVANT l'appel
    assert set(answered) == {"call", "key", "ts", "model", "text", "cost_eur", "usage"}
    assert reserved["call"] == answered["call"] and reserved["reserved_eur"] == pytest.approx(0.417)


def test_l_empreinte_change_avec_chaque_element_de_l_appel():
    base = prompt_key("m", "sys", "user", 400)
    assert base == prompt_key("m", "sys", "user", 400)
    assert len({base, prompt_key("m2", "sys", "user", 400), prompt_key("m", "sys2", "user", 400),
                prompt_key("m", "sys", "user2", 400), prompt_key("m", "sys", "user", 401)}) == 5
    assert prompt_key("m", "ab", "c", 1) != prompt_key("m", "a", "bc", 1)   # pas de collision par concaténation


def test_une_ligne_abimee_du_cache_est_ignoree(tmp_path):
    path = tmp_path / "cache" / "backtest.jsonl"
    path.parent.mkdir()
    good = {"call": "1", "key": "k", "ts": 1, "model": "m", "text": HOLD, "cost_eur": 0.2,
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0}}
    path.write_text("pas du json\n" + json.dumps({**good, "call": "2", "key": "x", "cost_eur": -5}) + "\n"
                    + json.dumps({"key": "y"}) + "\n" + json.dumps(good) + "\n"
                    + json.dumps({"call": "3", "key": "z", "ts": 1, "reserved_eur": "beaucoup"}) + "\n", encoding="utf-8")
    cache = ReplyCache(path)
    assert "k" in cache and "x" not in cache and "y" not in cache and "z" not in cache
    assert cache.spent_lifetime == pytest.approx(0.2)


def test_un_appel_reserve_sans_reponse_reste_compte(tmp_path):
    # Arrêt brutal ou panne entre l'appel et sa réponse : on suppose l'appel facturé, au pire coût.
    path = tmp_path / "c.jsonl"
    path.write_text(json.dumps({"call": "1", "key": "k", "ts": 1, "reserved_eur": 0.4}) + "\n"
                    + '{"call": "1", "key": "k", "ts": 1, "model": "m", "text": "{\\"action\\": "' + "\n", encoding="utf-8")
    cache = ReplyCache(path)
    assert "k" not in cache                                                 # pas de réponse : l'appel sera refait
    assert cache.spent_lifetime == pytest.approx(0.4)                       # mais son coût n'est pas oublié
    paid(cache, "k2", 0.1)
    assert ReplyCache(path).spent_lifetime == pytest.approx(0.5)            # le coût réel remplace la réservation


def test_sans_pouvoir_ecrire_le_cache_aucun_appel_ne_part(tmp_path):
    inner = Inner()
    blocked = tmp_path / "fichier"
    blocked.write_text("x", encoding="utf-8")                               # un fichier à la place du dossier du cache
    cache = ReplyCache(blocked / "backtest.jsonl")
    llm = CachingLLMClient(lambda: inner, cache, "m", cost_one_euro_per_ktok, 10.0, 100.0)
    for _ in range(2):
        with pytest.raises(LLMError, match="aucun appel payant sans comptabilité"):
            llm.complete("sys", "user", 400)
    assert inner.calls == 0 and llm.spent_run == 0.0


def test_une_reponse_payee_impossible_a_ecrire_arrete_les_appels_payants(tmp_path, monkeypatch):
    inner = Inner()
    llm, cache = client(tmp_path, inner)
    real_append = cache._append

    def flaky(row):
        if "text" in row:
            raise OSError("disque plein")
        real_append(row)

    monkeypatch.setattr(cache, "_append", flaky)
    assert llm.complete("sys", "user 0", 400).text == HOLD                  # payée : rendue, son coût entre dans le résultat
    assert llm.spent_run == pytest.approx(0.15) and llm.paid_calls == 1
    with pytest.raises(LLMError, match="arrêt des appels payants"):
        llm.complete("sys", "user 1", 400)
    assert inner.calls == 1
    assert llm.complete("sys", "user 0", 400).text == HOLD                  # la réponse en mémoire reste lisible
    # Sur disque il ne reste que la réservation : au prochain lancement elle est comptée au pire coût.
    assert ReplyCache(cache.path).spent_lifetime == pytest.approx(0.405)


# -- plafonds de dépense réelle ------------------------------------------------
def test_le_plafond_du_backtest_est_verifie_avant_l_appel_avec_une_estimation_pessimiste(tmp_path):
    inner = Inner()
    # Pire cas d'un appel : (len("sys") + len("user 0")) / 2 = 5 tokens en entrée + 400 en sortie = 0.405 EUR.
    assert worst_case_usage("sys", "user 0", 400) == LLMUsage(input_tokens=5, output_tokens=400)
    llm, _ = client(tmp_path, inner, run_cap=0.50)
    llm.complete("sys", "user 0", 400)                                      # 0 + 0.405 <= 0.50 : payé 0.15
    with pytest.raises(SpendCapReached, match="plafond de dépense réelle de ce backtest"):
        llm.complete("sys", "user 1", 400)                                  # 0.15 + 0.405 > 0.50 : refusé AVANT l'appel
    assert inner.calls == 1 and llm.spent_run == pytest.approx(0.15)
    assert llm.complete("sys", "user 0", 400).text == HOLD                  # le cache reste lisible, gratuit
    exact, _ = client(tmp_path / "b", Inner(), run_cap=0.405)
    exact.complete("sys", "user 0", 400)                                    # pile le plafond : accepté


def test_le_plafond_cumule_de_tous_les_backtests_tient_d_un_lancement_a_l_autre(tmp_path):
    first, _ = client(tmp_path, Inner(), total_cap=0.60)
    first.complete("sys", "user 0", 400)                                    # 0.15 payé
    inner = Inner()
    later, cache = client(tmp_path, inner, total_cap=0.50)                  # nouveau lancement, compteur du run à zéro
    assert cache.spent_lifetime == pytest.approx(0.15)
    with pytest.raises(SpendCapReached, match="tous les backtests"):
        later.complete("sys", "user 1", 400)                                # 0.15 + 0.405 > 0.50
    assert inner.calls == 0


def test_le_vrai_client_n_est_construit_qu_au_premier_appel_payant(tmp_path):
    built = []
    cache = ReplyCache(tmp_path / "c.jsonl")
    paid(cache, prompt_key("m", "sys", "user", 400), 0.1)
    llm = CachingLLMClient(lambda: built.append(1) or Inner(), cache, "m", cost_one_euro_per_ktok, 10.0, 100.0)
    llm.complete("sys", "user", 400)
    assert built == []                                                      # tout en cache : ni clé ni réseau
    llm.complete("sys", "autre", 400)
    llm.complete("sys", "encore", 400)
    assert built == [1]


def test_des_echecs_repetes_arretent_les_tentatives_payantes(tmp_path):
    inner = Inner(fail=MAX_FAILURES)
    llm, _ = client(tmp_path, inner)
    for i in range(MAX_FAILURES):
        with pytest.raises(LLMError, match="échoué"):
            llm.complete("sys", f"user {i}", 400)
    with pytest.raises(LLMError, match="n'essaie plus"):
        llm.complete("sys", "user x", 400)
    assert inner.calls == MAX_FAILURES and llm.paid_calls == 0 and llm.failed_calls == MAX_FAILURES
    # Un appel raté peut avoir été facturé : sa réservation reste comptée, dans le backtest et dans le cumul.
    assert llm.spent_run == pytest.approx(MAX_FAILURES * 0.405)
    assert ReplyCache(tmp_path / "cache" / "backtest.jsonl").spent_lifetime == pytest.approx(MAX_FAILURES * 0.405)

    # Un succès remet le compteur à zéro : seuls les échecs D'AFFILÉE comptent.
    flaky = Inner(fail_on=(1, 2, 4, 5))
    ok, _ = client(tmp_path / "b", flaky)
    outcomes = []
    for i in range(6):
        try:
            outcomes.append(ok.complete("sys", f"user {i}", 400).text == HOLD)
        except LLMError as exc:
            assert "échoué" in str(exc), str(exc)
            outcomes.append(False)
    assert outcomes == [False, False, True, False, False, True] and flaky.calls == 6


# -- estimation à blanc --------------------------------------------------------
def test_l_estimation_compte_ce_qui_reste_a_payer_sans_rien_appeler(tmp_path):
    cache = ReplyCache(tmp_path / "c.jsonl")
    paid(cache, prompt_key("m", "sys", "déjà payé", 400), 0.1)
    estimate = EstimatingClient(cache, "m", cost_one_euro_per_ktok)
    assert estimate.complete("sys", "déjà payé", 400) == LLMReply(HOLD, LLMUsage(1, 1), "m")    # la vraie réponse
    assert (estimate.calls, estimate.cached, estimate.typical_cost) == (1, 1, 0.0)
    estimate.complete("s" * 300, "u" * 300, 400)                            # 600 caractères
    assert (estimate.calls, estimate.cached) == (2, 1)
    assert estimate.typical_cost == pytest.approx((200 + 120) / 1000)       # 3 car./token, 120 tokens de sortie
    assert estimate.worst_cost == pytest.approx((300 + 400) / 1000)         # 2 car./token, sortie maximale
    assert estimate.worst_cost > estimate.typical_cost


# -- dans un backtest ----------------------------------------------------------
def test_un_backtest_rejoue_est_gratuit_et_donne_le_meme_resultat(tmp_path):
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + DAY, seed=1)
    cost_of = InferenceBudget(cfg.llm, Storage(":memory:")).cost_eur
    inner = Inner()
    path = tmp_path / "backtest.jsonl"
    first_client = CachingLLMClient(lambda: inner, ReplyCache(path), cfg.llm.model, cost_of, 1.0, 10.0)
    first = run_backtest(cfg, "llm", history, T0, T0 + DAY, llm_client=first_client)
    assert inner.calls == 24 and first_client.paid_calls == 24             # un appel par heure simulée
    assert first.api_cost > 0 and first.status == "alive"

    second_client = CachingLLMClient(lambda: pytest.fail("appel réel au second passage"), ReplyCache(path),
                                     cfg.llm.model, cost_of, 1.0, 10.0)
    second = run_backtest(cfg, "llm", history, T0, T0 + DAY, llm_client=second_client)
    assert second == first                                                  # même résultat, coût d'API compris
    assert (second_client.hits, second_client.paid_calls, second_client.spent_run) == (24, 0, 0.0)


def test_un_plafond_atteint_arrete_le_bot_sans_depasser(tmp_path):
    cfg = default_cfg(stake=50.0)
    history = synthetic_history(cfg.symbols, "1h", T0 - warmup_seconds(cfg), T0 + 2 * DAY, seed=1)
    inner = Inner()
    llm, _ = client(tmp_path, inner, run_cap=1.0)                           # 0.15 EUR par appel avec ce barème
    result = run_backtest(cfg, "llm", history, T0, T0 + 2 * DAY, llm_client=llm)
    assert llm.spent_run <= 1.0 and inner.calls == llm.paid_calls < 48
    assert result.status == "halted" and "plafond de dépense réelle" in result.reason


# -- ligne de commande ---------------------------------------------------------
@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("database: data/agent.db\nstake: 50\n", encoding="utf-8")
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: pytest.fail("vrai client Anthropic construit dans un test"))
    return tmp_path


ARGS = ["backtest", "--synthetic", "--days", "1", "--agents", "llm", "--end", "2025-10-01"]


def test_le_vrai_llm_exige_un_plafond_raisonnable(workdir, capsys):
    assert cli.main(ARGS) == 2
    assert "--max-api-eur" in capsys.readouterr().err
    for cap in ("0", "-1", "10.01", "nan"):
        assert cli.main([*ARGS, "--max-api-eur", cap]) == 2, cap
        assert "--max-api-eur doit être supérieur à 0 et au plus égal à llm.total_budget_eur (10 €)" in capsys.readouterr().err
    assert not (workdir / "data").exists()


def test_la_commande_applique_le_plafond_du_backtest_et_pas_un_autre(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    inner = Inner(input_tokens=5_000)                                       # appels chers : environ 0.0047 EUR pièce
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: inner)
    assert cli.main([*ARGS, "--max-api-eur", "0.06", "--yes"]) == 0
    out = capsys.readouterr().out
    assert 1 <= inner.calls < 24                                            # arrêté par le plafond, pas par la fin
    assert "halted" in out and "plafond de dépense réelle de ce backtest" in out
    paid = float(out.split("appels payés (")[1].split(" €")[0])
    assert 0 < paid <= 0.06


def test_la_commande_applique_le_plafond_cumule_de_la_config(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    cache = workdir / "data" / "llm-cache" / "backtest.jsonl"
    cache.parent.mkdir(parents=True)
    usage = {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0}
    row = {"call": "1", "key": "ancien", "ts": 1, "model": "m", "text": HOLD, "usage": usage}
    cache.write_text(json.dumps({**row, "cost_eur": 9.94}) + "\n", encoding="utf-8")
    inner = Inner(input_tokens=5_000)                                       # environ 0.0047 EUR pièce
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: inner)
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 0
    out = capsys.readouterr().out
    assert 1 <= inner.calls < 24                                            # arrêté par llm.total_budget_eur (10 EUR)
    assert "déjà payé par les backtests précédents : 9.94 € sur 10.00 €" in out
    assert "plafond de dépense réelle de tous les backtests" in out
    assert ReplyCache(cache).spent_lifetime <= 10.0

    # Cumul presque épuisé : l'estimation ne tient plus dedans, on refuse de lancer.
    cache.write_text(json.dumps({**row, "cost_eur": 9.999}) + "\n", encoding="utf-8")
    fresh = Inner()
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: fresh)
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 2
    assert "plafond cumulé des backtests" in capsys.readouterr().err and fresh.calls == 0


def test_un_seul_backtest_payant_a_la_fois(workdir, monkeypatch, capsys):
    from tradeagent.lock import InstanceLock

    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    inner = Inner()
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: inner)
    with InstanceLock(workdir / "data" / "llm-cache" / "backtest.jsonl"):   # un autre backtest payant est en cours
        assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 2
        assert "un autre backtest avec le vrai LLM est déjà en cours" in capsys.readouterr().err
        assert inner.calls == 0
        assert cli.main(["backtest", "--synthetic", "--days", "1", "--agents", "hold", "--end", "2025-10-01"]) == 0
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 0         # verrou rendu : ça repart
    assert inner.calls == 24
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 0         # et il est rendu après chaque backtest


def test_sans_cle_ou_sans_confirmation_rien_n_est_depense(workdir, monkeypatch, capsys):
    assert cli.main([*ARGS, "--max-api-eur", "0.50"]) == 2                  # pas de clé : refus avant toute question
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err

    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    for answer in ("non", "", "o", "yes"):
        monkeypatch.setattr("builtins.input", lambda prompt, a=answer: a)
        assert cli.main([*ARGS, "--max-api-eur", "0.50"]) == 1, answer
    out = capsys.readouterr().out
    assert "abandonné : rien n'a été dépensé" in out and "environ 24 appels" in out and "dont 0 déjà en cache" in out
    assert "clé-de-test" not in out

    def closed(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)                           # pas de terminal : refus
    assert cli.main([*ARGS, "--max-api-eur", "0.50"]) == 1
    assert not (workdir / "data" / "llm-cache" / "backtest.jsonl").exists()


def test_une_estimation_au_dessus_du_plafond_refuse_de_lancer(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("question posée alors que le plafond est trop bas"))
    assert cli.main([*ARGS, "--max-api-eur", "0.0001"]) == 2
    assert "l'estimation dépasse le plafond" in capsys.readouterr().err


def test_apres_confirmation_les_appels_sont_payes_une_fois_puis_relus_du_cache(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    inner = Inner()
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: inner)
    monkeypatch.setattr("builtins.input", lambda prompt: " OUI ")
    assert cli.main([*ARGS, "--max-api-eur", "0.50"]) == 0
    out = capsys.readouterr().out
    assert inner.calls == 24 and "vrai LLM : 24 appels payés" in out and "0 réponses relues du cache" in out
    assert (workdir / "data" / "llm-cache" / "backtest.jsonl").exists()
    assert not (workdir / "data" / "agent.db").exists()

    # Second lancement identique : tout est en cache, donc ni clé, ni question, ni appel.
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("question posée pour un backtest gratuit"))
    assert cli.main([*ARGS, "--max-api-eur", "0.50"]) == 0
    out = capsys.readouterr().out
    assert inner.calls == 24 and "dont 24 déjà en cache" in out and "vrai LLM : 0 appels payés" in out


def test_yes_saute_la_question_mais_pas_le_plafond(workdir, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clé-de-test")
    inner = Inner()
    monkeypatch.setattr(cli, "AnthropicClient", lambda model, **kwargs: inner)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("question posée malgré --yes"))
    assert cli.main([*ARGS, "--max-api-eur", "0.50", "--yes"]) == 0
    assert inner.calls == 24
    assert cli.main([*ARGS, "--yes"]) == 2                                  # --yes ne remplace pas --max-api-eur
