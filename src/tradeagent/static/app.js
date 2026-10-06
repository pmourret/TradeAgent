"use strict";
/*
 * Interface en LECTURE SEULE, direction « Le conseil » : elle interroge /api/snapshot et affiche ce qu'elle reçoit.
 * Règles à ne pas casser (voir tests/test_web.py) :
 *  - tout texte venant de la base (raison donnée par le LLM, messages...) passe par textContent,
 *    jamais par innerHTML : un LLM ne doit pas pouvoir injecter de HTML ou de script ;
 *  - aucun style en ligne ni setAttribute("style") : la CSP les interdit, on passe par des classes et par element.style ;
 *  - aucune requête hors de ce serveur, aucun bouton qui agit sur le bot.
 * Le mouvement ne montre que deux choses vraies : le temps qui passe vers le prochain cycle, et l'arrivée d'une
 * décision. Il se déclenche en comparant l'instantané reçu au précédent, jamais tout seul.
 */
(function () {
  const REFRESH_MS = 15000;
  const NBSP = " ";
  const SVGNS = "http://www.w3.org/2000/svg";
  const JOURNAL_GROUPS = 4;       // la feuille de style en masque un en mode compact
  const EVENT_GROUPS = 6;
  // Noms lisibles, fixés ici tant que l'instantané ne les donne pas (`sleeve_label`).
  const SLEEVES = { trend: "Suivi de tendance" };
  const AGENTS = { hold: "Référence hold", buyhold: "Achat puis conservation", quant: "Modèles quantitatifs",
    llm: "Agent LLM", "llm-fake": "Agent LLM simulé", chaos: "Agent aléatoire" };

  const state = {
    snap: null,           // dernier instantané reçu
    view: null,           // ce qu'on en déduit (mort, arrêté, muet, palier)
    receivedAt: 0,        // horloge du navigateur à la réception : sert à estimer l'heure du serveur entre deux lectures
    failed: false,
    order: null,          // dernier ordre vu arriver ; reste affiché jusqu'à la décision suivante
    fresh: { entry: false, order: false, equity: false },
  };

  const $ = (id) => document.getElementById(id);
  const page = $("page");
  const context = document.body.dataset.context === "web" ? "web" : "electron";
  const profile = /^[A-Za-z0-9_-]+$/.test(document.body.dataset.profile || "") ? document.body.dataset.profile : "";
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)");

  // ---------------------------------------------------------------- outils DOM
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function svg(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const key of Object.keys(attrs || {})) node.setAttribute(key, attrs[key]);
    return node;
  }
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }
  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }
  // Vrai si `key` a changé depuis le dernier appel : évite de reconstruire (et de relancer une animation) pour rien.
  function changed(node, key) {
    if (node.dataset.key === key) return false;
    node.dataset.key = key;
    return true;
  }
  // Rejoue une animation CSS : retirer la classe, forcer la mise en page, la remettre.
  function replay(node, cls) {
    node.classList.remove(cls);
    void node.offsetWidth;
    node.classList.add(cls);
  }

  // ---------------------------------------------------------------- formats
  let ccy = "EUR";
  const formatters = {};
  function nf(min, max) {
    const key = min + "/" + max;
    if (!formatters[key]) formatters[key] = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: min, maximumFractionDigits: max });
    return formatters[key];
  }
  const num = (n, d) => nf(d === undefined ? 2 : d, d === undefined ? 2 : d).format(n);
  const sym = () => ({ EUR: "€", USD: "$", GBP: "£" })[ccy] || ccy;
  const money = (n, d) => num(n, d) + NBSP + sym();
  const price = (n) => money(n, n >= 10000 ? 0 : 2);
  const quantity = (n) => nf(0, 6).format(n);
  // Le signe est toujours écrit : + − ou ± quand le résultat est nul à la précision affichée.
  function signOf(n, d) {
    const half = 0.5 * Math.pow(10, -(d === undefined ? 2 : d));
    return n >= half ? "pos" : n <= -half ? "neg" : "zero";
  }
  const glyphOf = { pos: "+", neg: "−", zero: "±" };
  const signed = (n, d) => glyphOf[signOf(n, d)] + num(Math.abs(n), d);
  const signedMoney = (n, d) => signed(n, d) + NBSP + sym();
  function setResult(node, text, sign) {
    node.textContent = text;
    node.classList.toggle("sign-pos", sign === "pos");
    node.classList.toggle("sign-neg", sign === "neg");
  }

  const dayFmt = new Intl.DateTimeFormat("fr-FR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const dateFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const clockFmt = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });
  const weekdayFmt = new Intl.DateTimeFormat("fr-FR", { weekday: "long" });
  const longDateFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "long" });

  const clock = (ts) => clockFmt.format(new Date(ts * 1000));
  function when(ts, now) {
    const d = new Date(ts * 1000);
    return now - ts < 6 * 86400 ? dayFmt.format(d) : dateFmt.format(d);
  }
  // « vendredi à 14:15 », ou « le 2 octobre à 14:15 » au-delà de six jours.
  function longWhen(ts, now) {
    const d = new Date(ts * 1000);
    return (now - ts < 6 * 86400 ? weekdayFmt.format(d) : "le " + longDateFmt.format(d)) + " à " + clock(ts);
  }
  // Début d'un regroupement : l'heure seule si c'est le même jour que sa fin.
  function since(oldest, newest, now) {
    return new Date(oldest * 1000).toDateString() === new Date(newest * 1000).toDateString() ? clock(oldest) : when(oldest, now);
  }
  function dur(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 172800) {
      const h = Math.floor(s / 3600);
      const m = Math.round((s % 3600) / 60);
      return m ? h + " h " + String(m).padStart(2, "0") : h + " h";
    }
    return Math.round(s / 86400) + " j";
  }
  // Durée d'une vie : « 12 j 4 h ».
  function lifeDur(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 86400) return dur(s);
    const h = Math.floor((s % 86400) / 3600);
    return Math.floor(s / 86400) + " j" + (h ? " " + h + " h" : "");
  }
  // Âge des dernières données quand la connexion est perdue : « 2 min 10 s ».
  function preciseDur(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min " + (s % 60) + " s";
    return dur(s);
  }
  function countdown(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);

  function icon(kind, size) {
    const base = { width: size || 16, height: size || 16, viewBox: "0 0 16 16", "aria-hidden": "true" };
    const stroke = Object.assign({ fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round" }, base);
    let root;
    if (kind === "alive") {
      root = svg("svg", base);
      root.appendChild(svg("circle", { cx: 8, cy: 8, r: 4.5, fill: "currentColor" }));
    } else if (kind === "cautious") {
      root = svg("svg", stroke);
      root.appendChild(svg("path", { d: "M8 2.5 14 13H2z" }));
      root.appendChild(svg("path", { d: "M8 6.5v3" }));
    } else if (kind === "defensive") {
      root = svg("svg", stroke);
      root.appendChild(svg("path", { d: "M8 1.8 13.5 4v4.2c0 3-2.4 5-5.5 6-3.1-1-5.5-3-5.5-6V4z" }));
    } else if (kind === "dead") {
      root = svg("svg", stroke);
      root.appendChild(svg("circle", { cx: 8, cy: 8, r: 6 }));
      root.appendChild(svg("path", { d: "m5.5 5.5 5 5m0-5-5 5" }));
    } else if (kind === "mute") {
      root = svg("svg", stroke);
      root.appendChild(svg("circle", { cx: 8, cy: 8, r: 6 }));
      root.appendChild(svg("path", { d: "M8 4.8V8l2 1.4" }));
    } else if (kind === "offline") {
      root = svg("svg", stroke);
      root.appendChild(svg("path", { d: "M2 6.5a9 9 0 0 1 12 0M4.5 9a5.4 5.4 0 0 1 7 0M2.5 2.5l11 11" }));
      root.appendChild(svg("circle", { cx: 8, cy: 12, r: 0.8, fill: "currentColor" }));
    } else if (kind === "chevron") {
      root = svg("svg", { width: 14, height: 14, viewBox: "0 0 12 12", "aria-hidden": "true", class: "chevron" });
      root.appendChild(svg("path", { d: "M2 2l4 4-4 4", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round" }));
    } else {
      root = svg("svg", Object.assign({ fill: "currentColor" }, base));
      root.appendChild(svg("rect", { x: 3.5, y: 3, width: 3, height: 10, rx: 0.8 }));
      root.appendChild(svg("rect", { x: 9.5, y: 3, width: 3, height: 10, rx: 0.8 }));
    }
    return root;
  }

  // ---------------------------------------------------------------- ce qu'on déduit de l'instantané
  // Un agent sans LLM (hold, board) ne paie aucun loyer : tout ce qui parle d'API est alors masqué.
  function usesApi(s) {
    return s.api.calls_life > 0 || s.agent === "llm" || s.agent === "llm-fake";
  }

  function viewOf(s) {
    const dead = s.status.state === "dead", halted = s.status.state === "halted";
    const idle = s.generated_at - s.money.last_update;
    const mute = !dead && !halted && idle > 2.5 * s.cycle_seconds + 60;
    const tier = dead || halted ? "normal" : s.risk_tier;
    const board = s.agent === "board";
    const v = { dead: dead, halted: halted, mute: mute, idle: idle, tier: tier, board: board,
      live: !dead && !halted && !mute, who: board ? "Le board" : "L'agent", mood: "" };
    if (dead) Object.assign(v, { label: "Mort", icon: "dead", tone: "dead", mood: "mood-dead" });
    else if (halted) Object.assign(v, { label: "Arrêté", icon: "halted", tone: "orange", mood: "mood-halted" });
    // Agent à l'arrêt (budget d'API épuisé...) : le code dresse le bilan et conseille, l'utilisateur décide.
    else if (s.advice && s.advice.decision) Object.assign(v, { label: "À l'arrêt", icon: "halted", tone: "orange" });
    else if (s.advice) Object.assign(v, { label: "En pause", icon: "cautious", tone: "warn" });
    else if (tier === "defensive") Object.assign(v, { label: "En vie · palier défensif", icon: "defensive", tone: "orange", mood: "mood-defensif" });
    else if (tier === "cautious") Object.assign(v, { label: "En vie · palier prudent", icon: "cautious", tone: "warn", mood: "mood-prudent" });
    else Object.assign(v, { label: "En vie · palier normal", icon: "alive", tone: "accent" });
    if (!v.mood && tier === "defensive") v.mood = "mood-defensif";
    if (!v.mood && tier === "cautious") v.mood = "mood-prudent";
    return v;
  }

  // Heure du serveur estimée entre deux lectures : celle de l'instantané, plus le temps écoulé ici depuis sa réception.
  function serverNow() {
    return state.snap.generated_at + (Date.now() - state.receivedAt) / 1000;
  }

  // D'où vient une décision, et sa raison sans le préfixe « trend: » que le board écrit devant.
  function sourceOf(j, sleeves) {
    const text = j.reasoning || "";
    const match = /^\s*([A-Za-z][\w-]*)\s*:\s+([\s\S]*)$/.exec(text);
    if (j.sleeve) return { from: j.sleeve, reason: match && match[1] === j.sleeve ? match[2] : text };
    if (match && (sleeves.indexOf(match[1]) >= 0 || match[1] === j.agent)) return { from: match[1], reason: match[2] };
    return { from: j.agent, reason: text };
  }

  function outcomeOf(j) {
    if (j.outcome) return j.outcome;
    if (j.action === "hold" || j.approved === null || j.approved === undefined) return "none";
    if (!j.approved) return "refused";
    return /réduit/i.test(j.verdict || "") ? "reduced" : "executed";
  }

  // Ce que le code a répondu, sans les mots qui ne disent rien de plus que l'étiquette.
  function detailOf(j) {
    const text = (j.verdict || "").replace(/^approuvé(,\s*)?/i, "");
    return text === "hold" ? "" : text;
  }

  function sleeveNames(s) {
    const names = [];
    for (const c of s.board || []) if (names.indexOf(c.sleeve) < 0) names.push(c.sleeve);
    return names.length ? names : Object.keys(SLEEVES);
  }

  // ---------------------------------------------------------------- rendu : ambiance, bandeau
  function renderMood(v) {
    for (const mood of ["mood-prudent", "mood-defensif", "mood-dead", "mood-halted"]) page.classList.toggle(mood, v.mood === mood);
  }

  function bannerOf(s, v) {
    const now = s.generated_at;
    if (state.failed) {
      return { icon: "offline", tone: "muted", title: "Connexion au serveur perdue",
        text: "Dernières données reçues il y a " + preciseDur((Date.now() - state.receivedAt) / 1000) +
          ". Nouvel essai toutes les 15 s ; ce qui suit est figé à " + clock(now) + "." };
    }
    if (v.dead) {
      const at = s.status.since ? "Le kill switch s'est déclenché " + longWhen(s.status.since, now) : "Le kill switch s'est déclenché";
      return { icon: "dead", tone: "dead", title: "L'agent est mort",
        text: at + (s.status.reason ? " : " + s.status.reason : "") +
          ". Plus aucun appel ; il ne repartira qu'avec une nouvelle vie (tradeagent reset)." };
    }
    if (v.halted) {
      return { icon: "halted", tone: "orange", title: "Bot arrêté, sans liquidation",
        text: (s.status.reason ? capital(s.status.reason) + ". " : "") + "Les positions sont conservées. Cherche la cause, puis tradeagent resume." };
    }
    if (v.mute) {
      return { icon: "mute", tone: "warn", title: "Le bot ne donne plus de nouvelles",
        text: "Aucun cycle depuis " + dur(v.idle) + " alors qu'un cycle a lieu toutes les " + dur(s.cycle_seconds) +
          ". Le processus tourne-t-il ? Les chiffres ci-dessous datent de " + clock(s.money.last_update) + "." };
    }
    if (s.advice) return { icon: v.icon, tone: v.tone, title: s.advice.title, text: s.advice.text };
    if (v.tier === "defensive") {
      return { icon: "defensive", tone: "orange", title: "Palier défensif : achats bloqués",
        text: "Drawdown de " + num(s.money.drawdown_pct, 1) + " % depuis le plus haut. " + v.who +
          " ne peut plus que vendre jusqu'à ce que l'equity remonte." };
    }
    return null;
  }

  function renderBanner(s, v) {
    const b = bannerOf(s, v), box = $("banner");
    box.hidden = !b;
    if (!b) {
      delete box.dataset.key;
      return;
    }
    if (changed(box, b.icon + "|" + b.tone)) {
      const holder = $("bannerIcon");
      holder.className = "banner-icon tone-" + b.tone;
      clear(holder);
      holder.appendChild(icon(b.icon, 20));
    }
    setText($("bannerTitle"), b.title);
    setText($("bannerText"), b.text);
  }

  // ---------------------------------------------------------------- rendu : sous-agents
  function sleeveCards(s, v) {
    const exit = state.order && state.order.action === "sell" ? state.order : null;
    function posture(n, name, verb, none) {
      if (v.dead) return ["rest", "Au repos : l'agent est mort"];
      if (v.halted) return ["pause", "En suspens, cibles gelées"];
      if (v.mute) return ["unknown", "Pas de nouvelles depuis " + dur(v.idle)];
      if (exit && (!exit.sleeve || exit.sleeve === name)) return ["exit", "Sort de " + exit.symbol];
      return n ? ["claim", verb + " " + n + (n > 1 ? " positions" : " position")] : ["rest", none];
    }
    if (v.board) {
      return sleeveNames(s).map((name) => {
        const claims = (s.board || []).filter((c) => c.sleeve === name);
        const label = (claims[0] && claims[0].sleeve_label) || SLEEVES[name] || name;
        return { name: label, posture: posture(claims.length, name, "Réclame", "Au repos"), none: "Aucune position réclamée.",
          rows: claims.map((c) => [c.symbol, c.value === null ? "n. d." : money(c.value)]) };
      });
    }
    const held = s.positions.filter((p) => p.quantity > 0);
    return [{ name: AGENTS[s.agent] || s.agent || "Agent", posture: posture(held.length, null, "Tient", "Aucune position"),
      none: "Aucune position tenue.", rows: held.map((p) => [p.symbol, p.value === null ? "n. d." : money(p.value)]) }];
  }

  function presence(kind, fresh) {
    const node = el("div", "presence is-" + kind);
    if (kind === "pause") {
      node.appendChild(icon("halted", 18));
      return node;
    }
    if (kind === "claim") node.appendChild(el("span", "halo"));
    node.appendChild(el("span", "dot"));
    if (kind === "exit") {
      const chevron = icon("chevron");
      if (fresh) chevron.classList.add("is-fresh");
      node.appendChild(chevron);
    }
    return node;
  }

  function renderSleeves(s, v) {
    const cards = sleeveCards(s, v);
    $("sleevesTitle").textContent = v.board ? "Sous-agents · " + cards.length : "Agent";
    const list = $("sleeveList");
    // Reconstruire relancerait le halo toutes les 15 s : on ne touche à rien tant que rien n'a changé.
    if (!changed(list, JSON.stringify(cards)) && !state.fresh.order) return;
    clear(list);
    for (const card of cards) {
      const box = el("div", "sleeve");
      const head = el("div", "sleeve-head");
      head.appendChild(presence(card.posture[0], state.fresh.order));
      const text = el("div", "sleeve-text");
      text.appendChild(el("span", "sleeve-name", card.name));
      text.appendChild(el("span", "sleeve-posture", card.posture[1]));
      head.appendChild(text);
      box.appendChild(head);
      for (const row of card.rows) {
        const line = el("div", "claim-row");
        line.appendChild(el("span", null, row[0]));
        line.appendChild(el("span", null, row[1]));
        box.appendChild(line);
      }
      if (!card.rows.length) box.appendChild(el("span", "no-claims", card.none));
      box.appendChild(el("div", "slot-actions"));     // emplacement réservé, vide : rien n'agit sur un agent ici
      list.appendChild(box);
    }
  }

  // ---------------------------------------------------------------- rendu : portefeuille
  function renderPortfolio(s, v) {
    const m = s.money;
    const holder = $("statusIcon");
    if (changed(holder, v.icon + "|" + v.tone)) {
      holder.className = "status-icon tone-" + v.tone;
      clear(holder);
      holder.appendChild(icon(v.icon, 14));
    }
    $("statusLabel").textContent = v.label;

    $("equity").textContent = money(m.equity);
    const box = $("equityBox");
    if (state.fresh.equity) replay(box, "value-changed");
    else box.classList.remove("value-changed");

    const tiny = Math.abs(m.net_result) > 0 && Math.abs(m.net_result) < 0.01 ? 4 : 2;
    setResult($("net"), signedMoney(m.net_result, tiny), signOf(m.net_result, tiny));
    $("drawdown").textContent = num(m.drawdown_pct, 1) + " %";
    $("life").textContent = v.dead && s.status.since
      ? "vie de " + lifeDur(s.status.since - s.life.started)
      : lifeDur(s.generated_at - s.life.started) + " de vie";

    // Le résultat net est valorisé aux prix du marché ; vendre coûterait encore glissement et frais.
    const exit = $("liquidation");
    exit.hidden = !(typeof m.liquidation_result === "number" && m.exit_costs >= 0.005);
    if (!exit.hidden) {
      exit.textContent = "";
      exit.appendChild(document.createTextNode("Si tout était vendu maintenant "));
      const b = el("b");
      setResult(b, signedMoney(m.liquidation_result), signOf(m.liquidation_result));
      exit.appendChild(b);
      exit.appendChild(document.createTextNode(" (frais de sortie " + money(m.exit_costs) + ")"));
    }

    const rent = $("rent");
    rent.hidden = !usesApi(s);
    if (!rent.hidden) {
      const spent = s.api.spent_life;
      rent.textContent = "Net du loyer : " + money(spent, spent > 0 && spent < 0.01 ? 4 : 2) + " d'API payés pendant cette vie (" + s.api.calls_life + " appels).";
    }

    // Une position reste affichée tant qu'il en reste : après une mort, c'est ce qui dit si la liquidation a tout vendu.
    const rows = $("positions"), alloc = $("alloc");
    clear(rows);
    clear(alloc);
    const entries = [];
    s.positions.forEach((p, i) => {
      if (p.quantity > 0) entries.push({ name: p.symbol, qty: quantity(p.quantity), value: p.value, share: p.share_pct, cls: "sw-" + (i % 4) });
    });
    entries.push({ name: "Cash", qty: "", value: m.cash, share: m.equity > 0 ? (m.cash / m.equity) * 100 : null, cls: "sw-cash" });
    for (const e of entries) {
      const row = el("div", "pos-row");
      const name = el("span", "pos-name");
      name.appendChild(el("span", "swatch " + e.cls));
      name.appendChild(el("span", null, e.name));
      if (e.qty) name.appendChild(el("span", "pos-qty", e.qty));
      row.appendChild(name);
      row.appendChild(el("span", null, e.value === null ? "n. d." : money(e.value)));
      row.appendChild(el("span", null, e.share === null ? "n. d." : num(e.share, 0) + " %"));
      rows.appendChild(row);
      if (e.share !== null && e.share > 0) {
        const part = el("div", e.cls);
        part.style.width = Math.min(100, e.share) + "%";
        alloc.appendChild(part);
      }
    }

    const d = s.day, max = s.thresholds.max_buys_per_day;
    if (d) setResult($("dayPnl"), signedMoney(d.pnl) + " (" + signed(d.pnl_pct) + " %)", signOf(d.pnl));
    else setResult($("dayPnl"), "aucun cycle", "zero");
    $("buysBox").hidden = !d;
    if (d) {
      const pips = $("buyPips");
      clear(pips);
      if (max <= 12) for (let i = 0; i < max; i++) pips.appendChild(el("i", i < d.buys ? "used" : ""));
      $("buys").textContent = d.buys + " sur " + max;
    }

    const announce = $("announce");
    const order = state.order;
    const text = v.dead ? "Vérifie ci-dessus qu'il ne reste aucune position : la liquidation peut avoir échoué."
      : order ? (order.refused ? "Ordre refusé : " : "Ordre reçu : ") + order.text : "";
    if (changed(announce, text) || state.fresh.order) {
      clear(announce);
      if (text) announce.appendChild(el("div", v.dead ? "dead-note" : "order-received" + (state.fresh.order ? " is-fresh" : ""), text));
    }

    // L'ordre qui part : un point qui va des sous-agents au portefeuille, une seule fois.
    const dot = $("orderDot");
    dot.hidden = !state.fresh.order;
    if (state.fresh.order) replay(dot, "order-dot");
    else dot.classList.remove("order-dot");
  }

  // Les références à battre. Chacune n'est affichée que si l'instantané la donne, jamais devinée ici : hold
  // (`reference`, lue dans la base du profil hold) et le marché (`market_reference`, buyhold acheté au début de
  // cette vie, valorisé aux derniers prix).
  function refPart(tag, title, sub, value, gap, who) {
    const part = el(tag, "ref-part");
    part.appendChild(el("span", "over", title));
    part.appendChild(el("span", "ref-sub", sub));
    part.appendChild(el("span", "ref-equity", money(value)));
    const sign = signOf(gap);
    const line = el("span", "ref-gap", "Écart " + who + " ");
    const b = el("b");
    setResult(b, signedMoney(gap), sign);
    line.appendChild(b);
    line.appendChild(document.createTextNode(" "));
    line.appendChild(el("i", null, sign === "pos" ? "devant" : sign === "neg" ? "derrière" : "à égalité"));
    part.appendChild(line);
    return part;
  }
  function renderReference(s, v) {
    const box = $("reference"), ref = s.reference, mkt = s.market_reference;
    const hold = ref && typeof ref.equity === "number" && typeof ref.net_result === "number" && /^[A-Za-z0-9_-]+$/.test(ref.profile || "");
    const market = mkt && typeof mkt.equity === "number" && typeof mkt.net_result === "number" && mkt.weights_pct;
    box.hidden = !hold && !market;
    if (box.hidden) return;
    clear(box);
    const card = el("div", "reference"), who = v.board ? "du board" : "de l'agent";
    if (hold) {
      const part = refPart(context === "web" ? "a" : "div", "Référence · " + ref.profile, "Ne fait rien. À battre.",
        ref.equity, s.money.net_result - ref.net_result, who);
      if (context === "web") part.setAttribute("href", "../" + ref.profile + "/");
      card.appendChild(part);
    }
    if (market) {
      const shares = Object.keys(mkt.weights_pct).map((k) => num(mkt.weights_pct[k], 0) + " % " + k.split("/")[0]);
      const sub = shares.join(" et ") + (mkt.late ? " achetés " + longWhen(mkt.started, s.generated_at) + ", gardés." : " achetés au début de la vie, gardés.");
      card.appendChild(refPart("div", "Marché · buyhold", sub, mkt.equity, s.money.net_result - mkt.net_result, who));
    }
    box.appendChild(card);
  }

  // ---------------------------------------------------------------- rendu : marges
  function marginWord(pct) {
    return pct < 1.5 ? "tendu" : pct < 4 ? "attentif" : "large";
  }

  function renderMargins(s, v) {
    const t = s.thresholds, max = t.max_drawdown_pct, dd = s.money.drawdown_pct;
    const rail = $("deathRail"), marks = $("deathMarks");
    if (changed(marks, [t.cautious_drawdown_pct, t.defensive_drawdown_pct, max].join("|"))) {
      clear(marks);
      rail.style.setProperty("--cut1", (t.cautious_drawdown_pct / max) * 100 + "%");
      rail.style.setProperty("--cut2", (t.defensive_drawdown_pct / max) * 100 + "%");
      for (const item of [[t.cautious_drawdown_pct, "prudent"], [t.defensive_drawdown_pct, "défensif"]]) {
        const mark = el("span", "rail-mark", item[1] + " " + num(item[0], 0) + " %");
        mark.style.left = (item[0] / max) * 100 + "%";
        marks.appendChild(mark);
      }
      marks.appendChild(el("span", "rail-mark is-end", "mort " + num(max, 0) + " %"));
    }
    rail.style.setProperty("--death", String(Math.max(0, Math.min(1, dd / max))));
    const ratio = num(dd, 1) + " % sur " + num(max, 0) + " %";
    $("deathText").textContent = v.dead ? (dd >= max ? "seuil atteint : " : "agent mort : ") + ratio
      : ratio + " · encore " + num(Math.max(0, max - dd), 1) + " pts";

    // Une barre par niveau de sortie. Les barres sont gardées d'une lecture à l'autre pour que leur largeur glisse.
    const box = $("claimRails"), claims = v.dead ? [] : (s.board || []).filter((c) => c.stop);
    if (changed(box, claims.map((c) => c.sleeve + "/" + c.symbol).join("|"))) {
      clear(box);
      for (let i = 0; i < claims.length; i++) {
        const group = el("div", "rail-group");
        const head = el("div", "rail-head");
        const label = el("span");
        label.appendChild(el("span", "rail-name"));
        label.appendChild(document.createTextNode(" "));
        label.appendChild(el("i"));
        head.appendChild(label);
        head.appendChild(el("span", "rail-value"));
        group.appendChild(head);
        const track = el("div", "rail");
        track.appendChild(el("div", "margin-fill claim-fill"));
        group.appendChild(track);
        box.appendChild(group);
      }
    }
    claims.forEach((c, i) => {
      const group = box.children[i], pct = c.stop_margin_pct;
      group.querySelector(".rail-name").textContent = "Sortie " + c.symbol;
      group.querySelector("i").textContent = "à " + price(c.stop);
      group.querySelector(".rail-value").textContent = pct === null ? "n. d." : signed(pct, 1) + " % · " + marginWord(pct);
      const fill = group.querySelector(".claim-fill");
      // Échelle de 0 à 8 % de marge : au-delà, la barre est pleine.
      const room = pct === null ? 1 : Math.max(0, Math.min(pct / 8, 1));
      fill.hidden = pct === null;
      fill.style.width = 100 - (1 - room) * 94 + "%";
      fill.classList.toggle("is-tense", pct !== null && pct < 1.5 && v.live && !state.failed);
    });
  }

  // ---------------------------------------------------------------- rendu : courbe
  function renderChart(s) {
    const box = $("chart"), pts = s.equity_series, m = s.money, lines = m.tier_lines;
    clear(box);
    $("chartMeta").textContent = "";
    if (!pts.length) return;
    const vals = pts.map((p) => p[1]);
    const lowest = Math.min(...vals), highest = Math.max(...vals);
    const pad = (highest - lowest) * 0.14 + m.stake * 0.004 || 1;
    const lo = lowest - pad, hi = highest + pad;
    const y = (value) => ((hi - value) / (hi - lo)) * 100;
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0], span = Math.max(1, t1 - t0);
    const x = (ts) => (pts.length < 2 ? 300 : ((ts - t0) / span) * 300);

    const chart = svg("svg", { viewBox: "0 0 300 100", preserveAspectRatio: "none", "aria-hidden": "true" });
    const path = pts.length < 2
      ? "M0," + y(vals[0]).toFixed(2) + " L300," + y(vals[0]).toFixed(2)
      : "M" + pts.map((p) => x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(2)).join(" L");
    chart.appendChild(svg("path", { d: path, class: "curve", "vector-effect": "non-scaling-stroke" }));
    box.appendChild(chart);

    const marks = [[m.stake, "mise " + money(m.stake, 0)], [lines.cautious, "prudent sous " + money(lines.cautious)],
      [lines.defensive, "défensif sous " + money(lines.defensive)], [lines.death, "mort sous " + money(lines.death)]];
    for (const mark of marks) {
      if (!(mark[0] > lo && mark[0] < hi)) continue;       // seulement les niveaux présents dans l'échelle
      const line = el("div", "chart-line");
      line.style.top = y(mark[0]).toFixed(2) + "%";
      line.appendChild(el("span", null, mark[1]));
      box.appendChild(line);
    }
    const end = el("div", "chart-end");
    end.style.top = y(vals[vals.length - 1]).toFixed(2) + "%";
    box.appendChild(end);

    $("chartMeta").textContent = dur(span) + " · nette du loyer";
    box.setAttribute("aria-label", "Courbe d'equity nette du loyer, de " + money(vals[0]) + " à " + money(vals[vals.length - 1]) +
      ". Prudent sous " + money(lines.cautious) + ", défensif sous " + money(lines.defensive) + ", mort sous " + money(lines.death) + ".");
  }

  // ---------------------------------------------------------------- rendu : loyer d'inférence
  function renderApi(s) {
    const a = s.api, block = $("apiBlock");
    block.hidden = !usesApi(s);
    if (block.hidden) return;
    $("apiCadence").textContent = "1 appel toutes les " + dur(a.call_every_seconds);

    const meters = $("apiMeters");
    clear(meters);
    for (const m of [["Aujourd'hui", a.spent_today, a.daily_budget], ["Total de l'expérience", a.spent_total, a.total_budget]]) {
      const ratio = m[2] > 0 ? m[1] / m[2] : 0;
      const wrap = el("div");
      const head = el("div", "meter-head");
      head.appendChild(el("span", null, m[0]));
      head.appendChild(el("span", null, money(m[1], 3) + " sur " + money(m[2], 2)));
      const bar = el("div", "meter" + (ratio >= 1 ? " is-dead" : ratio >= 0.8 ? " is-warn" : ""));
      const fill = el("i");
      fill.style.width = Math.min(100, ratio * 100) + "%";
      bar.appendChild(fill);
      wrap.appendChild(head);
      wrap.appendChild(bar);
      meters.appendChild(wrap);
    }

    const bars = $("apiBars"), labels = $("apiBarLabels");
    clear(bars);
    clear(labels);
    const top = Math.max(0, ...a.per_day.map((d) => d.cost));
    a.per_day.forEach((d, i) => {
      const bar = el("i", i === a.per_day.length - 1 ? "is-today" : "");
      bar.style.height = (top > 0 ? (d.cost / top) * 100 : 0) + "%";
      bar.title = d.date + " : " + money(d.cost, 3) + " (" + d.calls + " appels)";
      bars.appendChild(bar);
      labels.appendChild(el("span", null, String(parseInt(d.date.slice(8), 10))));
    });

    let note = "";
    if (s.status.state === "dead") note = "Plus aucun appel : l'agent est mort.";
    else if (s.status.state === "halted") note = "Bot arrêté : plus aucun appel tant que tu ne le reprends pas.";
    else if (a.spent_total >= a.total_budget) note = "Budget total épuisé : l'agent ne décide plus. Seul le kill switch veille encore.";
    else if (a.spent_today >= a.daily_budget) note = "Budget du jour épuisé : l'agent ne décide plus avant demain (UTC).";
    else if (s.advice) note = s.advice.title + ". Il n'est plus appelé et ne paie plus de loyer.";
    else if (a.next_call) {
      const wait = a.next_call - s.generated_at;
      note = wait > 60 ? "Prochain appel dans " + dur(wait) + "." : "Prochain appel imminent.";
      if (wait > 60 && a.wake_if_move_pct) note += " Réveil anticipé si un prix bouge de " + num(a.wake_if_move_pct, 1) + " %.";
    }
    $("apiNote").textContent = note;
  }

  // ---------------------------------------------------------------- rendu : journal, évènements
  // Les « attentes » consécutives sont regroupées en une seule entrée : sinon elles noient les ordres, qui sont
  // ce qu'on cherche dans ce journal. Les ventes de liquidation (faites par le moteur à la mort, hors de l'agent)
  // n'ont pas de décision : elles viennent des exécutions, et sont attribuées au kill switch.
  function journalGroups(s) {
    const sleeves = sleeveNames(s);
    const items = s.journal.map((j) => Object.assign({ key: j.ts, order: 0 }, { j: j, src: sourceOf(j, sleeves) }));
    for (const f of s.fills || []) {
      if (f.source !== "killswitch") continue;
      items.push({ key: f.ts, order: 1, src: { from: "kill switch", reason: "liquidation demandée par le kill switch" },
        j: { ts: f.ts, action: f.side, symbol: f.symbol, amount_quote: f.notional, approved: 1, verdict: "" } });
    }
    items.sort((a, b) => b.key - a.key || b.order - a.order);
    const groups = [];
    for (const item of items) {
      const last = groups[groups.length - 1], j = item.j;
      if (j.action !== "hold") groups.push({ hold: false, j: j, src: item.src });
      else if (last && last.hold && last.src.from === item.src.from) {
        last.count += 1;
        last.oldest = j.ts;
      } else groups.push({ hold: true, count: 1, newest: j.ts, oldest: j.ts, src: item.src });
    }
    return groups;
  }

  const TAGS = { executed: ["tag tag-accent", "Exécuté"], reduced: ["tag tag-outline", "Réduit"],
    refused: ["tag-line is-orange", "Refusé"], none: ["tag tag-neutral", "Aucun ordre"] };
  const ACTIONS = { buy: ["+", "Achat"], sell: ["−", "Vente"], hold: ["·", "Attente"] };

  function renderJournal(s) {
    const box = $("journal"), now = s.generated_at;
    clear(box);
    journalGroups(s).slice(0, JOURNAL_GROUPS).forEach((g, i) => {
      const j = g.hold ? { action: "hold" } : g.j;
      const fresh = i === 0 && state.fresh.entry;
      const entry = el("div", "entry" + (fresh ? " entry-new" : ""));
      const card = el("div", "entry-card");

      const top = el("div", "entry-top");
      top.appendChild(el("span", null, when(g.hold ? g.newest : j.ts, now)));
      top.appendChild(el("span", null, g.src.from + " → portefeuille"));
      if (fresh) top.appendChild(el("span", "new-label", "nouveau"));
      const tags = el("span", "entry-tags");
      const tag = TAGS[outcomeOf(j)] || TAGS.none;
      tags.appendChild(el("span", tag[0], tag[1]));
      top.appendChild(tags);
      card.appendChild(top);

      const action = ACTIONS[j.action] || ["·", j.action];
      const what = el("div", "entry-what");
      what.appendChild(el("b", null, action[0] + " " + action[1]));
      what.appendChild(document.createTextNode(" "));
      what.appendChild(el("span", null, g.hold
        ? (g.count > 1 ? g.count + " fois depuis " + since(g.oldest, g.newest, now) : "1 cycle")
        : (j.symbol || "n. d.") + (typeof j.amount_quote === "number" ? " · " + money(j.amount_quote) : "")));
      card.appendChild(what);

      const why = el("div", "entry-why", g.src.reason ? "«" + NBSP + g.src.reason + NBSP + "»" : "n. d.");
      const detail = g.hold ? "" : detailOf(j);
      if (detail) why.appendChild(el("span", null, " " + detail));
      card.appendChild(why);

      entry.appendChild(card);
      box.appendChild(entry);
    });
    $("journalMeta").textContent = nf(0, 0).format(s.counts.decisions) + " décisions · attentes regroupées";
  }

  const LEVELS = { info: ["tag tag-neutral", "info"], warning: ["tag-line is-warn", "alerte"],
    error: ["tag-line is-dead", "erreur"], critical: ["tag-line is-dead", "critique"] };

  function renderEvents(s) {
    const box = $("events"), now = s.generated_at;
    clear(box);
    // Même regroupement que pour le journal : dix erreurs identiques d'affilée = une seule entrée.
    const groups = [];
    for (const e of s.events) {
      const last = groups[groups.length - 1];
      if (last && last.level === e.level && last.message === e.message) {
        last.count += 1;
        last.oldest = e.ts;
      } else {
        groups.push({ level: e.level, message: e.message, count: 1, newest: e.ts, oldest: e.ts });
      }
    }
    for (const g of groups.slice(0, EVENT_GROUPS)) {
      const level = LEVELS[g.level] || ["tag-line", g.level];
      const item = el("div", "event");
      const head = el("div", "event-head");
      head.appendChild(el("span", level[0], level[1]));
      head.appendChild(el("span", null, when(g.newest, now) + (g.count > 1 ? " · ×" + g.count + " depuis " + since(g.oldest, g.newest, now) : "")));
      item.appendChild(head);
      item.appendChild(el("span", "event-msg", g.message));
      box.appendChild(item);
    }
    if (!groups.length) box.appendChild(el("span", "fine", "Aucun évènement."));
  }

  // ---------------------------------------------------------------- rendu : ce qui suit l'horloge
  function paintFreshness() {
    const node = $("updated"), dot = $("freshDot");
    dot.classList.toggle("is-off", state.failed);
    if (!state.snap) {
      setText(node, state.failed ? "Serveur injoignable…" : "Chargement…");
      return;
    }
    const age = (Date.now() - state.receivedAt) / 1000;
    setText(node, state.failed ? "Connexion perdue · il y a " + preciseDur(age) : "Actualisé il y a " + dur(age));
  }

  function paintCadence() {
    const s = state.snap, v = state.view, bar = $("cyclebar");
    bar.classList.toggle("is-late", !state.failed && v.mute);
    bar.classList.toggle("is-frozen", state.failed);
    const now = serverNow(), last = s.money.last_update, next = last + s.cycle_seconds;
    let lead, value, meta, deliberating = false;
    if (state.failed) {
      // Connexion perdue : le compte à rebours et la barre restent là où ils étaient.
      lead = "Dernier état connu :";
      value = v.dead ? "agent mort" : v.halted ? "bot arrêté" : "prochain conseil vers " + clock(next);
      meta = "compte à rebours suspendu";
    } else if (v.dead) {
      lead = "Plus aucun cycle depuis";
      value = s.status.since ? when(s.status.since, now) : "sa mort";
      meta = s.status.since ? "vie de " + lifeDur(s.status.since - s.life.started) : "";
      bar.style.setProperty("--cycle", "0");
    } else if (v.halted) {
      lead = "Cycles suspendus depuis";
      value = s.status.since ? when(s.status.since, now) : "l'arrêt";
      meta = "reprise manuelle seulement";
      bar.style.setProperty("--cycle", "0");
    } else if (v.mute) {
      lead = "Cycle attendu à " + clock(next) + ", en retard de";
      value = dur(now - next);
      meta = "dernier cycle à " + clock(last);
    } else {
      const left = next - now, elapsed = Math.max(0, now - last);
      lead = left > 0 ? "Prochain conseil dans" : "Prochain conseil";
      value = left > 0 ? countdown(left) : "imminent";
      const count = typeof s.cycles_life === "number" ? s.cycles_life : s.counts.decisions;
      meta = "cycle n° " + nf(0, 0).format(count) + " · toutes les " + dur(s.cycle_seconds);
      deliberating = left > 0 && left <= 30 && s.cycle_seconds >= 120;
      // Mouvement réduit : la barre avance par pas d'une minute.
      const shown = calm.matches ? Math.floor(elapsed / 60) * 60 : elapsed;
      bar.style.setProperty("--cycle", String(s.cycle_seconds > 0 ? Math.min(1, shown / s.cycle_seconds) : 0));
    }
    setText($("cadLead"), lead);
    setText($("cadValue"), value);
    setText($("cadMeta"), meta);
    const note = $("deliberating");
    note.hidden = !deliberating;
    if (deliberating) setText(note, v.who + " délibère");
  }

  function tick() {
    paintFreshness();
    if (state.snap && state.snap.has_data) paintCadence();
  }

  // ---------------------------------------------------------------- rendu complet
  function render() {
    const s = state.snap;
    ccy = s.quote_currency;
    $("empty").hidden = s.has_data;
    $("dash").hidden = !s.has_data;
    page.classList.toggle("is-frozen", state.failed);
    if (!s.has_data) {
      renderMood({ mood: "" });
      $("cyclebar").classList.remove("is-late", "is-frozen");
      $("cyclebar").style.setProperty("--cycle", "0");
      $("emptyCommand").textContent = profile ? "tradeagent run --profile " + profile : "tradeagent run";
      return;
    }
    const v = state.view = viewOf(s);
    renderMood(v);
    renderBanner(s, v);
    renderSleeves(s, v);
    renderPortfolio(s, v);
    renderReference(s, v);
    renderMargins(s, v);
    renderChart(s);
    renderApi(s);
    renderJournal(s);
    renderEvents(s);
  }

  // Compare l'instantané reçu au précédent : c'est la seule source des animations.
  function compare(previous, s) {
    const fresh = { entry: false, order: false, equity: false };
    if (!previous || !previous.has_data || !s.has_data) {
      if (!s.has_data) state.order = null;
      return fresh;
    }
    fresh.equity = previous.money.equity !== s.money.equity;
    const seen = previous.journal.length ? previous.journal[0].id : 0;
    const top = s.journal.length ? s.journal[0] : null;
    if (top && top.id > seen) {
      fresh.entry = true;
      state.order = null;
      if (top.action === "buy" || top.action === "sell") {
        const src = sourceOf(top, sleeveNames(s)), detail = detailOf(top);
        fresh.order = true;
        state.order = { action: top.action, symbol: top.symbol, sleeve: src.from === top.agent ? null : src.from,
          refused: outcomeOf(top) === "refused",
          text: (top.action === "buy" ? "achat " : "vente ") + top.symbol +
            (typeof top.amount_quote === "number" ? " · " + money(top.amount_quote) : "") + (detail ? ", " + detail : "") };
      }
    }
    return fresh;
  }

  // ---------------------------------------------------------------- boucle
  async function refresh() {
    try {
      const response = await fetch("api/snapshot", { cache: "no-store" });   // relatif : la page vit aussi sous /p/<profil>/
      if (response.status === 401) {     // interface distante, session expirée : la page renvoie vers la connexion
        window.location.reload();
        return;
      }
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "HTTP " + response.status);
      ccy = body.quote_currency;
      state.fresh = compare(state.snap, body);
      state.snap = body;
      state.receivedAt = Date.now();
      state.failed = false;
      render();
      replay($("freshDot"), "pinged");
    } catch (err) {
      state.failed = true;
      if (state.snap) {
        // Connexion perdue : tout se fige sur le dernier état connu, et on continue d'essayer.
        page.classList.add("is-frozen");
        if (state.snap.has_data) {
          renderBanner(state.snap, state.view);
          renderMargins(state.snap, state.view);
        }
      }
    }
    tick();
  }

  // La page s'adapte à sa propre largeur, pas à celle de l'écran : la fenêtre de l'application se redimensionne librement.
  function setMode(width) {
    page.dataset.mode = width < 600 ? "compact" : width < 1024 ? "moyen" : width < 1500 ? "large" : "tres-large";
  }
  setMode(page.getBoundingClientRect().width);
  if (window.ResizeObserver) new ResizeObserver((entries) => setMode(entries[0].contentRect.width)).observe(page);

  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
