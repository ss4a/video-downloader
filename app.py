"""
Video Downloader — a small local video downloader powered by yt-dlp.

The Flask backend exposes a tiny JSON API that the browser UI talks to:

    POST /api/info                  {url}  -> video metadata (no download)
    POST /api/download              {url}  -> starts a download job, returns {job_id}
    GET  /api/progress/<job_id>            -> job status / progress
    POST /api/open/<job_id>                -> reveal the finished file in the OS file manager

The server only listens on 127.0.0.1 and rejects requests whose Host header is
not localhost, so other websites / machines cannot drive it.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_from_directory
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

APP_NAME = "Video Downloader"
HOST = "127.0.0.1"
PORT = int(os.environ.get("VD_PORT", "0"))  # 0 = pick a free port at startup
DOWNLOAD_DIR = Path(
    os.environ.get("VD_DOWNLOAD_DIR", Path.home() / "Downloads" / APP_NAME)
).expanduser().resolve()
MAX_URL_LENGTH = 2048
JOB_TTL_SECONDS = 60 * 60  # forget finished jobs after an hour

FROZEN = getattr(sys, "frozen", False)
# PyInstaller unpacks data files to sys._MEIPASS; in source mode use this folder.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
STATIC_DIR = RESOURCE_DIR / "static"
BIN_DIR = RESOURCE_DIR / "bin"  # bundled ffmpeg / deno in packaged builds


def _find_tool(name: str) -> str | None:
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = BIN_DIR / exe
    if bundled.is_file():
        return str(bundled)
    return shutil.which(name)


FFMPEG_PATH = _find_tool("ffmpeg")
HAS_FFMPEG = FFMPEG_PATH is not None

# YouTube (and some other sites) need a JavaScript runtime. Prefer the bundled
# Deno, otherwise enable whichever supported runtimes are installed.
JS_RUNTIMES = {}
for _rt in ("deno", "node", "bun"):
    _p = _find_tool(_rt)
    if _p:
        JS_RUNTIMES[_rt] = {"path": _p}

app = Flask(__name__, static_folder=None)

if FROZEN and sys.stderr is None:
    # Windowed build has no console: keep a small log for troubleshooting.
    import logging
    from logging.handlers import RotatingFileHandler
    _log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    _log_dir.mkdir(parents=True, exist_ok=True)
    _h = RotatingFileHandler(_log_dir / "app.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[_h])


# --------------------------------------------------------------------------- #
# Security helpers
# --------------------------------------------------------------------------- #

@app.before_request
def _check_host():
    # Defends against DNS-rebinding: only accept requests addressed to localhost.
    if request.host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        abort(403)


class UserError(Exception):
    """An error whose message is safe and meaningful to show to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def validate_url(raw) -> str:
    if not isinstance(raw, str):
        raise UserError("Please paste a video link.")
    url = raw.strip()
    if not url:
        raise UserError("Please paste a video link.")
    if len(url) > MAX_URL_LENGTH:
        raise UserError("That link is too long.")
    if any(ch.isspace() for ch in url) or any(ord(ch) < 32 for ch in url):
        raise UserError("That doesn't look like a valid link.")

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise UserError("Links must start with http:// or https://")
    host = (parts.hostname or "").lower()
    if not host or "." not in host and host != "localhost":
        raise UserError("That doesn't look like a valid link.")

    # Refuse local / private network targets — this tool is for public videos.
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise UserError("Local network addresses are not allowed.")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local
               or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise UserError("Local network addresses are not allowed.")
    return url


def get_json_url() -> str:
    # Requiring a JSON body means cross-site pages can't submit this via a
    # plain form; they'd need a CORS preflight, which we never grant.
    if not request.is_json:
        raise UserError("Expected a JSON request.", 415)
    body = request.get_json(silent=True) or {}
    return validate_url(body.get("url"))


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# yt-dlp helpers
# --------------------------------------------------------------------------- #

class _QuietLogger:
    """Swallow yt-dlp console output; errors surface through exceptions."""

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "noplaylist": True,          # one video at a time
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "allow_unplayable_formats": False,  # never touch DRM-protected formats
    }
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def friendly_error(exc: Exception) -> str:
    """Turn a yt-dlp exception into a short, human message."""
    msg = ANSI_RE.sub("", str(exc))
    msg = re.sub(r"^ERROR:\s*", "", msg)
    low = msg.lower()

    checks = [
        ("unsupported url", "This website or link isn't supported."),
        ("drm", "This video is DRM-protected and can't be downloaded."),
        ("private video", "This video is private."),
        ("members-only", "This video is for channel members only."),
        ("join this channel", "This video is for channel members only."),
        ("sign in to confirm your age", "This video is age-restricted and requires signing in."),
        ("age-restricted", "This video is age-restricted and requires signing in."),
        ("age restricted", "This video is age-restricted and requires signing in."),
        ("sign in","This video requires signing in, so it can't be downloaded here."),
        ("login required", "This video requires signing in, so it can't be downloaded here."),
        ("paywall", "This video is behind a paywall."),
        ("premium", "This video requires a paid subscription."),
        ("not available in your country", "This video isn't available in your region."),
        ("geo restrict", "This video isn't available in your region."),
        ("geo-restrict", "This video isn't available in your region."),
        ("copyright", "This video was removed due to a copyright claim."),
        ("has been removed", "This video has been removed."),
        ("video unavailable", "This video is unavailable."),
        ("is not available", "This video is unavailable."),
        ("does not exist", "This video doesn't exist."),
        ("http error 404", "Video not found (404). Check the link."),
        ("http error 403", "Access to this video was denied (403)."),
        ("live event will begin", "This live stream hasn't started yet."),
        ("getaddrinfo failed", "Network error — check your internet connection."),
        ("name or service not known", "Network error — check your internet connection."),
        ("timed out", "The connection timed out. Please try again."),
        ("connection", "Network error — check your internet connection."),
        ("unable to download webpage", "Couldn't reach the website. Check the link and your connection."),
        ("requested format is not available", "No downloadable format was found for this video."),
    ]
    for needle, friendly in checks:
        if needle in low:
            return friendly
    # Fall back to yt-dlp's own message, trimmed.
    first_line = msg.strip().splitlines()[0] if msg.strip() else "Something went wrong."
    first_line = re.sub(r"^\[[^\]]+\]\s*[^:]*:\s*", "", first_line)
    return first_line[:300]


def format_selector() -> tuple[str, dict]:
    """Best quality, preferring MP4 so the file plays everywhere."""
    if HAS_FFMPEG:
        return (
            "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
            {"merge_output_format": "mp4"},
        )
    # Without ffmpeg we can't merge separate audio/video streams.
    return ("b[ext=mp4]/b", {})


def pick_thumbnail(info: dict) -> str | None:
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    # Prefer jpg/webp thumbnails with known width, largest under ~1280px.
    sized = [t for t in thumbs if t.get("width")]
    if sized:
        sized.sort(key=lambda t: t["width"])
        under = [t for t in sized if t["width"] <= 1280]
        return (under or sized)[-1]["url"]
    return info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)


def summarize(info: dict) -> dict:
    """Pick the fields the UI needs out of yt-dlp's huge info dict."""
    height = info.get("height")
    if not height:
        heights = [f.get("height") for f in (info.get("formats") or [])
                   if f.get("height") and f.get("vcodec") not in (None, "none")]
        height = max(heights) if heights else None

    filesize = info.get("filesize") or info.get("filesize_approx")
    if not filesize and info.get("requested_formats"):
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in info["requested_formats"]]
        if all(sizes):
            filesize = sum(sizes)

    upload_date = info.get("upload_date")  # YYYYMMDD
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "upload_date": upload_date,
        "thumbnail": pick_thumbnail(info),
        "site": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url"),
        "resolution": f"{height}p" if height else None,
        "filesize": filesize,
        "is_live": bool(info.get("is_live")),
        "description": (info.get("description") or "")[:400],
    }


# --------------------------------------------------------------------------- #
# Download jobs
# --------------------------------------------------------------------------- #

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _cleanup_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        for jid in [j for j, v in jobs.items() if v.get("finished_at", time.time()) < cutoff]:
            jobs.pop(jid, None)


def _update(job_id: str, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def run_download(job_id: str, url: str):
    job = jobs[job_id]
    parts_seen: list[str] = []
    n_streams = {"value": 1}  # set by CountStreamsPP before the download starts

    class CountStreamsPP(yt_dlp.postprocessor.PostProcessor):
        def run(self, info):
            n_streams["value"] = max(1, len(info.get("requested_formats") or []))
            _update(job_id, stage="Connecting…")
            return [], info

    def progress_hook(d):
        info = d.get("info_dict") or {}
        n_parts = n_streams["value"]
        fmt = str(info.get("format_id") or d.get("filename") or "")
        if fmt not in parts_seen:
            parts_seen.append(fmt)
        part_index = min(parts_seen.index(fmt), n_parts - 1)

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            overall = (part_index + min(frac, 1.0)) / n_parts * 100
            label = "Downloading"
            if n_parts > 1:
                kind = "video" if info.get("vcodec") not in (None, "none") else "audio"
                label = f"Downloading {kind} ({part_index + 1}/{n_parts})"
            _update(
                job_id,
                status="downloading",
                stage=label,
                # keep the bar monotonic
                percent=max(job.get("percent", 0.0), round(overall, 1)),
                downloaded_bytes=done,
                total_bytes=total,
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            overall = (part_index + 1) / n_parts * 100
            _update(job_id, percent=max(job.get("percent", 0.0), round(overall, 1)))

    def postprocessor_hook(d):
        name = d.get("postprocessor", "")
        if d["status"] == "started" and "CountStreams" not in name:
            stage = "Merging audio and video…" if "Merger" in name else "Finishing up…"
            _update(job_id, status="processing", stage=stage, percent=100.0, speed=None, eta=None)

    fmt, extra = format_selector()
    opts = base_opts() | extra | {
        "format": fmt,
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR / ".partial")},
        # Title is length-limited and sanitized by yt-dlp; the id keeps names unique.
        "outtmpl": {"default": "%(title).120B [%(id)s].%(ext)s"},
        "windowsfilenames": True,
        "trim_file_name": 180,
        "overwrites": False,
        "continuedl": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }

    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_post_processor(CountStreamsPP(), when="before_dl")
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist":
                raise UserError("Playlists aren't supported — paste a link to a single video.")
            downloads = info.get("requested_downloads") or []
            filepath = downloads[0].get("filepath") if downloads else None
            if not filepath:
                filepath = ydl.prepare_filename(info)

        path = Path(filepath).resolve()
        if not is_inside(path, DOWNLOAD_DIR) or not path.exists():
            raise UserError("The download finished but the file could not be located.")

        _update(
            job_id,
            status="finished",
            stage="Download complete",
            percent=100.0,
            speed=None,
            eta=None,
            filename=path.name,
            filepath=str(path),
            total_bytes=path.stat().st_size,
            finished_at=time.time(),
        )
    except UserError as e:
        _update(job_id, status="error", error=e.message, finished_at=time.time())
    except (DownloadError, ExtractorError) as e:
        _update(job_id, status="error", error=friendly_error(e), finished_at=time.time())
    except Exception as e:  # noqa: BLE001 — never let a thread die silently
        app.logger.exception("Download failed")
        _update(job_id, status="error", error=friendly_error(e), finished_at=time.time())
    finally:
        with jobs_lock:
            busy = any(j["status"] in ("queued", "downloading", "processing") for j in jobs.values())
        if not busy:
            try:  # remove the temp folder if nothing is left in it
                (DOWNLOAD_DIR / ".partial").rmdir()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.errorhandler(UserError)
def _user_error(e: UserError):
    return jsonify(error=e.message), e.status


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)  # safe_join blocks traversal


@app.get("/api/health")
def health():
    return jsonify(ok=True, ffmpeg=HAS_FFMPEG, yt_dlp=yt_dlp.version.__version__,
                   js_runtimes=sorted(JS_RUNTIMES), bundled=BIN_DIR.is_dir(),
                   download_dir=str(DOWNLOAD_DIR))


@app.post("/api/info")
def api_info():
    url = get_json_url()
    try:
        with yt_dlp.YoutubeDL(base_opts() | {"skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as e:
        raise UserError(friendly_error(e), 422)
    except Exception as e:  # noqa: BLE001
        app.logger.exception("Info extraction failed")
        raise UserError(friendly_error(e), 500)

    if not info:
        raise UserError("No video was found at that link.", 422)
    if info.get("_type") == "playlist":
        raise UserError("That's a playlist — paste a link to a single video.", 422)
    if info.get("is_live"):
        raise UserError("Live streams can't be downloaded while they're live.", 422)
    if not info.get("formats") and not info.get("url"):
        raise UserError("No downloadable video was found at that link.", 422)

    return jsonify(video=summarize(info), url=info.get("webpage_url") or url)


@app.post("/api/download")
def api_download():
    url = get_json_url()
    _cleanup_jobs()
    with jobs_lock:
        active = [j for j in jobs.values() if j["status"] in ("queued", "downloading", "processing")]
        if len(active) >= 3:
            raise UserError("Too many downloads running — wait for one to finish.", 429)
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Preparing download…", "percent": 0.0}
    threading.Thread(target=run_download, args=(job_id, url), daemon=True).start()
    return jsonify(job_id=job_id), 202


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise UserError("Download not found.", 404)
        public = {k: v for k, v in job.items() if k != "filepath"}
    public["folder"] = str(DOWNLOAD_DIR)
    return jsonify(public)


@app.post("/api/open/<job_id>")
def api_open(job_id):
    # The path comes from our own job record, never from the request.
    with jobs_lock:
        job = jobs.get(job_id)
        filepath = job.get("filepath") if job else None
    if not filepath:
        raise UserError("That file isn't available.", 404)
    path = Path(filepath).resolve()
    if not is_inside(path, DOWNLOAD_DIR) or not path.exists():
        raise UserError("The file was moved or deleted.", 404)

    system = platform.system()
    try:
        if system == "Windows":
            # explorer.exe mis-parses a fully quoted "/select,<path>" (which is what
            # Popen produces for a list arg with spaces) and opens Documents instead,
            # so build the command line ourselves. The path is server-generated and
            # sanitized by yt-dlp, but refuse quotes anyway.
            if '"' in str(path):
                raise UserError("Couldn't open the file location.", 500)
            explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
            subprocess.Popen(f'"{explorer}" /select,"{path}"')
        elif system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError:
        raise UserError("Couldn't open the file manager.", 500)
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #

def _pick_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _serve():
    from waitress import serve
    serve(app, host=HOST, port=PORT, threads=8, _quiet=True)


def _wait_until_up(timeout=15.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    """
    Modes:
      (default)   open a native desktop window (WebView2), fall back to browser
      --browser   run the server and open the default browser
      --server    run the server only (headless; used for testing)
      --dev       Flask debug server
    """
    global PORT
    if PORT == 0:
        PORT = _pick_port() if "--dev" not in sys.argv else 5000
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PORT}"

    if sys.stdout:  # no console in the windowed build
        print(f"\n  {APP_NAME} is running at {url}")
        print(f"  Downloads are saved to {DOWNLOAD_DIR}")
        print(f"  ffmpeg: {FFMPEG_PATH or 'NOT found (single-file formats only)'}")
        print(f"  JS runtimes: {', '.join(JS_RUNTIMES) or 'none (some YouTube videos may fail)'}\n",
              flush=True)

    if "--dev" in sys.argv:
        app.run(host=HOST, port=PORT, debug=True)
        return
    if "--server" in sys.argv:
        _serve()
        return

    threading.Thread(target=_serve, daemon=True).start()
    _wait_until_up()

    if "--browser" not in sys.argv:
        try:
            import webview  # pywebview -> Microsoft Edge WebView2 on Windows
            webview.create_window(APP_NAME, url, width=900, height=860,
                                  min_size=(420, 600), background_color="#0a0b10")
            webview.start(private_mode=False)
            return  # window closed -> exit (daemon server thread dies with us)
        except Exception:  # noqa: BLE001 — WebView2 missing etc.
            pass

    import webbrowser
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
