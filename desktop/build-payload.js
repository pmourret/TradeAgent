"use strict";
/*
 * `npm run payload` : prépare desktop/payload/, ce que la version portable livre à côté d'Electron.
 *  - la roue (wheel) de tradeagent, construite depuis le dépôt ;
 *  - constraints.txt : les versions exactes des dépendances du venv de développement, celles avec lesquelles
 *    les tests ont tourné (l'installation au premier lancement ne prend donc pas « la dernière version ») ;
 *  - config.yaml et .env.example, copiés au premier lancement s'ils sont absents ;
 *  - payload.json : version de l'application et nom de la roue.
 * Utilise le Python du .venv du dépôt (scripts/setup d'abord). Construire la roue demande le réseau (setuptools).
 */
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { venvPython, childEnv } = require("./lib/backend");

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(__dirname, "payload");
const python = venvPython(ROOT);
const pip = (args, options = {}) => execFileSync(python, ["-m", "pip", "--disable-pip-version-check", ...args],
  { cwd: ROOT, env: childEnv(), encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], ...options });

if (!fs.existsSync(python)) {
  console.error(`Aucun venv dans ${ROOT} : lance d'abord scripts\\setup.bat (ou scripts/setup.sh).`);
  process.exit(1);
}

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });

// setuptools construit dans l'arbre et réutilise build/lib : un module supprimé de src/ resterait dans la roue.
const BUILD = path.join(ROOT, "build");
fs.rmSync(BUILD, { recursive: true, force: true });
pip(["wheel", ROOT, "--no-deps", "--wheel-dir", OUT]);
fs.rmSync(BUILD, { recursive: true, force: true });
const wheels = fs.readdirSync(OUT).filter((f) => f.endsWith(".whl"));
if (wheels.length !== 1) {
  console.error(`Une seule roue attendue dans ${OUT}, trouvé : ${wheels.join(", ") || "aucune"}`);
  process.exit(1);
}

// Contraintes : seulement des lignes nom==version, sans tradeagent lui-même (il vient de la roue).
const frozen = pip(["freeze", "--exclude-editable"]).split(/\r?\n/)
  .filter((line) => /^[A-Za-z0-9_.-]+==[^\s;]+$/.test(line) && !/^tradeagent==/i.test(line));
if (!frozen.length) {
  console.error("pip freeze n'a rendu aucune dépendance : le venv du dépôt est-il installé ?");
  process.exit(1);
}
fs.writeFileSync(path.join(OUT, "constraints.txt"), frozen.join("\n") + "\n", "utf8");

for (const name of ["config.yaml", ".env.example"]) fs.copyFileSync(path.join(ROOT, name), path.join(OUT, name));

const { version } = require("./package.json");
fs.writeFileSync(path.join(OUT, "payload.json"), JSON.stringify({ version, wheel: wheels[0] }, null, 2) + "\n", "utf8");
console.log(`payload prêt : ${wheels[0]}, ${frozen.length} dépendances épinglées, version ${version}`);
