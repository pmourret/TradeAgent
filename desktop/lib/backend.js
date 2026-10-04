"use strict";
/*
 * Lien avec le projet Python : où est le venv, quels profils existent, comment lancer leur interface web.
 * Rien ici ne dépend d'Electron : ce fichier est testé avec `node --test`.
 * Les profils viennent de `profiles.py` (source unique de vérité) : aucun nom ni port n'est recopié côté Node.
 */
const { execFile } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");

const PROFILES_SNIPPET =
  "import json; from tradeagent.profiles import PROFILES; " +
  "print(json.dumps([{'name': p.name, 'port': p.port, 'description': p.description, " +
  "'costs_money': p.costs_money} for p in PROFILES.values()]))";

function venvPython(root, platform = process.platform) {
  return platform === "win32"
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : path.join(root, ".venv", "bin", "python");
}

function childEnv(env = process.env) {
  return { ...env, PYTHONUNBUFFERED: "1", PYTHONUTF8: "1" };
}

function parseProfiles(text) {
  const list = JSON.parse(text);
  if (!Array.isArray(list) || !list.length) throw new Error("aucun profil");
  for (const p of list) {
    if (typeof p.name !== "string" || !/^[a-z0-9-]+$/.test(p.name)) throw new Error("nom de profil invalide");
    if (!Number.isInteger(p.port) || p.port < 1 || p.port > 65535) throw new Error("port invalide");
  }
  return list;
}

function loadProfiles(python, root) {
  return new Promise((resolve, reject) => {
    execFile(python, ["-c", PROFILES_SNIPPET], { cwd: root, env: childEnv(), windowsHide: true }, (err, stdout, stderr) => {
      if (err) return reject(new Error((stderr || err.message).trim()));
      try {
        resolve(parseProfiles(stdout));
      } catch (exc) {
        reject(exc);
      }
    });
  });
}

// Exactement la commande qu'on taperait à la main (comme `launcher.py`), sans --open : la fenêtre, c'est nous.
function webArgs(profile, config = "config.yaml") {
  return ["-u", "-m", "tradeagent", "web", "--profile", profile, "--config", config];
}

function profileUrl(port) {
  return `http://127.0.0.1:${port}/`;
}

// La page d'un profil ne doit jamais quitter son propre serveur local.
function isProfileUrl(url, port) {
  try {
    return new URL(url).origin === `http://127.0.0.1:${port}`;
  } catch (exc) {
    return false;
  }
}

// Vrai seulement si c'est bien NOTRE serveur qui répond sur ce port (en-tête Server posé par web.py).
function probe(port, timeoutMs = 1000) {
  return new Promise((resolve) => {
    const req = http.request(
      { host: "127.0.0.1", port, path: "/api/snapshot", method: "HEAD", timeout: timeoutMs },
      (res) => {
        res.resume();
        resolve(String(res.headers.server || "").startsWith("tradeagent") ? "ours" : "foreign");
      });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve("down"));
    req.end();
  });
}

async function waitForServer(port, { timeoutMs = 15000, intervalMs = 200, stillAlive = () => true } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const state = await probe(port);
    if (state !== "down") return state;
    if (!stillAlive() || Date.now() > deadline) return "down";
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

module.exports = { venvPython, childEnv, parseProfiles, loadProfiles, webArgs, profileUrl, isProfileUrl, probe, waitForServer };
