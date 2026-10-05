"use strict";
/*
 * Barre d'onglets : un onglet par profil. Aucune action sur les bots ici.
 * Comme dans l'interface web : textContent uniquement, ni innerHTML ni style en ligne.
 */
(function () {
  const tabs = document.getElementById("tabs");
  const message = document.getElementById("message");
  const LABELS = { starting: "démarrage", ready: "prête", error: "erreur" };
  const BOTS = { running: "bot en marche", stopping: "arrêt du bot en cours", stopped: "bot arrêté", remote: "sur le serveur" };

  function render(state) {
    while (tabs.firstChild) tabs.removeChild(tabs.firstChild);
    for (const p of state.profiles) {
      const tab = document.createElement("button");
      tab.className = "tab";
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(p.name === state.active));
      // La pastille dit si le bot tourne (démarré par cette application) ; rouge = interface indisponible.
      const bot = BOTS[p.bot] || p.bot;
      tab.title = p.description + "\n" + bot + " · interface " + (LABELS[p.status] || p.status)
        + (p.bot === "remote" ? "\nLecture seule : les bots se pilotent sur le serveur." : "\nDémarrer ou arrêter : menu Bots.");
      const dot = document.createElement("span");
      dot.className = "dot " + (p.status === "error" ? "error" : "bot-" + p.bot);
      const label = document.createElement("span");
      label.textContent = p.name;
      tab.appendChild(dot);
      tab.appendChild(label);
      if (p.bot === "running" || p.bot === "stopping") {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = p.bot === "running" ? "en marche" : "arrêt…";
        tab.appendChild(badge);
      }
      tab.addEventListener("click", () => window.desktop.select(p.name));
      tabs.appendChild(tab);
    }

    const active = state.profiles.find((p) => p.name === state.active);
    const show = !active || active.status !== "ready";
    message.hidden = !show;
    if (show) {
      document.getElementById("messageTitle").textContent =
        !active ? state.error_title || "Impossible de démarrer"
          : active.status === "error" ? "Interface « " + active.name + " » indisponible"
            : "Démarrage de l'interface « " + active.name + " »…";
      document.getElementById("messageText").textContent = !active ? state.error || "" : active.error || "";
    }
  }

  window.desktop.onTabs(render);
  window.desktop.ready();
})();
