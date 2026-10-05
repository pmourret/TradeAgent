"use strict";
/*
 * Fenêtre de choix : l'adresse d'un serveur, ou cet ordinateur. Elle ne peut rien faire d'autre que proposer ce
 * choix au processus principal, qui le vérifie lui-même. textContent uniquement, ni innerHTML ni style en ligne.
 */
(function () {
  const form = document.getElementById("form");
  const address = document.getElementById("address");
  const error = document.getElementById("error");
  const button = document.getElementById("connect");

  window.connect.onCurrent((value) => { if (value && !address.value) address.value = value; });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    button.disabled = true;
    button.textContent = "Vérification…";
    const refusal = await window.connect.remote(address.value);
    button.disabled = false;
    button.textContent = "Se connecter";
    if (refusal) error.textContent = refusal;      // accepté : l'application redémarre sur le serveur
  });

  document.getElementById("local").addEventListener("click", () => window.connect.local());
  window.connect.ready();
})();
