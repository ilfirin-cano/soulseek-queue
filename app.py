import contextlib
import os
import random
import sqlite3
import threading
import time
import uuid
import unicodedata
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
    "match_threshold": int(os.environ.get("MATCH_THRESHOLD", "25")),
    "retry_match_threshold": int(os.environ.get("RETRY_MATCH_THRESHOLD", "15")),
    "retry_enabled": os.environ.get("RETRY_ENABLED", "true").lower() == "true",
}
lock = threading.Lock()
search_post_lock = threading.Lock()
download_post_lock = threading.Lock()
pause_event = threading.Event()
queue_event = threading.Event()
pool = ThreadPoolExecutor(max_workers=32)
download_pool = ThreadPoolExecutor(max_workers=4)
active = 0
stop_epoch = 0
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
            created REAL NOT NULL, updated REAL NOT NULL,
            first_pass_seconds REAL, search_seconds_total REAL NOT NULL DEFAULT 0,
            search_started_at REAL, batch_id TEXT, download_enabled INTEGER NOT NULL DEFAULT 1,
            download_id TEXT, download_requested_at REAL, download_state TEXT,
            filesize INTEGER NOT NULL DEFAULT 0)""")
        existing = {row["name"] for row in db.execute("PRAGMA table_info(tracks)")}
        if "first_pass_seconds" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN first_pass_seconds REAL")
        if "search_seconds_total" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN search_seconds_total REAL NOT NULL DEFAULT 0")
        if "search_started_at" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN search_started_at REAL")
        if "batch_id" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN batch_id TEXT")
        if "download_enabled" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN download_enabled INTEGER NOT NULL DEFAULT 1")
        if "download_id" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN download_id TEXT")
        if "download_requested_at" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN download_requested_at REAL")
        if "download_state" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN download_state TEXT")
        if "filesize" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN filesize INTEGER NOT NULL DEFAULT 0")
        for key, value in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, str(value)))
        # Requeue work interrupted by a container restart and preserve the search batch.
        db.execute("UPDATE tracks SET search_seconds_total=search_seconds_total+MAX(0,CAST(strftime('%s','now') AS REAL)-COALESCE(search_started_at,CAST(strftime('%s','now') AS REAL))), search_started_at=NULL, status=CASE WHEN pass=2 THEN 'retry_queued' WHEN pass=3 THEN 'sequential_queued' ELSE 'queued' END, search_id=NULL WHERE status IN ('claimed','searching','retrying','sequential_searching')")
        db.execute("UPDATE tracks SET batch_id=CAST(created AS TEXT) WHERE batch_id IS NULL")
        # Older releases treated 'downloaded' as 'accepted into slskd's queue'.
        db.execute("UPDATE tracks SET status='download_queued',download_requested_at=COALESCE(download_requested_at,updated) WHERE status='downloaded'")
        db.execute("UPDATE tracks SET status='download_queued',download_requested_at=COALESCE(download_requested_at,updated) WHERE status='downloading' AND username IS NOT NULL AND filename IS NOT NULL")


def settings():
    with connect() as db:
        result = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}
    for key in ("search_seconds", "retry_seconds", "concurrency", "match_threshold", "retry_match_threshold"):
        result[key] = int(result[key])
    result["retry_enabled"] = result["retry_enabled"].lower() == "true"
    return result


def api_request(method, path, **kwargs):
    cfg = settings()
    headers = {"X-API-Key": cfg["api_key"]} if cfg["api_key"] else {}
    method = method.upper()
    if method == "POST" and path == "/searches":
        operation_lock = search_post_lock
    elif method == "POST" and path.startswith("/transfers/downloads/"):
        operation_lock = download_post_lock
    else:
        operation_lock = contextlib.nullcontext()
    with operation_lock:
        for attempt in range(9):
            response = requests.request(method, cfg["slskd_url"] + "/api/v0" + path,
                                       headers=headers, timeout=30, **kwargs)
            if response.status_code == 429 and attempt < 8:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(20, 0.5 * (2 ** attempt))
                except ValueError:
                    delay = min(20, 0.5 * (2 ** attempt))
                response.close()
                time.sleep(max(0.25, delay) + random.uniform(0, 0.25))
                continue
            response.raise_for_status()
            return response.json() if response.content else None


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char if char.isalnum() else " " for char in value).strip()


def choose_file(responses, title):
    candidates = []
    needle = normalize(title)
    if not needle:
        return None
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
            basename = os.path.basename(os.path.splitext(name.replace("\\", "/"))[0])
            filename = normalize(basename)
            if not filename:
                continue
            score = SequenceMatcher(None, needle, filename).ratio()
            candidates.append((score, username, file))
    if not candidates:
        return None
    score, username, file = max(candidates, key=lambda item: item[0])
    return username, file, score


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


def flatten_transfer_files(value, username=None):
    files = []
    if isinstance(value, list):
        for item in value:
            files.extend(flatten_transfer_files(item, username))
    elif isinstance(value, dict):
        current_username = value.get("username", username)
        if value.get("filename") and value.get("state"):
            item = dict(value)
            item["username"] = current_username
            files.append(item)
        else:
            for child in value.values():
                if isinstance(child, (dict, list)):
                    files.extend(flatten_transfer_files(child, current_username))
    return files


def enqueue_download(track_id):
    with connect() as db:
        track = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not track:
        return
    try:
        payload = api_request(
            "POST",
            f"/transfers/downloads/{requests.utils.quote(track['username'], safe='')}",
            json=[{"filename": track["filename"], "size": track["filesize"] or 0}],
        )
        download_id = None
        for item in flatten_transfer_files(payload, track["username"]):
            if item.get("filename", "").replace("\\", "/").casefold() == track["filename"].replace("\\", "/").casefold():
                download_id = item.get("id")
                break
        update_track(track_id, status="download_queued", download_id=download_id,
                     download_state="Queued", result="Передано в slskd · ожидание загрузки", error=None)
    except Exception as exc:
        update_track(track_id, status="download_error", download_state="Error",
                     result=f"Не удалось передать файл в slskd: {str(exc)[:300]}", error=str(exc)[:400])


def schedule_download(track_id):
    with connect() as db:
        row = db.execute("SELECT status FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row or row["status"] not in ("found", "download_error"):
            return False
        db.execute("UPDATE tracks SET status='download_queuing',download_requested_at=?,updated=? WHERE id=?",
                   (time.time(), time.time(), track_id))
    download_pool.submit(enqueue_download, track_id)
    return True


def transfer_monitor():
    while True:
        try:
            with connect() as db:
                pending = db.execute("SELECT * FROM tracks WHERE status IN ('download_queued','downloading')").fetchall()
            if pending:
                transfers = flatten_transfer_files(api_request("GET", "/transfers/downloads", params={"includeRemoved": "true"}))
                for track in pending:
                    matches = [item for item in transfers
                               if str(item.get("username", "")).casefold() == str(track["username"] or "").casefold()
                               and item.get("filename", "").replace("\\", "/").casefold() == str(track["filename"] or "").replace("\\", "/").casefold()]
                    if track["download_id"]:
                        matches = [item for item in matches if str(item.get("id")) == str(track["download_id"])]
                    if not matches:
                        continue
                    transfer = matches[-1]
                    state = str(transfer.get("state", "Unknown"))
                    normalized = state.casefold()
                    if "completed" in normalized and "succeeded" in normalized:
                        status = "downloaded"
                    elif any(term in normalized for term in ("failed", "errored", "cancelled", "timedout", "rejected", "aborted")):
                        status = "download_error"
                    elif "inprogress" in normalized or "in progress" in normalized:
                        status = "downloading"
                    else:
                        status = "download_queued"
                    percent = transfer.get("percentComplete", transfer.get("percent_complete"))
                    progress = f" · {percent:.0f}%" if isinstance(percent, (int, float)) else ""
                    update_track(track["id"], status=status, download_id=transfer.get("id"),
                                 download_state=state,
                                 result=f"{state}{progress} · {track['filename']}",
                                 error=None if status != "download_error" else state)
        except Exception as exc:
            app.logger.warning("Download status polling failed: %s", exc)
        time.sleep(4)


def process_track(track_id):
    global active, stop_epoch
    started_monotonic = None
    with lock:
        active += 1
        started_stop_epoch = stop_epoch
    try:
        with connect() as db:
            track = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        cfg = settings()
        search_pass = track["pass"]
        is_retry = search_pass == 2
        is_sequential = search_pass == 3
        threshold = cfg["retry_match_threshold"] if search_pass >= 2 else cfg["match_threshold"]
        if pause_event.is_set():
            waiting_status = "queued" if search_pass == 1 else "retry_queued" if is_retry else "sequential_queued"
            update_track(track_id, status=waiting_status, result="Ожидает возобновления")
            return
        duration = cfg["retry_seconds"] if search_pass >= 2 else cfg["search_seconds"]
        started_at = time.time()
        started_monotonic = time.monotonic()
        running_status = "sequential_searching" if is_sequential else "retrying" if is_retry else "searching"
        update_track(track_id, status=running_status, error=None,
                     search_started_at=started_at,
                     result=f"Запрос: {track['query']} · {duration} с · порог {threshold}%")
        search_id = str(uuid.uuid4())
        try:
            api_request("POST", "/searches", json={
                "id": search_id, "searchText": track["query"], "searchTimeout": duration * 1000,
                "responseLimit": 100, "fileLimit": 1000,
            })
            update_track(track_id, search_id=search_id)
            with lock:
                stopped_during_search = stop_epoch != started_stop_epoch
            if stopped_during_search:
                api_request("PUT", f"/searches/{search_id}")
            responses, search_state = search_responses(search_id, duration)
        except Exception as exc:
            elapsed = round(time.monotonic() - started_monotonic, 2)
            total_seconds = round((track["search_seconds_total"] or 0) + elapsed, 2)
            timing = {"search_seconds_total": total_seconds, "search_started_at": None}
            if search_pass == 1:
                timing["first_pass_seconds"] = elapsed
                timing["pass"] = 2
                status = "retry_wait"
                result = f"Ошибка первого поиска · будет повтор после первичного прохода: {str(exc)[:280]}"
            elif is_retry:
                timing["pass"] = 3
                status = "error_queued"
                result = f"Ошибка повторного поиска · будет последовательная попытка: {str(exc)[:280]}"
            else:
                status = "error"
                result = f"Ошибка последовательного поиска: {str(exc)[:320]}"
            with lock:
                stopped_during_search = stop_epoch != started_stop_epoch
            if stopped_during_search:
                status = "queued" if search_pass == 1 else "retry_queued" if is_retry else "sequential_queued"
                result = "Поиск остановлен · трек оставлен в очереди"
                timing.pop("pass", None)
            update_track(track_id, status=status, result=result, error=None, **timing)
            return
        pass_seconds = round(time.monotonic() - started_monotonic, 2)
        total_seconds = round((track["search_seconds_total"] or 0) + pass_seconds, 2)
        timing = {"search_seconds_total": total_seconds, "search_started_at": None}
        if not is_retry:
            timing["first_pass_seconds"] = pass_seconds
        with lock:
            stopped_during_search = stop_epoch != started_stop_epoch
        if stopped_during_search:
            waiting_status = "queued" if search_pass == 1 else "retry_queued" if is_retry else "sequential_queued"
            update_track(track_id, status=waiting_status, search_id=None,
                         result="Поиск остановлен · трек оставлен в очереди", **timing)
            return
        response_count = len(responses)
        file_count = sum(len(peer.get("files", [])) for peer in responses if isinstance(peer, dict))
        audio_count = sum(1 for peer in responses if isinstance(peer, dict)
                          for candidate in peer.get("files", [])
                          if isinstance(candidate, dict)
                          and os.path.splitext(candidate.get("filename", ""))[1].lower() in AUDIO_EXTS)
        best = choose_file(responses, track["title"])
        search_summary = f"{response_count} пользователей · {audio_count}/{file_count} аудиофайлов · {search_state}"
        if not best or best[2] < threshold / 100:
            best_summary = "аудиофайлов с подходящим названием нет"
            if best:
                best_summary = f"лучшее совпадение {best[2]:.0%} (< {threshold}%) · {best[1].get('filename', '')}"
            if search_pass == 1 and cfg["retry_enabled"]:
                update_track(track_id, status="retry_wait", search_id=None, **{"pass": 2},
                             result=f"1-й проход: {search_summary} · {best_summary}; повтор: до {cfg['retry_seconds']} с, порог {cfg['retry_match_threshold']}%",
                             **timing)
            else:
                update_track(track_id, status="not_found", search_id=None,
                             result=f"{search_summary} · {best_summary}",
                             **timing)
            return
        username, file, score = best
        match_summary = f"совпадение {score:.0%} (порог {threshold}%) · {username} · {file.get('filename', '')}"
        update_track(track_id, status="found", username=username, filename=file.get("filename"),
                     filesize=file.get("size", 0),
                     result=f"{search_summary} · {match_summary} · отправка в slskd",
                     **timing)
        if track["download_enabled"]:
            schedule_download(track_id)
    except Exception as exc:
        values = {"status": "error", "error": str(exc)[:400], "search_started_at": None}
        if started_monotonic is not None:
            elapsed = round(time.monotonic() - started_monotonic, 2)
            with connect() as db:
                track = db.execute("SELECT search_seconds_total FROM tracks WHERE id=?", (track_id,)).fetchone()
            if track:
                values["search_seconds_total"] = round((track["search_seconds_total"] or 0) + elapsed, 2)
        update_track(track_id, **values)
    finally:
        with lock:
            active -= 1
        queue_event.set()


def scheduler():
    while True:
        queue_event.wait()
        queue_event.clear()
        try:
            while not pause_event.is_set():
                cfg = settings()
                with lock:
                    with connect() as db:
                        db.execute("""UPDATE tracks AS retry SET status='retry_queued'
                            WHERE retry.status='retry_wait' AND NOT EXISTS (
                                SELECT 1 FROM tracks AS primary_track
                                WHERE primary_track.batch_id=retry.batch_id AND primary_track.pass=1
                                  AND primary_track.status IN ('queued','claimed','searching'))""")
                        db.execute("""UPDATE tracks AS retry SET status='sequential_queued'
                            WHERE retry.status='error_queued' AND NOT EXISTS (
                                SELECT 1 FROM tracks AS retry_track
                                WHERE retry_track.batch_id=retry.batch_id AND retry_track.pass=2
                                  AND retry_track.status IN ('retry_wait','retry_queued','claimed','retrying'))""")
                        running = db.execute("SELECT COUNT(*) FROM tracks WHERE status IN ('claimed','searching','retrying','sequential_searching')").fetchone()[0]
                        slots = max(0, cfg["concurrency"] - running)
                        rows = []
                        if slots:
                            primary_count = db.execute("SELECT COUNT(*) FROM tracks WHERE status='queued' AND pass=1").fetchone()[0]
                            if primary_count:
                                rows = db.execute("SELECT id FROM tracks WHERE status='queued' AND pass=1 ORDER BY created LIMIT ?", (slots,)).fetchall()
                            else:
                                rows = db.execute("SELECT id FROM tracks WHERE status='retry_queued' AND pass=2 ORDER BY created LIMIT ?", (slots,)).fetchall()
                                if not rows and running == 0:
                                    rows = db.execute("SELECT id FROM tracks WHERE status='sequential_queued' AND pass=3 ORDER BY created LIMIT 1").fetchall()
                        for row in rows:
                            db.execute("UPDATE tracks SET status='claimed',updated=? WHERE id=?", (time.time(), row["id"]))
                if not rows:
                    break
                for row in rows:
                    pool.submit(process_track, row["id"])
        except Exception as exc:
            app.logger.exception("Scheduler error: %s", exc)


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
    batch_id = str(uuid.uuid4())
    download_enabled = 1 if body.get("download", True) else 0
    resumed = pause_event.is_set()
    with lock:
        pause_event.clear()
        with connect() as db:
            for line in lines:
                # Each line may be `Artist - Track`; arbitrary query text is also accepted.
                db.execute("INSERT INTO tracks(id,title,query,created,updated,batch_id,download_enabled) VALUES(?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), line, line, now, now, batch_id, download_enabled))
    queue_event.set()
    return jsonify(added=len(lines), resumed=resumed)


@app.delete("/api/tracks")
def clear_tracks():
    with connect() as db:
        db.execute("DELETE FROM tracks WHERE status IN ('downloaded','download_error','not_found','error')")
    return jsonify(ok=True)


@app.post("/api/tracks/download-found")
def download_found_tracks():
    with connect() as db:
        rows = db.execute("SELECT id FROM tracks WHERE status='found' ORDER BY created").fetchall()
    queued = sum(1 for row in rows if schedule_download(row["id"]))
    return jsonify(queued=queued)


@app.get("/api/control")
def get_control():
    return jsonify(paused=pause_event.is_set())


@app.post("/api/control")
def set_control():
    global stop_epoch
    body = request.get_json(silent=True) or {}
    if body.get("action") == "stop":
        with lock:
            stop_epoch += 1
            pause_event.set()
        queue_event.clear()
        with connect() as db:
            rows = db.execute("SELECT id,search_id FROM tracks WHERE status IN ('searching','retrying','sequential_searching')").fetchall()
        for row in rows:
            if row["search_id"]:
                try:
                    api_request("PUT", f"/searches/{row['search_id']}")
                except requests.RequestException as exc:
                    app.logger.warning("Could not stop slskd search %s: %s", row["search_id"], exc)
        return jsonify(ok=True, paused=True)
    if body.