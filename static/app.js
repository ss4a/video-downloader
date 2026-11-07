(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const VIEWS = ["home", "preview", "confirm", "download"];
  const BASE_TITLE = document.title;

  const state = {
    url: null,        // canonical URL returned by the backend
    video: null,      // metadata summary
    jobId: null,
    pollTimer: null,
    health: null,
  };

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
  function renderConfirm() {
    const v = state.video;
    setThumb($("#cf-thumb"), v.thumbnail);
    $("#cf-title").textContent = v.title;
    $("#cf-sub").textContent = [v.uploader, fmtDuration(v.duration)].filter(Boolean).join(" · ");
    $("#cf-quality").textContent = v.resolution ? `Best available (up to ${v.resolution})` : "Best available";
    const ffmpeg = state.health?.ffmpeg;
    $("#cf-format").textContent = ffmpeg === false ? "Single-file video" : "MP4 video + audio";
    $("#cf-folder").textContent = state.health?.download_dir || "Downloads folder";
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
    setThumb($("#dl-thumb"), v.thumbnail);
    $("#dl-title").textContent = v.title;
    $("#dl-stage").textContent = "Starting…";
    $("#dl-stats").textContent = "";
    dlCard.dataset.state = "running";
    setProgress(0);
    show("download");

    try {
      const { job_id } = await api("/api/download", { url: state.url });
      state.jobId = job_id;
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
    document.title = job.status === "finished" ? BASE_TITLE : `${Math.floor(job.percent || 0)}% · ${BASE_TITLE}`;

    if (job.status === "finished") {
      dlCard.dataset.state = "done";
      $("#dl-filename").textContent = job.filename || "";
      $("#dl-stats").textContent = fmtBytes(job.total_bytes) || "";
      notifyDone(job.filename);
      return;
    }
    if (job.status === "error") { fail(job.error); return; }

    dlCard.dataset.state = job.status === "processing" ? "processing" : "running";
    state.pollTimer = setTimeout(poll, 500);
  }

  function fail(msg) {
    stopPolling();
    document.title = BASE_TITLE;
    dlCard.dataset.state = "error";
    $("#dl-stage").textContent = "Download failed";
    $("#dl-error").textContent = msg || "The download failed.";
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
    stopPolling();
    state.jobId = null;
    state.url = null;
    state.video = null;
    document.title = BASE_TITLE;
    input.value = "";
    homeErr(null);
    show("home");
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
      case "confirm-download":
        // The ONLY place a download is started — after an explicit click.
        try {
          if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
        } catch { /* ignore */ }
        startDownload();
        break;
      case "retry":
        renderConfirm();
        show("confirm");
        break;
      case "open-folder":
        try { await api(`/api/open/${state.jobId}`, {}); }
        catch (err) { toast(err.message); }
        break;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#view-confirm").hidden) show("preview");
    else if (!$("#view-preview").hidden) startOver();
  });

  // ------------------------------------------------------------------ init
  api("/api/health").then((h) => { state.health = h; }).catch(() => {});
  show("home");
})();
