"use strict";
/*
 * Notifications de bureau (F3) : repérer, entre deux instantanés `/api/snapshot` d'un profil, ce qui mérite
 * de prévenir Pierre. Rien ici ne dépend d'Electron, du réseau ni d'un processus : ce fichier est testé
 * avec `node --test`, et il ne peut agir sur rien (sortant uniquement).
 *
 * Règles :
 *  - on notifie une TRANSITION, jamais un état : le premier instantané vu sert de référence (un bot déjà mort
 *    au lancement de l'application ne notifie pas, la page le montre) ;
 *  - une nouvelle vie (`reset`) refait la référence sans notifier ;
 *  - jamais le texte de l'agent dans une notification. La raison d'une mort est écrite par le kill switch seul,
 *    on la reprend ; celle d'une suspension cite la dernière erreur, qui peut contenir un bout de réponse du
 *    LLM ou un message d'erreur brut : on ne la reprend pas, elle reste sur la page du profil ;
 *  - un instantané illisible ou incohérent est ignoré : on garde le dernier état connu.
 */

const STATES = new Set(["alive", "halted", "dead"]);
const TIERS = new Set(["normal", "cautious", "defensive"]);
const STALE_CYCLES = 3;          // « sans cycle depuis trop longtemps » : 3 cycles manqués...
const STALE_MARGIN_S = 120;      // ... plus une marge (un cycle avec appel au LLM peut durer)
const REASON_MAX = 160;

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

function budgetState(api) {
  if (!api || typeof api !== "object") return "ok";
  const over = (spent, budget) => num(spent) !== null && num(budget) !== null && spent >= budget;   // comme budget.py
  if (over(api.spent_total, api.total_budget)) return "total";
  if (over(api.spent_today, api.daily_budget)) return "day";
  return "ok";
}

/*
 * Résumé d'un instantané : seulement ce qui sert à détecter une transition. `running` dit si le superviseur
 * fait tourner ce bot ; `prev` est le résumé précédent (ou null). Renvoie null si l'instantané est inutilisable.
 */
function digest(snap, prev, running) {
  if (!snap || typeof snap !== "object") return null;
  const at = num(snap.generated_at);
  if (at === null) return null;
  const cycle = num(snap.cycle_seconds) > 0 ? snap.cycle_seconds : 0;
  const d = {
    at, life: null, state: null, reason: "", tier: null, budget: "ok", lastCycle: null, stale: false, silence: 0,
    // Depuis quand on voit ce bot tourner : un bot qu'on vient de démarrer a forcément de vieilles données.
    runningSince: running ? (prev && prev.runningSince !== null ? prev.runningSince : at) : null,
  };
  if (snap.has_data === true) {
    const status = snap.status || {};
    if (!STATES.has(status.state) || !TIERS.has(snap.risk_tier)) return null;
    d.state = status.state;
    if (d.state === "dead") d.reason = String(status.reason || "").slice(0, REASON_MAX).replace(/[.\s]+$/, "");
    d.tier = snap.risk_tier;
    d.life = num((snap.life || {}).started);
    d.budget = budgetState(snap.api);
    d.lastCycle = num((snap.money || {}).last_update);
  }
  if (d.runningSince !== null && cycle > 0 && d.state !== "dead" && d.state !== "halted") {
    const since = Math.max(d.lastCycle === null ? 0 : d.lastCycle, d.runningSince);
    d.silence = at - since;
    d.stale = d.silence > STALE_CYCLES * cycle + STALE_MARGIN_S;
  }
  return d;
}

const TIER_TEXT = {
  normal: ["retour au palier normal", "Le portefeuille est remonté : les limites d'achat habituelles s'appliquent de nouveau."],
  cautious: ["palier prudent", "Le portefeuille a reculé depuis son plus haut (drawdown) : les limites d'achat sont réduites."],
  defensive: ["palier défensif", "Le portefeuille a nettement reculé depuis son plus haut (drawdown) : les achats sont bloqués, les ventes restent possibles."],
};

// Liste des notifications à émettre en passant de `prev` à `next` (deux résumés) pour le profil `name`.
function transitions(name, prev, next) {
  if (!prev || !next) return [];
  const out = [];
  if (next.stale && !prev.stale) {
    out.push({
      kind: "stale",
      title: `Le bot « ${name} » ne donne plus de nouvelles`,
      body: `Aucun cycle réussi depuis ${Math.round(next.silence / 60)} min alors qu'il est censé tourner. Regarde data/logs/${name}.log.`,
    });
  }
  if (prev.life === null || next.life !== prev.life) return out;   // première donnée ou nouvelle vie : référence

  if (next.state !== prev.state && next.state === "dead") {
    out.push({
      kind: "dead",
      title: `Le bot « ${name} » est mort`,
      // On n'affirme pas que tout est vendu : la liquidation peut être désactivée ou avoir échoué.
      body: `${next.reason || "Kill switch déclenché"}. Vérifie sur la page du profil qu'il ne reste aucune position. `
        + `Nouvelle vie : tradeagent reset --profile ${name}, en ligne de commande.`,
    });
  }
  if (next.state !== prev.state && next.state === "halted") {
    out.push({
      kind: "halted",
      title: `Le bot « ${name} » est suspendu`,
      body: "Trop d'erreurs d'affilée : rien n'a été vendu, le détail est sur la page du profil. "
        + `Reprise : tradeagent resume --profile ${name}, en ligne de commande.`,
    });
  }
  if (next.state === "alive" && next.tier !== prev.tier) {
    const [label, body] = TIER_TEXT[next.tier];
    out.push({ kind: "tier", title: `Bot « ${name} » : ${label}`, body });
  }
  if (next.budget !== prev.budget && next.budget === "day") {
    out.push({
      kind: "budget",
      title: `Bot « ${name} » : budget API du jour épuisé`,
      body: "L'agent est en pause jusqu'à demain (UTC). Le kill switch et les garde-fous continuent de tourner.",
    });
  }
  if (next.budget !== prev.budget && next.budget === "total") {
    out.push({
      kind: "budget",
      title: `Bot « ${name} » : budget API total épuisé`,
      body: "L'agent n'appelle plus le LLM. Le kill switch et les garde-fous continuent de tourner. Le plafond est dans config.yaml.",
    });
  }
  return out;
}

/*
 * Garde le dernier résumé de chaque profil et appelle `notify(profil, notification)` sur transition.
 * `fetchSnapshot(port)` renvoie l'instantané ou null ; `isRunning(profil)` vient du superviseur.
 */
class Watcher {
  constructor({ fetchSnapshot, isRunning, notify }) {
    this.fetchSnapshot = fetchSnapshot;
    this.isRunning = isRunning;
    this.notify = notify;
    this.seen = new Map();
    this.current = null;
  }

  // Jamais deux tours en même temps : un appel pendant un tour en cours attend ce tour, sans en lancer un autre.
  poll(profiles) {
    if (!this.current) {
      this.current = this.round(profiles).finally(() => { this.current = null; });
    }
    return this.current;
  }

  async round(profiles) {
    await Promise.all(profiles.map(async ({ name, port }) => {
      const prev = this.seen.get(name) || null;
      const next = digest(await this.fetchSnapshot(port), prev, this.isRunning(name));
      if (!next) return;
      this.seen.set(name, next);
      for (const note of transitions(name, prev, next)) this.notify(name, note);
    }));
  }

  view() {
    return Object.fromEntries(this.seen);
  }
}

module.exports = { digest, transitions, Watcher, STALE_CYCLES, STALE_MARGIN_S };
