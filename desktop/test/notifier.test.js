"use strict";
// Tests hors réseau, sans Electron ni Python : les instantanés sont fabriqués ici.
const assert = require("node:assert/strict");
const test = require("node:test");
const { digest, transitions, Watcher, STALE_CYCLES, STALE_MARGIN_S } = require("../lib/notifier");

const T0 = 1_800_000_000;

// Un instantané comme `build_snapshot` (dashboard.py), réduit aux champs lus par les notifications.
function snap(over = {}) {
  const base = {
    has_data: true, generated_at: T0, cycle_seconds: 60,
    status: { state: "alive", reason: "", since: null },
    risk_tier: "normal",
    life: { stake: 50, started: T0 - 3600 },
    money: { last_update: T0 - 10 },
    api: { spent_today: 0.01, daily_budget: 0.5, spent_total: 0.2, total_budget: 5 },
  };
  return { ...base, ...over };
}

// Notifications émises en voyant `before` puis `after` (le bot tourne ou non selon `running`).
function between(before, after, running = false) {
  const prev = digest(before, null, running);
  return transitions("llm", prev, digest(after, prev, running));
}

const kinds = (notes) => notes.map((n) => n.kind);

test("le premier instantané sert de référence : un bot déjà mort ne notifie pas", () => {
  const dead = digest(snap({ status: { state: "dead", reason: "perte totale" } }), null, false);
  assert.deepEqual(transitions("llm", null, dead), []);
  assert.deepEqual(between(snap(), snap({ generated_at: T0 + 15 })), []);
});

test("la mort notifie une fois, avec la raison du kill switch et la marche à suivre", () => {
  const notes = between(snap(), snap({ status: { state: "dead", reason: "perte totale de 50.2 % (plafond 50 %)." }, risk_tier: "defensive" }));
  assert.deepEqual(kinds(notes), ["dead"]);            // pas de notification de palier en plus
  assert.match(notes[0].title, /« llm » est mort/);
  assert.match(notes[0].body, /^perte totale de 50\.2 % \(plafond 50 %\)\. Vérifie sur la page du profil qu'il ne reste aucune position\./);
  assert.match(notes[0].body, /tradeagent reset --profile llm,/);
  assert.ok(!/liquid|vendu/.test(notes[0].body));      // la liquidation peut être désactivée ou avoir échoué
  const dead = snap({ status: { state: "dead", reason: "x" } });
  assert.deepEqual(between(dead, { ...dead, generated_at: T0 + 15 }), []);
});

test("la suspension (halted) notifie, la reprise non", () => {
  // La raison d'une suspension cite la dernière erreur, donc parfois un bout de réponse du LLM (llm_agent.py).
  const reason = "10 erreurs d'affilée, dernière : agent : JSON invalide | réponse : 'TEXTE-DU-LLM'";
  const halted = snap({ status: { state: "halted", reason } });
  const notes = between(snap(), halted);
  assert.deepEqual(kinds(notes), ["halted"]);
  assert.match(notes[0].body, /rien n'a été vendu.*tradeagent resume --profile llm,/);
  assert.ok(!JSON.stringify(notes).includes("TEXTE-DU-LLM"));
  assert.ok(!JSON.stringify(notes).includes("JSON invalide"));
  assert.equal(digest(halted, null, false).reason, "");
  assert.deepEqual(between(halted, { ...halted, generated_at: T0 + 15 }), []);      // déjà dit
  assert.deepEqual(between(halted, snap()), []);
});

test("chaque changement de palier notifie, dans les deux sens", () => {
  const cautious = snap({ risk_tier: "cautious" });
  const defensive = snap({ risk_tier: "defensive" });
  assert.match(between(snap(), cautious)[0].title, /palier prudent/);
  assert.match(between(cautious, defensive)[0].body, /achats sont bloqués, les ventes restent possibles/);
  assert.match(between(defensive, snap())[0].title, /retour au palier normal/);
  assert.deepEqual(kinds(between(snap(), defensive)), ["tier"]);
  assert.deepEqual(between(cautious, cautious), []);
});

test("le budget API épuisé notifie au franchissement, à la borne comme dans budget.py", () => {
  const api = (o) => snap({ api: { spent_today: 0.01, daily_budget: 0.5, spent_total: 0.2, total_budget: 5, ...o } });
  assert.deepEqual(between(snap(), api({ spent_today: 0.49 })), []);
  const day = between(snap(), api({ spent_today: 0.5 }));
  assert.deepEqual(kinds(day), ["budget"]);
  assert.match(day[0].title, /budget API du jour épuisé/);
  const total = between(api({ spent_today: 0.5 }), api({ spent_today: 0.5, spent_total: 5 }));
  assert.match(total[0].title, /budget API total épuisé/);
  assert.deepEqual(between(api({ spent_today: 0.5 }), api({ spent_today: 0.6 })), []);      // déjà dit
  assert.deepEqual(between(api({ spent_total: 5 }), api({ spent_total: 5.1 })), []);
  assert.deepEqual(between(api({ spent_today: 0.5 }), api({ spent_today: 0 })), []);        // nouveau jour : silence
});

test("une nouvelle vie (reset) refait la référence sans notifier", () => {
  const dead = snap({ status: { state: "dead", reason: "x" }, risk_tier: "defensive" });
  const reborn = snap({ life: { stake: 50, started: T0 + 100 }, generated_at: T0 + 200, money: { last_update: T0 + 190 } });
  assert.deepEqual(between(dead, reborn), []);
  const worse = { ...reborn, risk_tier: "cautious", life: { stake: 50, started: T0 + 300 } };
  assert.deepEqual(between(reborn, worse), []);
});

test("une base encore vide puis ses premières données ne notifient pas", () => {
  const empty = { has_data: false, generated_at: T0, cycle_seconds: 60 };
  assert.equal(digest(empty, null, false).state, null);
  assert.deepEqual(between(empty, snap({ status: { state: "dead", reason: "x" } })), []);
});

test("un bot en marche sans cycle depuis trop longtemps notifie une seule fois", () => {
  const limit = STALE_CYCLES * 60 + STALE_MARGIN_S;
  const at = (dt) => snap({ generated_at: T0 + dt });          // dernier cycle figé à T0 - 10
  const d0 = digest(at(0), null, true);
  const ok = digest(at(limit), d0, true);                      // pile à la limite : pas encore
  assert.equal(ok.stale, false);
  const late = digest(at(limit + 1), ok, true);
  const notes = transitions("llm", ok, late);
  assert.deepEqual(kinds(notes), ["stale"]);
  assert.match(notes[0].body, /Aucun cycle réussi depuis 5 min/);
  assert.match(notes[0].body, /data\/logs\/llm\.log/);
  assert.deepEqual(transitions("llm", late, digest(at(limit + 60), late, true)), []);
});

test("le silence ne compte que pour un bot que l'application fait tourner, et depuis qu'il tourne", () => {
  const old = snap({ money: { last_update: T0 - 86400 } });
  assert.equal(digest(old, null, false).stale, false);                       // bot arrêté : vieilles données normales
  const started = digest(old, digest(old, null, false), true);               // on vient de le démarrer
  assert.equal(started.stale, false);
  assert.equal(started.runningSince, T0);
  const later = digest({ ...old, generated_at: T0 + 10000 }, started, true); // ... et il n'a jamais fait de cycle
  assert.equal(later.stale, true);
  assert.equal(later.runningSince, T0);
  assert.equal(digest({ ...old, generated_at: T0 + 10000 }, later, false).stale, false);   // arrêté : plus de silence
  const halted = snap({ generated_at: T0 + 10000, status: { state: "halted", reason: "x" } });
  assert.equal(digest(halted, started, true).stale, false);                  // suspendu : déjà notifié autrement
});

test("un instantané illisible ou incohérent est ignoré", () => {
  for (const bad of [null, "texte", {}, { generated_at: "x" }, snap({ status: { state: "zombie" } }), snap({ risk_tier: "panic" }), snap({ status: null })]) {
    assert.equal(digest(bad, null, false), null);
  }
  assert.deepEqual(transitions("llm", digest(snap(), null, false), null), []);
});

test("le texte de l'agent n'entre jamais dans une notification, et la raison est bornée", () => {
  const secret = "RAISONNEMENT-DU-LLM";
  const after = snap({
    status: { state: "dead", reason: "r".repeat(1000) }, journal: [{ reasoning: secret }], agent: secret,
  });
  const [note] = between(snap(), after);
  assert.ok(!JSON.stringify(note).includes(secret));
  assert.ok(note.body.length < 320);
});

test("le veilleur notifie sur transition, garde l'état connu si la lecture échoue, et ne se chevauche pas", async () => {
  const answers = { 1: [snap(), null, snap({ risk_tier: "defensive", generated_at: T0 + 30 })], 2: [snap(), snap(), snap()] };
  const asked = [];
  const notes = [];
  const watcher = new Watcher({
    fetchSnapshot: async (port) => { asked.push(port); return answers[port].shift(); },
    isRunning: () => false,
    notify: (name, note) => notes.push([name, note.kind]),
  });
  const profiles = [{ name: "llm", port: 1 }, { name: "hold", port: 2 }];
  await watcher.poll(profiles);
  await watcher.poll(profiles);                       // lecture ratée pour llm : rien, et la référence reste
  assert.deepEqual(notes, []);
  assert.equal(watcher.view().llm.tier, "normal");
  const first = watcher.poll(profiles);
  await watcher.poll(profiles);                       // pas de second tour : on attend celui qui est en cours
  assert.deepEqual(notes, [["llm", "tier"]]);
  await first;
  assert.equal(asked.length, 6);
});

test("un agent à l'arrêt sans issue notifie une fois, avec le conseil fabriqué par le code", () => {
  const advice = { decision: true, title: "Agent à l'arrêt : à toi de décider", text: "Bilan de cette vie : -12.50 EUR net. Conseil : mettre fin à cette vie (tradeagent reset)." };
  const stuck = snap({ advice });
  const notes = between(snap(), stuck);
  assert.deepEqual(kinds(notes), ["idle"]);
  assert.match(notes[0].title, /« llm » est à l'arrêt : à toi de décider/);
  assert.equal(notes[0].body, advice.text);
  assert.deepEqual(between(stuck, { ...stuck, generated_at: T0 + 15 }), []);          // déjà dit
  assert.deepEqual(between(stuck, snap({ generated_at: T0 + 15 })), []);              // reparti : silence

  // Une simple pause jusqu'à demain ne dérange pas l'utilisateur, et un bot mort a déjà sa notification.
  assert.deepEqual(between(snap(), snap({ advice: { ...advice, decision: false } })), []);
  assert.deepEqual(kinds(between(snap(), snap({ advice, status: { state: "dead", reason: "x" } }))), ["dead"]);
  for (const bad of [null, "texte", { decision: "true", text: "x" }, { text: "x" }]) {
    assert.deepEqual(between(snap(), snap({ advice: bad })), []);
  }
  const long = between(snap(), snap({ advice: { decision: true, text: "a".repeat(5000) } }));
  assert.equal(long[0].body.length, 500);
});
