"""
End-to-end test against a running Video Downloader backend.

    python tests/e2e_test.py <port> [video_url]

Checks validation/error handling, metadata + quality options, a video download
at a chosen quality, an MP3 download (with tags + cover art), cancelling a
download, changing the save folder, and the update check.
"""
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1])
VIDEO = sys.argv[2] if len(sys.argv) > 2 else "https://www.youtube.com/watch?v=jNQXAC9IVRw"
LONG_VIDEO = "https://www.youtube.com/watch?v=YE7VzlLtp-4"  # Big Buck Bunny, for cancel test
BASE = f"http://127.0.0.1:{PORT}"


def call(path, body=None):
    req = urllib.request.Request(BASE + path)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    failures += 0 if cond else 1


def wait_job(job_id, stop_at=None, quiet=False):
    seen, job = [], {}
    for _ in range(1200):
        _, job = call(f"/api/progress/{job_id}")
        key = (job.get("stage"), int(job.get("percent") or 0) // 25 * 25)
        if key not in seen and not quiet:
            seen.append(key)
            print(f"     {job.get('status'):<11} {job.get('percent', 0):5.1f}%  {job.get('stage')}")
        if job.get("status") in ("finished", "error", "cancelled"):
            break
        if stop_at and stop_at(job):
            break
        time.sleep(0.4)
    return job


print("Health")
s, h = call("/api/health")
check("health ok", s == 200 and h.get("ok"), json.dumps(h))
check("ffmpeg available", h.get("ffmpeg") is True)

print("Validation & errors")
for url, expect in [
    ("", "paste"), ("notaurl", "http"), ("javascript:alert(1)", "http"),
    ("http://127.0.0.1/x", "Local"), ("https://example.com/", "supported"),
    ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "unavailable"),
]:
    s, r = call("/api/info", {"url": url})
    check(f"rejects {url!r}", s >= 400 and expect.lower() in r.get("error", "").lower(), f"-> {s} {r.get('error')}")
for body, expect in [({"url": VIDEO, "kind": "exe"}, "type"), ({"url": VIDEO, "quality": "9999p"}, "quality"),
                     ({"url": VIDEO, "quality": 5}, "quality")]:
    s, r = call("/api/download", body)
    check(f"rejects download {body}", s == 400 and expect in r.get("error", "").lower(), f"-> {r.get('error')}")

print("Metadata & quality options")
t = time.time()
s, r = call("/api/info", {"url": VIDEO})
v = r.get("video", {})
url = r.get("url", VIDEO)
opts = v.get("options", {})
heights = [o["height"] for o in opts.get("video", [])]
check("info fetched", s == 200 and v.get("title"), f"({time.time() - t:.1f}s) {v.get('title')!r}")
check("video qualities listed", len(heights) >= 2, f"{[o['label'] for o in opts.get('video', [])]}")
check("sizes estimated", all(o.get("size") for o in opts.get("video", [])))
check("mp3 option", "MP3" in opts.get("audio", {}).get("label", ""), opts.get("audio"))

print("Save folder")
_, before = call("/api/settings")
new_dir = Path(tempfile.gettempdir()) / f"vd-e2e-folder-{int(time.time())}"
s, r = call("/api/settings", {"download_dir": str(new_dir)})
check("change folder", s == 200 and Path(r.get("download_dir", "")) == new_dir.resolve() and new_dir.is_dir(), r)
s, r = call("/api/settings", {"download_dir": "relative/path"})
check("rejects relative folder", s == 400, r.get("error"))
s, r = call("/api/settings", {"download_dir": __file__})
check("rejects a file as folder", s == 400, r.get("error"))

print(f"Video download at lowest quality ({min(heights) if heights else '?'}p)")
s, r = call("/api/download", {"url": url, "kind": "video", "quality": min(heights)})
job = wait_job(r.get("job_id"))
check("finished", job.get("status") == "finished", job.get("error") or job.get("filename", ""))
check("chosen resolution respected", job.get("height") == min(heights), f"got {job.get('height')}p")
check("saved in new folder", (new_dir / job.get("filename", "?")).is_file(), job.get("folder"))
check("filepath not leaked", "filepath" not in job and "_cancel" not in job)

first_name = job.get("filename")
check("quality in filename", f"{min(heights)}p" in (first_name or ""), first_name)
s, r = call("/api/download", {"url": url, "kind": "video", "quality": min(heights)})
job = wait_job(r.get("job_id"), quiet=True)
check("same file again -> 'already in folder'", job.get("already_existed") is True and job.get("filename") == first_name,
      job.get("stage"))
s, r = call("/api/download", {"url": url, "kind": "video", "quality": max(heights)})
job = wait_job(r.get("job_id"), quiet=True)
check("different quality -> separate file", job.get("status") == "finished" and job.get("filename") != first_name
      and not job.get("already_existed") and job.get("height") == max(heights), job.get("filename"))

print("MP3 download")
s, r = call("/api/download", {"url": url, "kind": "audio"})
job = wait_job(r.get("job_id"))
check("finished", job.get("status") == "finished", job.get("error") or job.get("filename", ""))
mp3 = new_dir / job.get("filename", "?")
check("is .mp3", mp3.suffix == ".mp3" and mp3.is_file(), mp3.name)
try:
    from mutagen.id3 import ID3
    from mutagen.mp3 import MP3
    a = MP3(mp3)
    tags = ID3(mp3)
    check("mp3 bitrate ~192k", 150_000 <= a.info.bitrate <= 260_000, f"{a.info.bitrate // 1000} kbps, {a.info.length:.0f}s")
    check("title tag", bool(tags.get("TIT2")), str(tags.get("TIT2")))
    check("cover art embedded", any(k.startswith("APIC") for k in tags.keys()))
except Exception as e:  # noqa: BLE001
    check("mp3 readable", False, repr(e))
check("no leftover temp files", not (new_dir / ".partial").exists() and not list(new_dir.glob("*.webp")) and not list(new_dir.glob("*.jpg")),
      [p.name for p in new_dir.iterdir()])

print("Cancel")
s, r = call("/api/download", {"url": LONG_VIDEO, "kind": "video", "quality": 1080})
jid = r.get("job_id")
job = wait_job(jid, stop_at=lambda j: j.get("status") == "downloading" and (j.get("percent") or 0) > 3)
s, _ = call(f"/api/cancel/{jid}", {})
job = wait_job(jid, quiet=True)
check("cancelled", job.get("status") == "cancelled", job.get("status"))
time.sleep(0.5)
leftovers = [p.name for p in new_dir.iterdir() if "YE7VzlLtp-4" in p.name or p.name == ".partial"]
check("partial files removed", not leftovers, leftovers)

s, _ = call("/api/progress/does-not-exist")
check("unknown job -> 404", s == 404)

print("Update check")
s, u = call("/api/update?force=1")
check("update endpoint", s == 200 and "current" in u, json.dumps({k: u.get(k) for k in ("current", "latest", "available", "can_install", "error")}))

# restore the folder setting
call("/api/settings", {"download_dir": before.get("download_dir") if before.get("download_dir") != before.get("default_download_dir") else None})

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
