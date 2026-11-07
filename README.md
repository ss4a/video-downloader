# Video Downloader: developer guide

This is a small desktop app for downloading videos. The backend is Python (Flask + [yt-dlp](https://github.com/yt-dlp/yt-dlp)). The UI is plain HTML/CSS/JS, shown in a native window through pywebview (Edge WebView2).

```
app.py                 Flask backend: URL validation, metadata, download jobs, progress, "show in folder"
static/                index.html, app.css, app.js: the UI (link → preview → confirm → download)
tests/e2e_test.py      End-to-end API test against a running instance
packaging/             PyInstaller spec, Inno Setup script, icon generator, notices
build.ps1              One-command Windows release build
```

## Run from source

Requires Python 3.10+. FFmpeg and Deno are optional when running from source: the app uses them if they're on PATH, and they're bundled automatically in release builds.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python app.py            # native window
.\.venv\Scripts\python app.py --browser  # open in the default browser instead
.\.venv\Scripts\python app.py --server   # headless (set VD_PORT to fix the port)
```

Environment variables:
- `VD_PORT`: the port to use. By default the app picks a free port.
- `VD_DOWNLOAD_DIR`: the download folder. Default: `~/Downloads/Video Downloader`.

## Test

```powershell
$env:VD_PORT=5077; .\.venv\Scripts\python app.py --server   # terminal 1
.\.venv\Scripts\python tests\e2e_test.py 5077                # terminal 2
```

## Build the Windows release

You need Python and Inno Setup 6 (`winget install JRSoftware.InnoSetup`). Then run:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

The script downloads FFmpeg (yt-dlp's build) and Deno and checks their SHA-256 checksums. It then builds with PyInstaller and compiles the installer. Output goes to `Release\`.

## Security notes

- The server binds to `127.0.0.1` only. It rejects requests with a non-localhost `Host` header, which protects against DNS rebinding. It also requires JSON bodies, so other sites can't send it plain form posts.
- URLs must be http(s). Local and private-network addresses are refused.
- yt-dlp builds file names from a length-limited, sanitised template. Every file stays inside the download folder. "Show in folder" uses only the path the server recorded itself and never a path sent by the client.
- DRM-protected formats are never requested. Paywalled, private and sign-in-only videos fail with a clear message.
