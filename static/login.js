(() => {
  "use strict";
  const form = document.getElementById("login-form");
  const input = document.getElementById("password");
  const btn = document.getElementById("login-btn");
  const err = document.getElementById("login-error");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (btn.disabled) return;
    err.hidden = true;
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: input.value }),
      });
      if (res.ok) { window.location.replace("/"); return; }
      let data = {};
      try { data = await res.json(); } catch { /* non-JSON */ }
      err.textContent = data.error || "Couldn't sign in.";
    } catch {
      err.textContent = "Can't reach the app. Is it still running?";
    }
    err.hidden = false;
    btn.disabled = false;
    btn.classList.remove("loading");
    input.select();
  });
})();
