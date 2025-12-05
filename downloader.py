# downloader.py
import os
import subprocess
import json

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    USE_YTDLP_PYTHON = True
except ImportError:
    YoutubeDL = None
    DownloadError = None
    USE_YTDLP_PYTHON = False


def _log(log_callback, message: str):
    if log_callback is not None:
        log_callback(message)


def get_video_info(url: str, log_callback=None) -> dict:
    """
    Return {"title": str, "thumbnail_url": str|None} for a YouTube URL.
    Uses yt-dlp Python API if available, otherwise yt-dlp CLI.
    """
    if USE_YTDLP_PYTHON:
        _log(log_callback, "Getting metadata using yt-dlp Python module...")
        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
        }
        with YoutubeDL(ydl_opts) as ydl:  # type: ignore
            info = ydl.extract_info(url, download=False)
    else:
        _log(log_callback, "Getting metadata using yt-dlp CLI...")
        cmd = ["yt-dlp", "-J", "--no-playlist", url]
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "yt-dlp CLI not found. Install with `pip install yt-dlp` "
                "or put `yt-dlp` in your PATH."
            ) from e
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"yt-dlp metadata fetch failed with exit code {e.returncode}"
            ) from e

        info = json.loads(result.stdout)

    title = info.get("title", "Unknown title")
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb_url = thumbs[-1].get("url")

    return {"title": title, "thumbnail_url": thumb_url}


def download_audio(url: str, session_dir: str, log_callback=None) -> str:
    """
    Download audio from YouTube as WAV into session_dir and return WAV path.
    Uses yt-dlp Python API if available, otherwise yt-dlp CLI.
    """
    os.makedirs(session_dir, exist_ok=True)

    if USE_YTDLP_PYTHON:
        _log(log_callback, "Downloading using yt-dlp Python module...")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                    "preferredquality": "192",
                }
            ],
            "noplaylist": True,
        }

        try:
            with YoutubeDL(ydl_opts) as ydl:  # type: ignore
                info = ydl.extract_info(url, download=True)
                original_filename = ydl.prepare_filename(info)
        except DownloadError as e:  # type: ignore
            raise RuntimeError(f"yt-dlp download failed: {e}") from e

        base, _ = os.path.splitext(original_filename)
        wav_path = base + ".wav"
        if not os.path.exists(wav_path):
            raise FileNotFoundError(
                f"Expected WAV file not found: {wav_path}\n"
                "Check that ffmpeg is installed and in PATH."
            )
        return wav_path

    # CLI fallback
    _log(log_callback, "Downloading using yt-dlp CLI...")
    out_tmpl = os.path.join(session_dir, "%(title)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format",
        "wav",
        "--output",
        out_tmpl,
        "--no-playlist",
        url,
    ]
    _log(log_callback, "Running: " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as e:
        raise RuntimeError(
            "yt-dlp CLI not found. Install with `pip install yt-dlp` "
            "or put `yt-dlp` in your PATH."
        ) from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"yt-dlp CLI failed with exit code {e.returncode}") from e

    wav_files = [
        os.path.join(session_dir, f)
        for f in os.listdir(session_dir)
        if f.lower().endswith(".wav")
    ]
    if not wav_files:
        raise FileNotFoundError(
            "No WAV file found after yt-dlp run. "
            "Make sure ffmpeg is installed."
        )
    return wav_files[0]
