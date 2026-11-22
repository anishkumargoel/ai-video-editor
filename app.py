from pathlib import Path
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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


PROJECT_DIR = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_DIR / "static"
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


def generate_timestamped_music_prompt(video_path: str, user_context: str, model_id: str) -> str:
    """Upload a video, analyze scenes with user context, return a timestamped Lyria prompt."""
    import time
    from google import genai
    from google.genai import types

    client = genai.Client()
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
def generate_audio_prompt(project_id: int, payload: GenerateAudioPromptRequest):
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

    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set")
    try:
        import google.genai  # noqa: F401
    except ImportError:
        raise HTTPException(status_code=500, detail="google-genai is not installed")

    try:
        result = generate_timestamped_music_prompt(row["file_path"], text_context, model_id)
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

    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set")

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
        client = genai.Client()
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


def generate_structured_video_prompts(images, user_prompt: str, parts: int, model_id: str) -> VideoSeriesScript:
    """Send optional reference images + user instructions to Gemini and return a validated scene script."""
    from google import genai
    from google.genai import types

    client = genai.Client()
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
def generate_video_prompt(payload: GenerateVideoPromptRequest):
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

    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set")
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
        series = generate_structured_video_prompts(images, user_prompt, parts, model_id)
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
):
    """Generates a single video clip with synchronized audio using Google Veo."""
    import time
    from google import genai
    from google.genai import types

    client = genai.Client()

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
def generate_project_video(project_id: int, payload: GenerateSceneVideoRequest):
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

    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set")
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


@app.get("/api/gemini-key")
def get_gemini_key_status():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return {"configured": bool(key), "masked": mask_api_key(key)}


@app.post("/api/gemini-key")
def set_gemini_key(payload: GeminiKeyUpdate):
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY cannot be empty")
    os.environ["GEMINI_API_KEY"] = key
    return {"configured": True, "masked": mask_api_key(key)}


@app.delete("/api/gemini-key")
def clear_gemini_key():
    os.environ.pop("GEMINI_API_KEY", None)
    return {"configured": False, "masked": ""}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
