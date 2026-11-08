import contextlib
import filecmp
import hashlib
import json
import os
import pathlib
import random
import re
import shutil
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
    "burst_searches": os.environ.get("BURST_SEARCHES", "false").lower() == "true",
    "lastfm_api_key": os.environ.get("LASTFM_API_KEY", ""),
    "lastfm_shared_secret": os.environ.get("LASTFM_SHARED_SECRET", ""),
    "lastfm_username": os.environ.get("LASTFM_USERNAME", ""),
    "lastfm_session_key": "",
    "lastfm_auth_token": "",
}
lock = threading.Lock()
search_post_lock = threading.Lock()
download_post_lock = threading.Lock()
playlist_sync_lock = threading.Lock()
pause_event = threading.Event()
queue_event = threading.Event()
pool = ThreadPoolExecutor(max_workers=32)
download_pool = ThreadPoolExecutor(max_workers=4)
lastfm_cache = {}
lastfm_cache_lock = threading.Lock()
lastfm_request_lock = threading.Lock()
lastfm_last_request = 0.0
active = 0
stop_epoch = 0
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".aiff", ".wma"}
NAVIDROME_MUSIC_PATH = os.environ.get("NAVIDROME_MUSIC_PATH", "").strip()
NAVIDROME_PLAYLIST_PATH = os.environ.get("NAVIDROME_PLAYLIST_PATH", "").strip()
SLSKD_DOWNLOADS_PATH = os.environ.get("SLSKD_DOWNLOADS_PATH", "").strip()
DOWNLOAD_DESTINATION_PATH = os.environ.get("DOWNLOAD_DESTINATION_PATH", "").strip()
RECENT_PLAYLIST_ID = "recent_tracks"
RECENT_PLAYLIST_NAME = "Похожие на недавние прослушивания"


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
            filesize INTEGER NOT NULL DEFAULT 0, local_path TEXT,
            move_attempts INTEGER NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS recommendation_playlists (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            updated REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS recommendation_items (
            id TEXT PRIMARY KEY, playlist_id TEXT NOT NULL, artist TEXT NOT NULL,
            title TEXT NOT NULL, match REAL, source TEXT, url TEXT, position INTEGER NOT NULL,
            track_id TEXT, local_path TEXT, created REAL NOT NULL,
            FOREIGN KEY(playlist_id) REFERENCES recommendation_playlists(id),
            FOREIGN KEY(track_id) REFERENCES tracks(id))""")
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
        if "recommendation_id" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN recommendation_id TEXT")
        if "local_path" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN local_path TEXT")
        if "move_attempts" not in existing:
            db.execute("ALTER TABLE tracks ADD COLUMN move_attempts INTEGER NOT NULL DEFAULT 0")
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
    result["burst_searches"] = result["burst_searches"].lower() == "true"
    return result


def lastfm_signature(params, secret):
    message = "".join(f"{key}{params[key]}" for key in sorted(params)
                      if key not in ("format", "callback", "api_sig")) + secret
    return hashlib.md5(message.encode("utf-8")).hexdigest()


def lastfm_call(method, params=None, signed=False):
    global lastfm_last_request
    cfg = settings()
    if not cfg["lastfm_api_key"]:
        raise ValueError("Укажите API key Last.fm во вкладке рекомендаций.")
    values = {"method": method, "api_key": cfg["lastfm_api_key"], "format": "json"}
    values.update(params or {})
    if cfg["lastfm_session_key"] and method not in ("auth.getToken", "auth.getSession"):
        values["sk"] = cfg["lastfm_session_key"]
        signed = True
    if signed:
        if not cfg["lastfm_shared_secret"]:
            raise ValueError("Укажите Shared Secret Last.fm.")
        values["api_sig"] = lastfm_signature(values, cfg["lastfm_shared_secret"])
    with lastfm_request_lock:
        delay = 0.35 - (time.monotonic() - lastfm_last_request)
        if delay > 0:
            time.sleep(delay)
        lastfm_last_request = time.monotonic()
        proxy_url = os.environ.get("LASTFM_PROXY_URL", "").rstrip("/")
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        response = requests.get("https://ws.audioscrobbler.com/2.0/", params=values,
                                headers={"User-Agent": "SoulseekQueue/1.0 (Last.fm recommendations)"},
                                proxies=proxies, timeout=20)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise ValueError(f"Last.fm ({data['error']}): {data.get('message', 'ошибка API')}")
    return data


def lastfm_items(data, container, child):
    value = (data.get(container) or {}).get(child, [])
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def lastfm_artist_name(value):
    if isinstance(value, dict):
        return str(value.get("name") or value.get("#text") or "")
    return str(value or "")


def lastfm_recommendations(body):
    cfg = settings()
    mode = str(body.get("mode", "top_tracks"))
    period = str(body.get("period", "overall"))
    limit = max(1, min(50, int(body.get("limit", 25))))
    candidate_limit = min(200, max(50, limit * 5))
    query = str(body.get("query", "")).strip()
    cache_key = json.dumps([mode, period, query.casefold(), str(body.get("artist", "")).casefold(), limit, cfg["lastfm_username"]], ensure_ascii=False)
    now = time.time()
    with lastfm_cache_lock:
        cached = lastfm_cache.get(cache_key)
        if mode != "recent_tracks" and cached and now - cached[0] < 600:
            return random.sample(cached[1], min(limit, len(cached[1])))
    user = cfg["lastfm_username"]
    if mode in ("top_tracks", "recent_tracks", "loved_tracks", "top_artists") and (not user or not cfg["lastfm_session_key"]):
        raise ValueError("Подключите Last.fm: нужны API key, Shared Secret и авторизация аккаунта.")
    found = []
    if mode in ("top_tracks", "recent_tracks", "loved_tracks"):
        if mode == "top_tracks":
            seeds = lastfm_items(lastfm_call("user.getTopTracks", {"user": user, "period": period, "limit": 8}), "toptracks", "track")
        elif mode == "recent_tracks":
            seeds = lastfm_items(lastfm_call("user.getRecentTracks", {"user": user, "limit": 100}), "recenttracks", "track")
            seeds = [seed for seed in seeds
                     if seed.get("name") and lastfm_artist_name(seed.get("artist", {}))]
            seeds = [random.choice(seeds)] if seeds else []
        else:
            seeds = lastfm_items(lastfm_call("user.getLovedTracks", {"user": user, "limit": 8}), "lovedtracks", "track")
        for seed in seeds[:8]:
            artist_name = lastfm_artist_name(seed.get("artist", {}))
            track_name = seed.get("name", "")
            if artist_name and track_name:
                similar = lastfm_items(lastfm_call("track.getSimilar", {"artist": artist_name, "track": track_name, "limit": 100 if mode == "recent_tracks" else candidate_limit, "autocorrect": 1}), "similartracks", "track")
                found.extend({"artist": lastfm_artist_name(item.get("artist")),
                              "title": item.get("name", ""), "match": item.get("match"), "source": f"Похоже на {artist_name} — {track_name}", "url": item.get("url", ""), "type": "track"} for item in similar)
    elif mode == "top_artists":
        seeds = lastfm_items(lastfm_call("user.getTopArtists", {"user": user, "period": period, "limit": 3}), "topartists", "artist")
        for seed in seeds[:3]:
            seed_name = seed.get("name", "")
            similar = lastfm_items(lastfm_call("artist.getSimilar", {"artist": seed_name, "limit": candidate_limit, "autocorrect": 1}), "similarartists", "artist")
            for artist in similar[:3]:
                name = artist.get("name", "")
                tracks = lastfm_items(lastfm_call("artist.getTopTracks", {"artist": name, "limit": candidate_limit, "autocorrect": 1}), "toptracks", "track") if name else []
                found.extend({"artist": name, "title": track.get("name", ""), "match": artist.get("match"),
                              "source": f"Похож на {seed_name}", "url": track.get("url", ""), "type": "track"} for track in tracks)
    elif mode == "genre_tracks":
        if not query:
            raise ValueError("Введите жанр или тег.")
        tracks = lastfm_items(lastfm_call("tag.getTopTracks", {"tag": query, "limit": candidate_limit}), "tracks", "track")
        found = [{"artist": lastfm_artist_name(track.get("artist")),
                  "title": track.get("name", ""), "match": None, "source": f"Жанр: {query}", "url": track.get("url", ""), "type": "track"} for track in tracks]
    elif mode == "genre_artists":
        if not query:
            raise ValueError("Введите жанр или тег.")
        artists = lastfm_items(lastfm_call("tag.getTopArtists", {"tag": query, "limit": candidate_limit}), "topartists", "artist")
        found = [{"artist": item.get("name", ""), "title": "", "match": None, "source": f"Жанр: {query}", "url": item.get("url", ""), "type": "artist"} for item in artists]
    elif mode == "similar_artist":
        if not query:
            raise ValueError("Введите исполнителя.")
        artists = lastfm_items(lastfm_call("artist.getSimilar", {"artist": query, "limit": candidate_limit, "autocorrect": 1}), "similarartists", "artist")
        found = [{"artist": item.get("name", ""), "title": "", "match": item.get("match"), "source": f"Похож на {query}", "url": item.get("url", ""), "type": "artist"} for item in artists]
    elif mode == "similar_track":
        artist = str(body.get("artist", "")).strip()
        if not artist or not query:
            raise ValueError("Введите исполнителя и название трека.")
        tracks = lastfm_items(lastfm_call("track.getSimilar", {"artist": artist, "track": query, "limit": candidate_limit, "autocorrect": 1}), "similartracks", "track")
        found = [{"artist": lastfm_artist_name(item.get("artist")),
                  "title": item.get("name", ""), "match": item.get("match"), "source": f"Похоже на {artist} — {query}", "url": item.get("url", ""), "type": "track"} for item in tracks]
    else:
        raise ValueError("Неизвестный способ рекомендаций.")
    deduped, seen = [], set()
    for item in found:
        key = (item.get("artist", "").casefold(), item.get("title", "").casefold())
        if key != ("", "") and key not in seen:
            seen.add(key)
            deduped.append(item)
    with lastfm_cache_lock:
        lastfm_cache[cache_key] = (now, deduped)
    return random.sample(deduped, min(limit, len(deduped)))


def recommendation_item_id(item, playlist_id=RECENT_PLAYLIST_ID):
    key = "\0".join((playlist_id, item.get("artist", "").strip().casefold(), item.get("title", "").strip().casefold()))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def store_recent_recommendations(items):
    now = time.time()
    with connect() as db:
        db.execute("INSERT INTO recommendation_playlists(id,name,category,updated) VALUES(?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET name=excluded.name,updated=excluded.updated",
                   (RECENT_PLAYLIST_ID, RECENT_PLAYLIST_NAME, RECENT_PLAYLIST_ID, now))
        ids = []
        for position, item in enumerate(items):
            item_id = recommendation_item_id(item, RECENT_PLAYLIST_ID)
            ids.append(item_id)
            db.execute("""INSERT INTO recommendation_items
                (id,playlist_id,artist,title,match,source,url,position,created)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                playlist_id=excluded.playlist_id,artist=excluded.artist,title=excluded.title,
                match=excluded.match,source=excluded.source,url=excluded.url,
                position=excluded.position,created=excluded.created""",
                       (item_id, RECENT_PLAYLIST_ID, item.get("artist", ""), item.get("title", ""),
                        item.get("match"), item.get("source", ""), item.get("url", ""), position, now))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            db.execute(f"DELETE FROM recommendation_items WHERE playlist_id=? AND id NOT IN ({placeholders})",
                       [RECENT_PLAYLIST_ID, *ids])
        else:
            db.execute("DELETE FROM recommendation_items WHERE playlist_id=?", (RECENT_PLAYLIST_ID,))
    sync_navidrome_playlist()


def _write_navidrome_playlist():
    """Write the Navidrome-owned M3U source from recommendation rows with local media."""
    if not NAVIDROME_MUSIC_PATH or not NAVIDROME_PLAYLIST_PATH:
        return
    playlist_dir = pathlib.Path(NAVIDROME_PLAYLIST_PATH)
    playlist_dir.mkdir(parents=True, exist_ok=True)
    output = playlist_dir / f"{RECENT_PLAYLIST_NAME}.m3u"
    root = pathlib.Path(NAVIDROME_MUSIC_PATH).resolve()
    media_by_stem = {}
    for folder, _, names in os.walk(root):
        for name in names:
            path = pathlib.Path(folder) / name
            if path.suffix.lower() in AUDIO_EXTS:
                media_by_stem.setdefault(normalize(path.stem), []).append(path)
    with connect() as db:
        rows = db.execute("""SELECT ri.id,ri.artist,ri.title,ri.position,ri.local_path,
            t.filename,t.status,t.local_path AS track_local_path FROM recommendation_items ri
            LEFT JOIN tracks t ON t.id=ri.track_id
            WHERE ri.playlist_id=? ORDER BY ri.position""", (RECENT_PLAYLIST_ID,)).fetchall()
        lines = ["#EXTM3U"]
        for row in rows:
            relative = row["local_path"] or row["track_local_path"]
            if relative:
                candidate = (root / pathlib.PurePosixPath(relative)).resolve()
                if not candidate.is_relative_to(root) or not candidate.is_file():
                    relative = None
            if not relative:
                remote_base = normalize(os.path.splitext(os.path.basename(str(row["filename"] or "").replace("\\", "/")))[0])
                artist_title = normalize(f"{row['artist']} {row['title']}")
                title = normalize(row["title"])
                candidates = media_by_stem.get(remote_base, []) if remote_base else []
                if not candidates:
                    candidates = media_by_stem.get(artist_title, []) or media_by_stem.get(title, [])
                if len(candidates) == 1:
                    relative = candidates[0].relative_to(root).as_posix()
                if relative:
                    db.execute("UPDATE recommendation_items SET local_path=? WHERE id=?", (relative, row["id"]))
            if relative:
                label = " - ".join(part for part in (row["artist"], row["title"]) if part)
                lines.extend((f"#EXTINF:-1,{label}", relative.replace("\\", "/")))
    content = "\n".join(lines) + "\n"
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8-sig", newline="\n")
    os.replace(temporary, output)
    legacy_output = playlist_dir / "Last.fm - Recent recommendations.m3u"
    if legacy_output != output:
        legacy_output.unlink(missing_ok=True)


def sync_navidrome_playlist():
    with playlist_sync_lock:
        _write_navidrome_playlist()


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


def safe_path_segments(value):
    raw = str(value or "").replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or re.match(r"^[A-Za-z]:$", part) for part in parts):
        return None
    cleaned = [re.sub(r"[\x00-\x1f]", "", part).strip() for part in parts]
    return [part for part in cleaned if part] or None


def find_completed_file(track, transfer=None):
    if not SLSKD_DOWNLOADS_PATH or not os.path.isdir(SLSKD_DOWNLOADS_PATH):
        return None
    root = pathlib.Path(SLSKD_DOWNLOADS_PATH).resolve()
    expected_parts = safe_path_segments(track["filename"])
    if not expected_parts:
        return None
    size = int(track["filesize"] or 0)
    if transfer and transfer.get("localPath"):
        local = pathlib.Path(str(transfer["localPath"]))
        choices = [local] if local.is_absolute() else [root / local]
        for candidate in choices:
            try:
                resolved = candidate.resolve()
                if resolved.is_relative_to(root) and resolved.is_file() and (not size or resolved.stat().st_size == size):
                    return resolved
            except (OSError, ValueError):
                pass
    expected = (root / pathlib.Path(*expected_parts)).resolve()
    if expected.is_relative_to(root) and expected.is_file() and (not size or expected.stat().st_size == size):
        return expected
    # slskd may prepend a peer or destination directory; only accept one unambiguous file.
    target_name = expected_parts[-1].casefold()
    matches = []
    for folder, _, names in os.walk(root):
        for name in names:
            if name.casefold() != target_name:
                continue
            candidate = pathlib.Path(folder) / name
            try:
                if not size or candidate.stat().st_size == size:
                    matches.append(candidate.resolve())
                    if len(matches) > 1:
                        return None
            except OSError:
                continue
    return matches[0] if matches else None


def move_completed_file(track, transfer=None):
    if not SLSKD_DOWNLOADS_PATH or not DOWNLOAD_DESTINATION_PATH:
        return None
    def defer_move(message):
        attempts = int(track["move_attempts"] or 0) + 1
        values = {"move_attempts": attempts}
        if attempts == 1:
            values["result"] = message
        update_track(track["id"], **values)

    if not os.path.isdir(SLSKD_DOWNLOADS_PATH):
        defer_move(f"Скачано · папка загрузок недоступна: {SLSKD_DOWNLOADS_PATH}")
        return None
    remote_parts = safe_path_segments(track["filename"])
    if not remote_parts:
        return None
    destination_root = pathlib.Path(DOWNLOAD_DESTINATION_PATH).resolve()
    folder_name = RECENT_PLAYLIST_NAME if track["recommendation_id"] else "Загрузки"
    relative = pathlib.PurePosixPath(folder_name, remote_parts[-1])
    destination = (destination_root / pathlib.Path(*relative.parts)).resolve()
    if not destination.is_relative_to(destination_root):
        return None
    old_local_path = track["local_path"]
    source = None
    if old_local_path:
        existing = (destination_root / pathlib.PurePosixPath(old_local_path)).resolve()
        if existing.is_relative_to(destination_root) and existing.is_file():
            source = existing
    if not source:
        source = find_completed_file(track, transfer)
    if not source:
        defer_move("Скачано · ожидается файл в папке завершённых загрузок slskd")
        return None
    if source == destination and old_local_path == relative.as_posix():
        return None
    try:
        if source != destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if filecmp.cmp(source, destination, shallow=False):
                    source.unlink()
                else:
                    stem, suffix = destination.stem, destination.suffix
                    index = 2
                    while destination.exists():
                        destination = destination.with_name(f"{stem} ({index}){suffix}")
                        index += 1
                    shutil.move(str(source), str(destination))
            else:
                shutil.move(str(source), str(destination))
        local_path = destination.relative_to(destination_root).as_posix()
        update_track(track["id"], local_path=local_path, move_attempts=0,
                     result=f"Скачано · перемещено в {local_path}")
        with connect() as db:
            db.execute("UPDATE recommendation_items SET local_path=? WHERE track_id=?", (local_path, track["id"]))
        return local_path
    except OSError as exc:
        app.logger.warning("Could not move completed download %s: %s", track["id"], exc)
        defer_move(f"Скачано · ошибка переноса: {str(exc)[:220]}")
        return None


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
                    if status == "downloaded":
                        moved = move_completed_file(track, transfer)
                        if moved and track["recommendation_id"]:
                            sync_navidrome_playlist()
        except Exception as exc:
            app.logger.warning("Download status polling failed: %s", exc)
        time.sleep(4)


def recommendation_playlist_monitor():
    while True:
        try:
            if SLSKD_DOWNLOADS_PATH and DOWNLOAD_DESTINATION_PATH:
                with connect() as db:
                    pending = db.execute("SELECT * FROM tracks WHERE status='downloaded' AND move_attempts<10").fetchall()
                moved_any = False
                for track in pending:
                    if move_completed_file(track):
                        moved_any = True
                if moved_any:
                    sync_navidrome_playlist()
        except Exception as exc:
            app.logger.warning("Navidrome playlist sync failed: %s", exc)
        time.sleep(30)


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
                                effective_limit = cfg["concurrency"]
                                queued = db.execute("SELECT id,batch_id FROM tracks WHERE status='queued' AND pass=1 ORDER BY created LIMIT ?", (slots + 5,)).fetchall()
                                if cfg["burst_searches"] and cfg["concurrency"] <= 5 and queued:
                                    batch_id = queued[0]["batch_id"]
                                    first_five_open = db.execute("""SELECT COUNT(*) FROM (
                                        SELECT status FROM tracks WHERE batch_id=? AND pass=1 ORDER BY created LIMIT 5
                                    ) WHERE status IN ('queued','claimed','searching')""", (batch_id,)).fetchone()[0]
                                    if first_five_open:
                                        effective_limit = 5
                                        rows = db.execute("SELECT id FROM tracks WHERE status='queued' AND pass=1 AND batch_id=? ORDER BY created LIMIT ?", (batch_id, max(0, effective_limit-running))).fetchall()
                                if not rows:
                                    rows = db.execute("SELECT id FROM tracks WHERE status='queued' AND pass=1 ORDER BY created LIMIT ?", (max(0, effective_limit-running),)).fetchall()
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
    start = bool(body.get("start", True))
    resumed = pause_event.is_set() and start
    status = "queued" if start else "staged"
    recommendation_ids = body.get("recommendation_ids", [])
    if not isinstance(recommendation_ids, list):
        recommendation_ids = []
    recommendation_ids = [str(value) for value in recommendation_ids]
    added = 0
    skipped = 0
    with lock:
        if start:
            pause_event.clear()
        with connect() as db:
            for index, line in enumerate(lines):
                # Each line may be `Artist - Track`; arbitrary query text is also accepted.
                recommendation_id = recommendation_ids[index] if index < len(recommendation_ids) else None
                if recommendation_id:
                    existing_rec = db.execute("SELECT track_id FROM recommendation_items WHERE id=?", (recommendation_id,)).fetchone()
                    if existing_rec and existing_rec["track_id"]:
                        skipped += 1
                        continue
                track_id = str(uuid.uuid4())
                db.execute("INSERT INTO tracks(id,title,query,status,created,updated,batch_id,download_enabled,recommendation_id) VALUES(?,?,?,?,?,?,?,?,?)",
                           (track_id, line, line, status, now, now, batch_id, download_enabled, recommendation_id))
                if recommendation_id:
                    db.execute("UPDATE recommendation_items SET track_id=? WHERE id=?",
                               (track_id, recommendation_id))
                added += 1
    if start:
        queue_event.set()
    if recommendation_ids:
        sync_navidrome_playlist()
    return jsonify(added=added, skipped=skipped, resumed=resumed, staged=not start)


@app.delete("/api/tracks")
def clear_tracks():
    with connect() as db:
        db.execute("DELETE FROM tracks WHERE status IN ('downloaded','download_error','not_found','error','found')")
    return jsonify(ok=True)


@app.post("/api/tracks/start-staged")
def start_staged_tracks():
    with lock:
        pause_event.clear()
        with connect() as db:
            result = db.execute("UPDATE tracks SET status='queued',updated=? WHERE status='staged'", (time.time(),))
            started = result.rowcount
    if started:
        queue_event.set()
    return jsonify(started=started)


@app.post("/api/tracks/stop-clear")
def stop_and_clear_tracks():
    global stop_epoch
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
    with connect() as db:
        db.execute("DELETE FROM tracks")
    return jsonify(ok=True, paused=True, cleared=True)


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
    if body.get("action") == "resume":
        pause_event.clear()
        queue_event.set()
        return jsonify(ok=True, paused=False)
    return jsonify(error="Ожидалось действие stop или resume."), 400


@app.get("/api/settings")
def get_settings():
    cfg = settings()
    cfg["api_key_set"] = bool(cfg.pop("api_key"))
    cfg["lastfm_api_key_set"] = bool(cfg.pop("lastfm_api_key"))
    cfg["lastfm_shared_secret_set"] = bool(cfg.pop("lastfm_shared_secret"))
    cfg.pop("lastfm_session_key", None)
    cfg.pop("lastfm_auth_token", None)
    return jsonify(cfg)


@app.put("/api/settings")
def put_settings():
    body = request.get_json(silent=True) or {}
    try:
        values = {
            "slskd_url": body.get("slskd_url", "").strip().rstrip("/"),
            "search_seconds": max(2, min(600, int(body.get("search_seconds", 30)))),
            "retry_seconds": max(5, min(1800, int(body.get("retry_seconds", 90)))),
            "concurrency": max(1, min(32, int(body.get("concurrency", 3)))),
            "match_threshold": max(10, min(90, int(body.get("match_threshold", 25)))),
            "retry_match_threshold": max(10, min(90, int(body.get("retry_match_threshold", 15)))),
            "retry_enabled": str(bool(body.get("retry_enabled"))).lower(),
            "burst_searches": str(bool(body.get("burst_searches"))).lower(),
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


@app.get("/api/lastfm/config")
def get_lastfm_config():
    cfg = settings()
    return jsonify(api_key_set=bool(cfg["lastfm_api_key"]), secret_set=bool(cfg["lastfm_shared_secret"]),
                   connected=bool(cfg["lastfm_session_key"]), auth_pending=bool(cfg["lastfm_auth_token"]),
                   username=cfg["lastfm_username"])


@app.put("/api/lastfm/config")
def put_lastfm_config():
    body = request.get_json(silent=True) or {}
    values = {}
    for field, key in (("api_key", "lastfm_api_key"), ("shared_secret", "lastfm_shared_secret")):
        value = str(body.get(field, "")).strip()
        if value:
            values[key] = value
    username = str(body.get("username", "")).strip()
    if username:
        values["lastfm_username"] = username
    if not settings()["lastfm_api_key"] and "lastfm_api_key" not in values:
        return jsonify(error="Введите API key Last.fm."), 400
    if not settings()["lastfm_shared_secret"] and "lastfm_shared_secret" not in values:
        return jsonify(error="Введите Shared Secret Last.fm."), 400
    if values:
        with connect() as db:
            if ("lastfm_api_key" in values and values["lastfm_api_key"] != settings()["lastfm_api_key"]) or ("lastfm_shared_secret" in values and values["lastfm_shared_secret"] != settings()["lastfm_shared_secret"]):
                db.execute("UPDATE settings SET value='' WHERE key IN ('lastfm_session_key','lastfm_auth_token')")
            db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           [(key, value) for key, value in values.items()])
    return jsonify(ok=True)


@app.post("/api/lastfm/auth/start")
def lastfm_auth_start():
    try:
        token = lastfm_call("auth.getToken", signed=True).get("token")
        if not token:
            raise ValueError("Last.fm не вернул токен авторизации.")
        with connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='lastfm_auth_token'", (token,))
        cfg = settings()
        from urllib.parse import urlencode
        return jsonify(url="https://www.last.fm/api/auth/?" + urlencode({"api_key": cfg["lastfm_api_key"], "token": token}))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/lastfm/auth/complete")
def lastfm_auth_complete():
    try:
        token = settings()["lastfm_auth_token"]
        if not token:
            raise ValueError("Сначала нажмите «Подключить Last.fm» и подтвердите доступ.")
        session = lastfm_call("auth.getSession", {"token": token}, signed=True).get("session", {})
        if not session.get("key"):
            raise ValueError("Last.fm не вернул ключ сессии.")
        with connect() as db:
            db.executemany("UPDATE settings SET value=? WHERE key=?", [
                (session["key"], "lastfm_session_key"), (session.get("name", ""), "lastfm_username"), ("", "lastfm_auth_token")])
        return jsonify(connected=True, username=session.get("name", ""))
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.delete("/api/lastfm/session")
def delete_lastfm_session():
    with connect() as db:
        db.executemany("UPDATE settings SET value='' WHERE key=?", [("lastfm_session_key",), ("lastfm_auth_token",), ("lastfm_username",)])
    return jsonify(ok=True)


@app.post("/api/lastfm/recommendations")
def get_lastfm_recommendations():
    try:
        body = request.get_json(silent=True) or {}
        items = lastfm_recommendations(body)
        playlist_id = None
        if body.get("mode") == RECENT_PLAYLIST_ID:
            store_recent_recommendations(items)
            playlist_id = RECENT_PLAYLIST_ID
            for item in items:
                item["id"] = recommendation_item_id(item, RECENT_PLAYLIST_ID)
        return jsonify(items=items, playlist_id=playlist_id)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/lastfm/playlists/recent-tracks")
def get_recent_recommendation_playlist():
    with connect() as db:
        playlist = db.execute("SELECT * FROM recommendation_playlists WHERE id=?", (RECENT_PLAYLIST_ID,)).fetchone()
        if not playlist:
            return jsonify(id=RECENT_PLAYLIST_ID, name="Похожие на недавние прослушивания", items=[], updated_at=None)
        rows = db.execute("""SELECT ri.*,t.status AS download_status,t.download_state,
            t.result AS download_result FROM recommendation_items ri
            LEFT JOIN tracks t ON t.id=ri.track_id WHERE ri.playlist_id=? ORDER BY ri.position""",
                          (RECENT_PLAYLIST_ID,)).fetchall()
    items = []
    music_root = pathlib.Path(NAVIDROME_MUSIC_PATH).resolve() if NAVIDROME_MUSIC_PATH else None
    for row in rows:
        item = dict(row)
        available = False
        if item.get("local_path") and music_root:
            candidate = (music_root / pathlib.PurePosixPath(item["local_path"])).resolve()
            available = candidate.is_relative_to(music_root) and candidate.is_file()
        item["available"] = available
        item["state"] = "available" if item["available"] else item.get("download_status") or "recommended"
        items.append(item)
    return jsonify(id=playlist["id"], name=playlist["name"], category=playlist["category"],
                   updated_at=playlist["updated"], items=items)


init_db()
queue_event.set()
threading.Thread(target=scheduler, daemon=True, name="track-scheduler").start()
threading.Thread(target=transfer_monitor, daemon=True, name="transfer-monitor").start()
threading.Thread(target=recommendation_playlist_monitor, daemon=True, name="recommendation-playlist-monitor").start()
threading.Thread(target=sync_navidrome_playlist, daemon=True, name="navidrome-playlist-startup-sync").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))

