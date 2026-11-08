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

## Use it from your phone (web version)

`python app.py --web` serves the same app to a browser on another device. Instead of saving to a folder, the finished file is sent to the phone with a **Save to device** button. Files left on the server are deleted after an hour. The folder picker, "Show in folder" and the in-app updater are desktop-only and switched off here.

### Option A: GitHub Codespaces (runs on GitHub)

1. On github.com (phone or PC), open this repo, tap **Code → Codespaces → Create codespace on main**.
2. Wait for setup to finish (a few minutes the first time). The app starts by itself on port 8080.
3. Open the **Ports** tab and open the address for port 8080 (it looks like `https://<name>-8080.app.github.dev`). Bookmark it.

The port is private by default, so only you can open it while signed in to GitHub, and no extra password is needed. Don't change the port's visibility to Public. The Codespace stops after about 30 minutes of inactivity. To use it again, open github.com/codespaces and start it, then reopen your bookmark. Free accounts include roughly 60 hours a month.

YouTube sometimes blocks cloud servers ("Sign in to confirm you're not a bot"). If that happens, use option B.

### Option B: your Windows PC

Your phone and PC must be on the same Wi-Fi. Pick a password, then:

```powershell
$env:VD_PASSWORD="choose-a-password"; .\.venv\Scripts\python app.py --web
```

It prints an address like `http://192.168.1.20:8080/`. Open that on your phone and sign in. Allow Python through Windows Firewall (private networks) when asked. To use it away from home, install [Tailscale](https://tailscale.com/) on both devices and use the PC's Tailscale address instead.

Web-mode settings: `VD_PASSWORD` (required except in Codespaces), `VD_PORT` (default 8080), `VD_DOWNLOAD_DIR` (default `~/vd-downloads`).

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

- Web mode (`--web`) listens on the network instead of `127.0.0.1`. Outside Codespaces it refuses to start without `VD_PASSWORD`. Sign-in sets an HttpOnly, SameSite=Strict cookie, and 10 wrong passwords lock sign-in for 10 minutes.

- The desktop app binds to `127.0.0.1` only. It rejects requests with a non-localhost `Host` header, which protects against DNS rebinding. It also requires JSON bodies, so other sites can't send it plain form posts.
- URLs must be http(s). Local and private-network addresses are refused.
- yt-dlp builds file names from a length-limited, sanitised template. Every file stays inside the download folder. "Show in folder" uses only the path the server recorded itself and never a path sent by the client.
- DRM-protected formats are never requested. Paywalled, private and sign-in-only videos fail with a clear message.
