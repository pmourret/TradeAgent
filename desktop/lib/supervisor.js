"use strict";
/*
 * Supervision des bots : un `tradeagent run --profile X --stop-on-stdin` par profil démarré.
 * Rien ici ne dépend d'Electron : ce fichier est testé avec `node --test`.
 *
 * Règles :
 *  - on lance exactement la commande qu'on taperait à la main, plus --stop-on-stdin ; jamais --agent, --feed,
 *    reset ni resume : le superviseur démarre et arrête, rien d'autre ;
 *  - l'arrêt est poli : on écrit `stop` sur l'entrée standard, le bot finit son cycle et affiche son état ;
 *    l'arrêt sec n'arrive qu'après un délai (l'état est en base : un arrêt sec ne corrompt rien) ;
 *  - si ce processus meurt, l'entrée standard des bots se ferme et ils s'arrêtent d'eux-mêmes.
 */
const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");

const TAIL_LINES = 12;

function botArgs(profile, config = "config.yaml") {
  return ["-u", "-m", "tradeagent", "run", "--profile", profile, "--config", config, "--stop-on-stdin"];
}

// Journal d'un bot dans un fichier, avec rotation par taille : <nom>.log, puis .1 (le plus récent) à .<keep>.
class RotatingLog {
  constructor(file, { maxBytes = 1024 * 1024, keep = 3 } = {}) {
    this.file = file;
    this.maxBytes = maxBytes;
    this.keep = keep;
    fs.mkdirSync(path.dirname(file), { recursive: true });
  }

  write(line) {
    try {
      const size = fs.existsSync(this.file) ? fs.statSync(this.file).size : 0;
      if (size + Buffer.byteLength(line) + 1 > this.maxBytes && size > 0) this.rotate();
      fs.appendFileSync(this.file, line + "\n", "utf8");
    } catch (exc) {
      // Un disque plein ou un fichier verrouillé ne doit jamais arrêter la supervision.
    }
  }

  rotate() {
    for (let i = this.keep; i >= 1; i--) {
      const from = i === 1 ? this.file : `${this.file}.${i - 1}`;
      const to = `${this.file}.${i}`;
      if (!fs.existsSync(from)) continue;
      if (fs.existsSync(to)) fs.rmSync(to);
      fs.renameSync(from, to);
    }
  }
}

/*
 * États d'un bot : "stopped" → "running" → "stopping" → "stopped".
 * Évènement "change" (profil, état) à chaque transition ; l'état porte `code`, `expected` et `tail` à l'arrêt.
 */
class Supervisor extends EventEmitter {
  constructor({ python, root, env = process.env, logDir = null, graceMs = 30000, spawnFn = spawn, now = () => new Date() }) {
    super();
    this.python = python;
    this.root = root;
    this.env = { ...env, PYTHONUNBUFFERED: "1", PYTHONUTF8: "1" };
    this.logDir = logDir;
    this.graceMs = graceMs;
    this.spawnFn = spawnFn;
    this.now = now;
    this.bots = new Map();
  }

  state(profile) {
    const bot = this.bots.get(profile);
    if (!bot) return { status: "stopped", code: null, expected: true, tail: [] };
    return { status: bot.status, code: bot.code, expected: bot.expected, tail: [...bot.tail] };
  }

  running() {
    return [...this.bots.entries()].filter(([, b]) => b.status !== "stopped").map(([name]) => name);
  }

  start(profile) {
    const current = this.bots.get(profile);
    if (current && current.status !== "stopped") return false;   // jamais deux bots sur un profil

    const bot = { status: "running", code: null, expected: false, tail: [], child: null, timer: null, exited: null };
    const log = this.logDir ? new RotatingLog(path.join(this.logDir, `${profile}.log`)) : null;
    const note = (line) => {
      bot.tail.push(line);
      if (bot.tail.length > TAIL_LINES) bot.tail.shift();
      if (log) log.write(line);
      this.emit("line", profile, line);
    };
    if (log) log.write(`--- démarrage du bot ${profile} par l'application, ${this.now().toISOString()} ---`);

    const child = this.spawnFn(this.python, botArgs(profile), {
      cwd: this.root, env: this.env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    });
    bot.child = child;
    this.bots.set(profile, bot);

    let pending = "";
    const feed = (chunk) => {
      pending += String(chunk);
      const lines = pending.split(/\r?\n/);
      pending = lines.pop();
      for (const line of lines) if (line.trim()) note(line);
    };
    child.stdout.on("data", feed);
    child.stderr.on("data", feed);
    child.stdin.on("error", () => {});   // le bot a pu sortir avant qu'on lui écrive

    bot.exited = new Promise((resolve) => {
      let done = false;
      const finish = (code) => {
        if (done) return;
        done = true;
        if (pending.trim()) note(pending);
        clearTimeout(bot.timer);
        bot.code = code;
        bot.status = "stopped";
        bot.child = null;
        if (log) log.write(`--- bot ${profile} arrêté (code ${code}), ${this.now().toISOString()} ---`);
        this.emit("change", profile, this.state(profile));
        resolve(code);
      };
      child.on("error", (err) => {       // ex. python introuvable
        note(`lancement impossible : ${err.message}`);
        finish(null);
      });
      child.on("close", (code) => finish(code));
    });

    this.emit("change", profile, this.state(profile));
    return true;
  }

  // Demande l'arrêt et renvoie une promesse résolue quand le bot est sorti.
  stop(profile) {
    const bot = this.bots.get(profile);
    if (!bot || bot.status === "stopped") return Promise.resolve(null);
    if (bot.status === "running") {
      bot.status = "stopping";
      bot.expected = true;
      try {
        bot.child.stdin.write("stop\n");
        bot.child.stdin.end();
      } catch (exc) {
        // entrée déjà fermée : le bot est en train de sortir
      }
      bot.timer = setTimeout(() => {
        if (bot.child) bot.child.kill();
      }, this.graceMs);
      this.emit("change", profile, this.state(profile));
    }
    return bot.exited;
  }

  stopAll() {
    return Promise.all(this.running().map((name) => this.stop(name)));
  }

  // Dernier recours, synchrone (sortie brutale de l'application).
  killAll() {
    for (const bot of this.bots.values()) if (bot.child) bot.child.kill();
  }
}

module.exports = { Supervisor, RotatingLog, botArgs };
