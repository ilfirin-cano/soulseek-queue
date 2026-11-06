import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "/data/tracks.db")
DEFAULTS = {
    "slskd_url": os.environ.get("SLSKD_URL", "http://slskd:5030").rstrip("/"),
    "api_key": os.environ.get("SLSKD_API_KEY", ""),
    "search_seconds": int(os.environ.get("SEARCH_SECONDS", "30")),
    "retry_seconds": int(os.environ.get("RETRY_SECONDS", "90")),
    "concurrency": int(os.environ.get("CONCURRENCY", "3")),
    "retry_enabled": os.environ.get("RETRY_ENABLED", "true").lower() == "true",
}
lock = threading.Lock()
pause_event = threading.Event()
pool = ThreadPoolExecutor(max_workers=32)
active = 0
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".aiff", ".wma"}


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, query TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', pass INTEGER NOT NULL DEFAULT 1,
            search_id TEXT, result TEXT, username TEXT, filename TEXT, error TEXT,
            created REAL NOT NULL, updated REAL NOT NULL)""")
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, str(value)))
        # Requeue work interrupted by a container restart.
        db.execute("UPDATE tracks SET status=CASE WHEN pass=2 THEN 'retry_queued' ELSE 'queued' END, search_id=NULL WHERE status IN ('claimed','searching','retrying')")


def settings():
    with connect() as db:
        result = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}
    for key in ("search_seconds", "retry_seconds", "concurrency"):
        result[key] = int(result[key])
    result["retry_enabled"] = result["retry_enabled"].lower() == "true"
    return result


def api_request(method, path, **kwargs):
    cfg = settings()
    headers = {"X-API-Key": cfg["api_key"]} if cfg["api_key"] else {}
    response = requests.request(method, cfg["slskd_url"] + "/api/v0" + path,
                               headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def choose_file(responses, title):
    candidates = []
    needle = normalize(title)
    for peer in responses or []:
        if not isinstance(peer, dict):
            continue
        username = peer.get("username")
        for file in peer.get("files", []):
            if not isinstance(file, dict):
                continue
            name = file.get("filename", "")
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                continue
            score = SequenceMatcher(None, needle, normalize(os.path.splitext(os.path.basename(name))[0])).ratio()
            candidates.append((score, username, file))
    if not candidates:
        return None
    score, username, file = max(candidates, key=lambda item: item[0])
    return (username, file) if score >= 0.38 else None


def search_responses(search_id, duration):
    deadline = time.monotonic() + duration + 60
    last_state = "unknown"
    while time.monotonic() < deadline:
        state = api_request("GET", f"/searches/{search_id}") or {}
        last_state = str(state.get("state", "unknown"))
        normalized = last_state.casefold()
        if "completed" in normalized or "failed" in normalized or "cancelled" in normalized:
            time.sleep(1)
            responses = api_request("GET", f"/searches/{search_id}/responses")
            if isinstance(responses, dict):
                responses = responses.get("responses", responses.get("items", responses.get("data", [])))
            return responses or [], last_state
        time.sleep(1)
    raise TimeoutError(f"slskd did not finish search within {duration + 60}s (state: {last_state})")


def update_track(track_id, **values):
    values["updated"] = time.time()
    with connect() as db:
        db.execute("UPDATE tracks SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                   [*values.values(), track_id])


def process_track(track_id):
    global active
    with lock:
        active += 1
    try:
        with connect() as db:
            track = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        cfg = settings()
        is_retry = track["pass"] == 2
        if pause_event.is_set():
            update_track(track_id, status="queued" if not is_retry else "retry_queued")
            return
        update_track(track_id, status="retrying" if is_retry else "searching", error=None)
        duration = cfg["retry_seconds"] if is_retry else cfg["search_seconds"]
        search_id = str(uuid.uuid4())
        api_request("POST", "/searches", json={
            "id": search_id, "searchText": track["query"], "searchTimeout": duration * 1000,
            "responseLimit": 100, "fileLimit": 1000,
        })
        update_track(track_id, search_id=search_id)
        if pause_event.is_set():
            api_request("PUT", f"/searches/{search_id}")
        # Wait for slskd's actual terminal state; a fixed sleep can race late responses.
        responses, search_state = search_responses(search_id, duration)
        if pause_event.is_set():
            update_track(track_id, status="queued" if not is_retry else "retry_queued", search_id=None)
            return
        found = choose_file(responses, track["title"])
        if not found:
            if not is_retry and cfg["retry_enabled"]:
                update_track(track_id, status="retry_queued", search_id=None, **{"pass": 2})
            else:
                update_track(track_id, status="not_found")
            return
        username, file = found
        update_track(track_id, status="downloading", username=username, filename=file.get("filename"))
        api_request("POST", f"/transfers/downloads/{requests.utils.quote(username, safe='')}",
                    json=[{"filename": file["filename"], "size": file.get("size", 0)}])
        update_track(track_id, status="downloaded", result=f"{username} · {file['filename']}")
    except Exception as exc:
        update_track(track_id, status="error", error=str(exc)[:400])
    finally:
        with lock:
            active -= 1


def scheduler():
    while True:
        try:
            cfg = settings()
            with lock:
                with connect() as db:
                    running = db.execute("SELECT COUNT(*) FROM tracks WHERE status IN ('claimed','searching','retrying')").fetchone()[0]
                slots = 0 if pause_event.is_set() else max(0, cfg["concurrency"] - running)
            if slots:
                with connect() as db:
                    rows = db.execute("SELECT id FROM tracks WHERE status IN ('queued','retry_queued') ORDER BY created LIMIT ?", (slots,)).fetchall()
                    # Claim before submitting so the next scheduler tick cannot launch duplicates.
                    for row in rows:
                        db.execute("UPDATE tracks SET status='claimed',updated=? WHERE id=?", (time.time(), row["id"]))
                for row in rows:
                    pool.submit(process_track, row["id"])
        except Exception as exc:
            app.logger.exception("Scheduler error: %s", exc)
        time.sleep(1)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/tracks")
def list_tracks():
    with connect() as db:
        rows = db.execute("SELECT * FROM tracks ORDER BY created DESC LIMIT 500").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/tracks")
def add_tracks():
    body = request.get_json(silent=True) or {}
    lines = [line.strip() for line in body.get("tracks", "").splitlines() if line.strip()]
    if not lines:
        return jsonify(error="Вставьте хотя бы один трек."), 400
    if len(lines) > 500:
        return jsonify(error="За раз можно добавить не больше 500 треков."), 400
    now = time.time()
    with connect() as db:
        for line in lines:
            # Each line may be `Artist - Track`; arbitrary query text is also accepted.
            db.execute("INSERT INTO tracks(id,title,query,created,updated) VALUES(?,?,?,?,?)",
                       (str(uuid.uuid4()), line, line, now, now))
    return jsonify(added=len(lines))


@app.delete("/api/tracks")
def clear_tracks():
    with connect() as db:
        db.execute("DELETE FROM tracks WHERE status NOT IN ('searching','retrying','downloading')")
    return jsonify(ok=True)


@app.get("/api/control")
def get_control():
    return jsonify(paused=pause_event.is_set())


@app.post("/api/control")
def set_control():
    body = request.get_json(silent=True) or {}
    if body.get("action") == "stop":
        pause_event.set()
        with connect() as db:
            rows = db.execute("SELECT id,search_id FROM tracks WHERE status IN ('searching','retrying')").fetchall()
        for row in rows:
            if row["search_id"]:
                try:
                    api_request("PUT", f"/searches/{row['search_id']}")
                except requests.RequestException as exc:
                    app.logger.warning("Could not stop slskd search %s: %s", row["search_id"], exc)
        return jsonify(ok=True, paused=True)
    if body.get("action") == "resume":
        pause_event.clear()
        return jsonify(ok=True, paused=False)
    return jsonify(error="Ожидалось действие stop или resume."), 400


@app.get("/api/settings")
def get_settings():
    cfg = settings()
    cfg["api_key_set"] = bool(cfg.pop("api_key"))
    return jsonify(cfg)


@app.put("/api/settings")
def put_settings():
    body = request.get_json(silent=True) or {}
    try:
        values = {
            "slskd_url": body.get("slskd_url", "").strip().rstrip("/"),
            "search_seconds": max(5, min(600, int(body.get("search_seconds", 30)))),
            "retry_seconds": max(5, min(1800, int(body.get("retry_seconds", 90)))),
            "concurrency": max(1, min(32, int(body.get("concurrency", 3)))),
            "retry_enabled": str(bool(body.get("retry_enabled"))).lower(),
        }
        if not values["slskd_url"].startswith(("http://", "https://")):
            raise ValueError("Укажите адрес slskd с http:// или https://")
        if body.get("api_key"):
            values["api_key"] = body["api_key"].strip()
        with connect() as db:
            db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           [(k, str(v)) for k, v in values.items()])
        return jsonify(ok=True)
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400


init_db()
threading.Thread(target=scheduler, daemon=True, name="track-scheduler").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

