"use strict";
/*
 * Application de bureau tradeagent : visionneuse (F1), superviseur (F2), notifications de bureau (F3) et
 * version portable (F4 : au premier lancement, l'environnement Python est créé dans le dossier de l'application).
 *
 * Visionneuse : pour chaque profil, on lance `python -m tradeagent web --profile X` (l'interface web locale
 * en lecture seule, inchangée) et on l'affiche dans un onglet.
 *
 * Superviseur : démarrer et arrêter les bots, depuis le menu natif et la zone de notification UNIQUEMENT.
 * Aucune page (ni la barre d'onglets, ni la page d'un profil) ne peut agir sur un bot : il n'existe aucun
 * message IPC pour ça. Reprendre (`resume`) et réinitialiser (`reset`) restent en ligne de commande.
 * Fermer la fenêtre la range dans la zone de notification, les bots continuent ; « Quitter » les arrête
 * proprement (voir lib/supervisor.js).
 *
 * Notifications : le processus principal relit `/api/snapshot` de chaque profil (GET, boucle locale) et
 * prévient sur transition (mort, suspension, palier, silence, budget API épuisé ; voir lib/notifier.js).
 * Sortant uniquement : une notification ouvre la fenêtre sur le profil, rien de plus.
 *
 * Sécurité (le bot manipule de l'argent, même fictif) :
 *  - la page d'un profil tourne en bac à sable, sans Node, sans preload : elle n'a aucun moyen de parler
 *    au processus principal ;
 *  - elle ne peut ni naviguer ni charger quoi que ce soit hors de son serveur local, ni ouvrir de fenêtre,
 *    ni obtenir de permission (caméra, notifications...) ;
 *  - on n'affiche un port que si c'est bien notre serveur qui y répond.
 *
 * Mode distant (lib/remote.js) : au lieu de lancer ses propres interfaces, l'application affiche celle d'un
 * serveur (`tradeagent serve`, en HTTPS). Elle ne fait alors que montrer : aucun bot n'est démarré ni arrêté d'ici,
 * la fenêtre ne navigue que vers ce serveur, et la connexion se fait sur les pages du serveur lui-même. Le choix
 * (serveur ou cet ordinateur) est gardé dans data/desktop.json ; en changer redémarre l'application.
 */
const { app, BrowserWindow, WebContentsView, Menu, Notification, Tray, dialog, ipcMain, nativeImage, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const backend = require("./lib/backend");
const { Supervisor } = require("./lib/supervisor");
const { Watcher } = require("./lib/notifier");
const setup = require("./lib/setup");
const remote = require("./lib/remote");

const TAB_BAR_HEIGHT = 40;          // même valeur que .tabs dans shell.css
const PARTITION = "tradeagent";     // session à part, en mémoire : rien n'est écrit sur disque par les pages
const REMOTE_TIMEOUT_MS = 8000;
const PROBE_PARTITION = "tradeagent-probe";     // en mémoire, sans cookie : pour vérifier une adresse avant de l'adopter
// En développement : la racine du dépôt. Version portable : le dossier de l'exécutable, où tout vit (config.yaml,
// .env, data/, .venv), et `payload` = ce qui est livré avec l'application (voir lib/setup.js).
const { root: ROOT, payload: PAYLOAD } = setup.layout({
  packaged: app.isPackaged, execPath: process.execPath, resourcesPath: process.resourcesPath, env: process.env,
  devRoot: path.join(__dirname, ".."),
});
if (app.isPackaged) app.setPath("userData", path.join(ROOT, "data", "electron"));   // rien dans le dossier utilisateur
const SMOKE_DIR = process.env.TRADEAGENT_DESKTOP_SMOKE || "";   // vérification automatique : capture puis quitte
const SMOKE_BOT_ASKED = process.env.TRADEAGENT_DESKTOP_SMOKE_BOT || "";   // ... en démarrant puis arrêtant ce bot
const SMOKE_REMOTE = process.env.TRADEAGENT_DESKTOP_SMOKE_REMOTE || "";   // ... ou en mode distant, sur ce serveur
// Mode distant : session à part elle aussi, mais gardée sur disque (dans le dossier de données de l'application), pour
// que la connexion au serveur survive à un redémarrage. Le cookie de session du serveur expire de lui-même au bout
// de sept jours. En vérification automatique : session en mémoire, vierge, donc jamais connectée.
const REMOTE_PARTITION = SMOKE_DIR ? "tradeagent-remote-smoke" : "persist:tradeagent-remote";
const LOG_DIR = path.join(ROOT, "data", "logs");
const BOT_LABELS = { running: "en marche", stopping: "arrêt en cours…", stopped: "arrêté" };
const WATCH_MS = 15000;             // cadence de relecture des instantanés pour les notifications
const CONFIG_FILE = path.join(ROOT, "data", "desktop.json");     // serveur distant ou cet ordinateur

let win = null;
let tray = null;
let supervisor = null;
let watcher = null;
const liveNotes = new Set();        // une notification libérée par le ramasse-miettes perd son clic
const smokeNotes = [];              // en vérification automatique : notées dans state.json, pas affichées
let quitting = false;        // « Quitter » a été confirmé : la fenêtre peut vraiment se fermer
let trayHintShown = false;
let shellReady = false;
const state = { profiles: [], active: null, error_title: "", error: "", remote: null };
let connectWin = null;

// ---------------------------------------------------------------- état → barre d'onglets
function publish() {
  if (!win || win.isDestroyed() || !shellReady) return;
  win.webContents.send("tabs", {
    active: state.active,
    error_title: state.error_title,
    error: state.error,
    profiles: state.profiles.map((p) => ({
      name: p.name, description: p.description, status: p.status, error: p.error, bot: botStatus(p.name),
    })),
  });
}

function botStatus(name) {
  if (state.remote) return "remote";       // les bots tournent sur le serveur : l'application ne fait que regarder
  return supervisor ? supervisor.state(name).status : "stopped";
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
  ses.on("will-download", (event) => event.preventDefault());
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
    webPreferences: {
      partition: state.remote ? REMOTE_PARTITION : PARTITION, sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true,
    },
  });
  const wc = view.webContents;
  wc.setWindowOpenHandler(() => ({ action: "deny" }));
  wc.on("will-navigate", (event, url) => {
    const inside = state.remote ? remote.isRemoteUrl(url, state.remote) : backend.isProfileUrl(url, profile.port);
    if (!inside) event.preventDefault();
  });
  wc.on("will-attach-webview", (event) => event.preventDefault());
  if (state.remote) {
    // Une connexion, une déconnexion ou une session expirée se voient à une navigation : on relit l'état aussitôt.
    wc.on("did-navigate", () => { refreshRemote(); });
    wc.loadURL(profile.url);
  } else {
    wc.loadURL(backend.profileUrl(profile.port));
  }
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

// ---------------------------------------------------------------- mode distant (lecture seule)
function hardenRemoteSession() {
  const ses = session.fromPartition(REMOTE_PARTITION);
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  ses.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: !remote.allowsRequest(details.url, state.remote) });
  });
  ses.on("will-download", (event) => event.preventDefault());     // une page en lecture seule n'a rien à faire enregistrer
}

// Une lecture du serveur avec la session ouverte dans la fenêtre (son cookie). GET seulement, jamais de redirection suivie.
async function remoteText(url, maxBytes) {
  if (!remote.isRemoteUrl(url, state.remote)) throw new Error("adresse hors du serveur");
  const response = await session.fromPartition(REMOTE_PARTITION).fetch(url, {
    credentials: "include", redirect: "manual", cache: "no-store", signal: AbortSignal.timeout(REMOTE_TIMEOUT_MS),
  });
  return { status: response.status, text: await remote.boundedText(response, maxBytes || remote.MAX_SMALL_BYTES) };
}

function remoteTab(name) {
  const home = name === remote.HOME_TAB;
  return {
    name, status: "ready", error: "", child: null, view: null, port: name,     // `port` : la clé que lit la surveillance
    url: home ? state.remote + "/" : remote.profileUrl(state.remote, name),
    description: home ? "Page d'accueil du serveur : connexion, liste des profils, déconnexion" : `Profil ${name} sur le serveur`,
  };
}

function dropView(profile) {
  if (!profile.view) return;
  if (win && !win.isDestroyed()) win.contentView.removeChildView(profile.view);
  profile.view.webContents.close();
  profile.view = null;
}

let remoteBusy = null;
// Relit où en est la session sur le serveur et ajuste les onglets : l'accueil seul tant qu'on n'est pas connecté,
// un onglet par profil ensuite. Jamais deux relectures en même temps.
function refreshRemote() {
  if (!state.remote || quitting) return Promise.resolve();
  if (!remoteBusy) remoteBusy = applyRemote().catch((exc) => console.error(`[serveur] ${exc.message}`)).finally(() => { remoteBusy = null; });
  return remoteBusy;
}

async function applyRemote() {
  const answer = await remote.sessionState(state.remote, remoteText);
  const home = state.profiles.find((p) => p.name === remote.HOME_TAB);
  home.status = answer.state === "down" ? "error" : "ready";
  home.error = answer.state === "down" ? `${answer.error}\nAdresse : ${state.remote}\nPour la changer : menu Serveur.` : "";
  const before = state.profiles.filter((p) => p.name !== remote.HOME_TAB).map((p) => p.name);
  // Serveur injoignable un instant : on garde les onglets, la page de chacun dit elle-même que la connexion est perdue.
  const wanted = answer.state === "in" ? answer.names : answer.state === "down" ? before : [];
  if (JSON.stringify(wanted) !== JSON.stringify(before)) {
    for (const p of state.profiles) {
      if (p.name !== remote.HOME_TAB && !wanted.includes(p.name)) dropView(p);
    }
    state.profiles = [home, ...wanted.map((name) => state.profiles.find((p) => p.name === name) || remoteTab(name))];
    if (wanted.length && !before.length) {
      state.active = wanted[0];             // on vient de se connecter : droit sur le premier profil
    } else if (!state.profiles.some((p) => p.name === state.active)) {
      state.active = remote.HOME_TAB;       // session fermée ou expirée : retour à la page de connexion
      if (home.view) home.view.webContents.loadURL(home.url);
    }
    refreshMenus();
  }
  showActive();
}

async function bootRemote(origin) {
  state.remote = origin;
  hardenRemoteSession();
  state.profiles = [remoteTab(remote.HOME_TAB)];
  state.active = remote.HOME_TAB;
  refreshMenus();
  publish();
  await refreshRemote();
  watcher = new Watcher({
    fetchSnapshot: (name) => remote.fetchSnapshot(state.remote, name, remoteText),
    // Un bot du serveur est censé tourner en continu : un instantané qui vieillit est donc signalé. (Si c'est le
    // serveur entier qui ne répond plus, rien n'est notifié : l'onglet d'accueil le dit, pas une notification.)
    isRunning: () => true,
    notify: notifyUser,
  });
  await watch();
  setInterval(() => { refreshRemote().then(watch); }, WATCH_MS);
  if (SMOKE_DIR) smoke();
}

// ---------------------------------------------------------------- choix : un serveur, ou cet ordinateur
function openConnect() {
  if (connectWin && !connectWin.isDestroyed()) {
    connectWin.focus();
    return;
  }
  connectWin = new BrowserWindow({
    width: 560, height: 470, resizable: false, minimizable: false, maximizable: false, parent: visibleWindow(), modal: false,
    backgroundColor: "#0d1117", title: "tradeagent", autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "connect-preload.js"), sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  connectWin.setMenuBarVisibility(false);
  connectWin.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  connectWin.webContents.on("will-navigate", (event) => event.preventDefault());
  connectWin.on("closed", () => { connectWin = null; });
  connectWin.loadFile(path.join(__dirname, "connect.html"));
}

// Changer de mode redémarre l'application. La session ouverte sur l'ancien serveur est effacée du disque d'abord.
async function restart() {
  await session.fromPartition(REMOTE_PARTITION).clearStorageData().catch(() => {});
  // Sans --local ni --profile : ils rejoueraient l'ancien choix au redémarrage.
  app.relaunch({ args: process.argv.slice(1).filter((a) => a !== "--local" && !a.startsWith("--profile=")) });
  app.quit();
}

// Des bots tournent sur ce poste : on ne change pas de mode par-dessus (quitter les arrêterait, et un « Annuler » à
// la confirmation laisserait un réglage écrit mais pas appliqué).
function busyWithBots() {
  return supervisor && supervisor.running().length
    ? "Des bots tournent sur cet ordinateur. Arrête-les d'abord (menu Bots, Tout arrêter), puis recommence." : "";
}

// Rend "" si l'adresse est acceptée (l'application redémarre alors dessus), sinon le refus à montrer.
async function chooseRemote(text) {
  const busy = busyWithBots();
  if (busy) return busy;
  let origin;
  try {
    origin = remote.parseAddress(text);
  } catch (exc) {
    return String(exc.message || exc);
  }
  const fetchText = async (url) => {
    const response = await session.fromPartition(PROBE_PARTITION).fetch(url, {
      credentials: "omit", redirect: "manual", cache: "no-store", signal: AbortSignal.timeout(REMOTE_TIMEOUT_MS),
    });
    return { status: response.status, text: await remote.boundedText(response, remote.MAX_SMALL_BYTES) };
  };
  if (!(await remote.probe(origin, fetchText))) {
    return `Aucun serveur tradeagent ne répond à ${origin}. Vérifie l'adresse, et que ce poste est bien sur le même réseau que le serveur.`;
  }
  // Un bot a pu être démarré pendant la vérification (jusqu'à 8 s) : on revérifie juste avant d'écrire le réglage.
  const busyNow = busyWithBots();
  if (busyNow) return busyNow;
  remote.writeConfig(CONFIG_FILE, { remote: origin });
  await restart();
  return "";
}

async function chooseLocal() {
  if (busyWithBots()) return;
  remote.writeConfig(CONFIG_FILE, { mode: "local" });
  await restart();
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

// ---------------------------------------------------------------- première installation (version portable)
function announce(title, text) {
  state.error_title = title;
  state.error = text;
  publish();
}

// Rend vrai quand l'environnement Python est prêt. Les choix passent par des boîtes natives, pas par la page.
async function ensureInstalled() {
  for (;;) {
    let problem;
    try {
      const manifest = setup.readManifest(PAYLOAD);
      if (!setup.needsInstall(ROOT, manifest)) return true;
      announce("Première installation de tradeagent", "Recherche de Python…");
      const { python, tooOld } = await setup.findPython();
      if (python) {
        let step = "";
        await setup.install({
          root: ROOT, payload: PAYLOAD, manifest, python,
          onStep: (label) => { step = label; announce("Première installation de tradeagent", `${label}…`); },
          onLine: (line) => announce("Première installation de tradeagent", `${step}…\n\n${line.slice(0, 200)}`),
        });
        return true;
      }
      problem = { title: "Python est nécessaire", text: setup.missingPythonText(tooOld), download: true };
    } catch (exc) {
      problem = { title: "L'installation a échoué", text: String(exc.message || exc), download: false };
    }
    announce(problem.title, problem.text);
    if (SMOKE_DIR) {
      fs.mkdirSync(SMOKE_DIR, { recursive: true });
      fs.writeFileSync(path.join(SMOKE_DIR, "state.json"), JSON.stringify({ setup_error: problem }, null, 2));
      app.quit();
      return false;
    }
    const buttons = problem.download ? ["Ouvrir la page de téléchargement", "Réessayer", "Quitter"] : ["Réessayer", "Quitter"];
    const { response } = await dialog.showMessageBox(visibleWindow(), {
      type: "warning", title: "tradeagent", message: problem.title, detail: problem.text,
      buttons, defaultId: buttons.length - 2, cancelId: buttons.length - 1,
    });
    if (response === buttons.length - 1) {
      app.quit();
      return false;
    }
    if (problem.download && response === 0) shell.openExternal(setup.PYTHON_URL);
  }
}

async function boot() {
  let choice;
  try {
    choice = SMOKE_DIR && SMOKE_REMOTE ? { remote: remote.parseAddress(SMOKE_REMOTE) }
      : SMOKE_DIR || process.argv.includes("--local") ? { mode: "local" } : remote.readConfig(CONFIG_FILE);
  } catch (exc) {
    fatal("Adresse de serveur invalide", String(exc.message || exc));
    if (SMOKE_DIR) app.quit();
    return;
  }
  if (choice.remote) {
    await bootRemote(choice.remote);
    return;
  }
  if (!choice.mode && PAYLOAD) {
    // Version portable, premier lancement : on demande d'abord où tournent les bots. Un poste qui ne fait que
    // regarder un serveur n'a pas besoin de Python, donc on n'installe rien avant ce choix.
    fatal("Où tournent les bots ?", "Choisis dans la fenêtre qui vient de s'ouvrir : un serveur, ou cet ordinateur.\n(Menu Serveur pour la rouvrir.)");
    refreshMenus();
    openConnect();
    return;
  }
  if (PAYLOAD && !(await ensureInstalled())) return;
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
  supervisor = new Supervisor({ python, root: ROOT, logDir: LOG_DIR });
  supervisor.on("change", onBotChange);
  refreshMenus();
  publish();
  await Promise.all(state.profiles.map((p) => startWeb(p, python).then(() => {
    if (state.active === p.name) showActive();
    else publish();
  })));
  watcher = new Watcher({
    fetchSnapshot: backend.fetchSnapshot, isRunning: (name) => botStatus(name) === "running", notify: notifyUser,
  });
  await watch();
  setInterval(watch, WATCH_MS);
  if (SMOKE_DIR) smoke();
}

// ---------------------------------------------------------------- notifications de bureau (sortantes uniquement)
function watch() {
  if (!watcher || quitting) return Promise.resolve();
  const watched = state.profiles.filter((p) => p.status === "ready" && p.name !== remote.HOME_TAB);
  return watcher.poll(watched).catch((exc) => console.error(`[notifications] ${exc.message}`));
}

function notifyUser(name, { title, body }) {
  if (SMOKE_DIR) {
    smokeNotes.push({ profile: name, title, body });
    return;
  }
  if (!Notification.isSupported()) return;
  const note = new Notification({ title, body });
  liveNotes.add(note);
  note.on("click", () => { liveNotes.delete(note); showWindow(); select(name); });
  note.on("close", () => liveNotes.delete(note));
  note.show();
}

// ---------------------------------------------------------------- bots (menu natif et zone de notification)
async function startBot(name) {
  const profile = state.profiles.find((p) => p.name === name);
  if (!profile || !supervisor || botStatus(name) !== "stopped") return;
  if (profile.costs_money && !SMOKE_DIR) {
    const { response } = await dialog.showMessageBox(visibleWindow(), {
      type: "warning", title: "tradeagent", buttons: ["Démarrer", "Annuler"], defaultId: 1, cancelId: 1,
      message: `Démarrer le bot « ${name} » ?`,
      detail: "L'argent des ordres est fictif, mais ce profil appelle l'API du LLM, qui est facturée pour de vrai.\n"
        + "La dépense est plafonnée par le code (budgets par jour et au total dans config.yaml).",
    });
    if (response !== 0) return;
  }
  supervisor.start(name);
}

function stopBot(name) {
  if (supervisor) supervisor.stop(name);
}

function onBotChange(name, bot) {
  refreshMenus();
  publish();
  if (bot.status === "stopped") watch();   // un bot qui meurt écrit son état avant de sortir : le lire tout de suite
  if (bot.status !== "stopped" || bot.expected || quitting) return;
  // Arrêt que personne n'a demandé : refus au démarrage (clé absente, base déjà utilisée), mort, halted, plantage.
  const title = `Le bot « ${name} » s'est arrêté`;
  const detail = bot.tail.slice(-8).join("\n") || `code de sortie ${bot.code}`;
  if (win && win.isVisible() && win.isFocused()) {
    dialog.showMessageBox(win, { type: "warning", title: "tradeagent", message: title, detail });
  } else if (Notification.isSupported()) {
    const note = new Notification({ title, body: bot.tail[bot.tail.length - 1] || `code de sortie ${bot.code}` });
    note.on("click", () => { showWindow(); select(name); });
    note.show();
  }
}

function botItems() {
  if (state.remote) {
    return [{ label: "Les bots tournent sur le serveur : cette application ne fait que les montrer", enabled: false }];
  }
  const items = [];
  for (const p of state.profiles) {
    const status = botStatus(p.name);
    items.push({ label: `${p.name} : ${BOT_LABELS[status]}`, enabled: false });
    items.push({ label: `    Démarrer ${p.name}`, enabled: status === "stopped", click: () => startBot(p.name) });
    items.push({ label: `    Arrêter ${p.name}`, enabled: status === "running", click: () => stopBot(p.name) });
  }
  const running = supervisor ? supervisor.running().length : 0;
  items.push({ type: "separator" });
  items.push({ label: "Tout arrêter", enabled: running > 0, click: () => supervisor.stopAll() });
  items.push({ label: "Ouvrir le dossier des journaux", click: () => { fs.mkdirSync(LOG_DIR, { recursive: true }); shell.openPath(LOG_DIR); } });
  return items;
}

function visibleWindow() {
  return win && !win.isDestroyed() && win.isVisible() ? win : undefined;
}

function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function refreshMenus() {
  buildMenu();
  if (!tray) return;
  const running = supervisor ? supervisor.running().length : 0;
  tray.setToolTip(state.remote ? `tradeagent : ${new URL(state.remote).host}`
    : running ? `tradeagent — ${running} bot${running > 1 ? "s" : ""} en marche` : "tradeagent — aucun bot en marche");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Afficher la fenêtre", click: showWindow },
    { type: "separator" },
    ...botItems(),
    { type: "separator" },
    { label: "Quitter", click: () => app.quit() },
  ]));
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(path.join(__dirname, "icon.png")).resize({ width: 16, height: 16 }));
  tray.on("click", showWindow);
  refreshMenus();
}

// ---------------------------------------------------------------- menu
function buildMenu() {
  const activeView = () => (state.profiles.find((p) => p.name === state.active) || {}).view;
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: "Fichier",
      submenu: [
        { label: "Réduire dans la zone de notification", accelerator: "CmdOrCtrl+W", click: () => win && win.close() },
        { type: "separator" },
        { label: "Quitter (arrête les bots)", accelerator: "CmdOrCtrl+Q", click: () => app.quit() },
      ],
    },
    { label: "Bots", submenu: botItems() },
    {
      label: "Serveur",
      submenu: state.remote ? [
        { label: `Connecté à ${new URL(state.remote).host}`, enabled: false },
        { label: "Changer d'adresse…", click: openConnect },
        { label: "Utiliser cet ordinateur (mode local)", click: chooseLocal },
      ] : [
        { label: "Les interfaces et les bots tournent sur cet ordinateur", enabled: false },
        { label: "Se connecter à un serveur…", click: openConnect },
      ],
    },
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
          detail: (state.remote
            ? `Cette fenêtre affiche l'interface en lecture seule du serveur ${new URL(state.remote).host}.\n`
              + "Les bots tournent sur le serveur : rien ne se démarre ni ne s'arrête d'ici.\n"
            : "Cette fenêtre affiche l'interface locale en lecture seule de chaque profil.\n"
              + "Le menu Bots démarre et arrête les bots ; fermer la fenêtre ne les arrête pas, Quitter si.\n")
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
  const SMOKE_BOT = supervisor ? SMOKE_BOT_ASKED : "";      // en mode distant il n'y a aucun bot à démarrer
  if (SMOKE_BOT) await startBot(SMOKE_BOT);
  await new Promise((r) => setTimeout(r, SMOKE_BOT ? 7000 : 2500));
  fs.mkdirSync(SMOKE_DIR, { recursive: true });
  const during = SMOKE_BOT ? supervisor.state(SMOKE_BOT) : null;
  if (win.isVisible()) win.close();            // doit ranger la fenêtre, pas quitter ni arrêter le bot
  await new Promise((r) => setTimeout(r, 500));
  const hidden = { window_visible: win.isVisible(), bot_while_hidden: SMOKE_BOT ? supervisor.state(SMOKE_BOT).status : null };
  win.show();
  await new Promise((r) => setTimeout(r, 500));
  fs.writeFileSync(path.join(SMOKE_DIR, "shell.png"), (await win.webContents.capturePage()).toPNG());
  const active = state.profiles.find((p) => p.name === state.active);
  if (active && active.view) {
    fs.writeFileSync(path.join(SMOKE_DIR, "view.png"), (await active.view.webContents.capturePage()).toPNG());
  }
  if (SMOKE_BOT) await supervisor.stopAll();
  await watch();
  fs.writeFileSync(path.join(SMOKE_DIR, "state.json"), JSON.stringify({
    remote: state.remote, active: state.active,
    profiles: state.profiles.map((p) => ({ name: p.name, port: p.port, status: p.status, error: p.error })),
    hidden, bot_during: during, bot_after: SMOKE_BOT ? supervisor.state(SMOKE_BOT) : null,
    watch: watcher.view(), notifications: smokeNotes,
  }, null, 2));
  app.quit();
}

// ---------------------------------------------------------------- cycle de vie
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);

  ipcMain.on("select-tab", (event, name) => {
    if (win && event.sender === win.webContents) select(String(name));
  });
  ipcMain.on("shell-ready", (event) => {
    if (!win || event.sender !== win.webContents) return;
    shellReady = true;
    publish();
  });
  // La fenêtre de choix : trois messages, qui ne règlent que l'adresse affichée. Aucun n'agit sur un bot.
  const fromConnect = (event) => connectWin && !connectWin.isDestroyed() && event.sender === connectWin.webContents;
  ipcMain.handle("connect-remote", (event, address) => (fromConnect(event) ? chooseRemote(String(address).slice(0, 300)) : "refusé"));
  ipcMain.on("connect-local", (event) => { if (fromConnect(event)) chooseLocal(); });
  ipcMain.on("connect-ready", (event) => { if (fromConnect(event)) event.sender.send("connect-current", state.remote || ""); });

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
    win.on("close", (event) => {
      if (quitting) return;
      event.preventDefault();          // fermer = ranger : les bots et la surveillance continuent
      win.hide();
      if (!trayHintShown && !SMOKE_DIR && Notification.isSupported()) {
        trayHintShown = true;
        new Notification({
          title: "tradeagent continue en arrière-plan",
          body: "Les bots ne sont pas arrêtés. Icône dans la zone de notification : clic pour rouvrir, clic droit puis Quitter pour tout arrêter.",
        }).show();
      }
    });
    win.on("closed", () => { win = null; });
    win.loadFile(path.join(__dirname, "shell.html"));
    createTray();
    boot();
  });

  // Quitter : confirmation s'il reste des bots, arrêt propre de chacun, puis seulement la sortie.
  app.on("before-quit", (event) => {
    if (quitting) return;
    const running = supervisor ? supervisor.running() : [];
    if (!running.length) {
      finishQuit();
      return;
    }
    event.preventDefault();
    const ask = SMOKE_DIR ? Promise.resolve({ response: 0 }) : dialog.showMessageBox(visibleWindow(), {
      type: "question", title: "tradeagent", buttons: ["Arrêter les bots et quitter", "Annuler"], defaultId: 1, cancelId: 1,
      message: `Quitter arrête ${running.length > 1 ? "les bots" : "le bot"} : ${running.join(", ")}`,
      detail: "Chaque bot finit son cycle en cours, puis s'arrête. Les positions éventuelles sont conservées.\n"
        + "Pour les laisser tourner, ferme simplement la fenêtre.",
    });
    ask.then(async ({ response }) => {
      if (response !== 0) return;
      await supervisor.stopAll();
      finishQuit();
      app.quit();
    });
  });
  app.on("window-all-closed", () => {});   // la fenêtre est rangée, jamais fermée, tant qu'on n'a pas quitté
  process.on("exit", () => { if (supervisor) supervisor.killAll(); });
}

function finishQuit() {
  quitting = true;
  app.isQuitting = true;
  setup.killRunning();   // une installation en cours ne doit pas laisser pip tourner seul
  stopChildren();
  if (tray) tray.destroy();
}
