# Racing project video combiner
# Combines P1.mp4 through P13.mp4 in numeric order.

from pathlib import Path
import re
import shutil
import subprocess


PROJECT_DIR = Path(__file__).resolve().parent
INPUT_DIR = PROJECT_DIR / "inputvideos"
OUTPUT_FILE = PROJECT_DIR / "combined.mp4"


def natural_sort_key(path: Path):
    match = re.fullmatch(r"P(\d+)", path.stem, re.IGNORECASE)
    return (int(match.group(1)), path.name.lower()) if match else (10_000, path.name.lower())


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


def main():
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg and restart VS Code.")

    videos = sorted(
        (path for path in INPUT_DIR.glob("P*.mp4")
         if re.fullmatch(r"P\d+", path.stem, re.IGNORECASE)),
        key=natural_sort_key,
    )

    if not videos:
        raise FileNotFoundError(f"No P*.mp4 files found in {INPUT_DIR}")

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

    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(videos)}:v=1:a=1[outv][outa]"
    )

    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-r", "24",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(OUTPUT_FILE),
    ])

    subprocess.run(command, check=True)
    print(f"Combined {len(videos)} racing clips (P1 through P{natural_sort_key(videos[-1])[0]})")
    print(f"Created: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
