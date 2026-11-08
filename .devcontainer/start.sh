#!/usr/bin/env bash
# Starts the web version in the background whenever the Codespace starts.
cd "$(dirname "$0")/.." || exit 1

# Sites (YouTube especially) change often; a fresh yt-dlp fixes most failures.
pip install --user -q -U "yt-dlp[default]" || true

pkill -f "app.py --web" 2>/dev/null || true
setsid nohup python app.py --web > /tmp/video-downloader.log 2>&1 < /dev/null &
sleep 2
cat /tmp/video-downloader.log
