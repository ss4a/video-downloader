"""
Video Downloader — a small local video downloader powered by yt-dlp.

The Flask backend exposes a small JSON API that the UI talks to:

    POST /api/info              {url}                     -> metadata + quality options (no download)
    POST /api/download          {url, kind, quality}      -> start a download job -> {job_id}
    GET  /api/progress/<id>                               -> job status / progress
    POST /api/cancel/<id>                                 -> cancel a running download
    POST /api/open/<id>                                   -> reveal the finished file in Explorer
    GET  /api/settings          POST {download_dir}       -> read / change the save folder
    POST /api/choose-folder                               -> native folder picker (app window only)
    GET  /api/update                                      -> is a newer release available on GitHub?
    POST /api/update/install    GET /api/update/status    -> download + run the new installer

The server only listens on 127.0.0.1 and rejects requests whose Host header is
not localhost, so other websites / machines cannot drive it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_from_directory
import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

APP_NAME = "Video Downloader"
APP_VERSION = "1.1.0"
# For testing the updater only: pretend to be an older version.
CURRENT_VERSION = os.environ.get("VD_VERSION_OVERRIDE") or APP_VERSION
GITHUB_REPO = "ss4a/video-downloader"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
SETUP_ASSET = "VideoDownloader_Setup.exe"

HOST = "127.0.0.1"
PORT = int(os.environ.get("VD_PORT", "0"))  # 0 = pick a free port at startup
DEFAULT_DOWNLOAD_DIR = Path(
    os.environ.get("VD_DOWNLOAD_DIR", Path.home() / "Downloads" / APP_NAME)
).expanduser().resolve()
DATA_DIR = Path(os.environ.get("VD_DATA_DIR")
                or Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME)
SETTINGS_FILE = DATA_DIR / "settings.json"
MAX_URL_LENGTH = 2048
JOB_TTL_SECONDS = 60 * 60  # forget finished jobs after an hour
MP3_KBPS = 192

FROZEN = getattr(sys, "frozen", False)
# PyInstaller unpacks data files to sys._MEIPASS; in source mode use this folder.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
STATIC_DIR = RESOURCE_DIR / "static"
BIN_DIR = RESOURCE_DIR / "bin"  # bundled ffmpeg / deno in packaged builds
# Installed copies have an Inno Setup uninstaller next to the exe; the portable
# build and source runs don't, so they can't self-update.
EXE_DIR = Path(sys.executable).resolve().parent
IS_INSTALLED = FROZEN and any(EXE_DIR.glob("unins*.exe"))


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _h = RotatingFileHandler(DATA_DIR / "app.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
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


def json_body() -> dict:
    # Requiring a JSON body means cross-site pages can't submit this via a
    # plain form; they'd need a CORS preflight, which we never grant.
    if not request.is_json:
        raise UserError("Expected a JSON request.", 415)
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Settings (save folder)
# --------------------------------------------------------------------------- #

settings_lock = threading.Lock()


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(SETTINGS_FILE)


def get_download_dir() -> Path:
    custom = _load_settings().get("download_dir")
    if isinstance(custom, str) and custom:
        p = Path(custom)
        if p.is_absolute() and p.is_dir():
            return p.resolve()
    return DEFAULT_DOWNLOAD_DIR


def validate_folder(raw) -> Path:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1000:
        raise UserError("Please choose a folder.")
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        raise UserError("Please choose a full folder path.")
    p = p.resolve()
    if p.exists() and not p.is_dir():
        raise UserError("That's a file, not a folder.")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f".vd-write-test-{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        raise UserError("Can't save files in that folder. Choose another one.")
    return p


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
        ("sign in", "This video requires signing in, so it can't be downloaded here."),
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
        ("no space left", "Your disk is full. Free up some space and try again."),
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


def pick_thumbnail(info: dict) -> str | None:
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    # Prefer thumbnails with known width, largest under ~1280px.
    sized = [t for t in thumbs if t.get("width")]
    if sized:
        sized.sort(key=lambda t: t["width"])
        under = [t for t in sized if t["width"] <= 1280]
        return (under or sized)[-1]["url"]
    return info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)


def _fsize(f: dict, duration) -> float | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = f["tbr"] * 1000 / 8 * duration
    return size or None


def quality_label(h: int) -> str:
    return {4320: "8K", 2160: "4K"}.get(h, f"{h}p")


def media_options(info: dict) -> dict:
    """The video resolutions this video actually has, with size estimates."""
    duration = info.get("duration")
    formats = info.get("formats") or []
    has_video = lambda f: f.get("vcodec") not in (None, "none") and f.get("height")
    has_audio = lambda f: f.get("acodec") not in (None, "none")

    audio_only = [f for f in formats if has_audio(f) and not has_video(f)]
    # Mirrors the download preference: m4a first, then highest bitrate.
    audio_only.sort(key=lambda f: (f.get("ext") == "m4a", f.get("abr") or f.get("tbr") or 0))
    best_audio_size = _fsize(audio_only[-1], duration) if audio_only else None

    by_height: dict[int, list] = {}
    for f in formats:
        if has_video(f) and f["height"] >= 144:
            by_height.setdefault(int(f["height"]), []).append(f)

    def heuristic_size(h):
        cands = sorted(by_height[h], key=lambda f: (f.get("ext") == "mp4", f.get("tbr") or 0))
        best = cands[-1]
        size = _fsize(best, duration)
        if size and not has_audio(best) and best_audio_size:
            size += best_audio_size
        return size

    def selected_size(h):
        """Ask yt-dlp's own format selector what it would download for this
        height, so the estimate matches the real download."""
        from yt_dlp.utils import FormatSorter
        with yt_dlp.YoutubeDL(base_opts() | download_opts("video", h)) as y:
            fmts = [dict(f) for f in formats]
            fmts.sort(key=FormatSorter(y, info.get("_format_sort_fields") or []).calculate_preference)
            chosen = y._select_formats(fmts, y.build_format_selector(y.params["format"]))
        if not chosen:
            return None
        parts = chosen[0].get("requested_formats") or [chosen[0]]
        sizes = [_fsize(p, duration) for p in parts]
        return sum(sizes) if all(sizes) else None

    video = []
    for h in sorted(by_height, reverse=True):
        try:
            size = selected_size(h)
        except Exception:  # noqa: BLE001 — internal API; fall back to a heuristic
            size = heuristic_size(h)
        video.append({"height": h, "label": quality_label(h), "size": size})

    if not video and (info.get("height") or info.get("url")):
        video.append({"height": int(info.get("height") or 0), "label": "Best available",
                      "size": info.get("filesize") or info.get("filesize_approx")})

    return {
        "video": video,
        "audio": {
            "label": f"MP3 · {MP3_KBPS} kbps",
            "size": (MP3_KBPS * 1000 / 8 * duration) if duration else None,
        },
    }


def summarize(info: dict) -> dict:
    """Pick the fields the UI needs out of yt-dlp's huge info dict."""
    options = media_options(info)
    top = options["video"][0] if options["video"] else {}

    upload_date = info.get("upload_date")  # YYYYMMDD
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "upload_date": upload_date,
        "thumbnail": pick_thumbnail(info),
        "site": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url"),
        "resolution": top.get("label") if top.get("height") else None,
        "filesize": top.get("size"),
        "description": (info.get("description") or "")[:400],
        "options": options,
    }


def download_opts(kind: str, height: int | None) -> dict:
    if kind == "audio":
        if not HAS_FFMPEG:
            return {"format": "ba[ext=m4a]/ba/b"}
        return {
            "format": "ba/b",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": str(MP3_KBPS)},
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        }
    if not HAS_FFMPEG:  # can't merge separate streams; take the best single file
        return {"format": f"b[height<={height}]/b" if height else "b"}
    # Highest resolution up to the chosen height, preferring MP4/M4A so the
    # merged file plays everywhere.
    return {
        "format": "bv*+ba/b",
        "format_sort": [f"res:{height}" if height else "res", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
    }


# --------------------------------------------------------------------------- #
# Download jobs
# --------------------------------------------------------------------------- #

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
ACTIVE = ("queued", "downloading", "processing")


def _cleanup_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        for jid in [j for j, v in jobs.items() if v.get("finished_at", time.time()) < cutoff]:
            jobs.pop(jid, None)


def _update(job_id: str, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def _active_downloads() -> int:
    with jobs_lock:
        return sum(1 for j in jobs.values() if j["status"] in ACTIVE)


def run_download(job_id: str, url: str, kind: str, height: int | None,
                 cancel: threading.Event, dl_dir: Path):
    job = jobs[job_id]
    parts_seen: list[str] = []
    n_streams = {"value": 1}  # set by CountStreamsPP before the download starts
    existed_before = {"value": False}
    temp_dir = dl_dir / ".partial" / job_id

    class CountStreamsPP(yt_dlp.postprocessor.PostProcessor):
        def run(self, info):
            if cancel.is_set():
                raise DownloadCancelled()
            n_streams["value"] = max(1, len(info.get("requested_formats") or []))
            # yt-dlp won't overwrite: note whether the final file is already there.
            target = Path(self._downloader.prepare_filename(info))
            if kind == "audio" and HAS_FFMPEG:
                target = target.with_suffix(".mp3")
            existed_before["value"] = target.exists()
            _update(job_id, stage="Connecting…")
            return [], info

    def progress_hook(d):
        if cancel.is_set():
            raise DownloadCancelled()
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
            label = "Downloading audio" if kind == "audio" else "Downloading"
            if n_parts > 1:
                part = "video" if info.get("vcodec") not in (None, "none") else "audio"
                label = f"Downloading {part} ({part_index + 1}/{n_parts})"
            _update(
                job_id,
                status="downloading",
                stage=label,
                percent=max(job.get("percent", 0.0), round(overall, 1)),  # keep it monotonic
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
            stage = {
                "Merger": "Merging audio and video…",
                "ExtractAudio": "Converting to MP3…",
                "EmbedThumbnail": "Adding cover art…",
                "Metadata": "Adding title and artist…",
            }.get(name, "Finishing up…")
            _update(job_id, status="processing", stage=stage, percent=100.0, speed=None, eta=None)

    # Title is length-limited and sanitized by yt-dlp; the id (and, for video,
    # the resolution) keeps names unique so a 720p copy never stands in for 1080p.
    outtmpl = ("%(title).120B [%(id)s].%(ext)s" if kind == "audio"
               else "%(title).120B [%(id)s]%(height& {}p|)s.%(ext)s")
    opts = base_opts() | download_opts(kind, height) | {
        "paths": {"home": str(dl_dir), "temp": str(temp_dir)},
        "outtmpl": {"default": outtmpl},
        "windowsfilenames": True,
        "trim_file_name": 180,
        "overwrites": False,
        "continuedl": False,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }

    try:
        dl_dir.mkdir(parents=True, exist_ok=True)
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
        if not is_inside(path, dl_dir) or not path.exists():
            raise UserError("The download finished but the file could not be located.")

        existed = existed_before["value"]
        _update(
            job_id,
            status="finished",
            already_existed=existed,
            stage="Already in your folder" if existed else "Download complete",
            percent=100.0,
            speed=None,
            eta=None,
            filename=path.name,
            filepath=str(path),
            height=info.get("height") if kind == "video" else None,
            total_bytes=path.stat().st_size,
            finished_at=time.time(),
        )
    except Exception as e:  # noqa: BLE001 — never let a thread die silently
        if cancel.is_set() or isinstance(e, DownloadCancelled):
            _update(job_id, status="cancelled", stage="Download cancelled", finished_at=time.time())
        elif isinstance(e, UserError):
            _update(job_id, status="error", error=e.message, finished_at=time.time())
        else:
            if not isinstance(e, (DownloadError, ExtractorError)):
                app.logger.exception("Download failed")
            _update(job_id, status="error", error=friendly_error(e), finished_at=time.time())
    finally:
        # Partial files live in a per-job temp folder; remove whatever is left.
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            (dl_dir / ".partial").rmdir()  # only succeeds when empty
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Self-update (installed copies only)
# --------------------------------------------------------------------------- #

def _version_tuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def _https_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


_update_cache: dict = {"at": 0.0, "data": None}
_update_lock = threading.Lock()
update_state: dict = {"status": "idle"}


def check_for_update(force=False) -> dict:
    with _update_lock:
        if not force and _update_cache["data"] and time.time() - _update_cache["at"] < 6 * 3600:
            return _update_cache["data"]
    base = {"current": CURRENT_VERSION, "available": False, "page_url": RELEASES_PAGE,
            "can_install": IS_INSTALLED}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"VideoDownloader/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=8, context=_https_context()) as r:
            rel = json.loads(r.read().decode("utf-8"))
        tag = str(rel.get("tag_name") or "")
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == SETUP_ASSET), None)
        data = base | {
            "latest": tag.lstrip("vV"),
            "available": bool(tag) and _version_tuple(tag) > _version_tuple(CURRENT_VERSION),
            "notes": (rel.get("body") or "")[:2000],
            "size": asset.get("size") if asset else None,
            "page_url": rel.get("html_url") or RELEASES_PAGE,
        }
        # Only ever download the installer from this repo's own release assets.
        data["_tag"] = tag
        data["_asset_id"] = asset.get("id") if asset and isinstance(asset.get("id"), int) else None
        data["_asset_ok"] = bool(asset and re.fullmatch(r"v?\d+(\.\d+){0,3}", tag))
        data["can_install"] = IS_INSTALLED and data["_asset_ok"]
    except Exception as e:  # noqa: BLE001 — offline etc.: just no banner
        app.logger.info("Update check failed: %s", e)
        data = base | {"error": "Couldn't check for updates."}
    with _update_lock:
        _update_cache.update(at=time.time(), data=data)
    return data


def _run_update(info: dict):
    tag = info["_tag"]
    expected = info.get("size")
    dest = Path(tempfile.gettempdir()) / f"VideoDownloader_Setup_{tag}.exe"
    # Two routes to the same release asset of this repo: the API endpoint first
    # (it stayed up during github.com download outages), then the public link.
    sources = []
    if info.get("_asset_id"):
        sources.append((f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{info['_asset_id']}",
                        {"Accept": "application/octet-stream"}))
    sources.append((f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{SETUP_ASSET}", {}))

    def fetch(url, headers):
        req = urllib.request.Request(url, headers={"User-Agent": f"VideoDownloader/{APP_VERSION}", **headers})
        with urllib.request.urlopen(req, timeout=30, context=_https_context()) as r, \
                open(dest, "wb") as out:
            total = int(r.headers.get("Content-Length") or expected or 0)
            done = 0
            while chunk := r.read(1 << 16):
                out.write(chunk)
                done += len(chunk)
                if total:
                    update_state["percent"] = round(done / total * 100, 1)

    try:
        update_state.update(status="downloading", percent=0.0, error=None)
        for i, (url, headers) in enumerate(sources):
            try:
                fetch(url, headers)
                break
            except Exception as e:  # noqa: BLE001
                app.logger.info("Update download via %s failed: %s", url, e)
                update_state["percent"] = 0.0
                if i == len(sources) - 1:
                    raise
        if expected and dest.stat().st_size != expected:
            raise RuntimeError("size mismatch")
        with open(dest, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("not an executable")

        update_state.update(status="installing", percent=100.0)
        local = Path(os.environ.get("LOCALAPPDATA", "")).resolve()
        scope = "/CURRENTUSER" if local.parts and is_inside(EXE_DIR, local) else "/ALLUSERS"
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(dest), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/CLOSEAPPLICATIONS", "/RELAUNCH=1", scope],
                         creationflags=flags, close_fds=True)
        # Give the UI a moment to show "Installing…", then get out of the way.
        threading.Timer(1.5, lambda: os._exit(0)).start()
    except Exception as e:  # noqa: BLE001
        app.logger.exception("Update failed")
        dest.unlink(missing_ok=True)
        update_state.update(status="error", error="The update couldn't be downloaded. Try again later.")


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
    return jsonify(ok=True, version=APP_VERSION, ffmpeg=HAS_FFMPEG,
                   yt_dlp=yt_dlp.version.__version__, js_runtimes=sorted(JS_RUNTIMES),
                   bundled=BIN_DIR.is_dir(), installed=IS_INSTALLED,
                   download_dir=str(get_download_dir()))


@app.post("/api/info")
def api_info():
    url = validate_url(json_body().get("url"))
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
    body = json_body()
    url = validate_url(body.get("url"))
    kind = body.get("kind", "video")
    if kind not in ("video", "audio"):
        raise UserError("Unknown download type.")
    quality = body.get("quality", "best")
    height = None
    if kind == "video" and quality not in (None, "best"):
        if not isinstance(quality, int) or isinstance(quality, bool) or not 100 <= quality <= 10000:
            raise UserError("Unknown quality.")
        height = quality

    _cleanup_jobs()
    dl_dir = get_download_dir()
    cancel = threading.Event()
    with jobs_lock:
        if sum(1 for j in jobs.values() if j["status"] in ACTIVE) >= 3:
            raise UserError("Too many downloads running — wait for one to finish.", 429)
        if update_state.get("status") in ("downloading", "installing"):
            raise UserError("An update is being installed.", 409)
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"id": job_id, "status": "queued", "stage": "Preparing download…",
                        "percent": 0.0, "kind": kind, "folder": str(dl_dir), "_cancel": cancel}
    threading.Thread(target=run_download, args=(job_id, url, kind, height, cancel, dl_dir),
                     daemon=True).start()
    return jsonify(job_id=job_id), 202


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise UserError("Download not found.", 404)
        return jsonify({k: v for k, v in job.items() if k != "filepath" and not k.startswith("_")})


@app.post("/api/cancel/<job_id>")
def api_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise UserError("Download not found.", 404)
        if job["status"] in ACTIVE:
            job["_cancel"].set()
            job["stage"] = "Cancelling…"
    return jsonify(ok=True)


@app.post("/api/open/<job_id>")
def api_open(job_id):
    # The path comes from our own job record, never from the request.
    with jobs_lock:
        job = jobs.get(job_id)
        filepath = job.get("filepath") if job else None
        folder = Path(job["folder"]) if job else None
    if not filepath:
        raise UserError("That file isn't available.", 404)
    path = Path(filepath).resolve()
    if not is_inside(path, folder) or not path.exists():
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


@app.get("/api/settings")
def api_settings_get():
    return jsonify(download_dir=str(get_download_dir()), default_download_dir=str(DEFAULT_DOWNLOAD_DIR))


def _set_download_dir(raw) -> Path:
    with settings_lock:
        data = _load_settings()
        if raw is None:
            data.pop("download_dir", None)
            path = DEFAULT_DOWNLOAD_DIR
        else:
            path = validate_folder(raw)
            data["download_dir"] = str(path)
        try:
            _save_settings(data)
        except OSError:
            raise UserError("Couldn't save your settings.", 500)
    return path


@app.post("/api/settings")
def api_settings_set():
    body = json_body()
    if "download_dir" not in body:
        raise UserError("Nothing to change.")
    path = _set_download_dir(body["download_dir"])
    return jsonify(download_dir=str(path))


@app.post("/api/choose-folder")
def api_choose_folder():
    json_body()
    try:
        import webview
        window = webview.windows[0] if webview.windows else None
    except Exception:  # noqa: BLE001
        window = None
    if window is None:
        raise UserError("The folder picker is only available in the app window.", 501)
    result = window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(get_download_dir()))
    if not result:
        return jsonify(cancelled=True, download_dir=str(get_download_dir()))
    chosen = result[0] if isinstance(result, (list, tuple)) else result
    path = _set_download_dir(str(chosen))
    return jsonify(download_dir=str(path))


@app.get("/api/update")
def api_update():
    data = check_for_update(force=request.args.get("force") == "1")
    return jsonify({k: v for k, v in data.items() if not k.startswith("_")})


@app.post("/api/update/install")
def api_update_install():
    json_body()
    info = check_for_update()
    if not info.get("available"):
        raise UserError("You already have the latest version.", 409)
    if not info.get("can_install"):
        raise UserError("This copy can't update itself. Download the new version instead.", 409)
    if _active_downloads():
        raise UserError("Wait for your download to finish before updating.", 409)
    with _update_lock:
        if update_state.get("status") in ("downloading", "installing"):
            return jsonify(ok=True)
        update_state.update(status="downloading", percent=0.0, error=None)
    threading.Thread(target=_run_update, args=(info,), daemon=True).start()
    return jsonify(ok=True), 202


@app.get("/api/update/status")
def api_update_status():
    return jsonify(update_state)


@app.post("/api/open-releases")
def api_open_releases():
    json_body()
    import webbrowser
    webbrowser.open(RELEASES_PAGE)  # fixed URL, never from the request
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
    url = f"http://{HOST}:{PORT}"

    if sys.stdout:  # no console in the windowed build
        print(f"\n  {APP_NAME} {APP_VERSION} is running at {url}")
        print(f"  Downloads are saved to {get_download_dir()}")
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
            webview.create_window(f"{APP_NAME}", url, width=900, height=900,
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
