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

const IPC_MAIN = /ipcMain\.(?:on|once|handle|handleOnce)\(\s*["'`]([^"'`]+)/g;
const IPC_SEND = /ipcRenderer\.(?:send|invoke|sendSync|postMessage)\(\s*["'`]([^"'`]+)/g;
const IPC_LISTEN = /ipcRenderer\.(?:on|once|addListener)\(\s*["'`]([^"'`]+)/g;
const between = (text, start, end) => {
  const a = text.indexOf(start), b = text.indexOf(end, a + 1);
  assert.ok(a >= 0 && b > a, `repères introuvables : ${start} … ${end}`);
  return text.slice(a, b);
};

test("la barre d'onglets ne dispose que de deux messages, la fenêtre de choix de trois, et rien d'autre n'existe", () => {
  const main = read("main.js");
  assert.deepEqual(channels(main, IPC_MAIN), ["connect-local", "connect-ready", "connect-remote", "select-tab", "shell-ready"]);
  const preload = read("preload.js");
  assert.deepEqual(channels(preload, IPC_SEND), ["select-tab", "shell-ready"]);
  assert.deepEqual(channels(preload, IPC_LISTEN), ["tabs"]);
});

test("la fenêtre de choix ne peut que proposer une adresse ou cet ordinateur, et le processus principal vérifie", () => {
  const preload = read("connect-preload.js");
  assert.deepEqual(channels(preload, IPC_SEND), ["connect-local", "connect-ready", "connect-remote"]);
  assert.deepEqual(channels(preload, IPC_LISTEN), ["connect-current"]);
  const page = code(read("connect.js"));
  for (const forbidden of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(", "XMLHttpRequest"]) {
    assert.ok(!page.includes(forbidden), forbidden);
  }
  const html = read("connect.html");
  assert.ok(html.includes("default-src 'none'; script-src 'self'; style-src 'self'"));
  assert.ok(!/\sstyle=/.test(html) && !/\son[a-z]+=/.test(html) && !/(?:src|href|action)="https?:/.test(html));

  const main = code(read("main.js"));
  // Chaque message n'est écouté que s'il vient de la fenêtre de choix.
  assert.ok(main.includes("const fromConnect = (event) => connectWin && !connectWin.isDestroyed() && event.sender === connectWin.webContents;"));
  assert.ok(main.includes('ipcMain.handle("connect-remote", (event, address) => (fromConnect(event) ? chooseRemote('));
  assert.ok(main.includes('ipcMain.on("connect-local", (event) => { if (fromConnect(event)) chooseLocal(); });'));
  assert.ok(main.includes('ipcMain.on("connect-ready", (event) => { if (fromConnect(event)) event.sender.send("connect-current"'));
  // L'adresse est analysée, puis sondée sans cookie, et seulement alors retenue.
  const choose = between(main, "async function chooseRemote(", "async function chooseLocal(");
  const parsed = choose.indexOf("origin = remote.parseAddress(text);");
  const probed = choose.indexOf("if (!(await remote.probe(origin, fetchText))) {");
  const written = choose.indexOf("remote.writeConfig(CONFIG_FILE, { remote: origin });");
  assert.ok(parsed >= 0 && probed > parsed && written > probed);
  assert.ok(choose.includes("session.fromPartition(PROBE_PARTITION)") && choose.includes('credentials: "omit"') && choose.includes('redirect: "manual"'));
  assert.ok(choose.indexOf("if (busy) return busy;") >= 0 && choose.indexOf("if (busy) return busy;") < parsed);     // jamais par-dessus des bots locaux
  const recheck = choose.indexOf("if (busyNow) return busyNow;");
  assert.ok(recheck > probed && recheck < written);     // ni par-dessus un bot démarré pendant la vérification de l'adresse
  assert.ok(between(main, "async function chooseLocal(", "async function startWeb(").includes("if (busyWithBots()) return;"));
});

test("en mode distant la fenêtre ne sort pas du serveur et rien ne démarre ni n'arrête un bot", () => {
  const main = code(read("main.js"));
  const harden = between(main, "function hardenRemoteSession(", "async function remoteText(");
  assert.ok(harden.includes("ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));"));
  assert.ok(harden.includes("ses.setPermissionCheckHandler(() => false);"));
  assert.ok(harden.includes("callback({ cancel: !remote.allowsRequest(details.url, state.remote) });"));
  assert.ok(harden.includes('ses.on("will-download", (event) => event.preventDefault());'));

  const text = between(main, "async function remoteText(", "function remoteTab(");
  assert.ok(text.includes('if (!remote.isRemoteUrl(url, state.remote)) throw new Error('));
  assert.ok(text.includes('redirect: "manual"') && text.includes("remote.boundedText(response,"));
  assert.ok(!text.includes("method") && !text.includes("body:") && !text.includes("response.text()"));      // GET, sans corps, lecture bornée

  const view = between(main, "function createView(", "function layout(");
  assert.ok(view.includes("const inside = state.remote ? remote.isRemoteUrl(url, state.remote) : backend.isProfileUrl(url, profile.port);"));
  assert.ok(view.includes("if (!inside) event.preventDefault();"));
  assert.ok(view.includes('wc.setWindowOpenHandler(() => ({ action: "deny" }));'));
  assert.ok(view.includes("wc.loadURL(profile.url);") && !/loadURL\((?!profile\.url|backend\.profileUrl)/.test(view));

  // Tout le mode distant, d'un bloc : aucun moyen de lancer quoi que ce soit.
  const whole = between(main, "function hardenRemoteSession(", "function openConnect(");
  for (const forbidden of ["Supervisor", "startWeb", "spawn", "startBot", "stopBot", "loadProfiles", "venvPython", "supervisor.", "POST"]) {
    assert.ok(!whole.includes(forbidden), forbidden);
  }
  assert.ok(/function botItems\(\) \{\s*if \(state\.remote\) \{\s*return \[\{ label: "[^"]+", enabled: false \}\];\s*\}/.test(main));
  assert.ok(main.includes('if (state.remote) return "remote";'));

  // La vérification automatique ne réutilise jamais une session ouverte, et force le mode local sauf demande explicite.
  const raw = read("main.js");
  assert.ok(raw.includes('const REMOTE_PARTITION = SMOKE_DIR ? "tradeagent-remote-smoke" : "persist:tradeagent-remote";'));
  assert.ok(main.includes("choice = SMOKE_DIR && SMOKE_REMOTE ? { remote: remote.parseAddress(SMOKE_REMOTE) }"));
  assert.ok(main.includes(': SMOKE_DIR || process.argv.includes("--local") ? { mode: "local" } : remote.readConfig(CONFIG_FILE);'));
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
