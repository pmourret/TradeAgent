"use strict";
/*
 * Interface en LECTURE SEULE : elle interroge /api/snapshot et affiche ce qu'elle reçoit.
 * Règles à ne pas casser (voir tests/test_web.py) :
 *  - tout texte venant de la base (raison donnée par le LLM, messages...) passe par textContent,
 *    jamais par innerHTML : un LLM ne doit pas pouvoir injecter de HTML ou de script ;
 *  - aucun style en ligne ni setAttribute("style") : la CSP les interdit, on passe par element.style ;
 *  - aucune requête hors de ce serveur, aucun bouton qui agit sur le bot.
 */
(function () {
  const REFRESH_MS = 15000;
  const NBSP = " ";
  const SVGNS = "http://www.w3.org/2000/svg";
  const state = { snap: null, lastOk: 0, failed: false };

  const $ = (id) => document.getElementById(id);

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
  function setTone(node, base, tone) {
    node.className = base + (tone ? " tone-" + tone : "");
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
  const sign = (n) => (n > 0.00001 ? "+" : n < -0.00001 ? "−" : "");
  const signed = (n, d) => sign(n) + num(Math.abs(n), d);
  const dayFmt = new Intl.DateTimeFormat("fr-FR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const dateFmt = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  function when(ts, now) {
    const d = new Date(ts * 1000);
    return now - ts < 6 * 86400 ? dayFmt.format(d) : dateFmt.format(d);
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
  function niceTicks(lo, hi, wanted) {
    const raw = (hi - lo) / wanted;
    const pow = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map((k) => k * pow).find((s) => s >= raw);
    const ticks = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
    return { ticks, step };
  }

  // ---------------------------------------------------------------- état du bot
  function statusView(s) {
    const reason = s.status.reason ? s.status.reason + ". " : "";
    const dd = num(s.money.drawdown_pct, 1);
    if (s.status.state === "dead") {
      return { tone: "dead", label: "Mort", icon: "dead", sub: "Définitif jusqu'à un reset",
        banner: ["Le bot est mort", reason + "Plus aucun appel à l'agent ; il ne repartira qu'avec « tradeagent reset »."] };
    }
    if (s.status.state === "halted") {
      return { tone: "orange", label: "Arrêté", icon: "halted", sub: "Sans liquidation · reprise manuelle",
        banner: ["Bot arrêté (sans liquidation)", reason + "Les positions sont conservées. Cherche la cause, puis « tradeagent resume »."] };
    }
    if (s.risk_tier === "defensive") {
      return { tone: "orange", label: "Palier défensif", icon: "defensive", sub: "Achats bloqués, ventes seulement",
        banner: ["Palier défensif : achats bloqués", "Drawdown de " + dd + " % depuis le plus haut. L'agent ne peut plus que vendre jusqu'à ce que l'equity remonte."] };
    }
    if (s.risk_tier === "cautious") {
      return { tone: "warn", label: "Palier prudent", icon: "cautious", sub: "Tailles d'ordre réduites",
        banner: ["Palier prudent", "Drawdown de " + dd + " % depuis le plus haut. Les plafonds d'ordre et de position sont réduits."] };
    }
    return { tone: "ok", label: "En vie", icon: "alive", sub: "Palier normal · " + dur(s.generated_at - s.life.started) + " de vie", banner: null };
  }

  function icon(kind) {
    const base = { width: 16, height: 16, viewBox: "0 0 16 16", "aria-hidden": "true" };
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
    } else {
      root = svg("svg", Object.assign({ fill: "currentColor" }, base));
      root.appendChild(svg("rect", { x: 3.5, y: 3, width: 3, height: 10, rx: 0.8 }));
      root.appendChild(svg("rect", { x: 9.5, y: 3, width: 3, height: 10, rx: 0.8 }));
    }
    return root;
  }

  // ---------------------------------------------------------------- rendu
  function renderHeader(s) {
    const agent = $("chipAgent");
    agent.hidden = !s.agent;
    agent.textContent = s.agent ? "agent : " + s.agent : "";
    const model = $("chipModel");
    model.hidden = !(s.api.calls_life > 0 || s.agent === "llm");
    model.textContent = s.api.model;

    const note = $("botnote");
    const idle = s.generated_at - s.money.last_update;
    const stalled = s.status.state === "alive" && idle > 2.5 * s.cycle_seconds + 60;
    note.hidden = !stalled;
    note.textContent = stalled ? "Aucun cycle depuis " + dur(idle) + " — le bot tourne-t-il ?" : "";
  }

  function renderStatus(s) {
    const v = statusView(s);
    setTone($("pill"), "pill", v.tone);
    clear($("pillIcon"));
    $("pillIcon").appendChild(icon(v.icon));
    $("pillLabel").textContent = v.label;
    $("stateSub").textContent = v.sub;

    const banner = $("banner");
    banner.hidden = !v.banner;
    if (v.banner) {
      setTone(banner, "banner", v.tone);
      $("bannerTitle").textContent = v.banner[0];
      $("bannerText").textContent = v.banner[1];
    }
    return v;
  }

  function renderKpis(s) {
    const m = s.money, a = s.api;
    $("kEquity").textContent = money(m.equity);
    $("kEquitySub").textContent = signed(m.change_pct, 2) + " % par rapport à la mise de " + money(m.stake);
    $("kNet").textContent = signed(m.net_result, Math.abs(m.net_result) > 0 && Math.abs(m.net_result) < 0.01 ? 4 : 2) + NBSP + sym();
    $("kNetSub").textContent = "après " + money(a.spent_life, a.spent_life > 0 && a.spent_life < 0.01 ? 4 : 2) + " d'API (" + a.calls_life + " appels)";
    $("kDd").textContent = num(m.drawdown_pct, 1) + " %";
    $("kDdSub").textContent = "depuis le plus haut : " + money(m.peak_equity);
    $("kApi").textContent = money(a.spent_today, 3);
    $("kApiSub").textContent = "plafond du jour : " + money(a.daily_budget);
  }

  function renderChart(s, tone) {
    const plot = $("plot");
    clear(plot);
    const pts = s.equity_series;
    const m = s.money;
    if (!pts.length) return;

    const lines = m.tier_lines;
    const vals = pts.map((p) => p[1]);
    let hi = Math.max(m.stake, m.peak_equity, ...vals);
    let lo = Math.min(lines.death, ...vals);
    const pad = (hi - lo) * 0.06 || 1;
    hi += pad;
    lo -= pad;
    const yPct = (v) => ((hi - Math.min(Math.max(v, lo), hi)) / (hi - lo)) * 100;
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0], span = Math.max(1, t1 - t0);
    const xPct = (t) => (pts.length < 2 ? 100 : ((t - t0) / span) * 100);

    function zone(from, to, label, zoneTone) {
      const z = el("div", "zone tone-" + zoneTone);
      z.style.top = yPct(from) + "%";
      z.style.height = yPct(to) - yPct(from) + "%";
      z.appendChild(el("span", null, label));
      plot.appendChild(z);
    }
    zone(lines.cautious, lines.defensive, "PRUDENT", "warn");
    zone(lines.defensive, lines.death, "DÉFENSIF", "orange");
    zone(lines.death, lo, "MORT", "dead");

    const grid = niceTicks(lo, hi, 6);
    for (const v of grid.ticks) {
      const g = el("div", "gridline");
      g.style.top = yPct(v) + "%";
      g.appendChild(el("span", null, num(v, grid.step < 1 ? 1 : 0)));
      plot.appendChild(g);
    }
    const stake = el("div", "stakeline");
    stake.style.top = yPct(m.stake) + "%";
    stake.appendChild(el("span", null, "mise " + num(m.stake, 0) + NBSP + sym()));
    plot.appendChild(stake);

    if (pts.length > 1) {
      const line = pts.map((p) => xPct(p[0]).toFixed(2) + "," + yPct(p[1]).toFixed(2)).join(" ");
      const chart = svg("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" });
      chart.appendChild(svg("path", { d: "M0,100 L" + line.split(" ").join(" L") + " L100,100 Z", class: "area" }));
      chart.appendChild(svg("polyline", { points: line, class: "curve", "vector-effect": "non-scaling-stroke" }));
      plot.appendChild(chart);
    }
    const end = el("div", "endpoint tone-" + tone);
    end.style.left = xPct(t1) + "%";
    end.style.top = yPct(vals[vals.length - 1]) + "%";
    plot.appendChild(end);

    // Une graduation par minuit local ; au-delà de 8 jours on espace pour rester lisible.
    const days = Math.ceil(span / 86400);
    const every = Math.max(1, Math.ceil(days / 8));
    const cursor = new Date(t0 * 1000);
    cursor.setHours(24, 0, 0, 0);
    for (let i = 0; cursor.getTime() / 1000 <= t1 && i < 400; i++) {
      if (i % every === 0) {
        const tick = el("span", "xtick", cursor.getDate() + "/" + (cursor.getMonth() + 1));
        tick.style.left = xPct(cursor.getTime() / 1000) + "%";
        plot.appendChild(tick);
      }
      cursor.setDate(cursor.getDate() + 1);
    }

    $("chartMeta").textContent = dur(span) + " · " + pts.length + " points";
    $("chart").setAttribute("aria-label",
      "Courbe d'equity de " + money(vals[0]) + " à " + money(vals[vals.length - 1]) + ". Paliers : prudent sous " +
      money(lines.cautious, 1) + ", défensif sous " + money(lines.defensive, 1) + ", mort sous " + money(lines.death, 1) + ".");
  }

  function renderGauge(s, tone) {
    const t = s.thresholds, max = t.max_drawdown_pct, dd = s.money.drawdown_pct;
    const track = $("gaugeTrack");
    clear(track);
    for (const w of [t.cautious_drawdown_pct, t.defensive_drawdown_pct - t.cautious_drawdown_pct, max - t.defensive_drawdown_pct]) {
      const seg = el("div");
      seg.style.width = (w / max) * 100 + "%";
      track.appendChild(seg);
    }
    const fill = $("gaugeFill");
    setTone(fill, "gauge-fill", tone);
    fill.style.width = Math.min(100, (dd / max) * 100) + "%";

    const marks = $("gaugeMarks");
    clear(marks);
    const items = [[t.cautious_drawdown_pct, "Prudent"], [t.defensive_drawdown_pct, "Défensif"], [max, "Mort"]];
    items.forEach((item, i) => {
      const left = (item[0] / max) * 100 + "%";
      if (i < items.length - 1) {
        const cut = el("div", "gauge-cut");
        cut.style.left = left;
        marks.appendChild(cut);
      }
      const label = el("div", "gauge-mark");
      label.style.left = left;
      label.style.transform = i === items.length - 1 ? "translateX(-100%)" : "translateX(-50%)";
      label.appendChild(el("b", null, item[1]));
      label.appendChild(el("span", null, num(item[0], 0) + " %"));
      marks.appendChild(label);
    });
    $("gaugeText").textContent = "Drawdown actuel : " + num(dd, 1) + " % depuis le plus haut (" + money(s.money.peak_equity) +
      "). Mort sous " + money(s.money.tier_lines.death) + ".";
  }

  function kvRow(label, value) {
    const row = el("div");
    row.appendChild(el("span", null, label));
    row.appendChild(el("span", null, value));
    return row;
  }

  function renderDay(s) {
    const box = $("dayRows");
    clear(box);
    const d = s.day, t = s.thresholds;
    if (!d) {
      box.appendChild(kvRow("Variation du jour", "aucun cycle aujourd'hui"));
    } else {
      box.appendChild(kvRow("Variation du jour", signed(d.pnl, 2) + NBSP + sym() + " (" + signed(d.pnl_pct, 2) + " %)"));
      box.appendChild(kvRow("Plafond de perte", "−" + num(t.max_daily_loss_pct, 0) + " %"));
      box.appendChild(kvRow("Achats aujourd'hui", d.buys + " / " + t.max_buys_per_day));
    }
  }

  function renderPositions(s) {
    const rows = $("posRows"), alloc = $("alloc");
    clear(rows);
    clear(alloc);
    const equity = s.money.equity;
    const qty = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 0, maximumFractionDigits: 6 });
    const colors = ["var(--a1)", "var(--a2)"];
    const entries = s.positions.map((p, i) => ({
      name: p.symbol, color: colors[i % colors.length], qty: qty.format(p.quantity),
      price: p.price === null ? "—" : money(p.price, p.price >= 1000 ? 0 : 2),
      value: p.value, share: p.share_pct,
    }));
    entries.push({ name: "Cash " + ccy, color: "var(--a3)", qty: num(s.money.cash, 2), price: "—", value: s.money.cash,
      share: equity > 0 ? (s.money.cash / equity) * 100 : null });

    for (const e of entries) {
      const row = el("div", "trow");
      const name = el("span", "asset");
      const swatch = el("span", "swatch");
      swatch.style.background = e.color;
      name.appendChild(swatch);
      name.appendChild(el("span", null, e.name));
      row.appendChild(name);
      row.appendChild(el("span", "r", e.qty));
      row.appendChild(el("span", "r", e.price));
      row.appendChild(el("span", "r", e.value === null ? "—" : money(e.value)));
      row.appendChild(el("span", "r", e.share === null ? "—" : num(e.share, 0) + " %"));
      rows.appendChild(row);
      if (e.value !== null && e.value > 0 && equity > 0) {
        const part = el("div");
        part.style.width = (e.value / equity) * 100 + "%";
        part.style.background = e.color;
        alloc.appendChild(part);
      }
    }
  }

  function renderApi(s) {
    const a = s.api;
    $("apiCadence").textContent = a.calls_life > 0 || s.agent === "llm" ? "1 appel toutes les " + dur(a.call_every_seconds) : "";

    const meters = $("apiMeters");
    clear(meters);
    for (const m of [["Aujourd'hui", a.spent_today, a.daily_budget], ["Total de l'expérience", a.spent_total, a.total_budget]]) {
      const ratio = m[2] > 0 ? m[1] / m[2] : 0;
      const wrap = el("div");
      const head = el("div", "meter-head");
      head.appendChild(el("span", null, m[0]));
      head.appendChild(el("span", null, money(m[1], 3) + " / " + money(m[2], 2)));
      const bar = el("div", "meter" + (ratio >= 1 ? " tone-dead" : ratio >= 0.8 ? " tone-warn" : ""));
      const fill = el("div");
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
      const bar = el("div", i === a.per_day.length - 1 ? "today" : "");
      bar.style.height = (top > 0 ? (d.cost / top) * 100 : 0) + "%";
      bar.title = d.date + " : " + money(d.cost, 3) + " (" + d.calls + " appels)";
      bars.appendChild(bar);
      labels.appendChild(el("span", null, String(parseInt(d.date.slice(8), 10))));
    });

    let note = "";
    if (s.status.state === "dead") note = "Plus aucun appel : le bot est mort.";
    else if (s.status.state === "halted") note = "Bot arrêté : plus aucun appel tant que tu ne le reprends pas.";
    else if (a.spent_total >= a.total_budget) note = "Budget total épuisé : l'agent ne décide plus. Seul le kill switch veille encore.";
    else if (a.spent_today >= a.daily_budget) note = "Budget du jour épuisé : l'agent ne décide plus avant demain (UTC).";
    else if (a.calls_life === 0 && s.agent !== "llm") note = "Aucun appel LLM : cet agent n'utilise pas l'API.";
    else if (a.next_call) {
      const wait = a.next_call - s.generated_at;
      note = wait > 60 ? "Prochain appel dans " + dur(wait) + "." : "Prochain appel imminent.";
    }
    $("apiNote").textContent = note;
  }

  // Les « attentes » consécutives (le cas le plus fréquent avec un appel LLM par heure) sont regroupées
  // en une seule ligne : sinon elles noient les ordres, qui sont ce qu'on cherche dans ce journal.
  function groupJournal(rows) {
    const groups = [];
    for (const j of rows) {
      const last = groups[groups.length - 1];
      if (j.action !== "hold") {
        groups.push({ hold: false, j: j });
      } else if (last && last.hold) {
        last.count += 1;
        last.oldest = j.ts;
      } else {
        groups.push({ hold: true, count: 1, newest: j.ts, oldest: j.ts, reasoning: j.reasoning });
      }
    }
    return groups;
  }

  function renderJournal(s) {
    const rows = $("journalRows");
    clear(rows);
    const verdicts = {
      "1": ["Exécuté", "ok"],
      "0": ["Refusé", "orange"],
      "null": ["Aucun ordre", "muted"],
    };
    const actions = { buy: "Achat", sell: "Vente", hold: "Attente" };
    const groups = groupJournal(s.journal).slice(0, 15);
    for (const g of groups) {
      const row = el("div", "trow");
      const what = el("span", "stack");
      const verdict = el("span", "stack");

      if (g.hold) {
        row.appendChild(el("span", "mono muted small", when(g.newest, s.generated_at)));
        what.appendChild(el("b", null, "Attente"));
        what.appendChild(el("span", "mono muted small",
          g.count > 1 ? g.count + " fois, depuis " + when(g.oldest, s.generated_at) : "—"));
        verdict.appendChild(el("span", "tag tone-muted", "Aucun ordre"));
        row.appendChild(what);
        row.appendChild(verdict);
        row.appendChild(el("span", "muted wrap", g.reasoning || "—"));
      } else {
        const j = g.j;
        row.appendChild(el("span", "mono muted small", when(j.ts, s.generated_at)));
        what.appendChild(el("b", null, actions[j.action] || j.action));
        what.appendChild(el("span", "mono muted small", j.symbol ? j.symbol + " · " + money(j.amount_quote) : "—"));
        const [label, tone] = verdicts[String(j.approved)] || verdicts["null"];
        verdict.appendChild(el("span", "tag tone-" + tone, label));
        if (j.verdict) verdict.appendChild(el("span", "muted small wrap", j.verdict));
        row.appendChild(what);
        row.appendChild(verdict);
        row.appendChild(el("span", "muted wrap", j.reasoning || "—"));
      }
      rows.appendChild(row);
    }
    $("journalMeta").textContent = "attentes regroupées · " + s.counts.decisions + " décisions · " + s.counts.fills + " exécutions";
  }

  function renderEvents(s) {
    const box = $("eventRows");
    clear(box);
    const levels = { info: ["info", "ok"], warning: ["alerte", "warn"], error: ["erreur", "dead"], critical: ["critique", "dead"] };
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
    for (const g of groups) {
      const [label, tone] = levels[g.level] || [g.level, "muted"];
      const item = el("div", "stack");
      const head = el("div", "event-head");
      head.appendChild(el("span", "tag tone-" + tone, label));
      head.appendChild(el("span", "mono", when(g.newest, s.generated_at) + (g.count > 1 ? " · ×" + g.count + " depuis " + when(g.oldest, s.generated_at) : "")));
      item.appendChild(head);
      item.appendChild(el("div", "wrap", g.message));
      box.appendChild(item);
    }
  }

  function render(s) {
    ccy = s.quote_currency;
    $("empty").hidden = s.has_data;
    $("dash").hidden = !s.has_data;
    if (!s.has_data) {
      $("banner").hidden = true;
      $("botnote").hidden = true;
      $("chipAgent").hidden = true;
      $("chipModel").hidden = true;
      return;
    }
    renderHeader(s);
    const v = renderStatus(s);
    renderKpis(s);
    renderChart(s, v.tone);
    renderGauge(s, v.tone);
    renderDay(s);
    renderPositions(s);
    renderApi(s);
    renderJournal(s);
    renderEvents(s);
    $("pill").setAttribute("aria-label", "État du bot : " + v.label);
  }

  // ---------------------------------------------------------------- boucle
  function updateAge() {
    const node = $("updated");
    if (!state.lastOk) {
      node.textContent = state.failed ? "Serveur injoignable…" : "Chargement…";
      return;
    }
    const age = dur((Date.now() - state.lastOk) / 1000);
    node.textContent = state.failed ? "Connexion perdue · dernières données il y a " + age : "Actualisé il y a " + age;
  }

  async function refresh() {
    try {
      const response = await fetch("/api/snapshot", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "HTTP " + response.status);
      state.snap = body;
      state.lastOk = Date.now();
      state.failed = false;
      render(body);
    } catch (err) {
      state.failed = true;
    }
    updateAge();
  }

  refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(updateAge, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
