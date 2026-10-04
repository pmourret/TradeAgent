"use strict";
/*
 * Version portable (F4) : au premier lancement, préparer l'environnement Python dans le dossier de l'application.
 * Rien ici ne dépend d'Electron : ce fichier est testé avec `node --test`, sans réseau ni vrai Python.
 *
 * Règles :
 *  - tout vit dans le dossier de l'application (config.yaml, .env, data/, .venv) : rien dans le dossier utilisateur ;
 *  - on ne télécharge pas Python : s'il est absent ou trop ancien, on le dit, avec le lien et la version requise ;
 *  - `tradeagent` est installé depuis la roue livrée avec l'application ; ses dépendances viennent de PyPI, aux
 *    versions du fichier de contraintes livré (celles du venv de développement). Limites connues : pas de
 *    vérification par hachage, et une dépendance propre à une autre version de Python n'y figure pas ;
 *  - pip tourne isolé (ni variable PIP_*, ni pip.ini du PC), n'installe que des roues (aucun code de
 *    construction exécuté) et ne reçoit qu'un environnement réduit, sans les secrets du PC ;
 *  - config.yaml et .env ne sont jamais écrasés, et .env n'est jamais lu ;
 *  - le venv est refait quand la version de l'application change.
 */
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { venvPython, childEnv } = require("./backend");

const MIN_PYTHON = [3, 10];
const PYTHON_URL = "https://www.python.org/downloads/";
const MARKER = "tradeagent-install.json";      // dans .venv : écrit en dernier, donc absent si l'installation a échoué
const MANIFEST = "payload.json";
const WHEEL_RE = /^tradeagent-[0-9A-Za-z_.]+-py3-none-any\.whl$/;
const VERSION_SNIPPET = "import sys; print('.'.join(str(n) for n in sys.version_info[:3]))";
const TAIL_LINES = 12;
// Ce que les commandes d'installation reçoivent de l'environnement : de quoi tourner et passer un proxy, rien d'autre.
const ENV_KEPT = new Set(["PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
  "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
  "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE"]);
const running = new Set();       // commandes d'installation en cours, à tuer si l'application quitte

class SetupError extends Error {}

/*
 * Où sont les choses. En développement : la racine du dépôt, sans installation (scripts/setup s'en charge).
 * Application empaquetée : `root` est le dossier qui contient l'exécutable, `payload` ce qui est livré avec elle.
 */
function layout({ packaged, execPath, resourcesPath, env = {}, devRoot }) {
  if (!packaged) return { root: path.resolve(env.TRADEAGENT_ROOT || devRoot), payload: null };
  // PORTABLE_EXECUTABLE_DIR : posé par la cible « portable » d'electron-builder (exécutable unique auto-extrait).
  // TRADEAGENT_ROOT est ignoré ici : l'installation refait le .venv de `root`, qui ne doit jamais être celui du dépôt.
  const root = env.PORTABLE_EXECUTABLE_DIR || path.dirname(execPath);
  return { root: path.resolve(root), payload: path.join(resourcesPath, "python") };
}

function installEnv(env = process.env) {
  const kept = {};
  for (const [key, value] of Object.entries(env)) if (ENV_KEPT.has(key.toUpperCase())) kept[key] = value;
  return childEnv(kept);
}

// À la sortie de l'application : ne laisse pas un pip orphelin, qui verrouillerait le venv au lancement suivant.
function killRunning() {
  for (const child of running) child.kill();
}

function parseVersion(text) {
  const m = /^\s*(\d+)\.(\d+)\.(\d+)\s*$/.exec(String(text || ""));
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

function versionOk(version, min = MIN_PYTHON) {
  return version[0] > min[0] || (version[0] === min[0] && version[1] >= min[1]);
}

function candidates(platform = process.platform) {
  return platform === "win32" ? [["py", "-3"], ["python"], ["python3"]] : [["python3"], ["python"]];
}

// Lance une commande et rend { code, stdout, tail }. Ne lève jamais : une commande introuvable donne code null.
function runCommand(command, args, { cwd, env, onLine = () => {} } = {}) {
  return new Promise((resolve) => {
    const tail = [];
    let stdout = "";
    const note = (line) => {
      tail.push(line);
      if (tail.length > TAIL_LINES) tail.shift();
      onLine(line);
    };
    const keep = (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) if (line.trim()) note(line);
    };
    let child;
    try {
      child = spawn(command, args, { cwd, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    } catch (exc) {
      resolve({ code: null, stdout, tail: [exc.message] });
      return;
    }
    running.add(child);
    child.stdout.on("data", (chunk) => { stdout += String(chunk); keep(chunk); });
    child.stderr.on("data", keep);
    child.on("error", (err) => { running.delete(child); note(err.message); resolve({ code: null, stdout, tail }); });
    child.on("close", (code) => { running.delete(child); resolve({ code, stdout, tail }); });
  });
}

/*
 * Cherche un Python assez récent. Rend { python: { command, args, version } | null, tooOld: "3.8.10" | null }.
 * `tooOld` sert au message : « Python 3.8.10 trouvé, trop ancien ».
 */
async function findPython({ platform = process.platform, run = runCommand, env = installEnv() } = {}) {
  let tooOld = null;
  for (const [command, ...args] of candidates(platform)) {
    const res = await run(command, [...args, "-c", VERSION_SNIPPET], { env });
    const version = res.code === 0 ? parseVersion(res.stdout) : null;
    if (!version) continue;                     // absent, ou alias du Microsoft Store qui ne lance rien
    if (versionOk(version)) return { python: { command, args, version: version.join(".") }, tooOld: null };
    tooOld = tooOld || version.join(".");
  }
  return { python: null, tooOld };
}

function missingPythonText(tooOld) {
  const need = `Python ${MIN_PYTHON.join(".")} ou plus récent`;
  const found = tooOld ? `Python ${tooOld} est installé, mais il est trop ancien.` : "Python n'est pas installé sur ce PC (ou n'est pas dans le PATH).";
  return `${found}\ntradeagent a besoin de ${need}.\n\n`
    + `Télécharge-le sur ${PYTHON_URL}, coche « Add python.exe to PATH » pendant l'installation, puis choisis Réessayer.`;
}

// Ce qui est livré avec l'application : la roue de tradeagent, les contraintes, les modèles de config.
function readManifest(payload) {
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(path.join(payload, MANIFEST), "utf8"));
  } catch (exc) {
    throw new SetupError(`Application incomplète : ${MANIFEST} est absent ou illisible dans ${payload}. Télécharge-la de nouveau.`);
  }
  const ok = manifest && typeof manifest.version === "string" && manifest.version
    && typeof manifest.wheel === "string" && WHEEL_RE.test(manifest.wheel);
  const needed = ok ? [manifest.wheel, "constraints.txt", "config.yaml", ".env.example"] : [];
  if (!ok || needed.some((name) => !fs.existsSync(path.join(payload, name)))) {
    throw new SetupError(`Application incomplète : il manque des fichiers dans ${payload}. Télécharge-la de nouveau.`);
  }
  return { version: manifest.version, wheel: manifest.wheel };
}

function readMarker(root) {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, ".venv", MARKER), "utf8"));
  } catch (exc) {
    return null;
  }
}

// Vrai s'il faut (ré)installer : venv absent, installation jamais terminée, ou version de l'application changée.
function needsInstall(root, manifest, platform = process.platform) {
  const marker = readMarker(root);
  return !fs.existsSync(venvPython(root, platform)) || !marker || marker.version !== manifest.version;
}

function copyIfAbsent(from, to) {
  try {
    fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);   // échoue si la cible existe : jamais d'écrasement
    return true;
  } catch (exc) {
    if (exc.code === "EEXIST") return false;
    throw exc;
  }
}

/*
 * Installe dans `root`. `onStep(texte)` annonce chaque étape, `onLine(ligne)` relaie la sortie des commandes.
 * Lève SetupError avec un message qui dit quoi faire.
 */
async function install({ root, payload, manifest, python, platform = process.platform, run = runCommand, onStep = () => {}, onLine = () => {} }) {
  const env = installEnv();
  const venv = path.join(root, ".venv");
  const step = async (label, command, args) => {
    onStep(label);
    const res = await run(command, args, { cwd: root, env, onLine });
    if (res.code !== 0) {
      throw new SetupError(`${label} : échec (code ${res.code}).\n${res.tail.join("\n")}`);
    }
  };

  onStep("Préparation du dossier de l'application");
  try {
    fs.mkdirSync(path.join(root, "data"), { recursive: true });
    copyIfAbsent(path.join(payload, "config.yaml"), path.join(root, "config.yaml"));
    copyIfAbsent(path.join(payload, ".env.example"), path.join(root, ".env.example"));
    copyIfAbsent(path.join(payload, ".env.example"), path.join(root, ".env"));
  } catch (exc) {
    throw new SetupError(`Impossible d'écrire dans ${root} (${exc.code || exc.message}).\n`
      + "Place le dossier de l'application dans un endroit où tu as le droit d'écrire (pas Program Files), puis relance.");
  }
  try {
    fs.rmSync(venv, { recursive: true, force: true });      // version changée ou installation interrompue : on refait
  } catch (exc) {
    throw new SetupError(`Impossible de refaire ${venv} (${exc.code || exc.message}) : un programme l'utilise encore.\n`
      + "Ferme les autres fenêtres de tradeagent et les processus python.exe restants (Gestionnaire des tâches), puis choisis Réessayer.");
  }

  await step(`Création de l'environnement Python (Python ${python.version})`, python.command, [...python.args, "-m", "venv", venv]);
  await step("Téléchargement et installation des dépendances (connexion Internet nécessaire, quelques minutes)",
    venvPython(root, platform),
    // --isolated : ni variable PIP_* ni pip.ini du PC (donc pas d'autre index que PyPI) ; --only-binary : des roues
    // seulement, aucun code de construction exécuté ; --no-cache-dir : rien dans le dossier utilisateur.
    ["-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir", "--only-binary", ":all:",
      "--constraint", path.join(payload, "constraints.txt"), path.join(payload, manifest.wheel)]);

  fs.writeFileSync(path.join(venv, MARKER), JSON.stringify({ version: manifest.version, python: python.version }) + "\n", "utf8");
}

module.exports = {
  MIN_PYTHON, PYTHON_URL, SetupError, layout, parseVersion, versionOk, candidates, runCommand, findPython,
  missingPythonText, readManifest, needsInstall, install, installEnv, killRunning,
};
