(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const VIEWS = ["home", "preview", "confirm", "download"];
  const BASE_TITLE = document.title;

  const state = {
    url: null,        // canonical URL returned by the backend
    video: null,      // metadata summary (incl. quality options)
    kind: "video",    // "video" | "audio"
    jobId: null,
    jobRunning: false,
    pollTimer: null,
    health: null,
    folder: null,
    update: null,
  };

  // ------------------------------------------------------------------ prefs
  // Small per-user conveniences; the app works fine if storage is unavailable.
  const prefs = {
    get(key, fallback) {
      try { const v = localStorage.getItem("vd." + key); return v == null ? fallback : JSON.parse(v); }
      catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem("vd." + key, JSON.stringify(value)); } catch { /* ignore */ }
    },
  };
  state.kind = prefs.get("kind", "video") === "audio" ? "audio" : "video";

  // ------------------------------------------------------------------ utils
  const fmtDuration = (s) => {
    if (s == null || !isFinite(s)) return null;
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  };
  const fmtCount = (n) => n == null ? null :
    new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
  const fmtBytes = (b) => {
    if (!b || !isFinite(b)) return null;
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
  };
  const fmtDate = (iso) => {
    if (!iso) return null;
    const d = new Date(iso + "T00:00:00");
    return isNaN(d) ? iso : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };
  const fmtEta = (s) => {
    if (s == null || !isFinite(s)) return null;
    if (s < 60) return `${Math.max(1, Math.round(s))}s left`;
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${Math.round(s % 60)}s left` : `${Math.floor(m / 60)}h ${m % 60}m left`;
  };

  async function api(path, body) {
    const opts = body === undefined
      ? { method: "GET" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    let res;
    try {
      res = await fetch(path, opts);
    } catch {
      throw new Error("Can't reach the app's backend. Is it still running?");
    }
    let data = {};
    try { data = await res.json(); } catch { /* non-JSON */ }
    if (res.status === 401 && data.login) {
      window.location.href = "/login";
      throw new Error("Please sign in again.");
    }
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
    return data;
  }

  let toastTimer;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  function setThumb(img, src) {
    img.classList.remove("loaded");
    img.removeAttribute("src");
    if (!src) return;
    img.onload = () => img.classList.add("loaded");
    img.onerror = () => img.classList.remove("loaded");
    img.src = src;
  }

  // ------------------------------------------------------------------ views
  function show(view) {
    VIEWS.forEach((v) => { $(`#view-${v}`).hidden = v !== view; });
    const idx = VIEWS.indexOf(view);
    document.querySelectorAll(".steps li").forEach((li) => {
      const i = VIEWS.indexOf(li.dataset.step);
      li.classList.toggle("current", i === idx);
      li.classList.toggle("done", i < idx);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
    const focusTarget = { home: "#url-input", preview: '[data-action="to-confirm"]',
                          confirm: '[data-action="confirm-download"]' }[view];
    if (focusTarget) setTimeout(() => $(focusTarget)?.focus({ preventScroll: true }), 60);
  }

  // ------------------------------------------------------------------ home
  const form = $("#url-form"), input = $("#url-input"), fetchBtn = $("#fetch-btn");
  const homeError = $("#home-error"), homeLoading = $("#home-loading");

  function homeErr(msg) {
    homeError.textContent = msg || "";
    homeError.hidden = !msg;
    input.closest(".field").classList.toggle("invalid", !!msg);
  }

  function clientValidate(raw) {
    const v = raw.trim();
    if (!v) return "Please paste a video link.";
    let u;
    try { u = new URL(v); } catch { return "That doesn't look like a valid link."; }
    if (!/^https?:$/.test(u.protocol)) return "Links must start with http:// or https://";
    if (!u.hostname.includes(".")) return "That doesn't look like a valid link.";
    return null;
  }

  let fetchSeq = 0;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (fetchBtn.disabled) return;
    const raw = input.value;
    const problem = clientValidate(raw);
    if (problem) { homeErr(problem); input.focus(); return; }

    homeErr(null);
    const seq = ++fetchSeq;
    fetchBtn.disabled = true;
    fetchBtn.classList.add("loading");
    fetchBtn.querySelector(".btn-label").textContent = "Fetching…";
    homeLoading.hidden = false;
    try {
      const data = await api("/api/info", { url: raw.trim() });
      if (seq !== fetchSeq) return;
      state.url = data.url;
      state.video = data.video;
      renderPreview();
      show("preview");
    } catch (err) {
      if (seq === fetchSeq) homeErr(err.message);
    } finally {
      if (seq === fetchSeq) {
        fetchBtn.disabled = false;
        fetchBtn.classList.remove("loading");
        fetchBtn.querySelector(".btn-label").textContent = "Fetch Video";
        homeLoading.hidden = true;
      }
    }
  });

  input.addEventListener("input", () => { if (!homeError.hidden) homeErr(null); });

  $("#paste-btn").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { input.value = text.trim(); homeErr(null); }
      input.focus();
    } catch {
      input.focus();
      toast("Press Ctrl+V to paste");
    }
  });

  // ------------------------------------------------------------------ preview
  function renderPreview() {
    const v = state.video;
    setThumb($("#pv-thumb"), v.thumbnail);
    $("#pv-duration").textContent = fmtDuration(v.duration) || "";
    $("#pv-site").textContent = v.site || "";
    $("#pv-title").textContent = v.title;
    $("#pv-uploader").textContent = v.uploader || "";
    $("#pv-desc").textContent = v.description || "";

    const meta = [
      ["Duration", fmtDuration(v.duration)],
      ["Views", fmtCount(v.view_count)],
      ["Likes", fmtCount(v.like_count)],
      ["Published", fmtDate(v.upload_date)],
      ["Best quality", v.resolution],
      ["Approx. size", fmtBytes(v.filesize)],
    ].filter(([, val]) => val);
    const dl = $("#pv-meta");
    dl.replaceChildren(...meta.map(([k, val]) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = val;
      wrap.append(dt, dd);
      return wrap;
    }));
    dl.hidden = meta.length === 0;
  }

  // ------------------------------------------------------------------ confirm
  const qualitySelect = $("#cf-quality");

  function videoOptions() {
    return (state.video?.options?.video || []).filter((o) => o.height);
  }

  function renderQuality() {
    const ffmpeg = state.health?.ffmpeg !== false;
    document.querySelectorAll(".segmented [data-kind]").forEach((b) => {
      b.setAttribute("aria-checked", String(b.dataset.kind === state.kind));
      b.tabIndex = b.dataset.kind === state.kind ? 0 : -1;
    });
    $(".confirm-q").textContent = state.kind === "audio"
      ? "Is this the audio you want to download?"
      : "Is this the video you want to download?";

    const opts = [];
    if (state.kind === "audio") {
      const a = state.video?.options?.audio || {};
      const size = fmtBytes(a.size);
      opts.push({ value: "best", text: ffmpeg ? `${a.label || "MP3"}${size ? ` · ~${size}` : ""}` : "Best audio" });
      qualitySelect.disabled = true;
      $("#cf-format").textContent = ffmpeg ? "MP3 with cover art" : "Audio (M4A)";
    } else {
      const list = videoOptions();
      if (!list.length) {
        opts.push({ value: "best", text: "Best available" });
      } else {
        list.forEach((o, i) => {
          const size = fmtBytes(o.size);
          opts.push({ value: String(o.height),
                      text: `${o.label}${i === 0 ? " (best)" : ""}${size ? ` · ~${size}` : ""}` });
        });
      }
      qualitySelect.disabled = opts.length < 2;
      $("#cf-format").textContent = ffmpeg ? "MP4 (video + audio)" : "Single-file video";
    }

    qualitySelect.replaceChildren(...opts.map((o) => {
      const el = document.createElement("option");
      el.value = o.value; el.textContent = o.text;
      return el;
    }));

    if (state.kind === "video") {
      // Restore the last choice: same height, or the closest one below it.
      const saved = prefs.get("quality", "best");
      const list = videoOptions();
      let pick = list[0];
      if (saved !== "best" && list.length) {
        pick = list.find((o) => o.height <= saved) || list[list.length - 1];
      }
      if (pick) qualitySelect.value = String(pick.height);
    }
  }

  function renderFolder() {
    const f = state.folder || state.health?.download_dir || "Downloads";
    const el = $("#cf-folder");
    el.textContent = f;
    el.title = f;
  }

  function renderConfirm() {
    const v = state.video;
    setThumb($("#cf-thumb"), v.thumbnail);
    $("#cf-title").textContent = v.title;
    $("#cf-sub").textContent = [v.uploader, fmtDuration(v.duration)].filter(Boolean).join(" · ");
    renderQuality();
    renderFolder();
  }

  document.querySelector(".segmented").addEventListener("click", (e) => {
    const b = e.target.closest("[data-kind]");
    if (!b || b.dataset.kind === state.kind) return;
    state.kind = b.dataset.kind;
    prefs.set("kind", state.kind);
    renderQuality();
  });
  document.querySelector(".segmented").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    state.kind = state.kind === "video" ? "audio" : "video";
    prefs.set("kind", state.kind);
    renderQuality();
    $(`.segmented [data-kind="${state.kind}"]`).focus();
  });

  qualitySelect.addEventListener("change", () => {
    if (state.kind !== "video") return;
    const list = videoOptions();
    const h = parseInt(qualitySelect.value, 10);
    // Choosing the top option means "always best", not "always this height".
    prefs.set("quality", list.length && h === list[0].height ? "best" : h);
  });

  async function changeFolder(btn) {
    btn.disabled = true;
    try {
      const res = await api("/api/choose-folder", {});
      if (!res.cancelled) {
        state.folder = res.download_dir;
        renderFolder();
        toast("Save folder updated");
      }
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ download
  const dlCard = $("#dl-card");

  function setProgress(pct) {
    const p = Math.max(0, Math.min(100, pct || 0));
    $("#dl-fill").style.width = `${p}%`;
    $("#dl-percent").textContent = `${Math.floor(p)}%`;
    $("#dl-bar").setAttribute("aria-valuenow", String(Math.floor(p)));
  }

  async function startDownload() {
    const v = state.video;
    const body = { url: state.url, kind: state.kind, quality: "best" };
    if (state.kind === "video" && videoOptions().length) body.quality = parseInt(qualitySelect.value, 10);

    setThumb($("#dl-thumb"), v.thumbnail);
    $("#dl-title").textContent = v.title;
    $("#dl-stage").textContent = "Starting…";
    $("#dl-stats").textContent = "";
    const cancelBtn = $("#cancel-btn");
    cancelBtn.disabled = false;
    dlCard.dataset.state = "running";
    setProgress(0);
    show("download");

    try {
      const { job_id } = await api("/api/download", body);
      state.jobId = job_id;
      state.jobRunning = true;
      poll();
    } catch (err) {
      fail(err.message);
    }
  }

  function stopPolling() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  async function poll() {
    stopPolling();
    const jobId = state.jobId;
    let job;
    try {
      job = await api(`/api/progress/${jobId}`);
    } catch (err) {
      if (jobId === state.jobId) fail(err.message);
      return;
    }
    if (jobId !== state.jobId) return; // user moved on

    const stats = [];
    if (job.status === "downloading") {
      const done = fmtBytes(job.downloaded_bytes), total = fmtBytes(job.total_bytes);
      if (done && total) stats.push(`${done} / ${total}`);
      if (job.speed) stats.push(`${fmtBytes(job.speed)}/s`);
      const eta = fmtEta(job.eta); if (eta) stats.push(eta);
    }
    $("#dl-stats").textContent = stats.join(" · ");
    $("#dl-stage").textContent = job.stage || "";
    setProgress(job.percent);
    document.title = ["finished", "cancelled", "error"].includes(job.status)
      ? BASE_TITLE : `${Math.floor(job.percent || 0)}% · ${BASE_TITLE}`;

    if (job.status === "finished") {
      state.jobRunning = false;
      dlCard.dataset.state = "done";
      $("#dl-filename").textContent = job.filename || "";
      $("#dl-stats").textContent = fmtBytes(job.total_bytes) || "";
      if (state.health?.web) {
        // Web version: the file is on the server; hand it to this device.
        $(".done-msg").firstChild.textContent = "Ready: ";
        const a = $("#save-file-btn");
        a.href = `/api/file/${jobId}`;
        a.setAttribute("download", job.filename || "");
        notifyDone(job.filename);
        return;
      }
      $(".done-msg").firstChild.textContent = job.already_existed ? "You already have this file: " : "Saved as ";
      if (job.already_existed) toast("You already downloaded this one");
      else notifyDone(job.filename);
      return;
    }
    if (job.status === "cancelled") {
      state.jobRunning = false;
      dlCard.dataset.state = "cancelled";
      return;
    }
    if (job.status === "error") { fail(job.error); return; }

    dlCard.dataset.state = job.status === "processing" ? "processing" : "running";
    state.pollTimer = setTimeout(poll, 500);
  }

  function fail(msg) {
    stopPolling();
    state.jobRunning = false;
    document.title = BASE_TITLE;
    dlCard.dataset.state = "error";
    $("#dl-stage").textContent = "Download failed";
    $("#dl-error").textContent = msg || "The download failed.";
  }

  async function cancelDownload(btn) {
    if (!state.jobId) return;
    btn.disabled = true;
    $("#dl-stage").textContent = "Cancelling…";
    try { await api(`/api/cancel/${state.jobId}`, {}); }
    catch (err) { btn.disabled = false; toast(err.message); }
  }

  function notifyDone(filename) {
    toast("Download complete");
    try {
      if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
        new Notification("Download complete", { body: filename || state.video?.title || "" });
      }
    } catch { /* ignore */ }
  }

  function startOver() {
    if (state.jobRunning) {
      // Leaving the screen doesn't stop the download; make that explicit.
      if (!confirm("The download is still running. Cancel it and start over?")) return;
      api(`/api/cancel/${state.jobId}`, {}).catch(() => {});
    }
    stopPolling();
    state.jobId = null;
    state.jobRunning = false;
    state.url = null;
    state.video = null;
    document.title = BASE_TITLE;
    input.value = "";
    homeErr(null);
    show("home");
  }

  // ------------------------------------------------------------------ updates
  const banner = $("#update-banner");

  function renderUpdate() {
    const u = state.update;
    if (!u || !u.available) { banner.hidden = true; return; }
    banner.hidden = false;
    $("#update-progress").hidden = true;
    $("#update-now").hidden = false;
    $("#update-later").hidden = false;
    $("#update-title").textContent = `Version ${u.latest} is available`;
    if (u.can_install) {
      $("#update-sub").textContent = `You have ${u.current}. Updating takes about a minute and restarts the app.`;
      $("#update-now").textContent = "Update";
    } else {
      $("#update-sub").textContent = `You have ${u.current}. Get the new version from the download page.`;
      $("#update-now").textContent = "Download";
    }
  }

  async function checkForUpdate() {
    try {
      state.update = await api("/api/update");
      renderUpdate();
    } catch { /* offline: no banner */ }
  }

  async function runUpdate() {
    const u = state.update;
    if (!u) return;
    if (!u.can_install) {
      try { await api("/api/open-releases", {}); } catch (err) { toast(err.message); }
      return;
    }
    if (state.jobRunning) { toast("Wait for your download to finish, then update."); return; }
    try {
      await api("/api/update/install", {});
    } catch (err) { toast(err.message); return; }

    $("#update-now").hidden = true;
    $("#update-later").hidden = true;
    $("#update-progress").hidden = false;
    $("#update-title").textContent = `Updating to ${u.latest}…`;
    const tick = async () => {
      let s;
      try { s = await api("/api/update/status"); }
      catch {
        // The app closes itself to let the installer run.
        $("#update-sub").textContent = "Installing… the app will reopen in a moment.";
        return;
      }
      if (s.status === "downloading") {
        $("#update-sub").textContent = `Downloading update… ${Math.floor(s.percent || 0)}%`;
        $("#update-fill").style.width = `${s.percent || 0}%`;
      } else if (s.status === "installing") {
        $("#update-fill").style.width = "100%";
        $("#update-sub").textContent = "Installing… the app will reopen in a moment.";
      } else if (s.status === "error") {
        toast(s.error || "The update failed.");
        renderUpdate();
        return;
      }
      setTimeout(tick, 400);
    };
    tick();
  }

  // ------------------------------------------------------------------ actions
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    switch (btn.dataset.action) {
      case "start-over":
        startOver();
        break;
      case "to-confirm":
        renderConfirm();
        show("confirm");
        break;
      case "cancel-confirm":
        show("preview");
        break;
      case "change-folder":
        changeFolder(btn);
        break;
      case "confirm-download":
        // The ONLY place a download is started — after an explicit click.
        try {
          if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
        } catch { /* ignore */ }
        startDownload();
        break;
      case "cancel-download":
        cancelDownload(btn);
        break;
      case "retry":
        renderConfirm();
        show("confirm");
        break;
      case "open-folder":
        try { await api(`/api/open/${state.jobId}`, {}); }
        catch (err) { toast(err.message); }
        break;
      case "update-now":
        runUpdate();
        break;
      case "update-later":
        banner.hidden = true;
        break;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#view-confirm").hidden) show("preview");
    else if (!$("#view-preview").hidden) startOver();
  });

  // ------------------------------------------------------------------ init
  api("/api/health").then((h) => {
    state.health = h;
    if (h.web) {
      document.documentElement.classList.add("web");
      $("#cf-folder-row").hidden = true;
      $("#open-folder-btn").hidden = true;
      $("#save-file-btn").hidden = false;
    }
    $("#app-version").textContent = h.version ? `Video Downloader ${h.version}` : "";
  }).catch(() => {});
  api("/api/settings").then((s) => { state.folder = s.download_dir; }).catch(() => {});
  checkForUpdate();
  show("home");
})();
