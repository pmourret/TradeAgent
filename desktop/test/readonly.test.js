"use strict";
// Garde-fou de structure : aucune page ne doit pouvoir agir sur un bot. Démarrer et arrêter passent par le menu
// natif et la zone de notification, donc il ne doit exister aucun message IPC pour ça.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const read = (name) => fs.readFileSync(path.join(__dirname, "..", name), "utf8");
const code = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");   // sans les commentaires
const channels =(text, pattern) => [...text.matchAll(pattern)].map((m) => m[1]).sort();

test("les pages ne disposent que de deux messages : choisir un onglet et signaler qu'elles sont prêtes", () => {
  const main = read("main.js");
  assert.deepEqual(channels(main, /ipcMain\.(?:on|handle|once)\(\s*"([^"]+)"/g), ["select-tab", "shell-ready"]);
  const preload = read("preload.js");
  assert.deepEqual(channels(preload, /ipcRenderer\.(?:send|invoke|sendSync)\(\s*"([^"]+)"/g), ["select-tab", "shell-ready"]);
  assert.deepEqual(channels(preload, /ipcRenderer\.on\(\s*"([^"]+)"/g), ["tabs"]);
});

test("la page d'un profil n'a ni preload ni accès à Node", () => {
  const main = read("main.js");
  const view = main.slice(main.indexOf("new WebContentsView("), main.indexOf("const wc = view.webContents"));
  assert.ok(view.includes("sandbox: true") && view.includes("contextIsolation: true") && view.includes("nodeIntegration: false"));
  assert.ok(!view.includes("preload"));
});

test("la barre d'onglets n'écrit jamais de HTML et ne connaît aucune action sur les bots", () => {
  const shell = code(read("shell.js"));
  for (const forbidden of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("]) {
    assert.ok(!shell.includes(forbidden), forbidden);
  }
  assert.deepEqual(channels(shell, /window\.desktop\.(\w+)\(/g).filter((v, i, a) => a.indexOf(v) === i), ["onTabs", "ready", "select"]);
});

test("les notifications sont sortantes : lecture seule du serveur local, aucun moyen d'agir sur un bot", () => {
  const notifier = code(read(path.join("lib", "notifier.js")));
  for (const forbidden of ["require(", "spawn", "stdin", "fetch(", "process."]) {
    assert.ok(!notifier.includes(forbidden), forbidden);
  }
  const backend = code(read(path.join("lib", "backend.js")));
  assert.deepEqual(channels(backend, /method:\s*"([^"]+)"/g), ["GET", "HEAD"]);
  assert.ok(!backend.includes("req.write("));
});

test("le superviseur ne sait ni réinitialiser ni reprendre un bot", () => {
  const sup = code(read(path.join("lib", "supervisor.js")));
  for (const forbidden of ['"reset"', '"resume"', '"--yes"', '"live"', '"--agent"', '"--feed"']) {
    assert.ok(!sup.includes(forbidden), forbidden);
  }
});
