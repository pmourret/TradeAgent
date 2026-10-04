"use strict";
/*
 * Application de bureau tradeagent — phase F1 : visionneuse.
 *
 * Une coquille mince : pour chaque profil, on lance `python -m tradeagent web --profile X` (l'interface web
 * locale en lecture seule, inchangée) et on l'affiche dans un onglet. Cette phase ne démarre ni n'arrête
 * aucun bot.
 *
 * Sécurité (le bot manipule de l'argent, même fictif) :
 *  - la page d'un profil tourne en bac à sable, sans Node, sans preload : elle n'a aucun moyen de parler
 *    au processus principal ;
 *  - elle ne peut ni naviguer ni charger quoi que ce soit hors de son serveur local, ni ouvrir de fenêtre,
 *    ni obtenir de permission (caméra, notifications...) ;
 *  - on n'affiche un port que si c'est bien notre serveur qui y répond.
 */
const { app, BrowserWindow, WebContentsView, Menu, dialog, ipcMain, session } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const backend = require("./lib/backend");

const TAB_BAR_HEIGHT = 40;          // même valeur que .tabs dans shell.css
const PARTITION = "tradeagent";     // session à part, en mémoire : rien n'est écrit sur disque par les pages
const ROOT = path.resolve(process.env.TRADEAGENT_ROOT || path.join(__dirname, ".."));
const SMOKE_DIR = process.env.TRADEAGENT_DESKTOP_SMOKE || "";   // vérification automatique : capture puis quitte

let win = null;
let shellReady = false;
const state = { profiles: [], active: null, error_title: "", error: "" };

// ---------------------------------------------------------------- état → barre d'onglets
function publish() {
  if (!win || win.isDestroyed() || !shellReady) return;
  win.webContents.send("tabs", {
    active: state.active,
    error_title: state.error_title,
    error: state.error,
    profiles: state.profiles.map((p) => ({ name: p.name, description: p.description, status: p.status, error: p.error })),
  });
}

function fatal(title, text) {
  state.error_title = title;
  state.error = text;
  publish();
}

// ---------------------------------------------------------------- pages des profils
function hardenSession() {
  const ses = session.fromPartition(PARTITION);
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  const ports = new Set(state.profiles.map((p) => p.port));
  ses.webRequest.onBeforeRequest((details, callback) => {
    let ok = false;
    try {
      const url = new URL(details.url);
      ok = url.protocol === "data:" || (url.protocol === "http:" && url.hostname === "127.0.0.1" && ports.has(Number(url.port)));
    } catch (exc) {
      ok = false;
    }
    callback({ cancel: !ok });
  });
}

function createView(profile) {
  const view = new WebContentsView({
    webPreferences: { partition: PARTITION, sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true },
  });
  const wc = view.webContents;
  wc.setWindowOpenHandler(() => ({ action: "deny" }));
  wc.on("will-navigate", (event, url) => {
    if (!backend.isProfileUrl(url, profile.port)) event.preventDefault();
  });
  wc.on("will-attach-webview", (event) => event.preventDefault());
  wc.loadURL(backend.profileUrl(profile.port));
  return view;
}

function layout() {
  if (!win || win.isDestroyed()) return;
  const { width, height } = win.getContentBounds();
  for (const p of state.profiles) {
    if (p.view) p.view.setBounds({ x: 0, y: TAB_BAR_HEIGHT, width, height: Math.max(0, height - TAB_BAR_HEIGHT) });
  }
}

function showActive() {
  if (!win || win.isDestroyed()) return;
  for (const p of state.profiles) {
    if (p.view) win.contentView.removeChildView(p.view);
  }
  const active = state.profiles.find((p) => p.name === state.active);
  if (active && active.status === "ready") {
    if (!active.view) active.view = createView(active);
    win.contentView.addChildView(active.view);
    layout();
  }
  win.setTitle(active ? `tradeagent — ${active.name}` : "tradeagent");
  publish();
}

function select(name) {
  if (!state.profiles.some((p) => p.name === name)) return;
  state.active = name;
  showActive();
}

// ---------------------------------------------------------------- serveurs web (lecture seule)
async function startWeb(profile, python) {
  const already = await backend.probe(profile.port);
  if (already === "ours") {          // une interface tourne déjà (ex. `tradeagent up`) : on l'affiche telle quelle
    profile.status = "ready";
    return;
  }
  if (already === "foreign") {
    profile.status = "error";
    profile.error = `Le port ${profile.port} est utilisé par un autre programme que tradeagent. Ferme-le, puis relance l'application.`;
    return;
  }
  const tail = [];
  const child = spawn(python, backend.webArgs(profile.name), {
    cwd: ROOT, env: backend.childEnv(), windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
  });
  profile.child = child;
  const keep = (chunk) => {
    for (const line of String(chunk).split(/\r?\n/)) {
      if (!line.trim()) continue;
      console.log(`[${profile.name} ui] ${line}`);
      tail.push(line);
      if (tail.length > 8) tail.shift();
    }
  };
  child.stdout.on("data", keep);
  child.stderr.on("data", keep);
  child.on("error", (err) => keep(err.message));
  child.on("exit", (code) => {
    profile.child = null;
    if (app.isQuitting) return;
    profile.status = "error";
    profile.error = `L'interface du profil s'est arrêtée (code ${code}).\n${tail.join("\n")}`;
    if (state.active === profile.name) showActive();
    else publish();
  });

  const answer = await backend.waitForServer(profile.port, { stillAlive: () => profile.child !== null });
  if (answer === "ours") {
    profile.status = "ready";
  } else if (profile.status !== "error") {
    profile.status = "error";
    profile.error = `L'interface du profil ne répond pas sur le port ${profile.port}.\n${tail.join("\n")}`;
  }
}

function stopChildren() {
  for (const p of state.profiles) {
    if (p.child) p.child.kill();   // serveurs en lecture seule : rien à sauvegarder
  }
}

async function boot() {
  const python = backend.venvPython(ROOT);
  if (!fs.existsSync(python)) {
    fatal("Environnement Python absent",
      `Aucun venv dans ${ROOT}.\nLance d'abord scripts\\setup.bat (ou scripts/setup.sh), puis relance l'application.`);
    return;
  }
  let profiles;
  try {
    profiles = await backend.loadProfiles(python, ROOT);
  } catch (exc) {
    fatal("Impossible de lire les profils de tradeagent", String(exc.message || exc));
    return;
  }
  state.profiles = profiles.map((p) => ({ ...p, status: "starting", error: "", child: null, view: null }));
  const wanted = (process.argv.find((a) => a.startsWith("--profile=")) || "").slice("--profile=".length);
  state.active = state.profiles.some((p) => p.name === wanted) ? wanted : state.profiles[0].name;
  hardenSession();
  buildMenu();
  publish();
  await Promise.all(state.profiles.map((p) => startWeb(p, python).then(() => {
    if (state.active === p.name) showActive();
    else publish();
  })));
  if (SMOKE_DIR) smoke();
}

// ---------------------------------------------------------------- menu
function buildMenu() {
  const activeView = () => (state.profiles.find((p) => p.name === state.active) || {}).view;
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: "Fichier", submenu: [{ label: "Quitter", role: "quit" }] },
    {
      label: "Profils",
      submenu: state.profiles.map((p, i) => ({
        label: p.name, accelerator: `CmdOrCtrl+${i + 1}`, click: () => select(p.name),
      })),
    },
    {
      label: "Affichage",
      submenu: [
        { label: "Actualiser", accelerator: "F5", click: () => { const v = activeView(); if (v) v.webContents.reload(); } },
        { type: "separator" },
        { label: "Zoom avant", accelerator: "CmdOrCtrl+Plus", click: () => zoom(activeView(), 0.5) },
        { label: "Zoom arrière", accelerator: "CmdOrCtrl+-", click: () => zoom(activeView(), -0.5) },
        { label: "Taille réelle", accelerator: "CmdOrCtrl+0", click: () => zoom(activeView(), null) },
        { type: "separator" },
        { label: "Plein écran", role: "togglefullscreen" },
      ],
    },
    {
      label: "Aide",
      submenu: [{
        label: "À propos",
        click: () => dialog.showMessageBox(win, {
          type: "info", title: "tradeagent",
          message: "tradeagent — paper trading, argent fictif",
          detail: "Cette fenêtre affiche l'interface locale en lecture seule de chaque profil.\n"
            + "Reprendre un bot arrêté ou repartir d'une nouvelle vie reste en ligne de commande :\n"
            + "tradeagent resume · tradeagent reset",
        }),
      }],
    },
  ]));
}

function zoom(view, delta) {
  if (!view) return;
  view.webContents.setZoomLevel(delta === null ? 0 : view.webContents.getZoomLevel() + delta);
}

// ---------------------------------------------------------------- vérification automatique
async function smoke() {
  await new Promise((r) => setTimeout(r, 2500));
  fs.mkdirSync(SMOKE_DIR, { recursive: true });
  fs.writeFileSync(path.join(SMOKE_DIR, "shell.png"), (await win.webContents.capturePage()).toPNG());
  const active = state.profiles.find((p) => p.name === state.active);
  if (active && active.view) {
    fs.writeFileSync(path.join(SMOKE_DIR, "view.png"), (await active.view.webContents.capturePage()).toPNG());
  }
  fs.writeFileSync(path.join(SMOKE_DIR, "state.json"), JSON.stringify(
    state.profiles.map((p) => ({ name: p.name, port: p.port, status: p.status, error: p.error })), null, 2));
  app.quit();
}

// ---------------------------------------------------------------- cycle de vie
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });

  ipcMain.on("select-tab", (event, name) => {
    if (win && event.sender === win.webContents) select(String(name));
  });
  ipcMain.on("shell-ready", (event) => {
    if (!win || event.sender !== win.webContents) return;
    shellReady = true;
    publish();
  });

  app.whenReady().then(() => {
    win = new BrowserWindow({
      width: 1280, height: 860, minWidth: 720, minHeight: 480, backgroundColor: "#0d1117", title: "tradeagent",
      webPreferences: {
        preload: path.join(__dirname, "preload.js"), sandbox: true, contextIsolation: true, nodeIntegration: false,
      },
    });
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    win.webContents.on("will-navigate", (event) => event.preventDefault());
    win.on("page-title-updated", (event) => event.preventDefault());
    win.on("resize", layout);
    win.on("closed", () => { win = null; });
    win.loadFile(path.join(__dirname, "shell.html"));
    boot();
  });

  app.on("before-quit", () => {
    app.isQuitting = true;
    stopChildren();
  });
  app.on("window-all-closed", () => app.quit());
}
