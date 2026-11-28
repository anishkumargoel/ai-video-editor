from pathlib import Path
import hashlib
import math
import hmac
import re
import secrets
import time
from urllib.parse import quote
from datetime import datetime
import base64
import sqlite3
import shutil
import subprocess
import uuid
import os
from pydantic import BaseModel, Field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from cryptography.fernet import Fernet, InvalidToken


PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"
ENV_FILE = PROJECT_DIR / ".env"


def load_env_file():
    """Load KEY=VALUE pairs from .env into os.environ (real environment variables win)."""
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env_file()
DB_FILE = PROJECT_DIR / "video_editor.db"
CHUNK_SIZE = 1024 * 1024

app = FastAPI(title="Video Editing Tool")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def db():
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                input_video_folder TEXT NOT NULL,
                input_audio_folder TEXT NOT NULL,
                output_video_folder TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS flagged_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                video_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS combined_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                sequence_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS replaced_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                source_video_name TEXT NOT NULL,
                source_audio_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS unique_project_flag
                ON flagged_videos(project_id, video_name);

            CREATE TABLE IF NOT EXISTS video_prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_on TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model TEXT NOT NULL,
                instructions TEXT NOT NULL,
                image_files TEXT NOT NULL DEFAULT '',
                text_context TEXT NOT NULL DEFAULT '',
                parts INTEGER NOT NULL DEFAULT 0,
                scene_number INTEGER NOT NULL DEFAULT 0,
                series_id INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS audio_prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_on TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model TEXT NOT NULL,
                instructions TEXT NOT NULL
            );
        """)
        video_columns = {row[1] for row in connection.execute("PRAGMA table_info(video_prompts)")}
        for column in ("scene_number", "series_id"):
            if column not in video_columns:
                connection.execute(f"ALTER TABLE video_prompts ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(audio_prompts)")}
        for column in ("video_file", "text_context"):
            if column not in existing_columns:
                connection.execute(f"ALTER TABLE audio_prompts ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        if connection.execute("SELECT COUNT(*) FROM audio_prompts").fetchone()[0] == 0:
            connection.execute(
                "INSERT INTO audio_prompts (model, instructions) VALUES (?, ?)",
                ("lyria-3.5", RACE_PROMPT),
            )


RACE_PROMPT = (
    "Instrumental only, no vocals.\n"
    "[0:00 - 0:39] Tense arcade racing game countdown music with pulsing low-pass "
    "synthesizers and rising electronic tension. Layered with realistic foley: "
    "twin-turbo engines idling, sharp throttle revs, loud exhaust backfire flames, "
    "startled pigeon wing flaps, and a massive tire-screeching burnout launch.\n"
    "[0:40 - 1:42] Fast-paced 145 BPM street-racing gameplay music featuring driving "
    "breakbeat drums, gritty basslines, and energetic lead synths. Blended with "
    "high-RPM engine roars, prolonged tire squeals on cobblestone, a sharp metallic "
    "bumper crunch with sizzling sparks, and an explosive blue nitrous oxide boost whoosh.\n"
    "[1:43 - 2:21] Adrenaline-pumping electronic action score with a sudden suspenseful "
    "riser as a car slides narrowly beneath a silver SUV with a deep Doppler air-whoosh. "
    "Transitions into a dramatic, half-time slow-motion synth swell with airy pads while "
    "both cars launch airborne over a speed bump, slamming back into high-tempo breakbeats "
    "with heavy suspension thuds and landing sparks.\n"
    "[2:22 - 2:41] Climactic, high-octane racing game finale with heavy percussion hits "
    "and soaring electronic riffs. Synchronized with aggressive braking, prolonged "
    "screeching tire drifts as both cars spin 180 degrees in plumes of white smoke, "
    "and engines rumbling to a stop.\n"
    "[2:42 - 3:12] Upbeat, cool, groovy electronic-funk post-race menu theme with warm "
    "basslines, crisp drums, and celebratory synth chords. Paired with subtle engine "
    "cooling ticks, a hydraulic upward car door hiss, gentle breeze rustling a red scarf, "
    "and two friendly engine purrs as both cars cruise away."
)


init_db()


def find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    package_dir = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    candidates = [
        package_dir.glob("**/ffmpeg.exe") if package_dir.exists() else iter(()),
        Path("C:/ffmpeg/bin/ffmpeg.exe").parent.glob("ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe").parent.glob("ffmpeg.exe"),
    ]
    for matches in candidates:
        for candidate in matches:
            if candidate.is_file():
                return str(candidate)
    return None


def natural_sort_key(name: str):
    import re
    match = re.fullmatch(r"P(\d+)", Path(name).stem, re.IGNORECASE)
    return (int(match.group(1)), name.lower()) if match else (10_000, name.lower())


def resolve_project(project_id: int):
    with db() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def list_project_videos(project):
    folder = Path(project["input_video_folder"])
    if not folder.exists():
        return []
    videos = [
        path for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() == ".mp4"
    ]
    videos.sort(key=lambda path: natural_sort_key(path.name))
    return videos


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/projects")
def get_projects():
    with db() as connection:
        rows = connection.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/projects")
async def create_project(request: Request):
    data = await request.json()
    name = str(data.get("name", "")).strip()
    input_video_folder = str(data.get("input_video_folder", "")).strip()
    input_audio_folder = str(data.get("input_audio_folder", "")).strip()
    output_video_folder = str(data.get("output_video_folder", "")).strip()

    if not name or not input_video_folder or not input_audio_folder or not output_video_folder:
        raise HTTPException(status_code=400, detail="All project fields are required")

    video_folder = Path(input_video_folder)
    if not video_folder.exists():
        raise HTTPException(status_code=400, detail="Input video folder does not exist")

    audio_folder = Path(input_audio_folder)
    if not audio_folder.exists():
        raise HTTPException(status_code=400, detail="Input audio folder does not exist")

    Path(output_video_folder).mkdir(parents=True, exist_ok=True)

    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO projects
               (name, input_video_folder, input_audio_folder, output_video_folder)
               VALUES (?, ?, ?, ?)""",
            (name, str(video_folder.resolve()), str(audio_folder.resolve()), str(Path(output_video_folder).resolve())),
        )
        project_id = cursor.lastrowid

    return {"id": project_id, "name": name}


@app.get("/api/projects/{project_id}/videos")
def get_project_videos(project_id: int):
    project = resolve_project(project_id)
    result = []
    for video in list_project_videos(project):
        result.append({
            "name": video.name,
            "size_mb": round(video.stat().st_size / (1024 * 1024), 2),
            "stream_url": f"/api/projects/{project_id}/video/{quote(video.name, safe='')}",
        })
    return result


@app.get("/api/projects/{project_id}/video/{filename}")
def stream_project_video(project_id: int, filename: str, request: Request):
    project = resolve_project(project_id)
    video = next((item for item in list_project_videos(project) if item.name == filename), None)
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")

    file_size = video.stat().st_size
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1
    status_code = 200

    if range_header:
        try:
            range_value = range_header.strip().lower().replace("bytes=", "")
            range_start, range_end = range_value.split("-", 1)
            start = int(range_start) if range_start else max(file_size - int(range_end), 0)
            end = int(range_end) if range_end else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable", headers={"Content-Range": f"bytes */{file_size}"})
            status_code = 206
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")

    def iter_file():
        with video.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": "video/mp4",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type="video/mp4")


@app.post("/api/projects/{project_id}/flag")
async def flag_videos(project_id: int, request: Request):
    resolve_project(project_id)
    data = await request.json()
    names = data.get("video_names", [])
    if not isinstance(names, list) or not names:
        raise HTTPException(status_code=400, detail="Select at least one video")

    with db() as connection:
        for name in names:
            connection.execute(
                "INSERT OR IGNORE INTO flagged_videos (project_id, video_name) VALUES (?, ?)",
                (project_id, str(name)),
            )
    return {"count": len(names)}


@app.get("/api/projects/{project_id}/flagged")
def get_flagged(project_id: int):
    resolve_project(project_id)
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM flagged_videos WHERE project_id = ? ORDER BY created_at DESC, id DESC",
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/api/projects/{project_id}/flagged/{filename}")
def unflag_video(project_id: int, filename: str):
    resolve_project(project_id)
    with db() as connection:
        connection.execute(
            "DELETE FROM flagged_videos WHERE project_id = ? AND video_name = ?",
            (project_id, filename),
        )
    return {"success": True}


@app.post("/api/projects/{project_id}/combine")
async def combine_videos(project_id: int, request: Request):
    project = resolve_project(project_id)
    data = await request.json()
    names = data.get("video_names", [])
    if not isinstance(names, list) or not names:
        raise HTTPException(status_code=400, detail="Select at least one video")

    input_folder = Path(project["input_video_folder"])
    available = {path.name: path for path in list_project_videos(project)}
    videos = [available[name] for name in names if name in available]
    if not videos:
        raise HTTPException(status_code=400, detail="No valid selected videos")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    output_folder = Path(project["output_video_folder"])
    output_folder.mkdir(parents=True, exist_ok=True)
    unique_name = f"combined_{uuid.uuid4().hex[:12]}_{len(videos)}clips.mp4"
    output_file = output_folder / unique_name

    command = [ffmpeg, "-hide_banner", "-y"]
    for video in videos:
        command.extend(["-i", str(video)])

    filters = []
    concat_inputs = []
    for index in range(len(videos)):
        filters.append(
            f"[{index}:v:0]scale=1280:720:force_original_aspect_ratio=decrease,"
            f"pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p,"
            f"setsar=1,setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[{index}:a:0]aresample=48000:async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")

    filters.append("".join(concat_inputs) + f"concat=n={len(videos)}:v=1:a=1[outv][outa]")
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(output_file),
    ])

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-1000:]}")

    import json
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO combined_videos
               (project_id, name, file_path, sequence_json)
               VALUES (?, ?, ?, ?)""",
            (project_id, unique_name, str(output_file.resolve()), json.dumps(names)),
        )
        combined_id = cursor.lastrowid

    return {"id": combined_id, "name": unique_name, "file_path": str(output_file.resolve())}


@app.get("/api/projects/{project_id}/outputs")
def get_outputs(project_id: int):
    project = resolve_project(project_id)
    sync_existing_combined_outputs(project)  # Sync existing outputs
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM combined_videos WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        ).fetchall()

        live_rows = []
        for row in rows:
            video = Path(row["file_path"])
            if video.exists():
                live_rows.append(dict(row))
            else:
                connection.execute(
                    "DELETE FROM combined_videos WHERE project_id = ? AND name = ?",
                    (project_id, row["name"]),
                )

    return live_rows


@app.get("/api/projects/{project_id}/outputs/{filename}")
def stream_output(project_id: int, filename: str, request: Request):
    resolve_project(project_id)
    with db() as connection:
        row = connection.execute(
            "SELECT file_path FROM combined_videos WHERE project_id = ? AND name = ?",
            (project_id, filename),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Output video not found")

    video = Path(row["file_path"])
    if not video.exists():
        raise HTTPException(status_code=404, detail="Output file is missing")

    file_size = video.stat().st_size
    range_header = request.headers.get("range")
    start, end, status_code = 0, file_size - 1, 200

    if range_header:
        try:
            range_value = range_header.lower().replace("bytes=", "")
            range_start, range_end = range_value.split("-", 1)
            start = int(range_start) if range_start else max(file_size - int(range_end), 0)
            end = int(range_end) if range_end else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")
            status_code = 206
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")

    def iter_file():
        with video.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": "video/mp4",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type="video/mp4")


@app.delete("/api/projects/{project_id}/outputs/{filename}")
def delete_output(project_id: int, filename: str):
    resolve_project(project_id)

    video = None
    with db() as connection:
        row = connection.execute(
            "SELECT file_path FROM combined_videos WHERE project_id = ? AND name = ?",
            (project_id, filename),
        ).fetchone()

        if row is not None:
            video = Path(row["file_path"])

        connection.execute(
            "DELETE FROM combined_videos WHERE project_id = ? AND name = ?",
            (project_id, filename),
        )

    if video is not None and video.exists():
        video.unlink()
    elif not video:
        project = resolve_project(project_id)
        fallback = Path(project["output_video_folder"])
        fallback_video = fallback / filename
        if fallback_video.exists():
            fallback_video.unlink()

    return {"success": True, "name": filename}


class FolderSelectionRequest(BaseModel):
    title: str = "Select a folder"


@app.get("/api/browse")
def browse_folders(path: str = ""):
    if path:
        current = Path(path)
        if os.name == "nt":
            current = Path(str(current).replace("/", "\\"))
    elif os.name == "nt":
        current = Path.home()
    else:
        current = Path("/")

    if not current.exists() or not current.is_dir():
        raise HTTPException(status_code=400, detail="Folder does not exist")

    if os.name == "nt":
        drives = []
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                drives.append({"name": f"{letter}:\\", "path": str(drive)})
    else:
        drives = [{"name": "/", "path": "/"}]

    try:
        directories = []
        for item in current.iterdir():
            try:
                if item.is_dir():
                    directories.append({"name": item.name, "path": str(item.resolve())})
            except OSError:
                continue
        directories.sort(key=lambda item: item["name"].lower())
    except PermissionError:
        raise HTTPException(status_code=403, detail="Folder access is not allowed")

    parent = str(current.parent.resolve()) if current.parent != current else ""
    return {
        "current": str(current.resolve()),
        "parent": parent,
        "drives": drives,
        "directories": directories,
    }


@app.get("/api/projects/{project_id}/replace-audio/assets")
def replace_audio_assets(project_id: int):
    project = resolve_project(project_id)

    audio_folder = Path(project["input_audio_folder"])
    output_folder = Path(project["output_video_folder"])

    audio_extensions = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
    audios = []
    if audio_folder.exists():
        audios = sorted(
            (path for path in audio_folder.iterdir()
             if path.is_file() and path.suffix.lower() in audio_extensions),
            key=lambda path: path.name.lower(),
        )

    outputs = []
    if output_folder.exists():
        outputs = sorted(
            (path for path in output_folder.iterdir()
             if path.is_file() and path.suffix.lower() == ".mp4"
             and not path.name.startswith("replaced_")
             and not path.name.startswith("replaced-audio-")),
            key=lambda path: path.name.lower(),
        )

    with db() as connection:
        replaced_rows = connection.execute(
            "SELECT * FROM replaced_videos WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        ).fetchall()

    return {
        "audios": [{"name": path.name, "size_mb": round(path.stat().st_size / (1024 * 1024), 2)} for path in audios],
        "outputs": [{"name": path.name, "size_mb": round(path.stat().st_size / (1024 * 1024), 2)} for path in outputs],
        "replaced": [dict(row) for row in replaced_rows],
    }


@app.post("/api/projects/{project_id}/replace-audio")
async def replace_project_audio(project_id: int, request: Request):
    project = resolve_project(project_id)
    data = await request.json()
    audio_name = str(data.get("audio_name", "")).strip()
    video_name = str(data.get("video_name", "")).strip()

    if not audio_name or not video_name:
        raise HTTPException(status_code=400, detail="Select one MP3 and one MP4")

    audio_folder = Path(project["input_audio_folder"])
    output_folder = Path(project["output_video_folder"])
    audio_file = audio_folder / audio_name
    video_file = output_folder / video_name

    if not audio_file.is_file():
        raise HTTPException(status_code=404, detail="Selected audio file not found")
    if not video_file.is_file():
        raise HTTPException(status_code=404, detail="Selected output MP4 not found")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    output_folder.mkdir(parents=True, exist_ok=True)
    unique_name = f"replaced_{uuid.uuid4().hex[:12]}_{video_file.stem}.mp4"
    output_file = output_folder / unique_name

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(video_file),
        "-stream_loop", "-1",
        "-i", str(audio_file),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_file),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        if output_file.exists():
            output_file.unlink()
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-1000:]}")

    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO replaced_videos
               (project_id, name, file_path, source_video_name, source_audio_name)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, unique_name, str(output_file.resolve()), video_name, audio_name),
        )
        replaced_id = cursor.lastrowid

    return {
        "id": replaced_id,
        "name": unique_name,
        "file_path": str(output_file.resolve()),
        "source_video_name": video_name,
        "source_audio_name": audio_name,
    }


@app.get("/api/projects/{project_id}/replaced/{filename}")
def stream_replaced(project_id: int, filename: str, request: Request):
    resolve_project(project_id)
    with db() as connection:
        row = connection.execute(
            "SELECT file_path FROM replaced_videos WHERE project_id = ? AND name = ?",
            (project_id, filename),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Replaced video not found")

    video = Path(row["file_path"])
    if not video.exists():
        raise HTTPException(status_code=404, detail="Replaced video file is missing")

    file_size = video.stat().st_size
    range_header = request.headers.get("range")
    start, end, status_code = 0, file_size - 1, 200

    if range_header:
        try:
            range_value = range_header.lower().replace("bytes=", "")
            range_start, range_end = range_value.split("-", 1)
            start = int(range_start) if range_start else max(file_size - int(range_end), 0)
            end = int(range_end) if range_end else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")
            status_code = 206
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")

    def iter_file():
        with video.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Type": "video/mp4",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type="video/mp4")


@app.delete("/api/projects/{project_id}/replaced/{filename}")
def delete_replaced(project_id: int, filename: str):
    resolve_project(project_id)
    with db() as connection:
        row = connection.execute(
            "SELECT file_path FROM replaced_videos WHERE project_id = ? AND name = ?",
            (project_id, filename),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Replaced video not found")
        video = Path(row["file_path"])
        connection.execute(
            "DELETE FROM replaced_videos WHERE project_id = ? AND name = ?",
            (project_id, filename),
        )

    if video.exists():
        video.unlink()
    return {"success": True, "name": filename}


@app.get("/api/projects/{project_id}/source-audio/{filename}")
def stream_source_audio(project_id: int, filename: str, request: Request):
    project = resolve_project(project_id)
    audio_folder = Path(project["input_audio_folder"])
    audio_file = audio_folder / filename
    if not audio_file.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    file_size = audio_file.stat().st_size
    range_header = request.headers.get("range")
    start, end, status_code = 0, file_size - 1, 200

    if range_header:
        try:
            range_value = range_header.lower().replace("bytes=", "")
            range_start, range_end = range_value.split("-", 1)
            start = int(range_start) if range_start else max(file_size - int(range_end), 0)
            end = int(range_end) if range_end else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")
            status_code = 206
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")

    media_type = "audio/mpeg"
    if audio_file.suffix.lower() in {".wav"}: media_type = "audio/wav"
    elif audio_file.suffix.lower() in {".m4a"}: media_type = "audio/mp4"
    elif audio_file.suffix.lower() in {".aac"}: media_type = "audio/aac"
    elif audio_file.suffix.lower() in {".flac"}: media_type = "audio/flac"
    elif audio_file.suffix.lower() in {".ogg"}: media_type = "audio/ogg"

    def iter_file():
        with audio_file.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Content-Type": media_type}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type=media_type)


@app.get("/api/projects/{project_id}/source-output/{filename}")
def stream_source_output(project_id: int, filename: str, request: Request):
    project = resolve_project(project_id)
    video = Path(project["output_video_folder"]) / filename
    if not video.is_file():
        raise HTTPException(status_code=404, detail="Output MP4 not found")

    file_size = video.stat().st_size
    range_header = request.headers.get("range")
    start, end, status_code = 0, file_size - 1, 200

    if range_header:
        try:
            range_value = range_header.lower().replace("bytes=", "")
            range_start, range_end = range_value.split("-", 1)
            start = int(range_start) if range_start else max(file_size - int(range_end), 0)
            end = int(range_end) if range_end else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                raise HTTPException(status_code=416, detail="Range not satisfiable")
            status_code = 206
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")

    def iter_file():
        with video.open("rb") as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = file.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Content-Type": "video/mp4"}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type="video/mp4")


PROMPT_COLUMNS = "id, created_on, model, instructions, video_file, text_context"
DEFAULT_AUDIO_MODEL = "lyria-3.5"
DEFAULT_PROMPT_LLM = "gemini-3.8-flash"


class AudioPromptCreate(BaseModel):
    model: str
    instructions: str
    video_file: str = ""
    text_context: str = ""


class GenerateAudioPromptRequest(BaseModel):
    model_id: str = DEFAULT_PROMPT_LLM
    video_name: str
    text_context: str = ""


def insert_audio_prompt(model, instructions, video_file="", text_context=""):
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO audio_prompts (model, instructions, video_file, text_context) VALUES (?, ?, ?, ?)",
            (model, instructions, video_file, text_context),
        )
        row = connection.execute(
            f"SELECT {PROMPT_COLUMNS} FROM audio_prompts WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return dict(row)


@app.get("/api/audio-prompts")
def get_audio_prompts():
    with db() as connection:
        rows = connection.execute(
            f"SELECT {PROMPT_COLUMNS} FROM audio_prompts ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/audio-prompts")
def create_audio_prompt(payload: AudioPromptCreate):
    model = payload.model.strip()
    instructions = payload.instructions.strip()

    if not model or not instructions:
        raise HTTPException(status_code=400, detail="Model name and instructions are required")

    return insert_audio_prompt(model, instructions, payload.video_file.strip(), payload.text_context.strip())


def generate_timestamped_music_prompt(video_path: str, user_context: str, model_id: str, api_key: str) -> str:
    """Upload a video, analyze scenes with user context, return a timestamped Lyria prompt."""
    import time
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    video_file = client.files.upload(file=video_path)
    try:
        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            video_file = client.files.get(name=video_file.name)
        if video_file.state.name == "FAILED":
            raise RuntimeError(f"Video processing failed: {video_file.error}")

        instruction = f"""
Analyze the attached video chronologically from start to finish and generate a scene-by-scene
music and foley prompt formatted for Google Flow / Lyria audio generation.

Additional User Context:
{user_context}

Output Requirements:
1. Start the final prompt string with: "Instrumental only, no vocals."
2. Break the video down into logical scenes using exact [MM:SS - MM:SS] timestamps.
3. For each timestamped segment, describe:
   - Musical genre, tempo/energy, and specific instrumentation matching the scene's pacing.
   - Realistic foley and natural sound effects synchronized to the on-screen action.
4. Keep descriptions vivid, concrete, and formatted as a single ready-to-use prompt block.
"""
        response = client.models.generate_content(
            model=model_id,
            contents=[video_file, instruction],
            config=types.GenerateContentConfig(temperature=0.4),
        )
        return response.text or ""
    finally:
        try:
            client.files.delete(name=video_file.name)
        except Exception:
            pass


def clean_llm_prompt(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


@app.post("/api/projects/{project_id}/generate-audio-prompt")
def generate_audio_prompt(project_id: int, payload: GenerateAudioPromptRequest, request: Request):
    resolve_project(project_id)
    model_id = payload.model_id.strip() or DEFAULT_PROMPT_LLM
    video_name = payload.video_name.strip()
    text_context = payload.text_context.strip()

    if not video_name:
        raise HTTPException(status_code=400, detail="Select an output video")

    with db() as connection:
        row = connection.execute(
            "SELECT file_path FROM combined_videos WHERE project_id = ? AND name = ?",
            (project_id, video_name),
        ).fetchone()
    if row is None or not Path(row["file_path"]).is_file():
        raise HTTPException(status_code=404, detail="Selected output video not found")

    api_key = get_user_gemini_key(request)
    try:
        import google.genai  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="google-genai is not installed")

    try:
        result = generate_timestamped_music_prompt(row["file_path"], text_context, model_id, api_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Prompt generation failed: {exc}") from exc

    instructions = clean_llm_prompt(result)
    if not instructions:
        raise HTTPException(status_code=502, detail="The model returned an empty prompt")

    return insert_audio_prompt(DEFAULT_AUDIO_MODEL, instructions, video_name, text_context)


class AudioPromptUpdate(BaseModel):
    model: str
    instructions: str


@app.put("/api/audio-prompts/{prompt_id}")
def update_audio_prompt(prompt_id: int, payload: AudioPromptUpdate):
    model = payload.model.strip()
    instructions = payload.instructions.strip()
    if not model or not instructions:
        raise HTTPException(status_code=400, detail="Model name and instructions are required")
    with db() as connection:
        cursor = connection.execute(
            "UPDATE audio_prompts SET model = ?, instructions = ? WHERE id = ?",
            (model, instructions, prompt_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Audio prompt not found")
        row = connection.execute(
            f"SELECT {PROMPT_COLUMNS} FROM audio_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
    return dict(row)


@app.delete("/api/audio-prompts/{prompt_id}")
def delete_audio_prompt(prompt_id: int):
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM audio_prompts WHERE id = ?",
            (prompt_id,),
        )
    return {"success": True, "id": prompt_id, "deleted": cursor.rowcount > 0}


@app.post("/api/projects/{project_id}/generate-music")
async def generate_music(project_id: int, request: Request):
    project = resolve_project(project_id)
    data = await request.json()
    try:
        prompt_id = int(data.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Select one audio prompt")

    with db() as connection:
        row = connection.execute(
            "SELECT id, model, instructions FROM audio_prompts WHERE id = ?",
            (prompt_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Audio prompt not found")

    api_key = get_user_gemini_key(request)

    try:
        from google import genai
    except ImportError:
        raise HTTPException(status_code=500, detail="google-genai is not installed")

    audio_folder = Path(project["input_audio_folder"])
    audio_folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m%d%Y%H%M")
    filename = f"{row['id']}_{stamp}.mp3"
    output_path = audio_folder / filename
    if output_path.exists():
        stamp = datetime.now().strftime("%m%d%Y%H%M%S")
        filename = f"{row['id']}_{stamp}.mp3"
        output_path = audio_folder / filename

    try:
        client = genai.Client(api_key=api_key)
        interaction = client.interactions.create(
            model=row["model"],
            input=row["instructions"],
            response_format={"type": "audio"},
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Audio generation failed: {exc}") from exc

    generated_audio = getattr(interaction, "output_audio", None)
    audio_data = getattr(generated_audio, "data", None) if generated_audio else None
    if not audio_data:
        raise HTTPException(
            status_code=502,
            detail="No audio was returned. Check your API billing tier or prompt safety filters.",
        )

    output_path.write_bytes(base64.b64decode(audio_data))
    return {
        "name": filename,
        "model": row["model"],
        "stream_url": f"/api/projects/{project_id}/source-audio/{quote(filename, safe='')}",
    }


def sync_existing_combined_outputs(project):
    output_folder = Path(project["output_video_folder"])
    if not output_folder.exists():
        return

    import json
    with db() as connection:
        known = {
            row["file_path"]
            for row in connection.execute(
                "SELECT file_path FROM combined_videos WHERE project_id = ?",
                (project["id"],),
            ).fetchall()
        }

        for video in sorted(output_folder.glob("combined_*.mp4"), key=lambda path: path.name.lower()):
            file_path = str(video.resolve())
            if file_path in known:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO combined_videos
                   (project_id, name, file_path, sequence_json)
                   VALUES (?, ?, ?, ?)""",
                (project["id"], video.name, file_path, json.dumps([])),
            )


# ---------------------------------------------------------------------------
# Video prompts (image-based, one record per scene)
# ---------------------------------------------------------------------------
VIDEO_PROMPT_COLUMNS = "id, created_on, model, instructions, image_files, text_context, parts, scene_number, series_id"
DEFAULT_VIDEO_MODEL = "veo-3.1"
DEFAULT_VIDEO_PROMPT_LLM = "gemini-2.5-pro"
MAX_PROMPT_IMAGES = 10
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PARTS = 60
MAX_SCENE_NUMBER = 999


class ScenePrompt(BaseModel):
    scene_number: int = Field(description="Scene number, starting at 1.")
    has_dialogue: bool = Field(
        default=False,
        description="True when this scene contains spoken dialogue and lip-sync; False otherwise.",
    )
    cinematography: str = Field(
        description="Shot type, camera angle and movement/transitions, lens, depth of field, focus and motion details."
    )
    subject: str = Field(
        description=(
            "Scene-specific subject state and exact character count in frame: pose, expression, props, "
            "physical continuity details (e.g., mud, wings, clothing), and posture constraints."
        )
    )
    action: str = Field(
        description=(
            "Specific physical action covering an 8-second clip (maximum 8 seconds; never exceed 00:08). "
            "Use timestamped beats (e.g. '[00:00-00:02]', '[00:02-00:05]', '[00:05-00:08]') for multi-stage sequences "
            "or when requested; use vivid continuous prose for single-beat or dialogue scenes. "
            "If dialogue is requested, include spoken lines in quotes with explicit mouth/lip-sync cues kept concise "
            "enough to fit comfortably within 8 seconds."
        )
    )
    context: str = Field(
        description="Environment, setting, background elements, weather/atmosphere, and spatial continuity."
    )
    style_and_ambiance: str = Field(
        description="Visual medium/film look, lighting setup, atmosphere, color palette, and visual continuity."
    )
    audio: str = Field(
        description=(
            "Ambient sound, synchronized SFX, voice descriptions (if dialogue), and music/score cues or "
            "explicit 'No background music.' / 'No dialogue, no subtitles.' exclusions as appropriate."
        )
    )


class VideoSeriesScript(BaseModel):
    master_style_and_characters: str = Field(
        description=(
            "Shared visual bible prepended to every scene: exact art medium/film aesthetic, lighting style, "
            "and exhaustive physical details (build, hair, eyes, clothing, textures, colors, accessories) of every "
            "recurring character/subject from the reference image(s) or user instructions. Include lip-sync mouth "
            "articulation details when dialogue is requested, and end with 'No subtitles, no on-screen text.' "
            "unless on-screen text was requested."
        )
    )
    scenes: list[ScenePrompt] = Field(description="The requested number of standalone scenes, in story order.")


class VideoPromptCreate(BaseModel):
    model: str
    scene_number: int
    instructions: str


class ReferenceImage(BaseModel):
    name: str
    mime_type: str
    data: str  # base64 (no data-URL prefix)


class GenerateVideoPromptRequest(BaseModel):
    model_id: str = DEFAULT_VIDEO_PROMPT_LLM
    parts: int = 30
    text_context: str = ""
    images: list[ReferenceImage] = Field(default_factory=list)


def insert_video_prompt(connection, model, instructions, scene_number, image_files="", text_context="", parts=0, series_id=0):
    cursor = connection.execute(
        """INSERT INTO video_prompts
           (model, instructions, image_files, text_context, parts, scene_number, series_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (model, instructions, image_files, text_context, parts, scene_number, series_id),
    )
    new_id = cursor.lastrowid
    if not series_id:
        series_id = new_id
        connection.execute("UPDATE video_prompts SET series_id = ? WHERE id = ?", (series_id, new_id))
    row = connection.execute(
        f"SELECT {VIDEO_PROMPT_COLUMNS} FROM video_prompts WHERE id = ?", (new_id,)
    ).fetchone()
    return dict(row)


@app.get("/api/video-prompts")
def get_video_prompts():
    with db() as connection:
        rows = connection.execute(
            f"SELECT {VIDEO_PROMPT_COLUMNS} FROM video_prompts ORDER BY series_id DESC, scene_number, id"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/video-prompts")
def create_video_prompt(payload: VideoPromptCreate):
    model = payload.model.strip()
    instructions = payload.instructions.strip()
    if not model or not instructions:
        raise HTTPException(status_code=400, detail="Model name and instructions are required")
    if not 1 <= payload.scene_number <= MAX_SCENE_NUMBER:
        raise HTTPException(status_code=400, detail=f"Scene must be between 1 and {MAX_SCENE_NUMBER}")
    with db() as connection:
        return insert_video_prompt(connection, model, instructions, payload.scene_number)


@app.put("/api/video-prompts/{prompt_id}")
def update_video_prompt(prompt_id: int, payload: VideoPromptCreate):
    model = payload.model.strip()
    instructions = payload.instructions.strip()
    if not model or not instructions:
        raise HTTPException(status_code=400, detail="Model name and instructions are required")
    if not 1 <= payload.scene_number <= MAX_SCENE_NUMBER:
        raise HTTPException(status_code=400, detail=f"Scene must be between 1 and {MAX_SCENE_NUMBER}")
    with db() as connection:
        cursor = connection.execute(
            "UPDATE video_prompts SET model = ?, scene_number = ?, instructions = ? WHERE id = ?",
            (model, payload.scene_number, instructions, prompt_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Video prompt not found")
        row = connection.execute(
            f"SELECT {VIDEO_PROMPT_COLUMNS} FROM video_prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
    return dict(row)


@app.delete("/api/video-prompts/{prompt_id}")
def delete_video_prompt(prompt_id: int):
    with db() as connection:
        cursor = connection.execute("DELETE FROM video_prompts WHERE id = ?", (prompt_id,))
    return {"success": True, "id": prompt_id, "deleted": cursor.rowcount > 0}


def generate_structured_video_prompts(images, user_prompt: str, parts: int, model_id: str, api_key: str) -> VideoSeriesScript:
    """Send optional reference images + user instructions to Gemini and return a validated scene script."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    image_guidance = (
        "Reference image(s) are attached. Analyze them in extreme visual detail to lock in the exact visual medium, "
        "textures, outfits, colors, proportions, and character/subject appearances in 'master_style_and_characters'."
        if images
        else "No reference image is attached. Establish a cohesive, vivid visual medium and exhaustive character/subject "
        "descriptions in 'master_style_and_characters' directly from the user's instructions."
    )
    system_instruction = (
        "You are an expert AI video director, screenwriter, and cinematographer specializing in structured Google Veo / Flow prompts.\n"
        f"{image_guidance}\n\n"
        f"Generate exactly {parts} standalone, self-contained scene prompts in order. Every scene is generated as an "
        "independent video request, so each scene must stand on its own while maintaining visual and story continuity.\n\n"
        "Adapt every aspect of the prompts to match the user's instructions:\n"
        "1. Format & Genre: Match whatever style is shown in the reference image(s) or requested by the user "
        "(e.g. photorealistic 35mm cinema, stop-motion felt/fabric animation, 3D animation, noir, fantasy, action, etc.).\n"
        "2. Story Structure: Support either a continuous multi-part narrative (preserving exact props, character counts, "
        "weather, lighting progression, and physical continuity from scene to scene) OR standalone episodic adventures "
        "sharing the same cast, according to the user's request.\n"
        "3. Dialogue & Lip-Sync: If the user asks for dialogue or lip-sync, set has_dialogue=true, include natural spoken "
        "lines in quotes with explicit mouth/lip-sync cues sized to fit cleanly inside the clip duration, describe each "
        "character's voice in 'audio', and mention articulated lip-syncing mouths in 'master_style_and_characters'. "
        "If dialogue is not requested, set has_dialogue=false, omit spoken lines, and end 'audio' with 'No dialogue, no subtitles.'\n"
        "4. Clip Duration & Pacing (STRICT 8-SECOND MAX): The video generation API creates clips up to a maximum of "
        "8 seconds (supported durations: 4, 6, or 8 seconds). By default, design every scene strictly for an 8-second "
        "duration—never design a scene longer than 8 seconds (only use 4s or 6s if the user explicitly requests a shorter "
        "clip). Keep actions and spoken dialogue concise enough to fit cleanly within 8 seconds. Use timestamped beats "
        "bounded within [00:00-00:08] (e.g. [00:00-00:02], [00:02-00:05], [00:05-00:08]) in 'action' whenever a scene has "
        "multi-stage transitions or the user asks for timed beats; use vivid continuous action prose for single-beat or "
        "dialogue-focused scenes.\n"
        "5. Audio & Music: Explicitly anchor ambient sound and synchronized SFX. Include orchestral/musical score cues when "
        "appropriate or requested; explicitly write 'No background music.' when the user wants natural SFX/dialogue only or "
        "plans to add music separately.\n"
        "6. Precision & Positive Framing: Prefer positive visual descriptions of what is in frame. Explicitly state exact "
        "character counts and physical posture constraints when needed to prevent continuity drift. End "
        "'master_style_and_characters' with 'No subtitles, no on-screen text.' unless on-screen text is requested."
    )
    contents = [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in images]
    contents.append(user_prompt)

    response = client.models.generate_content(
        model=model_id,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=VideoSeriesScript,
            temperature=0.7,
            max_output_tokens=min(65535, max(8192, 900 * parts + 2000)),
        ),
    )
    return VideoSeriesScript.model_validate_json(response.text)


def format_video_scene(master_style: str, scene: ScenePrompt) -> str:
    """Format one scene with the shared visual bible prepended for individual Veo requests."""
    action_label = "Action & Lip-Sync" if scene.has_dialogue else "Action"
    parts = []
    if master_style.strip():
        parts.append(f"[Master Style & Characters]: {master_style.strip()}")
    parts.extend([
        f"[Cinematography]: {scene.cinematography.strip()}",
        f"[Subject]: {scene.subject.strip()}",
        f"[{action_label}]: {scene.action.strip()}",
        f"[Context]: {scene.context.strip()}",
        f"[Style & Ambiance]: {scene.style_and_ambiance.strip()}",
        f"[Audio]: {scene.audio.strip()}",
    ])
    return "\n\n".join(parts)


@app.post("/api/generate-video-prompt")
def generate_video_prompt(payload: GenerateVideoPromptRequest, request: Request):
    model_id = payload.model_id.strip() or DEFAULT_VIDEO_PROMPT_LLM
    text_context = payload.text_context.strip()
    parts = payload.parts

    if not 1 <= parts <= MAX_PARTS:
        raise HTTPException(status_code=400, detail=f"Number of parts must be between 1 and {MAX_PARTS}")
    if not payload.images and not text_context:
        raise HTTPException(status_code=400, detail="Provide text instructions or upload at least one reference image")
    if len(payload.images) > MAX_PROMPT_IMAGES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_PROMPT_IMAGES} images are allowed")

    images = []
    for image in payload.images:
        if not image.mime_type.lower().startswith("image/"):
            raise HTTPException(status_code=400, detail=f"{image.name} is not an image")
        try:
            data = base64.b64decode(image.data, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{image.name} could not be decoded")
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail=f"{image.name} is empty or larger than 15 MB")
        images.append((data, image.mime_type))

    api_key = get_user_gemini_key(request)
    try:
        import google.genai  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="google-genai is not installed")

    user_prompt = (
        f"Create a {parts}-part video generation prompt series. Each scene must be individually created "
        f"as a standalone clip with a maximum duration of 8 seconds (default 8 seconds).\n\n"
        f"User instructions:\n{text_context or 'Create an engaging series featuring the subject(s) in the reference image(s).'}"
    )

    try:
        series = generate_structured_video_prompts(images, user_prompt, parts, model_id, api_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Prompt generation failed: {exc}") from exc

    if len(series.scenes) < parts:
        raise HTTPException(
            status_code=502,
            detail=f"The model returned {len(series.scenes)} scenes but {parts} were requested. Try again or lower the number of parts.",
        )

    image_names = ", ".join(image.name for image in payload.images)
    records = []
    with db() as connection:
        series_id = 0
        for number, scene in enumerate(series.scenes[:parts], start=1):
            record = insert_video_prompt(
                connection, DEFAULT_VIDEO_MODEL, format_video_scene(series.master_style_and_characters, scene), number,
                image_names, text_context, parts, series_id,
            )
            series_id = record["series_id"]
            records.append(record)
    return records


DEFAULT_VEO_API_MODEL = "veo-3.1-generate-preview"
ALLOWED_VEO_DURATIONS = {4, 6, 8}
ALLOWED_ASPECT_RATIOS = {"16:9", "9:16"}


class GenerateSceneVideoRequest(BaseModel):
    prompt_id: int
    model: str = DEFAULT_VEO_API_MODEL
    aspect_ratio: str = "16:9"
    duration_seconds: int = 8
    image: ReferenceImage | None = None


def generate_scene_video(
    prompt: str,
    output_path: Path,
    model: str = DEFAULT_VEO_API_MODEL,
    reference_image: tuple[bytes, str] | None = None,
    aspect_ratio: str = "16:9",
    duration_seconds: int = 8,
    api_key: str = "",
):
    """Generates a single video clip with synchronized audio using Google Veo."""
    import time
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    image_input = None
    if reference_image is not None:
        image_bytes, mime_type = reference_image
        image_input = types.Image(image_bytes=image_bytes, mime_type=mime_type)

    operation = client.models.generate_videos(
        model=model,
        prompt=prompt,
        image=image_input,
        config=types.GenerateVideosConfig(
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_seconds,
            person_generation="allow_adult",
        ),
    )

    while not operation.done:
        time.sleep(10)
        operation = client.operations.get(operation)

    if not operation.response or not operation.response.generated_videos:
        raise RuntimeError(
            "Video generation completed without returning a video. "
            "Check safety filters or prompt phrasing."
        )

    generated_video = operation.response.generated_videos[0]
    client.files.download(file=generated_video.video)
    generated_video.video.save(str(output_path))


@app.post("/api/projects/{project_id}/generate-video")
def generate_project_video(project_id: int, payload: GenerateSceneVideoRequest, request: Request):
    project = resolve_project(project_id)

    if payload.duration_seconds not in ALLOWED_VEO_DURATIONS:
        raise HTTPException(status_code=400, detail="Duration must be 4, 6, or 8 seconds (max 8s)")
    if payload.aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail="Aspect ratio must be 16:9 or 9:16")

    with db() as connection:
        row = connection.execute(
            f"SELECT {VIDEO_PROMPT_COLUMNS} FROM video_prompts WHERE id = ?",
            (payload.prompt_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Video prompt not found")

    reference_image = None
    if payload.image is not None:
        if not payload.image.mime_type.lower().startswith("image/"):
            raise HTTPException(status_code=400, detail=f"{payload.image.name} is not an image")
        try:
            image_bytes = base64.b64decode(payload.image.data, validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{payload.image.name} could not be decoded")
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail=f"{payload.image.name} is empty or larger than 15 MB")
        reference_image = (image_bytes, payload.image.mime_type)

    api_key = get_user_gemini_key(request)
    try:
        import google.genai  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="google-genai is not installed")

    model = payload.model.strip() or DEFAULT_VEO_API_MODEL
    input_video_folder = Path(project["input_video_folder"])
    input_video_folder.mkdir(parents=True, exist_ok=True)

    prefix = f"scene_{row['scene_number']}" if row["scene_number"] else f"prompt_{row['id']}"
    stamp = datetime.now().strftime("%m%d%Y%H%M")
    filename = f"{prefix}_{stamp}.mp4"
    output_path = input_video_folder / filename
    if output_path.exists():
        stamp = datetime.now().strftime("%m%d%Y%H%M%S")
        filename = f"{prefix}_{stamp}.mp4"
        output_path = input_video_folder / filename

    try:
        generate_scene_video(
            prompt=row["instructions"],
            output_path=output_path,
            model=model,
            reference_image=reference_image,
            aspect_ratio=payload.aspect_ratio,
            duration_seconds=payload.duration_seconds,
            api_key=api_key,
        )
    except Exception as exc:
        if output_path.exists() and output_path.stat().st_size == 0:
            output_path.unlink()
        raise HTTPException(status_code=502, detail=f"Video generation failed: {exc}") from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        if output_path.exists():
            output_path.unlink()
        raise HTTPException(status_code=502, detail="Video generation produced an empty file")

    return {
        "name": filename,
        "scene_number": row["scene_number"],
        "model": model,
        "stream_url": f"/api/projects/{project_id}/video/{quote(filename, safe='')}",
    }


class GeminiKeyUpdate(BaseModel):
    api_key: str


def mask_api_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def get_user_gemini_key(request: Request) -> str:
    """Return the logged-in user's decrypted Gemini API key or raise a helpful error."""
    session = getattr(request.state, "session", None)
    if session is None or session["role"] != "user":
        raise HTTPException(status_code=403, detail="Only registered users can use Gemini features")
    with db() as connection:
        row = connection.execute(
            "SELECT gemini_key_enc FROM app_users WHERE id = ?", (session["user_id"],)
        ).fetchone()
    key = decrypt_text(row["gemini_key_enc"]) if row and row["gemini_key_enc"] else ""
    if not key:
        raise HTTPException(status_code=400, detail="Add your Gemini API key with the Manage Gemini Key button first")
    return key


def _require_user_session(request: Request):
    session = getattr(request.state, "session", None)
    if session is None or session["role"] != "user":
        raise HTTPException(status_code=403, detail="Only registered users can manage a Gemini key")
    return session


@app.get("/api/gemini-key")
def get_gemini_key_status(request: Request):
    session = _require_user_session(request)
    with db() as connection:
        row = connection.execute(
            "SELECT gemini_key_enc FROM app_users WHERE id = ?", (session["user_id"],)
        ).fetchone()
    key = decrypt_text(row["gemini_key_enc"]) if row and row["gemini_key_enc"] else ""
    return {"configured": bool(key), "masked": mask_api_key(key)}


@app.post("/api/gemini-key")
@app.put("/api/gemini-key")
def set_gemini_key(payload: GeminiKeyUpdate, request: Request):
    session = _require_user_session(request)
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="Gemini API key cannot be empty")
    with db() as connection:
        connection.execute(
            "UPDATE app_users SET gemini_key_enc = ? WHERE id = ?",
            (encrypt_text(key), session["user_id"]),
        )
    return {"configured": True, "masked": mask_api_key(key)}


@app.delete("/api/gemini-key")
def clear_gemini_key(request: Request):
    session = _require_user_session(request)
    with db() as connection:
        connection.execute("UPDATE app_users SET gemini_key_enc = '' WHERE id = ?", (session["user_id"],))
    return {"configured": False, "masked": ""}


# ---------------------------------------------------------------------------
# Crop media (remove parts from the start, end or middle of a video/audio file)
# ---------------------------------------------------------------------------
CROP_VIDEO_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
}
CROP_AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
}
MIN_KEEP_SECONDS = 0.05


def crop_types(kind: str) -> dict:
    if kind == "video":
        return CROP_VIDEO_TYPES
    if kind == "audio":
        return CROP_AUDIO_TYPES
    if kind == "image":
        return CROP_IMAGE_TYPES
    raise HTTPException(status_code=400, detail="Kind must be 'video', 'audio', or 'image'")


def resolve_media_file(path: str, kind: str) -> Path:
    types = crop_types(kind)
    if not path:
        raise HTTPException(status_code=400, detail="Select a media file")
    file = Path(path)
    if not file.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    if file.suffix.lower() not in types:
        raise HTTPException(status_code=400, detail=f"Unsupported {kind} file type '{file.suffix}'")
    return file.resolve()


def probe_media(ffmpeg: str, file: Path) -> dict:
    """Read duration and stream info from `ffmpeg -i` output."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-i", str(file)], capture_output=True, text=True)
    info = result.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", info)
    duration = 0.0
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    has_video = bool(re.search(r"Stream #\S+.*Video:", info)) and "attached pic" not in info
    has_audio = bool(re.search(r"Stream #\S+.*Audio:", info))
    return {"duration": duration, "has_video": has_video, "has_audio": has_audio}


@app.get("/api/media/browse")
def browse_media(kind: str, path: str = ""):
    types = crop_types(kind)
    current = Path(path) if path else Path.home()
    if not current.exists() or not current.is_dir():
        raise HTTPException(status_code=400, detail="Folder does not exist")

    drives = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                drives.append({"name": f"{letter}:\\", "path": str(drive)})

    directories, files = [], []
    try:
        for item in current.iterdir():
            try:
                if item.is_dir():
                    directories.append({"name": item.name, "path": str(item.resolve())})
                elif item.is_file() and item.suffix.lower() in types:
                    files.append({
                        "name": item.name,
                        "path": str(item.resolve()),
                        "size_mb": round(item.stat().st_size / (1024 * 1024), 2),
                    })
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="Folder access is not allowed")
    directories.sort(key=lambda entry: entry["name"].lower())
    files.sort(key=lambda entry: entry["name"].lower())
    parent = str(current.parent.resolve()) if current.parent != current else ""
    return {"current": str(current.resolve()), "parent": parent, "drives": drives,
            "directories": directories, "files": files}


@app.get("/api/media/stream")
def stream_media(kind: str, path: str, request: Request):
    file = resolve_media_file(path, kind)
    media_type = crop_types(kind)[file.suffix.lower()]
    file_size = file.stat().st_size
    start, end, status_code = 0, file_size - 1, 200

    range_header = request.headers.get("range")
    if range_header:
        try:
            range_value = range_header.strip().lower().replace("bytes=", "")
            range_start, range_end = range_value.split("-", 1)
            start = int(range_start) if range_start else max(file_size - int(range_end), 0)
            end = int(range_end) if range_end and range_start else file_size - 1
            end = min(end, file_size - 1)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Range header")
        if start > end or start >= file_size:
            raise HTTPException(status_code=416, detail="Range not satisfiable",
                                headers={"Content-Range": f"bytes */{file_size}"})
        status_code = 206

    def iter_file():
        with file.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = handle.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Content-Type": media_type}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    return StreamingResponse(iter_file(), status_code=status_code, headers=headers, media_type=media_type)


@app.get("/api/media/info")
def media_info(kind: str, path: str):
    file = resolve_media_file(path, kind)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")
    return {"name": file.name, **probe_media(ffmpeg, file)}


class CropMediaRequest(BaseModel):
    kind: str
    path: str
    remove: list[list[float]]


@app.post("/api/media/crop")
def crop_media(payload: CropMediaRequest, request: Request = None, queued: bool = False):
    file = resolve_media_file(payload.path, payload.kind)
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    info = probe_media(ffmpeg, file)
    duration = info["duration"]
    if duration <= 0:
        raise HTTPException(status_code=400, detail="Could not read the media duration")
    if payload.kind == "video" and not info["has_video"]:
        raise HTTPException(status_code=400, detail="The selected file has no video stream")
    if payload.kind == "audio" and not info["has_audio"]:
        raise HTTPException(status_code=400, detail="The selected file has no audio stream")

    # Normalize removed ranges: validate, clamp to [0, duration], sort and merge overlaps.
    ranges = []
    for item in payload.remove:
        if len(item) != 2 or not all(math.isfinite(value) for value in item):
            raise HTTPException(status_code=400, detail="Each removed part needs a start and end time")
        start, end = max(0.0, item[0]), min(duration, item[1])
        if end > start:
            ranges.append([start, end])
    if not ranges:
        raise HTTPException(status_code=400, detail="Mark at least one part to remove")
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    keep, cursor = [], 0.0
    for start, end in merged:
        if start - cursor >= MIN_KEEP_SECONDS:
            keep.append((cursor, start))
        cursor = end
    if duration - cursor >= MIN_KEEP_SECONDS:
        keep.append((cursor, duration))
    if not keep:
        raise HTTPException(status_code=400, detail="Nothing would remain after removing those parts")

    # Never overwrite: always create a new, unique file next to the original.
    extension = ".mp4" if payload.kind == "video" else file.suffix.lower()
    output_path = file.with_name(f"{file.stem}_cropped_{uuid.uuid4().hex[:8]}{extension}")
    while output_path.exists():
        output_path = file.with_name(f"{file.stem}_cropped_{uuid.uuid4().hex[:8]}{extension}")

    use_video = payload.kind == "video"
    use_audio = info["has_audio"]
    filters, labels = [], []
    for index, (start, end) in enumerate(keep):
        if use_video:
            filters.append(f"[0:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]")
            labels.append(f"[v{index}]")
        if use_audio:
            filters.append(f"[0:a:0]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]")
            labels.append(f"[a{index}]")
    outputs = ("[outv]" if use_video else "") + ("[outa]" if use_audio else "")
    filters.append(
        "".join(labels)
        + f"concat=n={len(keep)}:v={1 if use_video else 0}:a={1 if use_audio else 0}{outputs}"
    )

    command = [ffmpeg, "-hide_banner", "-y", "-i", str(file), "-filter_complex", ";".join(filters)]
    if use_video:
        command += ["-map", "[outv]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if use_audio:
        command += ["-map", "[outa]"]
        audio_codecs = {
            ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
            ".wav": ["-c:a", "pcm_s16le"],
            ".flac": ["-c:a", "flac"],
            ".ogg": ["-c:a", "libvorbis", "-q:a", "5"],
        }
        command += audio_codecs.get(extension, ["-c:a", "aac", "-b:a", "192k"])
    if extension in (".mp4", ".m4a", ".mov"):
        command += ["-movflags", "+faststart"]
    command.append(str(output_path))

    kept_seconds = sum(end - start for start, end in keep)

    def _run(on_progress=None):
        try:
            run_ffmpeg_with_progress(command, kept_seconds, on_progress)
        except subprocess.CalledProcessError as error:
            if output_path.exists():
                output_path.unlink()
            raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-800:]}")
        return {
            "name": output_path.name,
            "path": str(output_path),
            "folder": str(output_path.parent),
            "original_duration": round(duration, 3),
            "new_duration": round(kept_seconds, 3),
        }

    if queued:
        session = _require_user_session(request)
        tool_label = "Crop Video" if payload.kind == "video" else "Crop Audio"
        return JSONResponse(enqueue_media_job(session["user_id"], tool_label, file.name, _run), status_code=202)
    return _run()


# ---------------------------------------------------------------------------
# Video effects (timed segments, presets, color grading, textures & projects)
# ---------------------------------------------------------------------------
VIDEO_EFFECT_PRESETS = {"none", "grayscale", "sepia", "retro", "film"}
VIDEO_TRANSITION_TYPES = {
    "fade_black",
    "fade_white",
    "fade_in",
    "fade_out",
    "blur_dissolve",
    "wipe_left",
    "wipe_right",
    "zoom_flash",
}


class EffectTransition(BaseModel):
    start: float
    end: float
    type: str = "fade_black"


class EffectSettings(BaseModel):
    preset: str = "none"
    contrast: float = 1.0       # 0.5 to 2.0
    saturation: float = 1.0     # 0.0 to 2.0
    warmth: float = 0.0         # -1.0 to 1.0
    white_balance: float = 0.0  # -1.0 to 1.0 (magenta <-> green tint balance)
    light_leaks: float = 0.0    # 0.0 to 1.0
    film_grain: float = 0.0     # 0.0 to 1.0
    scratches: float = 0.0      # 0.0 to 1.0
    vhs: float = 0.0            # 0.0 to 1.0
    glitch: float = 0.0         # 0.0 to 1.0
    lens_flares: float = 0.0    # 0.0 to 1.0
    speed: float = 1.0          # 0.25 to 4.0 (1.0 = normal speed)


class EffectSegment(EffectSettings):
    start: float = 0.0
    end: float | None = None    # None means until the end of the video


class VideoEffectsRequest(EffectSettings):
    path: str
    segments: list[EffectSegment] = Field(default_factory=list)
    transitions: list[EffectTransition] = Field(default_factory=list)


class EffectsProjectPayload(BaseModel):
    name: str
    video_path: str = ""
    segments: list[EffectSegment] = Field(default_factory=list)
    transitions: list[EffectTransition] = Field(default_factory=list)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _has_visual_effect(settings: EffectSettings) -> bool:
    preset = (settings.preset or "none").strip().lower()
    return (
        preset != "none"
        or abs(_clamp(settings.contrast, 0.5, 2.0) - 1.0) > 0.005
        or abs(_clamp(settings.saturation, 0.0, 2.0) - 1.0) > 0.005
        or abs(_clamp(settings.warmth, -1.0, 1.0)) > 0.005
        or abs(_clamp(settings.white_balance, -1.0, 1.0)) > 0.005
        or any(
            _clamp(v, 0.0, 1.0) > 0.005
            for v in (
                settings.light_leaks,
                settings.film_grain,
                settings.scratches,
                settings.vhs,
                settings.glitch,
                settings.lens_flares,
            )
        )
    )


def _has_any_effect(settings: EffectSettings) -> bool:
    return _has_visual_effect(settings) or abs(_clamp(settings.speed, 0.25, 4.0) - 1.0) > 0.005


def _atempo_filters(speed: float) -> list[str]:
    """Decompose any speed in [0.25, 4.0] into chained FFmpeg atempo filters (each in [0.5, 2.0])."""
    s = _clamp(speed, 0.25, 4.0)
    if abs(s - 1.0) <= 0.005:
        return []
    filters = []
    while s < 0.5:
        filters.append("atempo=0.5")
        s /= 0.5
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    filters.append(f"atempo={s:.4f}")
    return filters


def build_segment_effect_graphs(in_label: str, out_label: str, settings: EffectSettings, tag: str) -> list[str]:
    """Build FFmpeg filter_complex statements transforming [in_label] -> [out_label]."""
    preset = (settings.preset or "none").strip().lower()
    if preset not in VIDEO_EFFECT_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unsupported preset '{preset}'")

    if not _has_visual_effect(settings):
        return [f"[{in_label}]format=yuv420p[{out_label}]"]

    contrast = _clamp(settings.contrast, 0.5, 2.0)
    saturation = _clamp(settings.saturation, 0.0, 2.0)
    warmth = _clamp(settings.warmth, -1.0, 1.0)
    white_balance = _clamp(settings.white_balance, -1.0, 1.0)
    light_leaks = _clamp(settings.light_leaks, 0.0, 1.0)
    film_grain = _clamp(settings.film_grain, 0.0, 1.0)
    scratches = _clamp(settings.scratches, 0.0, 1.0)
    vhs = _clamp(settings.vhs, 0.0, 1.0)
    glitch = _clamp(settings.glitch, 0.0, 1.0)
    lens_flares = _clamp(settings.lens_flares, 0.0, 1.0)

    chain = ["format=rgb24"]

    if preset == "grayscale":
        chain.append("hue=s=0")
    elif preset == "sepia":
        chain.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")
    elif preset == "retro":
        chain.append("curves=r='0/0.06 0.5/0.54 1/0.94':g='0/0.04 0.5/0.50 1/0.92':b='0/0.09 0.5/0.46 1/0.82'")
        chain.append("eq=saturation=0.82:contrast=0.95")
        chain.append("vignette=PI/5")
    elif preset == "film":
        chain.append("curves=r='0/0.02 0.25/0.22 0.75/0.80 1/0.98':g='0/0.02 0.5/0.50 1/0.97':b='0/0.05 0.25/0.27 0.75/0.74 1/0.92'")
        chain.append("eq=contrast=1.12:saturation=1.08")
        chain.append("vignette=PI/6")

    if abs(contrast - 1.0) > 0.005 or abs(saturation - 1.0) > 0.005:
        chain.append(f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}")

    if abs(warmth) > 0.005 or abs(white_balance) > 0.005:
        r_shift = _clamp(0.35 * warmth - 0.12 * white_balance, -1.0, 1.0)
        g_shift = _clamp(0.30 * white_balance, -1.0, 1.0)
        b_shift = _clamp(-0.35 * warmth - 0.12 * white_balance, -1.0, 1.0)
        chain.append(
            f"colorbalance=rs={r_shift*0.6:.3f}:gs={g_shift*0.6:.3f}:bs={b_shift*0.6:.3f}"
            f":rm={r_shift:.3f}:gm={g_shift:.3f}:bm={b_shift:.3f}"
            f":rh={r_shift*0.8:.3f}:gh={g_shift*0.8:.3f}:bh={b_shift*0.8:.3f}"
        )

    if vhs > 0.005:
        shift = max(1, int(round(2 + 6 * vhs)))
        chain.append(f"rgbashift=rh={shift}:bh={-shift}:edge=smear")
        chain.append(f"drawgrid=w=0:h=4:t=1:c=black@{0.28 * vhs:.2f}")
        chain.append(f"noise=alls={int(round(4 + 12 * vhs))}:allf=t")

    if glitch > 0.005:
        g_shift = max(2, int(round(4 + 16 * glitch)))
        gv_shift = max(1, int(round(2 * glitch)))
        chain.append(f"rgbashift=rh={g_shift}:bh={-g_shift}:rv={gv_shift}:edge=smear")
        alpha = min(0.65, 0.20 + 0.40 * glitch)
        chain.append(
            f"drawbox=x=0:y='mod(floor(t*14)*197,ih)':w=iw:h='max(4,ih/26)':color=cyan@{alpha:.2f}:t=fill:enable='gt(sin(t*19.7),0.45)'"
        )
        chain.append(
            f"drawbox=x=0:y='mod(floor(t*11)*311+53,ih)':w=iw:h='max(3,ih/34)':color=magenta@{alpha:.2f}:t=fill:enable='gt(cos(t*23.1),0.50)'"
        )

    if scratches > 0.005:
        s_alpha = min(0.75, 0.25 + 0.45 * scratches)
        chain.append(
            f"drawbox=x='mod(floor(t*12)*373+91,iw)':y=0:w=2:h=ih:color=white@{s_alpha:.2f}:t=fill:enable='gt(sin(t*7.3),-0.1)'"
        )
        chain.append(
            f"drawbox=x='mod(floor(t*9)*521+211,iw)':y=0:w=1:h=ih:color=black@{s_alpha:.2f}:t=fill:enable='gt(cos(t*11.1),0.0)'"
        )
        chain.append(
            f"drawbox=x='mod(floor(t*15)*257+37,iw)':y=0:w=1:h=ih:color=white@{s_alpha * 0.8:.2f}:t=fill:enable='gt(sin(t*13.7),0.25)'"
        )

    if film_grain > 0.005:
        chain.append(f"noise=alls={int(round(6 + 38 * film_grain))}:allf=t+u")

    graphs = [f"[{in_label}]{','.join(chain)}[{tag}_base]"]
    current = f"{tag}_base"

    if light_leaks > 0.005:
        op = min(0.85, 0.20 + 0.65 * light_leaks)
        graphs.append(
            "color=c=black:s=160x90:r=24,format=rgb24,"
            "geq="
            "r='clip(255*(exp(-((X-W*(0.08+0.07*sin(T*0.9)))^2+(Y-H*0.45)^2)/(2*(W*0.26)^2))*(0.75+0.25*sin(T*1.3))"
            "+0.85*exp(-((X-W*0.94)^2+(Y-H*(0.28+0.12*cos(T*0.7)))^2)/(2*(W*0.22)^2))),0,255)':"
            "g='clip(145*exp(-((X-W*(0.08+0.07*sin(T*0.9)))^2+(Y-H*0.45)^2)/(2*(W*0.24)^2))*(0.75+0.25*sin(T*1.3))"
            "+55*exp(-((X-W*0.94)^2+(Y-H*(0.28+0.12*cos(T*0.7)))^2)/(2*(W*0.20)^2)),0,255)':"
            "b='clip(35*exp(-((X-W*(0.08+0.07*sin(T*0.9)))^2+(Y-H*0.45)^2)/(2*(W*0.22)^2))"
            "+120*exp(-((X-W*0.94)^2+(Y-H*(0.28+0.12*cos(T*0.7)))^2)/(2*(W*0.22)^2)),0,255)',"
            f"gblur=sigma=10[{tag}_leak_s]"
        )
        graphs.append(f"[{tag}_leak_s][{current}]scale2ref[{tag}_leak_f][{tag}_v_l]")
        graphs.append(f"[{tag}_v_l][{tag}_leak_f]blend=all_mode=screen:all_opacity={op:.2f}:shortest=1[{tag}_leak]")
        current = f"{tag}_leak"

    if lens_flares > 0.005:
        op = min(0.90, 0.25 + 0.65 * lens_flares)
        graphs.append(
            "color=c=black:s=160x90:r=24,format=rgb24,"
            "geq="
            "r='clip(255*exp(-((X-W*(0.22+0.12*sin(T*0.6)))^2+(Y-H*0.22)^2)/(2*(W*0.10)^2))"
            "+180*exp(-((Y-H*0.22)^2)/(2*(H*0.035)^2))*exp(-((X-W*(0.22+0.12*sin(T*0.6)))^2)/(2*(W*0.45)^2))"
            "+130*exp(-((X-W*(0.65-0.08*sin(T*0.6)))^2+(Y-H*0.62)^2)/(2*(W*0.07)^2)),0,255)':"
            "g='clip(235*exp(-((X-W*(0.22+0.12*sin(T*0.6)))^2+(Y-H*0.22)^2)/(2*(W*0.10)^2))"
            "+210*exp(-((Y-H*0.22)^2)/(2*(H*0.035)^2))*exp(-((X-W*(0.22+0.12*sin(T*0.6)))^2)/(2*(W*0.45)^2))"
            "+190*exp(-((X-W*(0.65-0.08*sin(T*0.6)))^2+(Y-H*0.62)^2)/(2*(W*0.07)^2)),0,255)':"
            "b='clip(195*exp(-((X-W*(0.22+0.12*sin(T*0.6)))^2+(Y-H*0.22)^2)/(2*(W*0.10)^2))"
            "+255*exp(-((Y-H*0.22)^2)/(2*(H*0.035)^2))*exp(-((X-W*(0.22+0.12*sin(T*0.6)))^2)/(2*(W*0.45)^2))"
            "+245*exp(-((X-W*(0.65-0.08*sin(T*0.6)))^2+(Y-H*0.62)^2)/(2*(W*0.07)^2)),0,255)',"
            f"gblur=sigma=5[{tag}_flare_s]"
        )
        graphs.append(f"[{tag}_flare_s][{current}]scale2ref[{tag}_flare_f][{tag}_v_f]")
        graphs.append(f"[{tag}_v_f][{tag}_flare_f]blend=all_mode=screen:all_opacity={op:.2f}:shortest=1[{tag}_flare]")
        current = f"{tag}_flare"

    graphs.append(f"[{current}]format=yuv420p[{out_label}]")
    return graphs


def build_transitions_graphs(in_label: str, out_label: str, transitions: list[EffectTransition], duration: float) -> list[str]:
    """Build in-place FFmpeg filter_complex statements for timed transitions without altering clip duration."""
    if not transitions:
        return []
    resolved = []
    for tr in transitions:
        t_type = (tr.type or "fade_black").strip().lower()
        if t_type not in VIDEO_TRANSITION_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported transition type '{t_type}'")
        if not math.isfinite(tr.start) or not math.isfinite(tr.end):
            raise HTTPException(status_code=400, detail="Transition times must be valid numbers")
        start = max(0.0, float(tr.start))
        end = min(duration, float(tr.end)) if duration > 0 else float(tr.end)
        if duration > 0 and start >= duration:
            continue
        if end - start < 0.05:
            raise HTTPException(status_code=400, detail="Each transition must be at least 0.05 seconds long")
        resolved.append((start, end, t_type))

    if not resolved:
        return []

    resolved.sort(key=lambda item: item[0])
    for i in range(1, len(resolved)):
        if resolved[i][0] < resolved[i - 1][1] - 0.01:
            raise HTTPException(status_code=400, detail="Transitions cannot overlap")

    graphs: list[str] = []
    linear: list[str] = []
    current = in_label

    def flush_linear(tag: str):
        nonlocal current
        if linear:
            nxt = f"tr_{tag}"
            graphs.append(f"[{current}]{','.join(linear)}[{nxt}]")
            current = nxt
            linear.clear()

    for idx, (start, end, t_type) in enumerate(resolved):
        d = end - start
        half = d / 2.0
        mid = start + half
        q = d / 4.0
        if t_type == "fade_black":
            linear.append(f"fade=t=out:st={start:.3f}:d={half:.3f}:color=black")
            linear.append(f"fade=t=in:st={mid:.3f}:d={half:.3f}:color=black")
        elif t_type == "fade_white":
            linear.append(f"fade=t=out:st={start:.3f}:d={half:.3f}:color=white")
            linear.append(f"fade=t=in:st={mid:.3f}:d={half:.3f}:color=white")
        elif t_type == "fade_in":
            linear.append(f"fade=t=in:st={start:.3f}:d={d:.3f}:color=black")
        elif t_type == "fade_out":
            linear.append(f"fade=t=out:st={start:.3f}:d={d:.3f}:color=black")
        elif t_type == "blur_dissolve":
            linear.append(f"gblur=sigma=5:enable='between(t,{start:.3f},{start+q:.3f})+between(t,{end-q:.3f},{end:.3f})'")
            linear.append(f"gblur=sigma=16:enable='between(t,{start+q:.3f},{end-q:.3f})'")
        elif t_type == "zoom_flash":
            linear.append(f"rgbashift=rh=12:bh=-12:edge=smear:enable='between(t,{start:.3f},{end:.3f})'")
            linear.append(f"eq=brightness=0.45:contrast=1.25:enable='between(t,{mid-q:.3f},{mid+q:.3f})'")
        elif t_type in ("wipe_left", "wipe_right"):
            flush_linear(f"pre{idx}")
            x_expr = (
                f"if(lt(t,{mid:.3f}),W*(1-(t-{start:.3f})/{half:.3f}),-W*((t-{mid:.3f})/{half:.3f}))"
                if t_type == "wipe_left"
                else f"if(lt(t,{mid:.3f}),-W*(1-(t-{start:.3f})/{half:.3f}),W*((t-{mid:.3f})/{half:.3f}))"
            )
            graphs.append(f"color=c=black:s=16x16:r=24[tr_blk{idx}]")
            graphs.append(f"[tr_blk{idx}][{current}]scale2ref[tr_curt{idx}][tr_base{idx}]")
            graphs.append(
                f"[tr_base{idx}][tr_curt{idx}]overlay=x='{x_expr}':y=0:shortest=1:enable='between(t,{start:.3f},{end:.3f})'[tr_w{idx}]"
            )
            current = f"tr_w{idx}"

    if linear:
        graphs.append(f"[{current}]{','.join(linear)},format=yuv420p[{out_label}]")
    else:
        graphs.append(f"[{current}]format=yuv420p[{out_label}]")
    return graphs


def _map_source_time_to_output(t: float, pieces: list[tuple[float, float, float]]) -> float:
    """Map a source timestamp to output timestamp given contiguous (start, end, speed) pieces."""
    out_t = 0.0
    for start, end, speed in pieces:
        if t <= start:
            return out_t
        if t <= end:
            return out_t + (t - start) / speed
        out_t += (end - start) / speed
    return out_t


def build_video_effects_filtergraph(
    payload: VideoEffectsRequest, duration: float = 0.0, has_audio: bool = False
) -> tuple[str, str, str | None]:
    """Build FFmpeg filter_complex for full-video effects, timed segments, speed control, and transitions.
    Returns (filter_complex, video_out_label, audio_out_label_or_None).
    """
    if not payload.segments:
        speed = _clamp(payload.speed, 0.25, 4.0)
        speed_changed = abs(speed - 1.0) > 0.005
        out_duration = (duration / speed) if (duration > 0 and speed_changed) else duration
        mapped_trs = [
            EffectTransition(
                start=tr.start / speed if speed_changed else tr.start,
                end=tr.end / speed if speed_changed else tr.end,
                type=tr.type,
            )
            for tr in payload.transitions
        ]
        tr_graphs = build_transitions_graphs("vpre", "vout", mapped_trs, out_duration)
        has_full = _has_any_effect(payload)
        if not has_full and not tr_graphs:
            raise HTTPException(status_code=400, detail="Select at least one preset, adjustment, effect, speed, or transition")
        base_label = "vpre" if tr_graphs else "vout"
        graphs = []
        v_in = "0:v:0"
        if speed_changed:
            graphs.append(f"[0:v:0]setpts=(PTS-STARTPTS)/{speed:.4f}[vspd]")
            v_in = "vspd"
        if _has_visual_effect(payload):
            graphs.extend(build_segment_effect_graphs(v_in, base_label, payload, "full"))
        else:
            graphs.append(f"[{v_in}]format=yuv420p[{base_label}]")
        graphs.extend(tr_graphs)

        audio_out = None
        if speed_changed and has_audio:
            atempo = ",".join(_atempo_filters(speed))
            graphs.append(f"[0:a:0]{atempo},asetpts=PTS-STARTPTS[aout]")
            audio_out = "[aout]"
        return ";".join(graphs), "[vout]", audio_out

    if duration <= 0:
        raise HTTPException(status_code=400, detail="Could not read the video duration")

    resolved = []
    for seg in payload.segments:
        if not math.isfinite(seg.start) or (seg.end is not None and not math.isfinite(seg.end)):
            raise HTTPException(status_code=400, detail="Segment times must be valid numbers")
        start = max(0.0, float(seg.start))
        end = duration if seg.end is None else min(duration, float(seg.end))
        if start >= duration:
            continue
        if end <= start:
            raise HTTPException(status_code=400, detail="Each segment's end time must be after its start time")
        resolved.append((start, end, seg))

    resolved.sort(key=lambda item: item[0])
    for i in range(1, len(resolved)):
        if resolved[i][0] < resolved[i - 1][1] - 0.01:
            raise HTTPException(status_code=400, detail="Effect segments cannot overlap")

    # Build contiguous pieces covering [0, duration]; uncovered gaps stay original (1.0x, no effects)
    pieces: list[tuple[float, float, EffectSettings | None]] = []
    cursor = 0.0
    for start, end, seg in resolved:
        if start - cursor >= 0.02:
            pieces.append((cursor, start, None))
        pieces.append((start, end, seg))
        cursor = max(cursor, end)
    if duration - cursor >= 0.02:
        pieces.append((cursor, duration, None))

    timing_pieces = [
        (start, end, _clamp(seg.speed, 0.25, 4.0) if seg is not None else 1.0)
        for start, end, seg in pieces
    ]
    any_speed_change = any(abs(spd - 1.0) > 0.005 for _, _, spd in timing_pieces)
    out_duration = sum((end - start) / spd for start, end, spd in timing_pieces)

    mapped_trs = [
        EffectTransition(
            start=_map_source_time_to_output(tr.start, timing_pieces),
            end=_map_source_time_to_output(tr.end, timing_pieces),
            type=tr.type,
        )
        for tr in payload.transitions
    ]
    tr_graphs = build_transitions_graphs("vpre", "vout", mapped_trs, out_duration)

    if not any(_has_any_effect(seg) for _, _, seg in resolved) and not tr_graphs:
        raise HTTPException(status_code=400, detail="Add at least one effect segment, speed change, or transition")

    graphs = []
    concat_inputs = []
    process_audio = any_speed_change and has_audio

    for idx, (start, end, seg) in enumerate(pieces):
        spd = _clamp(seg.speed, 0.25, 4.0) if seg is not None else 1.0
        pts_expr = f"(PTS-STARTPTS)/{spd:.4f}" if abs(spd - 1.0) > 0.005 else "PTS-STARTPTS"
        if seg is None or not _has_visual_effect(seg):
            graphs.append(f"[0:v:0]trim=start={start:.3f}:end={end:.3f},setpts={pts_expr},format=yuv420p[seg{idx}]")
        else:
            graphs.append(f"[0:v:0]trim=start={start:.3f}:end={end:.3f},setpts={pts_expr}[raw{idx}]")
            graphs.extend(build_segment_effect_graphs(f"raw{idx}", f"seg{idx}", seg, f"s{idx}"))
        concat_inputs.append(f"[seg{idx}]")

        if process_audio:
            atempo_parts = _atempo_filters(spd)
            a_chain = [f"atrim=start={start:.3f}:end={end:.3f}", "asetpts=PTS-STARTPTS"]
            a_chain.extend(atempo_parts)
            a_chain.append("aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo")
            graphs.append(f"[0:a:0]{','.join(a_chain)}[aseg{idx}]")
            concat_inputs.append(f"[aseg{idx}]")

    base_label = "vpre" if tr_graphs else "vout"
    audio_out = "[aout]" if process_audio else None
    if len(pieces) == 1:
        graphs.append(f"[seg0]format=yuv420p[{base_label}]")
        if process_audio:
            graphs.append("[aseg0]anull[aout]")
    else:
        a_flag = 1 if process_audio else 0
        a_out_lbl = "[aout]" if process_audio else ""
        graphs.append(f"{''.join(concat_inputs)}concat=n={len(pieces)}:v=1:a={a_flag}[{base_label}]{a_out_lbl}")

    graphs.extend(tr_graphs)
    return ";".join(graphs), "[vout]", audio_out


@app.post("/api/media/effects")
def apply_video_effects(payload: VideoEffectsRequest, request: Request = None, queued: bool = False):
    file = resolve_media_file(payload.path, "video")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    info = probe_media(ffmpeg, file)
    if not info["has_video"]:
        raise HTTPException(status_code=400, detail="The selected file has no video stream")

    duration = info.get("duration") or 0.0
    filter_complex, out_label, audio_label = build_video_effects_filtergraph(
        payload, duration=duration, has_audio=bool(info["has_audio"])
    )

    output_path = file.with_name(f"{file.stem}_fx_{uuid.uuid4().hex[:8]}.mp4")
    while output_path.exists():
        output_path = file.with_name(f"{file.stem}_fx_{uuid.uuid4().hex[:8]}.mp4")

    if duration > 0:
        source_kbps = int((file.stat().st_size * 8) / (duration * 1000))
        maxrate_kbps = max(2500, min(16000, int(source_kbps * 1.5)))
    else:
        maxrate_kbps = 8000

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(file),
        "-filter_complex", filter_complex,
        "-map", out_label,
    ]
    if audio_label:
        command += ["-map", audio_label, "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-map", "0:a?", "-c:a", "copy"]
    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]

    def _run(on_progress=None):
        try:
            run_ffmpeg_with_progress(command, duration, on_progress)
        except subprocess.CalledProcessError as error:
            if output_path.exists():
                output_path.unlink()
            raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-800:]}")
        return {
            "name": output_path.name,
            "path": str(output_path),
            "folder": str(output_path.parent),
        }

    if queued:
        session = _require_user_session(request)
        return JSONResponse(enqueue_media_job(session["user_id"], "Video Effects", file.name, _run), status_code=202)
    return _run()


def _row_to_effects_project(row) -> dict:
    import json
    try:
        segments = json.loads(row["segments_json"])
    except Exception:
        segments = []
    try:
        transitions = json.loads(row["transitions_json"]) if "transitions_json" in row.keys() else []
    except Exception:
        transitions = []
    return {
        "id": row["id"],
        "name": row["name"],
        "video_path": row["video_path"],
        "segments": segments,
        "transitions": transitions,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/effects-projects")
def list_effects_projects(request: Request):
    session = _require_user_session(request)
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM effects_projects WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (session["user_id"],),
        ).fetchall()
    return [_row_to_effects_project(row) for row in rows]


@app.post("/api/effects-projects")
def create_effects_project(payload: EffectsProjectPayload, request: Request):
    import json
    session = _require_user_session(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Effects project name is required")
    segments_json = json.dumps([seg.model_dump() for seg in payload.segments])
    transitions_json = json.dumps([tr.model_dump() for tr in payload.transitions])
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO effects_projects (user_id, name, video_path, segments_json, transitions_json)
               VALUES (?, ?, ?, ?, ?)""",
            (session["user_id"], name, payload.video_path.strip(), segments_json, transitions_json),
        )
        row = connection.execute(
            "SELECT * FROM effects_projects WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row_to_effects_project(row)


@app.put("/api/effects-projects/{project_id}")
def update_effects_project(project_id: int, payload: EffectsProjectPayload, request: Request):
    import json
    session = _require_user_session(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Effects project name is required")
    segments_json = json.dumps([seg.model_dump() for seg in payload.segments])
    transitions_json = json.dumps([tr.model_dump() for tr in payload.transitions])
    with db() as connection:
        cursor = connection.execute(
            """UPDATE effects_projects
               SET name = ?, video_path = ?, segments_json = ?, transitions_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (name, payload.video_path.strip(), segments_json, transitions_json, project_id, session["user_id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Effects project not found")
        row = connection.execute(
            "SELECT * FROM effects_projects WHERE id = ?", (project_id,)
        ).fetchone()
    return _row_to_effects_project(row)


@app.delete("/api/effects-projects/{project_id}")
def delete_effects_project(project_id: int, request: Request):
    session = _require_user_session(request)
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM effects_projects WHERE id = ? AND user_id = ?",
            (project_id, session["user_id"]),
        )
    return {"success": True, "id": project_id, "deleted": cursor.rowcount > 0}


# ---------------------------------------------------------------------------
# Segmented Bokeh Effect (local CPU CSRT tracking + ONNX U2-Net / GrabCut)
# ---------------------------------------------------------------------------
import tempfile as _tempfile

MODELS_DIR = PROJECT_DIR / "models"
U2NETP_PATH = MODELS_DIR / "u2netp.onnx"
U2NETP_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
BOKEH_PREVIEW_DIR = PROJECT_DIR / "bokeh_previews"
_u2net_session = None


class BokehBox(BaseModel):
    x: float          # normalized 0..1 left
    y: float          # normalized 0..1 top
    w: float          # normalized 0..1 width
    h: float          # normalized 0..1 height
    time: float = 0.0 # anchor timestamp in seconds (relative to full video)


class BokehSegment(BaseModel):
    start: float
    end: float
    boxes: list[BokehBox]
    blur_strength: int = 25          # 5..65
    edge_feather: int = 12           # 0..40
    segmentation_mode: str = "auto"  # auto | grabcut | feathered
    segment_every: int = 2           # 1..5
    preview_path: str = ""           # optional already-rendered preview clip to reuse


class BokehSegmentPreviewRequest(BokehSegment):
    path: str


class BokehRequest(BaseModel):
    path: str
    segments: list[BokehSegment] = Field(default_factory=list)
    boxes: list[BokehBox] = Field(default_factory=list)
    blur_strength: int = 25
    edge_feather: int = 12
    segmentation_mode: str = "auto"
    segment_every: int = 2


class BokehProjectPayload(BaseModel):
    name: str
    video_path: str = ""
    segments: list[BokehSegment] = Field(default_factory=list)


def _create_csrt_tracker(cv2):
    if hasattr(cv2, "TrackerCSRT_create"):
        return cv2.TrackerCSRT_create()
    if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create"):
        return cv2.legacy.TrackerCSRT_create()
    raise RuntimeError("OpenCV CSRT tracker is not available; install opencv-contrib-python-headless")


def _get_u2net_session():
    global _u2net_session
    if _u2net_session is not None:
        return _u2net_session
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    if not U2NETP_PATH.is_file() or U2NETP_PATH.stat().st_size < 1_000_000:
        try:
            import urllib.request
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            tmp_model = U2NETP_PATH.with_suffix(".onnx.tmp")
            urllib.request.urlretrieve(U2NETP_URL, str(tmp_model))
            if tmp_model.stat().st_size > 1_000_000:
                tmp_model.replace(U2NETP_PATH)
            else:
                tmp_model.unlink(missing_ok=True)
                return None
        except Exception:
            return None
    try:
        _u2net_session = ort.InferenceSession(str(U2NETP_PATH), providers=["CPUExecutionProvider"])
        return _u2net_session
    except Exception:
        return None


def _segment_crop_u2net(cv2, np, session, crop_bgr):
    h, w = crop_bgr.shape[:2]
    rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    inp = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    inp = (inp - mean) / std
    inp = np.transpose(inp, (2, 0, 1))[None, ...]

    input_name = session.get_inputs()[0].name
    pred = session.run(None, {input_name: inp})[0][0, 0]
    p_min, p_max = float(pred.min()), float(pred.max())
    mask = (pred - p_min) / (p_max - p_min) if (p_max - p_min) > 1e-6 else np.zeros_like(pred, dtype=np.float32)
    mask = cv2.resize(mask.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip((mask - 0.25) / 0.50, 0.0, 1.0)


def _segment_crop_grabcut(cv2, np, crop_bgr, inner_rect):
    h, w = crop_bgr.shape[:2]
    scale = min(1.0, 240.0 / max(w, h, 1))
    sw, sh = max(16, int(round(w * scale))), max(16, int(round(h * scale)))
    small = cv2.resize(crop_bgr, (sw, sh), interpolation=cv2.INTER_AREA) if scale < 1.0 else crop_bgr
    rx, ry, rw, rh = [int(round(v * scale)) for v in inner_rect]
    rx = max(1, min(sw - 3, rx))
    ry = max(1, min(sh - 3, ry))
    rw = max(2, min(sw - rx - 1, rw))
    rh = max(2, min(sh - ry - 1, rh))

    gc_mask = np.zeros((sh, sw), np.uint8)
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(small, gc_mask, (rx, ry, rw, rh), bgd_model, fgd_model, 2, cv2.GC_INIT_WITH_RECT)
        fg = np.where((gc_mask == cv2.GC_FGD) | (gc_mask == cv2.GC_PR_FGD), 1.0, 0.0).astype(np.float32)
    except Exception:
        fg = np.zeros((sh, sw), np.float32)
        cv2.ellipse(fg, (rx + rw // 2, ry + rh // 2), (max(1, rw // 2), max(1, rh // 2)), 0, 0, 360, 1.0, -1)
    if scale < 1.0:
        fg = cv2.resize(fg, (w, h), interpolation=cv2.INTER_LINEAR)
    return fg


def _make_bokeh_kernel(np, radius: int):
    r = max(3, int(radius))
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    disk = ((x * x + y * y) <= r * r).astype(np.float32)
    return disk / disk.sum()


def _apply_bokeh_blur(cv2, np, frame_bgr, blur_strength: int, kernel):
    h, w = frame_bgr.shape[:2]
    sw, sh = max(2, w // 2), max(2, h // 2)
    small = cv2.resize(frame_bgr, (sw, sh), interpolation=cv2.INTER_AREA)
    blurred = cv2.filter2D(small, -1, kernel, borderType=cv2.BORDER_REPLICATE)
    k = max(3, (blur_strength // 2) | 1)
    blurred = cv2.GaussianBlur(blurred, (k, k), 0)
    return cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)


def _render_bokeh_segment_file(
    ffmpeg: str,
    source_file: Path,
    seg: BokehSegment,
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    maxrate_kbps: int,
    include_audio: bool = False,
    on_progress=None,
) -> tuple[int, str]:
    """Render a single [seg.start, seg.end] bokeh clip to output_path. Returns (frames_written, engine)."""
    import cv2
    import numpy as np

    mode = (seg.segmentation_mode or "auto").strip().lower()
    blur_strength = int(_clamp(seg.blur_strength, 5, 65))
    edge_feather = int(_clamp(seg.edge_feather, 0, 40))
    segment_every = int(_clamp(seg.segment_every, 1, 5))

    u2net_session = _get_u2net_session() if mode == "auto" else None
    engine_used = "u2netp-onnx" if u2net_session is not None else ("feathered" if mode == "feathered" else "grabcut")

    seg_duration = max(0.05, seg.end - seg.start)
    target_frames = max(1, int(round(seg_duration * fps)))

    anchors = []
    for box in seg.boxes:
        bx = max(0.0, min(0.99, float(box.x)))
        by = max(0.0, min(0.99, float(box.y)))
        bw = max(0.01, min(1.0 - bx, float(box.w)))
        bh = max(0.01, min(1.0 - by, float(box.h)))
        rel_time = max(0.0, float(box.time) - seg.start)
        rel_frame = min(target_frames - 1, max(0, int(round(rel_time * fps))))
        anchors.append({
            "start_frame": rel_frame,
            "rect": (
                int(round(bx * width)),
                int(round(by * height)),
                max(8, int(round(bw * width))),
                max(8, int(round(bh * height))),
            ),
            "tracker": None,
            "last_rect": None,
            "last_mask": None,
            "last_seg_frame": -999,
        })

    track_scale = min(1.0, 640.0 / max(width, 1))
    tw, th = max(32, int(round(width * track_scale))), max(32, int(round(height * track_scale)))
    bokeh_kernel = _make_bokeh_kernel(np, max(3, blur_strength // 3))

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", f"{fps:.4f}",
        "-i", "pipe:0",
    ]
    if include_audio:
        command += [
            "-ss", f"{seg.start:.3f}", "-t", f"{seg_duration:.3f}",
            "-i", str(source_file),
            "-map", "0:v:0", "-map", "1:a?",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
        ]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output_path),
    ]

    cap = cv2.VideoCapture(str(source_file))
    if not cap.isOpened():
        raise HTTPException(status_code=400, detail="Unable to open video file")
    if seg.start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, seg.start * 1000.0)

    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_idx = 0
    try:
        while frame_idx < target_frames:
            ok, frame = cap.read()
            if not ok:
                break
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)

            track_frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA) if track_scale < 1.0 else frame
            combined_mask = np.zeros((height, width), dtype=np.float32)
            any_active = False

            for anchor in anchors:
                if frame_idx < anchor["start_frame"]:
                    continue
                if frame_idx == anchor["start_frame"]:
                    rx, ry, rw, rh = anchor["rect"]
                    tr_rect = (
                        int(round(rx * track_scale)),
                        int(round(ry * track_scale)),
                        max(6, int(round(rw * track_scale))),
                        max(6, int(round(rh * track_scale))),
                    )
                    tracker = _create_csrt_tracker(cv2)
                    tracker.init(track_frame, tr_rect)
                    anchor["tracker"] = tracker
                    anchor["last_rect"] = (rx, ry, rw, rh)
                    cur_rect = (rx, ry, rw, rh)
                elif anchor["tracker"] is not None:
                    tracked, box_t = anchor["tracker"].update(track_frame)
                    if tracked:
                        tx, ty, tbw, tbh = box_t
                        cur_rect = (
                            int(round(tx / track_scale)),
                            int(round(ty / track_scale)),
                            max(8, int(round(tbw / track_scale))),
                            max(8, int(round(tbh / track_scale))),
                        )
                        anchor["last_rect"] = cur_rect
                    else:
                        cur_rect = anchor["last_rect"]
                else:
                    continue

                if cur_rect is None:
                    continue

                rx, ry, rw, rh = cur_rect
                x1 = max(0, min(width - 2, rx))
                y1 = max(0, min(height - 2, ry))
                x2 = max(x1 + 2, min(width, rx + rw))
                y2 = max(y1 + 2, min(height, ry + rh))
                bw_c, bh_c = x2 - x1, y2 - y1
                if bw_c < 4 or bh_c < 4:
                    continue

                any_active = True
                if engine_used == "feathered":
                    sub = np.zeros((bh_c, bw_c), dtype=np.float32)
                    cv2.ellipse(sub, (bw_c // 2, bh_c // 2), (max(1, bw_c // 2), max(1, bh_c // 2)), 0, 0, 360, 1.0, -1)
                    combined_mask[y1:y2, x1:x2] = np.maximum(combined_mask[y1:y2, x1:x2], sub)
                else:
                    pad_x, pad_y = int(round(bw_c * 0.15)), int(round(bh_c * 0.15))
                    px1, py1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
                    px2, py2 = min(width, x2 + pad_x), min(height, y2 + pad_y)
                    pw, ph = px2 - px1, py2 - py1

                    need_seg = anchor["last_mask"] is None or (frame_idx - anchor["last_seg_frame"]) >= segment_every
                    if need_seg:
                        crop = frame[py1:py2, px1:px2]
                        if u2net_session is not None:
                            seg_m = _segment_crop_u2net(cv2, np, u2net_session, crop)
                        else:
                            seg_m = _segment_crop_grabcut(cv2, np, crop, (x1 - px1, y1 - py1, bw_c, bh_c))
                        if float(seg_m.max()) < 0.05:
                            seg_m = np.zeros((ph, pw), dtype=np.float32)
                            cv2.ellipse(seg_m, (pw // 2, ph // 2), (max(1, bw_c // 2), max(1, bh_c // 2)), 0, 0, 360, 1.0, -1)
                        anchor["last_mask"] = seg_m
                        anchor["last_seg_frame"] = frame_idx
                    else:
                        seg_m = cv2.resize(anchor["last_mask"], (pw, ph), interpolation=cv2.INTER_LINEAR)

                    combined_mask[py1:py2, px1:px2] = np.maximum(combined_mask[py1:py2, px1:px2], seg_m)

            if any_active:
                if edge_feather > 0:
                    fk = max(3, (edge_feather * 2 + 1) | 1)
                    combined_mask = cv2.GaussianBlur(combined_mask, (fk, fk), 0)
                blurred_bg = _apply_bokeh_blur(cv2, np, frame, blur_strength, bokeh_kernel)
                alpha = combined_mask[..., None]
                out_frame = np.clip(frame.astype(np.float32) * alpha + blurred_bg.astype(np.float32) * (1.0 - alpha), 0, 255).astype(np.uint8)
            else:
                out_frame = frame

            proc.stdin.write(out_frame.tobytes())
            frame_idx += 1
            if on_progress:
                on_progress(min(99, int((frame_idx / target_frames) * 100)))

        proc.stdin.close()
        stderr_data = proc.stderr.read().decode("utf-8", errors="replace")
        ret = proc.wait()
        if ret != 0:
            if output_path.exists():
                output_path.unlink()
            raise HTTPException(status_code=500, detail=f"FFmpeg failed: {stderr_data[-800:]}")
    except HTTPException:
        raise
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        if output_path.exists():
            output_path.unlink()
        raise HTTPException(status_code=500, detail=f"Bokeh processing failed: {exc}") from exc
    finally:
        cap.release()

    return frame_idx, engine_used


def _probe_video_geometry(file: Path):
    try:
        import cv2
    except ImportError:
        raise HTTPException(status_code=500, detail="OpenCV (opencv-contrib-python-headless) is not installed")
    cap = cv2.VideoCapture(str(file))
    if not cap.isOpened():
        raise HTTPException(status_code=400, detail="Unable to open video file")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 24.0)
    cap.release()
    if fps <= 0 or fps > 240:
        fps = 24.0
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="Unable to read video dimensions")
    return width, height, fps


@app.post("/api/media/bokeh/preview-segment")
def preview_bokeh_segment(payload: BokehSegmentPreviewRequest, request: Request = None, queued: bool = False):
    file = resolve_media_file(payload.path, "video")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")
    if not payload.boxes:
        raise HTTPException(status_code=400, detail="Draw at least one bounding box for this segment")
    if (payload.segmentation_mode or "auto").strip().lower() not in {"auto", "grabcut", "feathered"}:
        raise HTTPException(status_code=400, detail="Invalid segmentation mode")

    info = probe_media(ffmpeg, file)
    duration = info.get("duration") or 0.0
    if duration <= 0:
        raise HTTPException(status_code=400, detail="Could not read video duration")

    start = max(0.0, float(payload.start))
    end = min(duration, float(payload.end))
    if end - start < 0.1:
        raise HTTPException(status_code=400, detail="Segment must be at least 0.1 seconds long")

    width, height, fps = _probe_video_geometry(file)
    source_kbps = int((file.stat().st_size * 8) / (duration * 1000))
    maxrate_kbps = max(2500, min(16000, int(source_kbps * 1.5)))

    BOKEH_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    preview_file = BOKEH_PREVIEW_DIR / f"bokeh_prev_{uuid.uuid4().hex[:8]}.mp4"
    seg = BokehSegment(
        start=start, end=end, boxes=payload.boxes,
        blur_strength=payload.blur_strength, edge_feather=payload.edge_feather,
        segmentation_mode=payload.segmentation_mode, segment_every=payload.segment_every,
    )

    def _run(on_progress=None):
        frames, engine = _render_bokeh_segment_file(
            ffmpeg, file, seg, preview_file, width, height, fps, maxrate_kbps,
            include_audio=bool(info["has_audio"]), on_progress=on_progress,
        )
        return {
            "name": preview_file.name,
            "path": str(preview_file),
            "start": round(start, 2),
            "end": round(end, 2),
            "frames": frames,
            "engine": engine,
            "stream_url": f"/api/media/stream?kind=video&path={quote(str(preview_file), safe='')}",
        }

    if queued:
        session = _require_user_session(request)
        label = f"{file.name} ({start:.1f}s–{end:.1f}s)"
        return JSONResponse(enqueue_media_job(session["user_id"], "Bokeh Preview", label, _run), status_code=202)
    return _run()


@app.post("/api/media/bokeh")
def apply_bokeh_effect(payload: BokehRequest, request: Request = None, queued: bool = False):
    file = resolve_media_file(payload.path, "video")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    info = probe_media(ffmpeg, file)
    duration = info.get("duration") or 0.0
    if not info["has_video"] or duration <= 0:
        raise HTTPException(status_code=400, detail="Could not read video stream or duration")

    segments_in = list(payload.segments)
    if not segments_in and payload.boxes:
        segments_in = [BokehSegment(
            start=0.0, end=duration, boxes=payload.boxes,
            blur_strength=payload.blur_strength, edge_feather=payload.edge_feather,
            segmentation_mode=payload.segmentation_mode, segment_every=payload.segment_every,
        )]
    if not segments_in:
        raise HTTPException(status_code=400, detail="Add at least one bokeh segment")

    resolved: list[BokehSegment] = []
    for seg in segments_in:
        if not seg.boxes:
            raise HTTPException(status_code=400, detail="Each bokeh segment needs at least one bounding box")
        if (seg.segmentation_mode or "auto").strip().lower() not in {"auto", "grabcut", "feathered"}:
            raise HTTPException(status_code=400, detail="Invalid segmentation mode")
        start = max(0.0, float(seg.start))
        end = min(duration, float(seg.end))
        if start >= duration:
            continue
        if end - start < 0.1:
            raise HTTPException(status_code=400, detail="Each bokeh segment must be at least 0.1 seconds long")
        resolved.append(seg.model_copy(update={"start": start, "end": end}))

    if not resolved:
        raise HTTPException(status_code=400, detail="Add at least one bokeh segment within the video duration")

    resolved.sort(key=lambda s: s.start)
    for i in range(1, len(resolved)):
        if resolved[i].start < resolved[i - 1].end - 0.01:
            raise HTTPException(status_code=400, detail="Bokeh segments cannot overlap")

    width, height, fps = _probe_video_geometry(file)
    source_kbps = int((file.stat().st_size * 8) / (duration * 1000))
    maxrate_kbps = max(2500, min(16000, int(source_kbps * 1.5)))

    output_path = file.with_name(f"{file.stem}_bokeh_{uuid.uuid4().hex[:8]}.mp4")
    while output_path.exists():
        output_path = file.with_name(f"{file.stem}_bokeh_{uuid.uuid4().hex[:8]}.mp4")

    # Build chronological pieces: (start, end, BokehSegment | None)
    pieces: list[tuple[float, float, BokehSegment | None]] = []
    cursor = 0.0
    for seg in resolved:
        if seg.start - cursor >= 0.02:
            pieces.append((cursor, seg.start, None))
        pieces.append((seg.start, seg.end, seg))
        cursor = max(cursor, seg.end)
    if duration - cursor >= 0.02:
        pieces.append((cursor, duration, None))

    def _run(on_progress=None):
        tmp_dir = Path(_tempfile.mkdtemp(prefix="bokeh_build_"))
        total_frames = 0
        engines: set[str] = set()
        try:
            # 1. Render or reuse each bokeh segment (takes 0..85% of progress)
            seg_files: dict[int, Path] = {}
            bokeh_indices = [i for i, (_, _, s) in enumerate(pieces) if s is not None]
            total_bokeh_sec = sum(pieces[i][1] - pieces[i][0] for i in bokeh_indices) or 1.0
            done_sec = 0.0

            for idx in bokeh_indices:
                start, end, seg = pieces[idx]
                seg_len = end - start
                reusable = Path(seg.preview_path) if seg.preview_path else None
                if reusable is not None and reusable.is_file() and reusable.parent.resolve() == BOKEH_PREVIEW_DIR.resolve():
                    seg_files[idx] = reusable
                    engines.add("reused-preview")
                else:
                    seg_out = tmp_dir / f"bokeh_{idx}.mp4"

                    def _seg_prog(pct, base=done_sec, slen=seg_len):
                        if on_progress:
                            overall = ((base + slen * (pct / 100.0)) / total_bokeh_sec) * 85.0
                            on_progress(min(85, int(overall)))

                    frames, eng = _render_bokeh_segment_file(
                        ffmpeg, file, seg, seg_out, width, height, fps, maxrate_kbps,
                        include_audio=False, on_progress=_seg_prog,
                    )
                    seg_files[idx] = seg_out
                    total_frames += frames
                    engines.add(eng)
                done_sec += seg_len
                if on_progress:
                    on_progress(min(85, int((done_sec / total_bokeh_sec) * 85.0)))

            # 2. Assemble non-bokeh gaps + bokeh segments in exact chronological order with original audio (85..100%)
            cmd = [ffmpeg, "-hide_banner", "-y", "-i", str(file)]
            input_map: dict[int, int] = {}
            next_inp = 1
            for idx in bokeh_indices:
                cmd += ["-i", str(seg_files[idx])]
                input_map[idx] = next_inp
                next_inp += 1

            graphs = []
            concat_labels = []
            for idx, (start, end, seg) in enumerate(pieces):
                if seg is None:
                    graphs.append(
                        f"[0:v:0]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
                        f"fps={fps:.4f},scale={width}:{height},setsar=1,format=yuv420p[p{idx}]"
                    )
                else:
                    inp_i = input_map[idx]
                    graphs.append(
                        f"[{inp_i}:v:0]setpts=PTS-STARTPTS,fps={fps:.4f},scale={width}:{height},setsar=1,format=yuv420p[p{idx}]"
                    )
                concat_labels.append(f"[p{idx}]")

            if len(concat_labels) == 1:
                graphs.append(f"{concat_labels[0]}format=yuv420p[vout]")
            else:
                graphs.append(f"{''.join(concat_labels)}concat=n={len(concat_labels)}:v=1:a=0,format=yuv420p[vout]")

            cmd += [
                "-filter_complex", ";".join(graphs),
                "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k",
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ]

            def _mux_prog(pct):
                if on_progress:
                    on_progress(min(99, 85 + int(pct * 0.14)))

            try:
                run_ffmpeg_with_progress(cmd, duration, _mux_prog)
            except subprocess.CalledProcessError as error:
                if output_path.exists():
                    output_path.unlink()
                raise HTTPException(status_code=500, detail=f"FFmpeg assembly failed: {error.stderr[-800:]}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return {
            "name": output_path.name,
            "path": str(output_path),
            "folder": str(output_path.parent),
            "segments": len(resolved),
            "frames": total_frames,
            "engine": ", ".join(sorted(engines)) if engines else "bokeh",
        }

    if queued:
        session = _require_user_session(request)
        return JSONResponse(enqueue_media_job(session["user_id"], "Bokeh Final", file.name, _run), status_code=202)
    return _run()


def _row_to_bokeh_project(row) -> dict:
    import json
    try:
        segments = json.loads(row["segments_json"])
    except Exception:
        segments = []
    # Attach live preview stream_url if the saved preview file still exists on disk
    for seg in segments:
        pp = (seg.get("preview_path") or "").strip()
        if pp and Path(pp).is_file():
            seg["preview_stream_url"] = f"/api/media/stream?kind=video&path={quote(pp, safe='')}"
        else:
            seg["preview_path"] = ""
            seg["preview_stream_url"] = ""
    return {
        "id": row["id"],
        "name": row["name"],
        "video_path": row["video_path"],
        "segments": segments,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/bokeh-projects")
def list_bokeh_projects(request: Request):
    session = _require_user_session(request)
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM bokeh_projects WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (session["user_id"],),
        ).fetchall()
    return [_row_to_bokeh_project(row) for row in rows]


@app.post("/api/bokeh-projects")
def create_bokeh_project(payload: BokehProjectPayload, request: Request):
    import json
    session = _require_user_session(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Bokeh project name is required")
    segments_json = json.dumps([seg.model_dump() for seg in payload.segments])
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO bokeh_projects (user_id, name, video_path, segments_json)
               VALUES (?, ?, ?, ?)""",
            (session["user_id"], name, payload.video_path.strip(), segments_json),
        )
        row = connection.execute(
            "SELECT * FROM bokeh_projects WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return _row_to_bokeh_project(row)


@app.put("/api/bokeh-projects/{project_id}")
def update_bokeh_project(project_id: int, payload: BokehProjectPayload, request: Request):
    import json
    session = _require_user_session(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Bokeh project name is required")
    segments_json = json.dumps([seg.model_dump() for seg in payload.segments])
    with db() as connection:
        cursor = connection.execute(
            """UPDATE bokeh_projects
               SET name = ?, video_path = ?, segments_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (name, payload.video_path.strip(), segments_json, project_id, session["user_id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Bokeh project not found")
        row = connection.execute(
            "SELECT * FROM bokeh_projects WHERE id = ?", (project_id,)
        ).fetchone()
    return _row_to_bokeh_project(row)


@app.delete("/api/bokeh-projects/{project_id}")
def delete_bokeh_project(project_id: int, request: Request):
    session = _require_user_session(request)
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM bokeh_projects WHERE id = ? AND user_id = ?",
            (project_id, session["user_id"]),
        )
    return {"success": True, "id": project_id, "deleted": cursor.rowcount > 0}


# ---------------------------------------------------------------------------
# Spatial Crop, Rotate & Flip + Ken Burns (Photo to Pan & Zoom Video)
# ---------------------------------------------------------------------------
CROP_IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp",
}
KEN_BURNS_MOTIONS = {
    "zoom_in", "zoom_out", "pan_left_to_right", "pan_right_to_left",
    "pan_top_to_bottom", "pan_bottom_to_top", "zoom_in_top_right",
}
KEN_BURNS_RESOLUTIONS = {
    "1920x1080": (1920, 1080),
    "1280x720": (1280, 720),
    "1080x1920": (1080, 1920),
    "1080x1080": (1080, 1080),
}


class SpatialTransformRequest(BaseModel):
    path: str
    crop_x: float = 0.0        # normalized 0..1
    crop_y: float = 0.0        # normalized 0..1
    crop_w: float = 1.0        # normalized 0..1
    crop_h: float = 1.0        # normalized 0..1
    rotate: int = 0            # 0, 90, 180, 270
    flip_h: bool = False
    flip_v: bool = False


class KenBurnsRequest(BaseModel):
    path: str
    duration_seconds: float = 8.0   # 1..120
    motion: str = "zoom_in"
    zoom_amount: float = 1.3        # 1.05..2.0
    resolution: str = "1280x720"
    fps: int = 30                   # 24 or 30


@app.post("/api/media/transform")
def transform_video(payload: SpatialTransformRequest, request: Request = None, queued: bool = False):
    file = resolve_media_file(payload.path, "video")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    if payload.rotate not in (0, 90, 180, 270):
        raise HTTPException(status_code=400, detail="Rotate must be 0, 90, 180, or 270 degrees")

    cx = _clamp(payload.crop_x, 0.0, 0.98)
    cy = _clamp(payload.crop_y, 0.0, 0.98)
    cw = _clamp(payload.crop_w, 0.02, 1.0 - cx)
    ch = _clamp(payload.crop_h, 0.02, 1.0 - cy)
    has_crop = cx > 0.002 or cy > 0.002 or cw < 0.998 or ch < 0.998
    if not has_crop and payload.rotate == 0 and not payload.flip_h and not payload.flip_v:
        raise HTTPException(status_code=400, detail="Select a crop area, rotation, or flip")

    info = probe_media(ffmpeg, file)
    if not info["has_video"]:
        raise HTTPException(status_code=400, detail="The selected file has no video stream")

    filters = []
    if has_crop:
        # Ensure even width/height >= 16 for yuv420p H.264
        filters.append(
            f"crop=w='max(16,trunc(iw*{cw:.5f}/2)*2)':h='max(16,trunc(ih*{ch:.5f}/2)*2)'"
            f":x='trunc(iw*{cx:.5f}/2)*2':y='trunc(ih*{cy:.5f}/2)*2'"
        )
    if payload.rotate == 90:
        filters.append("transpose=1")
    elif payload.rotate == 180:
        filters.append("hflip,vflip")
    elif payload.rotate == 270:
        filters.append("transpose=2")
    if payload.flip_h:
        filters.append("hflip")
    if payload.flip_v:
        filters.append("vflip")
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")

    duration = info.get("duration") or 0.0
    if duration > 0:
        source_kbps = int((file.stat().st_size * 8) / (duration * 1000))
        maxrate_kbps = max(2500, min(16000, int(source_kbps * 1.5)))
    else:
        maxrate_kbps = 8000

    output_path = file.with_name(f"{file.stem}_transformed_{uuid.uuid4().hex[:8]}.mp4")
    while output_path.exists():
        output_path = file.with_name(f"{file.stem}_transformed_{uuid.uuid4().hex[:8]}.mp4")

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(file),
        "-vf", ",".join(filters),
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
    def _run(on_progress=None):
        try:
            run_ffmpeg_with_progress(command, duration, on_progress)
        except subprocess.CalledProcessError as error:
            if output_path.exists():
                output_path.unlink()
            raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-800:]}")
        return {"name": output_path.name, "path": str(output_path), "folder": str(output_path.parent)}

    if queued:
        session = _require_user_session(request)
        return JSONResponse(enqueue_media_job(session["user_id"], "Crop, Rotate & Flip", file.name, _run), status_code=202)
    return _run()


@app.post("/api/media/ken-burns")
def generate_ken_burns_video(payload: KenBurnsRequest, request: Request = None, queued: bool = False):
    file = resolve_media_file(payload.path, "image")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    duration = float(payload.duration_seconds)
    if not math.isfinite(duration) or duration < 1.0 or duration > 120.0:
        raise HTTPException(status_code=400, detail="Video length must be between 1 and 120 seconds")
    motion = (payload.motion or "zoom_in").strip().lower()
    if motion not in KEN_BURNS_MOTIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported motion '{motion}'")
    if payload.resolution not in KEN_BURNS_RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Unsupported output resolution")
    fps = 24 if payload.fps == 24 else 30
    zoom_max = _clamp(payload.zoom_amount, 1.05, 2.0)

    out_w, out_h = KEN_BURNS_RESOLUTIONS[payload.resolution]
    frames = max(fps, int(round(duration * fps)))
    denom = max(1, frames - 1)
    dz = zoom_max - 1.0
    # Fit the entire image inside 2x target (no pre-cropping) so z=1.0 shows 100% of the full photo
    pre_w, pre_h = out_w * 2, out_h * 2
    p = f"min(1,max(0,(on-1)/{denom}))"

    if motion == "zoom_in":
        z_expr = f"1+{dz:.4f}*{p}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "zoom_out":
        z_expr = f"{zoom_max:.4f}-{dz:.4f}*{p}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "pan_left_to_right":
        z_expr = f"{zoom_max:.4f}"
        x_expr = f"(iw-iw/zoom)*{p}"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "pan_right_to_left":
        z_expr = f"{zoom_max:.4f}"
        x_expr = f"(iw-iw/zoom)*(1-{p})"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "pan_top_to_bottom":
        z_expr = f"{zoom_max:.4f}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = f"(ih-ih/zoom)*{p}"
    elif motion == "pan_bottom_to_top":
        z_expr = f"{zoom_max:.4f}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = f"(ih-ih/zoom)*(1-{p})"
    else:  # zoom_in_top_right
        z_expr = f"1+{dz:.4f}*{p}"
        x_expr = f"(iw-iw/zoom)*(0.5+0.5*{p})"
        y_expr = f"(ih-ih/zoom)*(0.5-0.5*{p})"

    vf = (
        f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=decrease,"
        f"pad={pre_w}:{pre_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d={frames}:s={out_w}x{out_h}:fps={fps},"
        f"format=yuv420p"
    )

    output_path = file.with_name(f"{file.stem}_kenburns_{uuid.uuid4().hex[:8]}.mp4")
    while output_path.exists():
        output_path = file.with_name(f"{file.stem}_kenburns_{uuid.uuid4().hex[:8]}.mp4")

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(file),
        "-vf", vf,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    def _run(on_progress=None):
        try:
            run_ffmpeg_with_progress(command, duration, on_progress)
        except subprocess.CalledProcessError as error:
            if output_path.exists():
                output_path.unlink()
            raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-800:]}")
        return {
            "name": output_path.name,
            "path": str(output_path),
            "folder": str(output_path.parent),
            "duration": round(duration, 2),
            "stream_url": f"/api/media/stream?kind=video&path={quote(str(output_path), safe='')}",
        }

    if queued:
        session = _require_user_session(request)
        return JSONResponse(enqueue_media_job(session["user_id"], "Ken Burns", file.name, _run), status_code=202)
    return _run()


# ---------------------------------------------------------------------------
# FIFO Media Job Queue (single worker thread, live % progress)
# ---------------------------------------------------------------------------
import queue as _queue
import threading as _threading

_job_queue: "_queue.Queue[int]" = _queue.Queue()
_jobs_lock = _threading.Lock()
_jobs_store: dict[int, dict] = {}
_jobs_order: list[int] = []
_job_seq = 0
_worker_started = False


def _update_job(job_id: int, **fields):
    with _jobs_lock:
        job = _jobs_store.get(job_id)
        if job is not None:
            job.update(fields)


def _job_public(job: dict) -> dict:
    return {
        "id": job["id"],
        "tool": job["tool"],
        "label": job["label"],
        "status": job["status"],
        "progress": job["progress"],
        "result": job["result"],
        "error": job["error"],
        "created_at": job["created_at"],
    }


def _job_worker_loop():
    while True:
        job_id = _job_queue.get()
        try:
            with _jobs_lock:
                job = _jobs_store.get(job_id)
                if job is None:
                    continue
                job["status"] = "running"
                job["progress"] = 0
                runner = job["runner"]

            def report_progress(pct: int):
                _update_job(job_id, progress=max(0, min(99, int(pct))))

            result = runner(report_progress)
            _update_job(job_id, status="completed", progress=100, result=result, error="")
        except HTTPException as exc:
            _update_job(job_id, status="failed", error=str(exc.detail))
        except Exception as exc:
            _update_job(job_id, status="failed", error=str(exc))
        finally:
            _job_queue.task_done()


def _ensure_job_worker():
    global _worker_started
    with _jobs_lock:
        if not _worker_started:
            t = _threading.Thread(target=_job_worker_loop, daemon=True, name="media-job-worker")
            t.start()
            _worker_started = True


def enqueue_media_job(user_id: int, tool: str, label: str, runner) -> dict:
    global _job_seq
    _ensure_job_worker()
    with _jobs_lock:
        _job_seq += 1
        job_id = _job_seq
        job = {
            "id": job_id,
            "user_id": user_id,
            "tool": tool,
            "label": label,
            "status": "pending",
            "progress": 0,
            "result": None,
            "error": "",
            "created_at": datetime.now().strftime("%H:%M:%S"),
            "runner": runner,
        }
        _jobs_store[job_id] = job
        _jobs_order.append(job_id)
        pub = _job_public(job)
    _job_queue.put(job_id)
    return pub


def run_ffmpeg_with_progress(command: list[str], expected_duration: float, on_progress=None):
    """Run an FFmpeg command and report 0..99% progress via -progress pipe:1."""
    if on_progress is None:
        subprocess.run(command, check=True, capture_output=True, text=True)
        return

    cmd = [command[0], "-progress", "pipe:1", "-nostats"] + command[1:]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    stderr_chunks: list[str] = []

    def _drain_stderr():
        if proc.stderr:
            stderr_chunks.append(proc.stderr.read())

    err_thread = _threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    try:
        if proc.stdout:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") and expected_duration > 0:
                    val = line.split("=", 1)[1].strip()
                    if val.isdigit():
                        sec = int(val) / 1_000_000.0
                        on_progress(min(99, int((sec / expected_duration) * 100)))
        ret = proc.wait()
        err_thread.join(timeout=2)
        if ret != 0:
            err_text = "".join(stderr_chunks)
            raise subprocess.CalledProcessError(ret, cmd, stderr=err_text)
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise


@app.get("/api/jobs")
def list_media_jobs(request: Request):
    session = _require_user_session(request)
    uid = session["user_id"]
    with _jobs_lock:
        user_jobs = [_job_public(_jobs_store[jid]) for jid in _jobs_order if jid in _jobs_store and _jobs_store[jid]["user_id"] == uid]
    running = [j for j in user_jobs if j["status"] == "running"]
    pending = [j for j in user_jobs if j["status"] == "pending"]
    finished = [j for j in reversed(user_jobs) if j["status"] in ("completed", "failed")]
    return running + pending + finished


@app.delete("/api/jobs/finished")
def clear_finished_jobs(request: Request):
    session = _require_user_session(request)
    uid = session["user_id"]
    removed = 0
    with _jobs_lock:
        to_del = [
            jid for jid in list(_jobs_order)
            if jid in _jobs_store and _jobs_store[jid]["user_id"] == uid and _jobs_store[jid]["status"] in ("completed", "failed")
        ]
        for jid in to_del:
            _jobs_store.pop(jid, None)
            _jobs_order.remove(jid)
            removed += 1
    return {"success": True, "removed": removed}


# ---------------------------------------------------------------------------
# Authentication (admin from .env, users stored encrypted in SQLite)
# ---------------------------------------------------------------------------
SESSION_COOKIE = "session"
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_LOGIN_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
ADMIN_USER_ID = -1
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
PROTECTED_STATIC_PATHS = {"/static/index.html", "/static/users.html"}
PUBLIC_PATHS = {"/login", "/api/auth/login"}

_login_failures: dict[tuple[str, str], list[float]] = {}


def get_fernet() -> Fernet:
    """Return the Fernet cipher; generate and persist ENCRYPTION_KEY in .env if missing."""
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        with ENV_FILE.open("a", encoding="utf-8") as env_file:
            env_file.write(f"\nENCRYPTION_KEY={key}\n")
        os.environ["ENCRYPTION_KEY"] = key
    return Fernet(key.encode())


FERNET = get_fernet()
LOOKUP_KEY = hashlib.sha256(b"username-lookup:" + os.environ["ENCRYPTION_KEY"].encode()).digest()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def username_lookup_hash(username: str) -> str:
    return hmac.new(LOOKUP_KEY, normalize_username(username).encode(), hashlib.sha256).hexdigest()


def encrypt_text(value: str) -> str:
    return FERNET.encrypt(value.encode()).decode()


def decrypt_text(value: str) -> str:
    try:
        return FERNET.decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


DUMMY_PASSWORD_HASH = hash_password(secrets.token_hex(8))


def init_auth_db():
    with db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username_hash TEXT NOT NULL UNIQUE,
                username_enc TEXT NOT NULL,
                full_name_enc TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                gemini_key_enc TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                expires_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS effects_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                video_path TEXT NOT NULL DEFAULT '',
                segments_json TEXT NOT NULL,
                transitions_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES app_users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS bokeh_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                video_path TEXT NOT NULL DEFAULT '',
                segments_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES app_users(id) ON DELETE CASCADE
            );
        """)
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(app_users)")}
        if "gemini_key_enc" not in user_columns:
            connection.execute("ALTER TABLE app_users ADD COLUMN gemini_key_enc TEXT NOT NULL DEFAULT ''")
        fx_columns = {row[1] for row in connection.execute("PRAGMA table_info(effects_projects)")}
        if "transitions_json" not in fx_columns:
            connection.execute("ALTER TABLE effects_projects ADD COLUMN transitions_json TEXT NOT NULL DEFAULT '[]'")
        connection.execute("DELETE FROM app_sessions WHERE expires_at < ?", (time.time(),))


init_auth_db()


def admin_credentials():
    return os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")


if not all(admin_credentials()):
    print("WARNING: ADMIN_USERNAME / ADMIN_PASSWORD are not set in .env - admin login is disabled.")


def create_session(user_id: int, role: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        connection.execute(
            "INSERT INTO app_sessions (token_hash, user_id, role, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, role, time.time() + SESSION_TTL_SECONDS),
        )
    return token


def get_session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db() as connection:
        row = connection.execute(
            "SELECT user_id, role, expires_at FROM app_sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < time.time():
            connection.execute("DELETE FROM app_sessions WHERE token_hash = ?", (token_hash,))
            return None
        if row["role"] == "admin":
            admin_name = admin_credentials()[0]
            if not admin_name:
                return None
            return {"user_id": ADMIN_USER_ID, "role": "admin", "username": admin_name, "name": "Administrator"}
        user = connection.execute(
            "SELECT username_enc, full_name_enc FROM app_users WHERE id = ?", (row["user_id"],)
        ).fetchone()
        if user is None:
            connection.execute("DELETE FROM app_sessions WHERE token_hash = ?", (token_hash,))
            return None
        return {
            "user_id": row["user_id"],
            "role": "user",
            "username": decrypt_text(user["username_enc"]),
            "name": decrypt_text(user["full_name_enc"]),
        }


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    session = get_session(request)
    request.state.session = session

    if path in PUBLIC_PATHS or (path.startswith("/static/") and path not in PROTECTED_STATIC_PATHS):
        return await call_next(request)

    is_api = path.startswith("/api/")
    if session is None:
        if is_api:
            return JSONResponse({"detail": "Login required"}, status_code=401)
        return RedirectResponse("/login")

    if session["role"] == "admin":
        admin_allowed = (
            path in ("/admin/users", "/static/users.html")
            or path.startswith("/api/admin/")
            or path.startswith("/api/auth/")
        )
        if not admin_allowed:
            if is_api:
                return JSONResponse({"detail": "Administrators can only manage users"}, status_code=403)
            return RedirectResponse("/admin/users")

    is_admin_route = path.startswith("/api/admin/") or path == "/admin/users" or path == "/static/users.html"
    if is_admin_route and session["role"] != "admin":
        if is_api:
            return JSONResponse({"detail": "Administrator access required"}, status_code=403)
        return RedirectResponse("/")

    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterUserRequest(BaseModel):
    name: str
    username: str
    password: str


def _client_key(request: Request, username: str):
    host = request.client.host if request.client else "unknown"
    return (host, normalize_username(username))


def _recent_failures(key):
    now = time.time()
    attempts = [stamp for stamp in _login_failures.get(key, []) if now - stamp < LOGIN_FAILURE_WINDOW_SECONDS]
    if attempts:
        _login_failures[key] = attempts
    else:
        _login_failures.pop(key, None)
    return attempts


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    session = getattr(request.state, "session", None)
    if session:
        return RedirectResponse("/admin/users" if session["role"] == "admin" else "/")
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/admin/users", include_in_schema=False)
def admin_users_page():
    return FileResponse(STATIC_DIR / "users.html")


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    username = payload.username.strip()
    password = payload.password
    key = _client_key(request, username)
    if len(_recent_failures(key)) >= MAX_LOGIN_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in a few minutes.")

    admin_name, admin_password = admin_credentials()
    admin_ok = bool(admin_name and admin_password) and hmac.compare_digest(
        username.encode(), admin_name.encode()
    ) and hmac.compare_digest(password.encode(), admin_password.encode())

    role, user_id = None, None
    if admin_ok:
        role, user_id = "admin", ADMIN_USER_ID
    else:
        with db() as connection:
            row = connection.execute(
                "SELECT id, password_hash FROM app_users WHERE username_hash = ?",
                (username_lookup_hash(username),),
            ).fetchone()
        stored_hash = row["password_hash"] if row else DUMMY_PASSWORD_HASH
        password_ok = verify_password(password, stored_hash)
        if row and password_ok:
            role, user_id = "user", row["id"]

    if role is None:
        _login_failures.setdefault(key, []).append(time.time())
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _login_failures.pop(key, None)
    token = create_session(user_id, role)
    response = JSONResponse({"success": True, "role": role})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True, samesite="lax", path="/"
    )
    return response


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db() as connection:
            connection.execute(
                "DELETE FROM app_sessions WHERE token_hash = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
    response = JSONResponse({"success": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/auth/me")
def auth_me(request: Request):
    session = request.state.session
    return {"username": session["username"], "name": session["name"], "role": session["role"]}


@app.get("/api/admin/users")
def list_users():
    with db() as connection:
        rows = connection.execute(
            "SELECT id, username_enc, full_name_enc, created_at FROM app_users ORDER BY id"
        ).fetchall()
    return [
        {
            "id": row["id"],
            "name": decrypt_text(row["full_name_enc"]),
            "username": decrypt_text(row["username_enc"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@app.post("/api/admin/users")
def register_user(payload: RegisterUserRequest):
    name = payload.name.strip()
    username = payload.username.strip()
    password = payload.password

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not USERNAME_PATTERN.match(username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 characters: letters, numbers, dot, dash or underscore",
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if normalize_username(username) == normalize_username(admin_credentials()[0]):
        raise HTTPException(status_code=409, detail="That username is reserved")

    try:
        with db() as connection:
            cursor = connection.execute(
                """INSERT INTO app_users (username_hash, username_enc, full_name_enc, password_hash)
                   VALUES (?, ?, ?, ?)""",
                (username_lookup_hash(username), encrypt_text(username), encrypt_text(name), hash_password(password)),
            )
            new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already exists")
    return {"id": new_id, "name": name, "username": username}


class ResetPasswordRequest(BaseModel):
    password: str


@app.post("/api/admin/users/{user_id}/reset-password")
@app.put("/api/admin/users/{user_id}/reset-password")
def reset_user_password(user_id: int, payload: ResetPasswordRequest):
    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    with db() as connection:
        cursor = connection.execute(
            "UPDATE app_users SET password_hash = ? WHERE id = ?",
            (hash_password(payload.password), user_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")
        connection.execute("DELETE FROM app_sessions WHERE user_id = ? AND role = 'user'", (user_id,))
    return {"success": True, "id": user_id}


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int):
    with db() as connection:
        cursor = connection.execute("DELETE FROM app_users WHERE id = ?", (user_id,))
        connection.execute("DELETE FROM app_sessions WHERE user_id = ? AND role = 'user'", (user_id,))
    return {"success": True, "id": user_id, "deleted": cursor.rowcount > 0}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
