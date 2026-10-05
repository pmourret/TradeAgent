"use strict";
/*
 * Mode distant : l'application affiche l'interface d'un serveur (`tradeagent serve`, derrière un proxy HTTPS)
 * au lieu de lancer la sienne. Rien ici ne dépend d'Electron : ce fichier est testé avec `node --test`.
 *
 * En mode distant l'application ne fait que montrer : elle ne démarre ni n'arrête aucun bot, et ne parle au
 * serveur qu'en lecture (les seules écritures, connexion et déconnexion, sont les formulaires des pages du
 * serveur, que l'utilisateur remplit lui-même).
 */
const fs = require("node:fs");
const path = require("node:path");

const HOME_TAB = "accueil";             // l'onglet de la page d'accueil du serveur (connexion, liste des profils)
const MAX_PROFILES = 16;
const MAX_BODY_BYTES = 4 * 1024 * 1024;     // un instantané
const MAX_SMALL_BYTES = 64 * 1024;          // la liste des profils, le contrôle de santé
const NAME = /^[a-z0-9-]{1,32}$/;

// L'adresse saisie par l'utilisateur, ramenée à une origine HTTPS. Lève une erreur qui dit quoi corriger.
function parseAddress(text) {
  let value = String(text || "").trim();
  if (!value) throw new Error("Saisis l'adresse du serveur, par exemple https://trade.exemple.org");
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) value = "https://" + value;
  let url;
  try {
    url = new URL(value);
  } catch (exc) {
    throw new Error("Cette adresse n'est pas valide. Exemple : https://trade.exemple.org");
  }
  if (url.protocol !== "https:") throw new Error("L'adresse doit commencer par https:// : le mot de passe ne voyage pas en clair.");
  if (url.username || url.password) throw new Error("Ne mets ni identifiant ni mot de passe dans l'adresse : ils se saisissent sur la page de connexion.");
  if ((url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) {
    throw new Error("Donne seulement le nom du serveur, sans chemin : https://" + url.host);
  }
  if (!url.hostname.includes(".")) throw new Error("Donne le nom complet du serveur, par exemple trade.exemple.org");
  return url.origin;
}

// Le fichier de réglage de l'application : {remote: origine} ou {mode: "local"}. Illisible ou absent : rien de choisi.
function readConfig(file) {
  let data;
  try {
    data = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (exc) {
    return {};
  }
  if (data && typeof data.remote === "string") {
    try {
      return { remote: parseAddress(data.remote) };
    } catch (exc) {
      return {};
    }
  }
  return data && data.mode === "local" ? { mode: "local" } : {};
}

function writeConfig(file, config) {
  const clean = config.remote ? { remote: parseAddress(config.remote) } : { mode: "local" };
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(clean, null, 2) + "\n", "utf8");
  return clean;
}

// Le corps d'une réponse, lu par morceaux et abandonné dès qu'il dépasse `maxBytes` : un serveur ne doit pas pouvoir
// remplir la mémoire de l'application. Lève si la limite est dépassée.
async function boundedText(response, maxBytes) {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxBytes) {
      await reader.cancel().catch(() => {});
      throw new Error("réponse trop volumineuse");
    }
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks).toString("utf8");
}

// Une page du serveur ne doit jamais en sortir.
function isRemoteUrl(url, origin) {
  try {
    return new URL(url).origin === origin;
  } catch (exc) {
    return false;
  }
}

// Ce que la session distante a le droit de charger : le serveur, et les images en ligne (data:) de ses pages.
function allowsRequest(url, origin) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "data:" || parsed.origin === origin;
  } catch (exc) {
    return false;
  }
}

function profileUrl(origin, name) {
  if (!NAME.test(name)) throw new Error("nom de profil invalide");
  return `${origin}/p/${name}/`;
}

function snapshotUrl(origin, name) {
  return profileUrl(origin, name) + "api/snapshot";
}

// La réponse de /api/profiles. Seul le nom est repris, et les chemins sont refabriqués ici : ce que dit le serveur
// ne décide jamais d'une adresse à charger.
function parseProfiles(text) {
  const list = JSON.parse(text);
  if (!Array.isArray(list) || !list.length || list.length > MAX_PROFILES) throw new Error("liste de profils invalide");
  const names = [];
  for (const item of list) {
    if (!item || typeof item.name !== "string" || !NAME.test(item.name) || item.name === HOME_TAB) throw new Error("nom de profil invalide");
    if (names.includes(item.name)) throw new Error("profil en double");
    names.push(item.name);
  }
  return names;
}

/*
 * Où en est la session sur le serveur. `fetchText(url, maxBytes)` rend {status, text} ou lève (serveur injoignable,
 * réponse trop volumineuse).
 *   {state: "in", names}   connecté : la liste des profils
 *   {state: "out"}         pas de session (ou pas encore de compte) : seule la page d'accueil du serveur est montrée
 *   {state: "down", error} le serveur ne répond pas, ou ne répond pas comme un serveur tradeagent
 */
async function sessionState(origin, fetchText) {
  let answer;
  try {
    answer = await fetchText(origin + "/api/profiles", MAX_SMALL_BYTES);
  } catch (exc) {
    // Certains moteurs lèvent sur une redirection qu'on leur demande de ne pas suivre (serveur sans compte encore) :
    // si le serveur répond à son contrôle de santé, il est là, et c'est sa page d'accueil qui dira quoi faire.
    if (await probe(origin, fetchText)) return { state: "out" };
    return { state: "down", error: "Le serveur ne répond pas : " + String(exc.message || exc).slice(0, 200) };
  }
  if (answer.status === 200) {
    try {
      return { state: "in", names: parseProfiles(answer.text) };
    } catch (exc) {
      return { state: "down", error: "Le serveur a répondu autre chose qu'une liste de profils. Est-ce bien un serveur tradeagent ?" };
    }
  }
  // 303 : pas encore de compte, le serveur renvoie vers sa création. 0 : la même redirection, que certains
  // moteurs masquent quand on leur demande de ne pas la suivre.
  if (answer.status === 401 || answer.status === 303 || answer.status === 0) return { state: "out" };
  return { state: "down", error: `Le serveur a répondu ${answer.status}. Est-ce bien un serveur tradeagent ?` };
}

// L'instantané d'un profil du serveur, ou null (pas de session, serveur absent, réponse trop grosse ou illisible).
async function fetchSnapshot(origin, name, fetchText) {
  try {
    const answer = await fetchText(snapshotUrl(origin, name), MAX_BODY_BYTES);
    if (answer.status !== 200) return null;
    const snap = JSON.parse(answer.text);
    return snap && typeof snap === "object" && !Array.isArray(snap) ? snap : null;
  } catch (exc) {
    return null;
  }
}

// Vrai si un serveur tradeagent répond à cette adresse (son contrôle de santé, public, répond « ok »).
async function probe(origin, fetchText) {
  try {
    const answer = await fetchText(origin + "/healthz", MAX_SMALL_BYTES);
    return answer.status === 200 && answer.text.trim() === "ok";
  } catch (exc) {
    return false;
  }
}

module.exports = {
  HOME_TAB, MAX_BODY_BYTES, MAX_SMALL_BYTES, boundedText, parseAddress, readConfig, writeConfig, isRemoteUrl, allowsRequest, profileUrl, snapshotUrl,
  parseProfiles, sessionState, fetchSnapshot, probe,
};
