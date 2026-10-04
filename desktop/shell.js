"use strict";
/*
 * Barre d'onglets : un onglet par profil. Aucune action sur les bots ici.
 * Comme dans l'interface web : textContent uniquement, ni innerHTML ni style en ligne.
 */
(function () {
  const tabs = document.getElementById("tabs");
  const message = document.getElementById("message");
  const LABELS = { starting: "démarrage", ready: "prête", error: "erreur" };

  function render(state) {
    while (tabs.firstChild) tabs.removeChild(tabs.firstChild);
    for (const p of state.profiles) {
      const tab = document.createElement("button");
      tab.className = "tab";
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(p.name === state.active));
      tab.title = p.description + " (interface " + (LABELS[p.status] || p.status) + ")";
      const dot = document.createElement("span");
      dot.className = "dot " + p.status;
      const label = document.createElement("span");
      label.textContent = p.name;
      tab.appendChild(dot);
      tab.appendChild(label);
      tab.addEventListener("click", () => window.desktop.select(p.name));
      tabs.appendChild(tab);
    }

    const active = state.profiles.find((p) => p.name === state.active);
    const show = !active || active.status !== "ready";
    message.hidden = !show;
    if (show) {
      document.getElementById("messageTitle").textContent =
        !active ? state.error_title || "Impossible de démarrer"
          : active.status === "error" ? "Interface du profil « " + active.name + " » indisponible"
            : "Démarrage de l'interface du profil « " + active.name + " »…";
      document.getElementById("messageText").textContent = !active ? state.error || "" : active.error || "";
    }
  }

  window.desktop.onTabs(render);
  window.desktop.ready();
})();
