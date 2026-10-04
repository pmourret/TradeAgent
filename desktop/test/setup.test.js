"use strict";
// Tests hors réseau, sans Electron, sans vrai Python ni pip : les commandes sont simulées.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const setup = require("../lib/setup");
const { venvPython } = require("../lib/backend");

const WHEEL = "tradeagent-0.1.0-py3-none-any.whl";
const PY = { command: "py", args: ["-3"], version: "3.12.3" };

// Un dossier d'application vide et ce qui est livré avec elle.
function sandbox(t, manifest = { version: "0.1.0", wheel: WHEEL }) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tradeagent-setup-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const root = path.join(dir, "app");
  const payload = path.join(dir, "payload");
  fs.mkdirSync(root);
  fs.mkdirSync(payload);
  fs.writeFileSync(path.join(payload, WHEEL), "roue");
  fs.writeFileSync(path.join(payload, "constraints.txt"), "ccxt==4.4.0\n");
  fs.writeFileSync(path.join(payload, "config.yaml"), "mode: paper\n");
  fs.writeFileSync(path.join(payload, ".env.example"), "ANTHROPIC_API_KEY=\n");
  if (manifest) fs.writeFileSync(path.join(payload, "payload.json"), JSON.stringify(manifest));
  return { root, payload };
}

// Faux `run` : note les commandes ; `python -m venv` crée le python du venv, comme le vrai.
function fakeRun(root, { failOn = null } = {}) {
  const calls = [];
  const run = async (command, args) => {
    calls.push([command, ...args]);
    if (failOn && args.includes(failOn)) return { code: 1, stdout: "", tail: ["ERROR: Could not find a version", "No matching distribution"] };
    if (args.includes("venv")) {
      const python = venvPython(root);
      fs.mkdirSync(path.dirname(python), { recursive: true });
      fs.writeFileSync(python, "");
    }
    return { code: 0, stdout: "", tail: [] };
  };
  return { run, calls };
}

test("en développement rien ne change ; empaquetée, tout vit dans le dossier de l'exécutable", () => {
  const dev = setup.layout({ packaged: false, execPath: "/x/electron", resourcesPath: "/x/res", env: {}, devRoot: "/depot" });
  assert.deepEqual(dev, { root: path.resolve("/depot"), payload: null });
  assert.equal(setup.layout({ packaged: false, env: { TRADEAGENT_ROOT: "/autre" }, devRoot: "/depot" }).root, path.resolve("/autre"));

  const exe = path.join("/apps", "tradeagent", "tradeagent.exe");
  const res = path.join("/apps", "tradeagent", "resources");
  const packed = setup.layout({ packaged: true, execPath: exe, resourcesPath: res, env: {}, devRoot: "/depot" });
  assert.deepEqual(packed, { root: path.resolve("/apps/tradeagent"), payload: path.join(res, "python") });
  // Exécutable unique auto-extrait : le dossier qui compte est celui où l'utilisateur a posé le .exe.
  const single = setup.layout({ packaged: true, execPath: exe, resourcesPath: res, env: { PORTABLE_EXECUTABLE_DIR: "/cle-usb" } });
  assert.equal(single.root, path.resolve("/cle-usb"));
  // Empaquetée, TRADEAGENT_ROOT est ignoré : l'installation refait le .venv de la racine, jamais celui du dépôt.
  const pointed = setup.layout({ packaged: true, execPath: exe, resourcesPath: res, env: { TRADEAGENT_ROOT: "/depot" } });
  assert.equal(pointed.root, path.resolve("/apps/tradeagent"));
});

test("pip ne reçoit ni les secrets ni la configuration pip du PC", () => {
  const env = setup.installEnv({
    Path: "C:\\Windows", SystemRoot: "C:\\Windows", TEMP: "C:\\Temp", HTTPS_PROXY: "http://proxy:8080",
    ANTHROPIC_API_KEY: "sk-secret", BITVAVO_API_SECRET: "secret", PIP_INDEX_URL: "https://ailleurs.test/simple",
    PIP_EXTRA_INDEX_URL: "https://ailleurs.test/simple", PIP_CONFIG_FILE: "C:\\pip.ini", PYTHONPATH: "C:\\x",
  });
  assert.deepEqual(env, {
    Path: "C:\\Windows", SystemRoot: "C:\\Windows", TEMP: "C:\\Temp", HTTPS_PROXY: "http://proxy:8080",
    PYTHONUNBUFFERED: "1", PYTHONUTF8: "1",
  });
});

test("la version de Python est lue strictement et comparée au minimum 3.10", () => {
  assert.deepEqual(setup.parseVersion("3.12.3\r\n"), [3, 12, 3]);
  for (const bad of ["", "Python 3.12.3", "3.12", "3.12.3rc1", null]) assert.equal(setup.parseVersion(bad), null);
  assert.ok(setup.versionOk([3, 10, 0]));
  assert.ok(setup.versionOk([3, 13, 1]));
  assert.ok(setup.versionOk([4, 0, 0]));
  assert.ok(!setup.versionOk([3, 9, 18]));
  assert.ok(!setup.versionOk([2, 7, 18]));
});

test("Python est cherché sans rien télécharger : py -3, puis python, puis python3", async () => {
  const answers = { py: { code: null, stdout: "", tail: ["ENOENT"] }, python: { code: 0, stdout: "3.11.4\n", tail: [] } };
  const asked = [];
  const run = async (command, args) => { asked.push([command, ...args.slice(0, -1)]); return answers[command] || { code: 9009, stdout: "", tail: [] }; };
  const found = await setup.findPython({ platform: "win32", run, env: {} });
  assert.deepEqual(found, { python: { command: "python", args: [], version: "3.11.4" }, tooOld: null });
  assert.deepEqual(asked, [["py", "-3", "-c"], ["python", "-c"]]);
  assert.deepEqual(setup.candidates("linux"), [["python3"], ["python"]]);
});

test("Python absent ou trop ancien : un message avec la version requise et le lien, rien d'autre", async () => {
  const none = await setup.findPython({ platform: "win32", run: async () => ({ code: 9009, stdout: "", tail: [] }), env: {} });
  assert.deepEqual(none, { python: null, tooOld: null });
  const old = await setup.findPython({ platform: "win32", run: async () => ({ code: 0, stdout: "3.8.10\n", tail: [] }), env: {} });
  assert.deepEqual(old, { python: null, tooOld: "3.8.10" });
  const silent = await setup.findPython({ platform: "win32", run: async () => ({ code: 0, stdout: "", tail: [] }), env: {} });
  assert.equal(silent.python, null);                // alias du Microsoft Store : code 0 parfois, mais aucune version
  const broken = await setup.findPython({ platform: "win32", run: async () => ({ code: 1, stdout: "3.12.3\n", tail: [] }), env: {} });
  assert.equal(broken.python, null);                // un Python qui sort en erreur n'est pas retenu

  for (const text of [setup.missingPythonText(null), setup.missingPythonText("3.8.10")]) {
    assert.match(text, /Python 3\.10 ou plus récent/);
    assert.ok(text.includes("https://www.python.org/downloads/"));
  }
  assert.match(setup.missingPythonText("3.8.10"), /Python 3\.8\.10 est installé, mais il est trop ancien/);
  assert.equal(setup.PYTHON_URL, "https://www.python.org/downloads/");
});

test("l'installation crée le venv puis installe la roue livrée, aux versions épinglées", async (t) => {
  const { root, payload } = sandbox(t);
  const manifest = setup.readManifest(payload);
  assert.equal(setup.needsInstall(root, manifest), true);
  const { run, calls } = fakeRun(root);
  const steps = [];
  await setup.install({ root, payload, manifest, python: PY, run, onStep: (s) => steps.push(s) });

  assert.deepEqual(calls[0], ["py", "-3", "-m", "venv", path.join(root, ".venv")]);
  assert.deepEqual(calls[1], [venvPython(root), "-m", "pip", "--isolated", "install", "--disable-pip-version-check", "--no-input",
    "--no-cache-dir", "--only-binary", ":all:", "--constraint", path.join(payload, "constraints.txt"), path.join(payload, WHEEL)]);
  assert.equal(calls.length, 2);
  for (const forbidden of ["--index-url", "--extra-index-url", "--trusted-host", "--pre", "-e", "--upgrade"]) {
    assert.ok(!calls[1].includes(forbidden), forbidden);
  }
  assert.equal(steps.length, 3);
  assert.equal(fs.readFileSync(path.join(root, "config.yaml"), "utf8"), "mode: paper\n");
  assert.ok(fs.existsSync(path.join(root, ".env")));
  assert.ok(fs.existsSync(path.join(root, "data")));
  assert.equal(setup.needsInstall(root, manifest), false);
});

test("config.yaml et .env existants ne sont jamais écrasés", async (t) => {
  const { root, payload } = sandbox(t);
  fs.writeFileSync(path.join(root, "config.yaml"), "stake: 50\n");
  fs.writeFileSync(path.join(root, ".env"), "ANTHROPIC_API_KEY=ne-pas-toucher\n");
  await setup.install({ root, payload, manifest: setup.readManifest(payload), python: PY, run: fakeRun(root).run });
  assert.equal(fs.readFileSync(path.join(root, "config.yaml"), "utf8"), "stake: 50\n");
  assert.equal(fs.readFileSync(path.join(root, ".env"), "utf8"), "ANTHROPIC_API_KEY=ne-pas-toucher\n");
});

test("une installation ratée dit quoi faire et sera refaite au prochain lancement", async (t) => {
  const { root, payload } = sandbox(t);
  const manifest = setup.readManifest(payload);
  await assert.rejects(
    setup.install({ root, payload, manifest, python: PY, run: fakeRun(root, { failOn: "pip" }).run }),
    (err) => err instanceof setup.SetupError && /dépendances.*échec \(code 1\)/s.test(err.message) && err.message.includes("No matching distribution"));
  assert.ok(fs.existsSync(venvPython(root)));                 // le venv existe, mais l'installation n'est pas finie
  assert.equal(setup.needsInstall(root, manifest), true);
  await assert.rejects(
    setup.install({ root, payload, manifest, python: PY, run: fakeRun(root, { failOn: "venv" }).run }),
    /Création de l'environnement Python.*échec/s);
  assert.equal(setup.needsInstall(root, manifest), true);
});

test("le venv est refait quand la version de l'application change", async (t) => {
  const { root, payload } = sandbox(t);
  const manifest = setup.readManifest(payload);
  await setup.install({ root, payload, manifest, python: PY, run: fakeRun(root).run });
  const stale = path.join(root, ".venv", "ancien-paquet.txt");
  fs.writeFileSync(stale, "x");
  assert.equal(setup.needsInstall(root, manifest), false);
  const next = { ...manifest, version: "0.2.0" };
  assert.equal(setup.needsInstall(root, next), true);
  await setup.install({ root, payload, manifest: next, python: PY, run: fakeRun(root).run });
  assert.ok(!fs.existsSync(stale));                           // venv reparti de zéro
  assert.equal(setup.needsInstall(root, next), false);
  assert.equal(setup.needsInstall(root, manifest), true);
  fs.rmSync(venvPython(root));
  assert.equal(setup.needsInstall(root, next), true);         // venv abîmé : le marqueur seul ne suffit pas
});

test("une application incomplète est refusée avec un message clair", (t) => {
  const missing = sandbox(t, null);
  assert.throws(() => setup.readManifest(missing.payload), setup.SetupError);
  for (const manifest of [
    { version: "0.1.0", wheel: "../../ailleurs.whl" }, { version: "0.1.0", wheel: "autre-1.0-py3-none-any.whl" },
    { version: "", wheel: WHEEL }, { wheel: WHEEL }, { version: "0.1.0", wheel: "tradeagent-0.2.0-py3-none-any.whl" }, [],
  ]) {
    const box = sandbox(t, manifest);
    fs.writeFileSync(path.join(box.payload, "autre-1.0-py3-none-any.whl"), "une roue qui n'est pas tradeagent");
    assert.throws(() => setup.readManifest(box.payload), /Application incomplète/, JSON.stringify(manifest));
  }
  for (const name of ["constraints.txt", "config.yaml", ".env.example", WHEEL]) {
    const box = sandbox(t);
    fs.rmSync(path.join(box.payload, name));
    assert.throws(() => setup.readManifest(box.payload), /Application incomplète/, name);
  }
});

test("un dossier où l'on ne peut pas écrire donne un message qui dit quoi faire", async (t) => {
  const { root, payload } = sandbox(t);
  fs.writeFileSync(path.join(root, "data"), "un fichier à la place du dossier");
  await assert.rejects(
    setup.install({ root, payload, manifest: setup.readManifest(payload), python: PY, run: fakeRun(root).run }),
    /Impossible d'écrire dans .*Program Files/s);
});

test("une vraie commande est lancée, sa sortie relayée, et une commande introuvable ne lève pas", async () => {
  const lines = [];
  const ok = await setup.runCommand(process.execPath, ["-e", "console.log('3.12.3'); console.error('bruit')"], { onLine: (l) => lines.push(l) });
  assert.equal(ok.code, 0);
  assert.deepEqual(setup.parseVersion(ok.stdout), [3, 12, 3]);
  assert.deepEqual(lines.sort(), ["3.12.3", "bruit"]);
  // Quitter pendant l'installation tue la commande en cours au lieu de la laisser orpheline.
  const started = Date.now();
  const long = setup.runCommand(process.execPath, ["-e", "setTimeout(() => {}, 60000)"]);
  await new Promise((r) => setTimeout(r, 300));
  setup.killRunning();
  assert.notEqual((await long).code, 0);
  assert.ok(Date.now() - started < 10000);
  const gone = await setup.runCommand("commande-qui-n-existe-pas-tradeagent", []);
  assert.equal(gone.code, null);
  assert.ok(gone.tail.length > 0);
});

test("la version portable ne livre ni secret ni données : seulement le code et le payload", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  assert.deepEqual(pkg.build.extraResources, [{ from: "payload", to: "python" }]);
  for (const entry of pkg.build.files) {
    assert.ok(!/\.env|data|payload|\.venv|\*\*\/\*$/.test(entry) || entry === "lib/**", entry);
  }
  const builder = fs.readFileSync(path.join(__dirname, "..", "build-payload.js"), "utf8");
  assert.ok(builder.includes('["config.yaml", ".env.example"]'));
  assert.ok(!/["'`]\.env["'`]/.test(builder));                // jamais le vrai .env
  assert.ok(builder.includes('"--exclude-editable"') && builder.includes("constraints.txt"));
});
