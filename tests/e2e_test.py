"""
End-to-end test against a running Video Downloader backend.

    python tests/e2e_test.py <port> [video_url]

Checks validation/error handling, fetches metadata, runs a real download while
polling progress, and verifies the resulting file exists in the download folder.
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

PORT = int(sys.argv[1])
VIDEO = sys.argv[2] if len(sys.argv) > 2 else "https://www.youtube.com/watch?v=jNQXAC9IVRw"
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

print("Metadata")
t = time.time()
s, r = call("/api/info", {"url": VIDEO})
v = r.get("video", {})
check("info fetched", s == 200 and v.get("title"), f"({time.time() - t:.1f}s) {v.get('title')!r} by {v.get('uploader')!r}, {v.get('duration')}s")
check("has thumbnail", bool(v.get("thumbnail")))

print("Download")
s, r = call("/api/download", {"url": r.get("url", VIDEO)})
check("job started", s == 202 and r.get("job_id"))
job_id = r.get("job_id")
seen = []
job = {}
for _ in range(1200):
    _, job = call(f"/api/progress/{job_id}")
    key = (job.get("stage"), int(job.get("percent") or 0) // 10 * 10)
    if key not in seen:
        seen.append(key)
        print(f"     {job.get('status'):<11} {job.get('percent', 0):5.1f}%  {job.get('stage')}")
    if job.get("status") in ("finished", "error"):
        break
    time.sleep(0.5)
check("download finished", job.get("status") == "finished", job.get("error") or job.get("filename", ""))
if job.get("status") == "finished":
    f = Path(job["folder"]) / job["filename"]
    check("file exists on disk", f.is_file() and f.stat().st_size > 10_000, f"{f} ({f.stat().st_size if f.exists() else 0} bytes)")
    check("filepath not leaked to client", "filepath" not in job)

s, _ = call("/api/progress/does-not-exist")
check("unknown job -> 404", s == 404)

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
