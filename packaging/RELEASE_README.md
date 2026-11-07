# Video Downloader 1.0.0

Paste a video link, check the preview, confirm, and download. It runs on Windows 10 and 11 (64-bit), and everything it needs comes with it. You don't need to install Python, Node.js, yt-dlp or FFmpeg.

## What's in this folder

| File | What it is |
|---|---|
| `VideoDownloader_Setup.exe` | **Installer.** Send this file to anyone who wants the app. |
| `VideoDownloader_Portable.exe` | Portable version. Runs without installing; you can keep it on a USB stick. |
| `source/` | The full source code and build scripts. |

## Install

1. Double-click **`VideoDownloader_Setup.exe`**.
2. Pick **Install for me only** (no admin rights needed) or **Install for all users**.
3. Follow the wizard. You can change the install folder and choose whether to add a desktop shortcut.
4. Leave **Launch Video Downloader** ticked and click **Finish**.

The installer always adds a Start Menu shortcut.

> Windows SmartScreen may say *"Windows protected your PC"* because the installer isn't code-signed. Click **More info → Run anyway**.

## Use

1. Paste a link, for example from YouTube or Vimeo, and click **Fetch Video**.
2. Check the preview: thumbnail, title, channel, duration, quality and size.
3. Click **Continue**, then **Confirm & Download**. Nothing downloads until you confirm.
4. Watch the progress bar. When the download finishes, click **Show in folder**.

Videos are saved to **`Downloads\Video Downloader`** as MP4 files at the best available quality.

Only download videos you have the right to save. The app does not get around DRM, paywalls, private videos or sign-in requirements.

## Portable version

Double-click `VideoDownloader_Portable.exe`. Each launch takes a few extra seconds because the app unpacks itself first. It saves videos to the same `Downloads\Video Downloader` folder.

## Uninstall

Go to **Settings → Apps → Installed apps → Video Downloader → Uninstall**. Your downloaded videos are **not** deleted.

## Troubleshooting

- **Black or empty window:** the app needs the Microsoft Edge WebView2 Runtime. Windows 11 already has it, and the installer adds it to Windows 10 when it's missing. If it still can't load, the app opens in your web browser instead.
- **A site stops working:** sites change often. Updated releases include a newer yt-dlp.
- **Log file:** `%LOCALAPPDATA%\Video Downloader\app.log`
