# Video Editor

A local web-based AI video and audio production studio built with **FastAPI**, **SQLite**, **FFmpeg**, **Google GenAI (Gemini, Veo, Lyria)**, and a standalone dark-mode HTML/CSS/JavaScript frontend.

It supports an end-to-end local media workflow: generating structured multi-scene video prompts, rendering video clips with Google Veo, generating audio prompts and music/soundtracks with Gemini and Lyria, previewing and sequencing MP4 clips, flagging clips, combining clips into normalized MP4s, and replacing video audio tracks.

---

## 1. Directory Structure

```text
video editor/
├── app.py               # FastAPI backend, SQLite persistence, range streaming, FFmpeg & GenAI orchestration
├── static/
│   ├── index.html       # Single-page dark-mode frontend (HTML, CSS, vanilla JS)
│   ├── login.html       # Login page
│   └── users.html       # Admin-only user registration page
├── combine_videos.py    # Standalone normalized MP4 concatenation CLI helper
├── replace_audio.py     # Standalone audio-replacement CLI helper
├── requirements.txt     # Python package dependencies
├── .env                 # Local secrets: admin login + encryption key (git-ignored, create from .env.example)
├── .env.example         # Template for .env
├── README.md            # Project documentation
└── video_editor.db      # SQLite database (auto-created on first run of app.py)
```

---

## 2. Requirements

### Operating System & System Packages
- **OS**: Windows 10 / Windows 11 (primary target; uses Windows drive/path browsing and WinGet FFmpeg discovery; also compatible with Linux/macOS if `ffmpeg` is on `PATH`).
- **Python**: Python **3.10+** (tested on Python 3.10).
- **FFmpeg**: Required for combining MP4 clips and replacing audio tracks.
  - Install on Windows via WinGet:
    ```powershell
    winget install Gyan.FFmpeg
    ```
  - `app.py` automatically detects `ffmpeg` on `PATH` or in:
    - `%LOCALAPPDATA%\Microsoft\WinGet\Packages\**\ffmpeg.exe`
    - `C:\ffmpeg\bin\ffmpeg.exe`
    - `C:\Program Files\ffmpeg\bin\ffmpeg.exe`
- **Modern Web Browser**: Chrome, Edge, or Firefox with HTML5 `<video>` / `<audio>` and Clipboard API support.

### Python Dependencies (`requirements.txt`)
| Package | Version | Purpose |
|---|---|---|
| `fastapi` | `0.121.0` | REST API framework, static file serving, HTTP byte-range streaming |
| `uvicorn` | `0.38.0` | ASGI server (imported and launched directly from `app.py`) |
| `google-genai` | `1.52.0` | Google Gemini (prompt generation), Veo (video generation), and Lyria (music/audio generation) SDK |
| `cryptography` | `46.0.3` | Fernet encryption of stored user names/usernames |
| `pydantic` | *(installed with FastAPI)* | Request validation and structured JSON schemas for Gemini outputs |

All other backend modules (`sqlite3`, `pathlib`, `subprocess`, `base64`, `uuid`, `datetime`, `shutil`, `time`, `os`, `json`, `urllib.parse`) are part of the Python standard library. The frontend uses zero external npm/CDN dependencies.

### Authentication & `.env`
The whole app (UI and every `/api/*` route) requires login. No default admin credentials or encryption key are committed to this repository — you must create your own `.env` file before running the app.

| Variable | Purpose |
|---|---|
| `ADMIN_USERNAME` | Admin login username (not stored in SQLite). |
| `ADMIN_PASSWORD` | Admin login password (not stored in SQLite). |
| `ENCRYPTION_KEY` | Fernet symmetric key used to encrypt stored user names, usernames, and per-user Gemini API keys in SQLite. **Back it up** — losing it makes encrypted fields unreadable. |

#### Generating `.env` credentials and encryption key
1. Copy the template:
   ```powershell
   Copy-Item .env.example .env
   ```
2. Generate a strong admin password:
   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(16))"
   ```
3. Generate a Fernet `ENCRYPTION_KEY`:
   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
4. Paste your chosen `ADMIN_USERNAME`, generated `ADMIN_PASSWORD`, and `ENCRYPTION_KEY` into `.env`:
   ```env
   ADMIN_USERNAME=your-admin-username
   ADMIN_PASSWORD=your-generated-password
   ENCRYPTION_KEY=your-generated-fernet-key
   ```
   *(If `ENCRYPTION_KEY` is omitted, `app.py` automatically generates one and appends it to `.env` on first startup.)*

- **Admin** logs in with the `.env` credentials and lands on `/admin/users`, where they can add users, reset user passwords, and delete users. The admin has no access to projects, video/audio features, or Gemini keys (those routes return `403` / redirect).
- **Registered users** log in at `/login` and then see the normal application. They cannot open the registration page or call `/api/admin/*`.
- **Storage (`app_users` table)**: name, username and Gemini key are Fernet-encrypted; a keyed HMAC of the username is stored for login lookup/uniqueness; passwords are one-way scrypt hashes with a per-user salt (passwords are never stored in reversible form).
- **Sessions**: random token in an `HttpOnly`, `SameSite=Lax` cookie (12 hours), stored hashed in `app_sessions`. Logout, password reset, or deleting a user ends their sessions.
- Logins are rate-limited (5 failures per 5 minutes per client + username).
- Usernames: 3-32 characters (letters, numbers, `.`, `-`, `_`), case-insensitive. Passwords: at least 8 characters.

### Gemini API key (per user)
AI features (**Generate video prompt**, **Generate video**, **Generate audio prompt**, **Generate audio**) use the logged-in user's own Gemini API key:
- Click **Manage Gemini Key** in the header to add, change (save again) or clear your key.
- The key is stored **Fernet-encrypted** in `app_users.gemini_key_enc`; the UI only ever shows a masked value (`AIza...1234`).
- If no key is saved, AI endpoints return a 400 asking the user to add one. There is no shared/global `GEMINI_API_KEY` fallback.

---

## 3. Installation & Running the App

1. **Create/activate a Python 3.10+ environment** (optional but recommended):
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. **Install Python dependencies**:
   ```powershell
   pip install -r requirements.txt
   ```

3. **Start the application**:
   ```powershell
   python app.py
   ```
   `app.py` starts Uvicorn on `http://127.0.0.1:8000` and automatically initializes `video_editor.db`.

4. **Open in browser**:
   - Web UI: `http://127.0.0.1:8000/`
   - Interactive OpenAPI docs: `http://127.0.0.1:8000/docs`

---

## 4. Application Components

### Project Landing Page
- Create a project with a name and three local directories:
  - **Input video folder** (must exist)
  - **Input audio folder** (must exist)
  - **Output video folder** (created automatically if missing)
- Includes a non-blocking, server-backed in-page folder browser (`GET /api/browse`).
- Lists existing projects from SQLite for one-click reopening.
- Right-side **Tools** sidebar: **Crop Video**, **Crop Audio**, and **Video Effects** (presets: Grayscale, Sepia, Retro, Film; color grading: Contrast, Saturation, Warmth, White Balance; vintage/stylized textures: Light leaks, Film grain, Scratches, VHS distortion, Glitch, Lens flares — with live preview and FFmpeg rendering to a new non-overwriting `_fx_` file in the same folder).
  - **Crop Video** and **Crop Audio** buttons open an interactive media trimmer where you can browse local video/audio files, preview playback (with optional skipping of marked cuts), remove segments from the start, end, or any one or more parts of the middle, and save to a new non-overwriting file (`<original>_cropped_<id>.<ext>`) in the same folder.
- Header controls: **Manage Gemini Key** (add/change/clear your own Gemini key) and **`Home`** (return to the home screen with the project list and project creation form).

### Workspace Tabs

1. **Generate video prompt** *(default tab)*
   - **LLM-based generation**: Select a Gemini model (`gemini-2.5-pro` default, `gemini-3.8-pro`, `gemini-3.8-flash`, `gemini-2.5-flash`), optionally upload up to 10 reference images (15 MB max each), specify the number of parts/scenes (1–60), and enter text instructions.
   - Generalized system prompt adapts to any visual style (photorealistic cinema, stop-motion, 3D, etc.), continuous stories or episodic scenes, dialogue with lip-sync or silent action, and defaults strictly to **8-second clips** (matching the Veo API max).
   - Saves one self-contained record per scene in `video_prompts` formatted with `[Master Style & Characters]`, `[Cinematography]`, `[Subject]`, `[Action]` / `[Action & Lip-Sync]`, `[Context]`, `[Style & Ambiance]`, and `[Audio]`.
   - **Manual form**: Add individual scene prompts (`model`, `scene_number`, `instructions`).
   - **Saved video prompts**: Inline **Edit**, **Copy**, and **Delete** per scene.

2. **Generate video**
   - Lists all saved video prompt scenes.
   - Configure **Veo model** (`veo-3.1-generate-preview` default, `veo-3.0-generate-001`), **Aspect ratio** (`16:9`, `9:16`), and **Duration** (`8s`, `6s`, `4s`).
   - Per scene: optionally upload a reference image, **Edit** the prompt inline, click **Generate video**, and preview the rendered MP4.
   - Saves generated MP4 files directly into the project's **Input videos** folder (`scene_<N>_<MMDDYYYYHHMM>.mp4`) and refreshes the Input videos tab.

3. **Generate audio prompt**
   - **LLM-based generation from video**: Select a Gemini model (`gemini-3.8-flash` default), choose an existing output MP4, provide text context, and generate a timestamped instrumental/foley prompt via the Gemini Files API.
   - **Manual form**: Add custom audio prompts (`model`, `instructions`).
   - **Saved prompts**: Inline **Edit** and **Delete** per record.

4. **Generate audio**
   - Select one saved audio prompt and click **Generate music** using Google GenAI (`lyria-3.5` by default).
   - Saves the generated MP3 into the project's **Input audio** folder (`<prompt_id>_<MMDDYYYYHHMM>.mp3`) and previews it in an HTML5 `<audio>` player.

5. **Input videos**
   - Lists `.mp4` files from the project's input video folder (sorted naturally, e.g., `P2.mp4` before `P10.mp4`).
   - Byte-range streaming preview (`HTTP 206` / `416`), checkbox selection, drag-and-drop reordering, **Flag**, and **Combine**.
   - **Combine** normalizes all selected clips via FFmpeg (`1280x720`, centered padding, `24 fps`, `yuv420p`, `libx264` CRF 18, `48 kHz` stereo AAC 192 kbps, timestamp resets, `+faststart`) into a unique `combined_<uuid12>_<N>clips.mp4`.

6. **Flagged videos**
   - Preview flagged input clips and bulk-remove flags from SQLite.

7. **Output videos**
   - Synchronizes and lists `combined_*.mp4` files from the output folder, plays via range streaming, and supports idempotent bulk deletion from disk and SQLite.

8. **Replace audio**
   - 4-column layout: **MP3 / audio files**, **Output MP4 files**, **Replaced videos**, and a dynamic `<audio>`/`<video>` preview panel.
   - Uses FFmpeg with `-stream_loop -1`, `-shortest`, `-c:v copy`, and `-c:a aac -b:a 192k` so shorter audio loops seamlessly and longer audio cuts cleanly at the video end. Produces `replaced_<uuid12>_<stem>.mp4`.

---

## 5. SQLite Data Model (`video_editor.db`)

Automatically created and migrated by `init_db()` on startup:

| Table | Purpose | Key Columns |
|---|---|---|
| `projects` | Project folder configurations | `id`, `name`, `input_video_folder`, `input_audio_folder`, `output_video_folder`, `created_at` |
| `flagged_videos` | Flagged input MP4 filenames per project | `id`, `project_id`, `video_name`, `created_at` |
| `combined_videos` | Combined MP4 metadata & clip order | `id`, `project_id`, `name`, `file_path`, `sequence_json`, `created_at` |
| `replaced_videos` | Replaced-audio MP4 outputs | `id`, `project_id`, `name`, `file_path`, `source_video_name`, `source_audio_name`, `created_at` |
| `audio_prompts` | Audio/music prompts (seeded with 1 default `lyria-3.5` prompt if empty) | `id`, `created_on`, `model`, `instructions`, `video_file`, `text_context` |
| `app_users` | Registered users (encrypted) | `id`, `username_hash`, `username_enc`, `full_name_enc`, `password_hash`, `gemini_key_enc`, `created_at` |
| `app_sessions` | Active login sessions | `token_hash`, `user_id`, `role`, `expires_at` |
| `effects_projects` | Saved per-user timed Video Effects projects | `id`, `user_id`, `name`, `video_path`, `segments_json`, `created_at`, `updated_at` |
| `video_prompts` | Per-scene video prompts grouped by series | `id`, `created_on`, `model`, `instructions`, `image_files`, `text_context`, `parts`, `scene_number`, `series_id` |

---

## 6. REST API Summary

- **Auth**: `GET /login`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`
- **Admin only** (the admin can use only these and `/api/auth/*`): `GET /admin/users`, `GET /api/admin/users`, `POST /api/admin/users`, `POST|PUT /api/admin/users/{id}/reset-password`, `DELETE /api/admin/users/{id}`
- **UI & Key**: `GET /`, `GET /api/browse`, `GET /api/gemini-key`, `POST|PUT /api/gemini-key`, `DELETE /api/gemini-key` (users only)
- **Projects**: `GET /api/projects`, `POST /api/projects`
- **Video Prompts & Generation**: `GET /api/video-prompts`, `POST /api/video-prompts`, `PUT /api/video-prompts/{id}`, `DELETE /api/video-prompts/{id}`, `POST /api/generate-video-prompt`, `POST /api/projects/{id}/generate-video`
- **Audio Prompts & Generation**: `GET /api/audio-prompts`, `POST /api/audio-prompts`, `PUT /api/audio-prompts/{id}`, `DELETE /api/audio-prompts/{id}`, `POST /api/projects/{id}/generate-audio-prompt`, `POST /api/projects/{id}/generate-music`
- **Input / Flagged / Output Videos**: `GET /api/projects/{id}/videos`, `GET /api/projects/{id}/video/{filename}`, `POST /api/projects/{id}/flag`, `GET /api/projects/{id}/flagged`, `DELETE /api/projects/{id}/flagged/{filename}`, `POST /api/projects/{id}/combine`, `GET /api/projects/{id}/outputs`, `GET /api/projects/{id}/outputs/{filename}`, `DELETE /api/projects/{id}/outputs/{filename}`
- **Crop Media**: `GET /api/media/browse`, `GET /api/media/info`, `GET /api/media/stream`, `POST /api/media/crop`, `POST /api/media/effects`, `GET|POST /api/effects-projects`, `PUT|DELETE /api/effects-projects/{id}`
- **Replace Audio**: `GET /api/projects/{id}/replace-audio/assets`, `POST /api/projects/{id}/replace-audio`, `GET /api/projects/{id}/replaced/{filename}`, `DELETE /api/projects/{id}/replaced/{filename}`, `GET /api/projects/{id}/source-audio/{filename}`, `GET /api/projects/{id}/source-output/{filename}`

---

## 7. Standalone CLI Scripts

- **`combine_videos.py`**: Standalone script to normalize and concatenate MP4 clips with FFmpeg.
- **`replace_audio.py`**: Standalone script to replace an MP4's audio track with a looping/trimmed MP3.
