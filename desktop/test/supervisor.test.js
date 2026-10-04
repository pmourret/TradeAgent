"use strict";
// Tests hors réseau, sans Electron ni Python : les « bots » sont de petits scripts Node.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { spawn } = require("node:child_process");
const { Supervisor, RotatingLog, botArgs } = require("../lib/supervisor");

// Un faux bot : se comporte comme `tradeagent run --stop-on-stdin` (s'arrête sur `stop` ou à la fermeture).
const POLITE = `
  console.log("cycle 1 | hold");
  let buf = "";
  const bye = (why) => { console.log(why + " : le bot s'arrête proprement"); console.log("état : ALIVE"); process.exit(0); };
  process.stdin.on("data", (d) => { buf += d; if (buf.split(/\\r?\\n/).some((l) => l.trim() === "stop")) bye("arrêt demandé"); });
  process.stdin.on("end", () => bye("entrée standard fermée"));
`;
const STUBBORN = `console.log("cycle 1"); process.stdin.resume(); process.stdin.on("end", () => {}); setInterval(() => {}, 1000);`;
const CRASH = `console.error("erreur de configuration : ANTHROPIC_API_KEY absente"); process.exit(2);`;

function make(script, options = {}) {
  const calls = [];
  const spawnFn = (python, args, opts) => {
    calls.push({ python, args, opts });
    return spawn(process.execPath, ["-e", script], { stdio: opts.stdio });
  };
  const sup = new Supervisor({ python: "PY", root: "/projet", env: { PATH: process.env.PATH }, spawnFn, ...options });
  return { sup, calls };
}

function nextChange(sup, wanted) {
  return new Promise((resolve) => {
    const on = (profile, state) => {
      if (state.status !== wanted) return;
      sup.off("change", on);
      resolve({ profile, state });
    };
    sup.on("change", on);
  });
}

function sawLine(sup, needle) {
  return new Promise((resolve) => {
    const on = (_profile, line) => {
      if (!line.includes(needle)) return;
      sup.off("line", on);
      resolve(line);
    };
    sup.on("line", on);
  });
}

test("un bot est lancé comme à la main, avec l'arrêt par l'entrée standard et rien d'autre", () => {
  const args = botArgs("llm");
  assert.deepEqual(args, ["-u", "-m", "tradeagent", "run", "--profile", "llm", "--config", "config.yaml", "--stop-on-stdin"]);
  for (const forbidden of ["--agent", "--feed", "--max-cycles", "--cycle-seconds", "reset", "resume", "--yes", "live"]) {
    assert.ok(!args.includes(forbidden), forbidden);
  }
});

test("démarrer lance le bon processus, dans le dossier du projet, en UTF-8", async () => {
  const { sup, calls } = make(POLITE);
  const line = sawLine(sup, "cycle 1");
  assert.equal(sup.start("demo"), true);
  await line;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].python, "PY");
  assert.deepEqual(calls[0].args, botArgs("demo"));
  assert.equal(calls[0].opts.cwd, "/projet");
  assert.equal(calls[0].opts.env.PYTHONUTF8, "1");
  assert.equal(sup.state("demo").status, "running");
  assert.deepEqual(sup.running(), ["demo"]);
  await sup.stop("demo");
});

test("jamais deux bots sur le même profil", async () => {
  const { sup, calls } = make(POLITE);
  const line = sawLine(sup, "cycle 1");
  assert.equal(sup.start("demo"), true);
  assert.equal(sup.start("demo"), false);
  await line;
  assert.equal(calls.length, 1);
  await sup.stop("demo");
  assert.equal(sup.start("demo"), true);          // une fois arrêté, on peut relancer
  await sup.stop("demo");
});

test("arrêter est poli : le bot finit et affiche son état, la sortie est attendue", async () => {
  const { sup } = make(POLITE);
  const line = sawLine(sup, "cycle 1");
  sup.start("demo");
  await line;
  const stopping = nextChange(sup, "stopping");
  const code = sup.stop("demo");
  await stopping;
  assert.equal(await code, 0);
  const state = sup.state("demo");
  assert.equal(state.status, "stopped");
  assert.equal(state.expected, true);
  assert.ok(state.tail.some((l) => l.includes("arrêt demandé")));
  assert.ok(state.tail.some((l) => l.includes("état : ALIVE")));
  assert.deepEqual(sup.running(), []);
});

test("un bot qui n'obéit pas est arrêté de force après le délai", async () => {
  const { sup } = make(STUBBORN, { graceMs: 200 });
  const line = sawLine(sup, "cycle 1");
  sup.start("demo");
  await line;
  const started = Date.now();
  await sup.stop("demo");
  assert.equal(sup.state("demo").status, "stopped");
  assert.ok(Date.now() - started >= 150 && Date.now() - started < 10000);
});

test("un bot qui s'arrête tout seul est signalé comme inattendu, avec ses dernières lignes", async () => {
  const { sup } = make(CRASH);
  const stopped = nextChange(sup, "stopped");
  sup.start("llm");
  const { profile, state } = await stopped;
  assert.equal(profile, "llm");
  assert.equal(state.code, 2);
  assert.equal(state.expected, false);
  assert.ok(state.tail.join("\n").includes("ANTHROPIC_API_KEY absente"));
});

test("un Python introuvable donne un arrêt propre avec un message, pas une exception", async () => {
  const sup = new Supervisor({ python: path.join(os.tmpdir(), "python-qui-n-existe-pas"), root: os.tmpdir() });
  const stopped = nextChange(sup, "stopped");
  sup.start("demo");
  const { state } = await stopped;
  assert.equal(state.expected, false);
  assert.ok(state.tail.join("\n").includes("lancement impossible"));
});

test("tout arrêter attend la sortie de chaque bot", async () => {
  const { sup } = make(POLITE);
  const lines = Promise.all([sawLine(sup, "cycle 1"), sawLine(sup, "cycle 1")]);
  sup.start("hold");
  sup.start("demo");
  await lines;
  await sup.stopAll();
  assert.deepEqual(sup.running(), []);
  assert.deepEqual(await sup.stopAll(), []);       // rien à arrêter : pas d'erreur
});

test("la sortie du bot est écrite dans un journal par profil", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ta-logs-"));
  const { sup } = make(POLITE, { logDir: dir, now: () => new Date("2026-10-04T10:00:00Z") });
  const line = sawLine(sup, "cycle 1");
  sup.start("demo");
  await line;
  await sup.stop("demo");
  const text = fs.readFileSync(path.join(dir, "demo.log"), "utf8");
  assert.ok(text.includes("démarrage du bot demo par l'application, 2026-10-04T10:00:00.000Z"));
  assert.ok(text.includes("cycle 1 | hold") && text.includes("état : ALIVE"));
  assert.ok(text.includes("bot demo arrêté (code 0)"));
  fs.rmSync(dir, { recursive: true, force: true });
});

test("le journal tourne par taille et ne garde qu'un nombre fixe de fichiers", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ta-rot-"));
  const file = path.join(dir, "demo.log");
  const log = new RotatingLog(file, { maxBytes: 100, keep: 2 });
  for (let i = 0; i < 40; i++) log.write(`ligne ${String(i).padStart(2, "0")} ${"x".repeat(20)}`);
  assert.deepEqual(fs.readdirSync(dir).sort(), ["demo.log", "demo.log.1", "demo.log.2"]);
  for (const name of fs.readdirSync(dir)) assert.ok(fs.statSync(path.join(dir, name)).size <= 100);
  assert.ok(fs.readFileSync(file, "utf8").includes("ligne 39"));          // le plus récent est dans le fichier courant
  fs.rmSync(dir, { recursive: true, force: true });
});
