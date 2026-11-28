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
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
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
    raise HTTPException(status_code=400, detail="Kind must be 'video' or 'audio'")


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
def crop_media(payload: CropMediaRequest):
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
        command += ["-map", "[outv]", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
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

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        if output_path.exists():
            output_path.unlink()
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-800:]}")

    kept_seconds = sum(end - start for start, end in keep)
    return {
        "name": output_path.name,
        "path": str(output_path),
        "folder": str(output_path.parent),
        "original_duration": round(duration, 3),
        "new_duration": round(kept_seconds, 3),
    }


# ---------------------------------------------------------------------------
# Video effects (presets, color grading, vintage & stylized textures)
# ---------------------------------------------------------------------------
VIDEO_EFFECT_PRESETS = {"none", "grayscale", "sepia", "retro", "film"}


class VideoEffectsRequest(BaseModel):
    path: str
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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def build_video_effects_filtergraph(payload: VideoEffectsRequest) -> tuple[str, str]:
    """Build an FFmpeg filter_complex graph for presets, color grading, and textures.
    Returns (filter_complex, output_label).
    """
    preset = (payload.preset or "none").strip().lower()
    if preset not in VIDEO_EFFECT_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unsupported preset '{preset}'")

    contrast = _clamp(payload.contrast, 0.5, 2.0)
    saturation = _clamp(payload.saturation, 0.0, 2.0)
    warmth = _clamp(payload.warmth, -1.0, 1.0)
    white_balance = _clamp(payload.white_balance, -1.0, 1.0)
    light_leaks = _clamp(payload.light_leaks, 0.0, 1.0)
    film_grain = _clamp(payload.film_grain, 0.0, 1.0)
    scratches = _clamp(payload.scratches, 0.0, 1.0)
    vhs = _clamp(payload.vhs, 0.0, 1.0)
    glitch = _clamp(payload.glitch, 0.0, 1.0)
    lens_flares = _clamp(payload.lens_flares, 0.0, 1.0)

    has_any = (
        preset != "none"
        or abs(contrast - 1.0) > 0.005
        or abs(saturation - 1.0) > 0.005
        or abs(warmth) > 0.005
        or abs(white_balance) > 0.005
        or any(v > 0.005 for v in (light_leaks, film_grain, scratches, vhs, glitch, lens_flares))
    )
    if not has_any:
        raise HTTPException(status_code=400, detail="Select at least one preset, adjustment, or effect")

    chain = ["format=rgb24"]

    # 1. Presets
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

    # 2. Color grading
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

    # 3. Direct frame textures (fast C filters)
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

    graphs = [f"[0:v:0]{','.join(chain)}[vbase]"]
    current = "vbase"

    # 4. Procedural screen-blended overlays rendered at low res (160x90) for speed, then scaled up
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
            "gblur=sigma=10[leak_small]"
        )
        graphs.append(f"[leak_small][{current}]scale2ref[leak_full][v_for_leak]")
        graphs.append(f"[v_for_leak][leak_full]blend=all_mode=screen:all_opacity={op:.2f}:shortest=1[vleak]")
        current = "vleak"

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
            "gblur=sigma=5[flare_small]"
        )
        graphs.append(f"[flare_small][{current}]scale2ref[flare_full][v_for_flare]")
        graphs.append(f"[v_for_flare][flare_full]blend=all_mode=screen:all_opacity={op:.2f}:shortest=1[vflare]")
        current = "vflare"

    graphs.append(f"[{current}]format=yuv420p[vout]")
    return ";".join(graphs), "[vout]"


@app.post("/api/media/effects")
def apply_video_effects(payload: VideoEffectsRequest):
    file = resolve_media_file(payload.path, "video")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=500, detail="FFmpeg was not found")

    info = probe_media(ffmpeg, file)
    if not info["has_video"]:
        raise HTTPException(status_code=400, detail="The selected file has no video stream")

    filter_complex, out_label = build_video_effects_filtergraph(payload)

    output_path = file.with_name(f"{file.stem}_fx_{uuid.uuid4().hex[:8]}.mp4")
    while output_path.exists():
        output_path = file.with_name(f"{file.stem}_fx_{uuid.uuid4().hex[:8]}.mp4")

    command = [
        ffmpeg, "-hide_banner", "-y",
        "-i", str(file),
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        if output_path.exists():
            output_path.unlink()
        raise HTTPException(status_code=500, detail=f"FFmpeg failed: {error.stderr[-800:]}")

    return {
        "name": output_path.name,
        "path": str(output_path),
        "folder": str(output_path.parent),
    }


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
        """)
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(app_users)")}
        if "gemini_key_enc" not in user_columns:
            connection.execute("ALTER TABLE app_users ADD COLUMN gemini_key_enc TEXT NOT NULL DEFAULT ''")
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
