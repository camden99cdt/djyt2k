"""Pipeline orchestration for download, separation, and key detection."""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import demucs_runner
import downloader
from key_detection import detect_key_string

LogCallback = Callable[[str], None]
StatusCallback = Callable[[str], None]


@dataclass
class PipelineResult:
    """Container for results from running the media pipeline."""

    title: str
    thumbnail_url: Optional[str]
    session_dir: str
    audio_path: str
    stems_dir: Optional[str]
    song_key_text: Optional[str]

    @property
    def separated(self) -> bool:
        return self.stems_dir is not None


class PipelineRunner:
    """Coordinates download, optional separation, and key detection."""

    def __init__(
        self,
        log_callback: LogCallback,
        status_callback: StatusCallback,
        cache_dir_factory: Optional[Callable[[], str]] = None,
    ):
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.cache_dir_factory = cache_dir_factory or self._create_unique_cache_dir

    def process(self, url: str, skip_separation: bool) -> PipelineResult:
        self._set_status("Working...")
        self._log(f"Starting process for URL: {url}")

        try:
            info = self._fetch_video_info(url)
            title = info.get("title", "Unknown title")
            thumb_url = info.get("thumbnail_url")

            session_dir = self.cache_dir_factory()
            self._log(f"Using cache directory: {session_dir}")

            audio_path = self._download_audio(url, session_dir)
            song_key_text = self._detect_song_key(audio_path)
            stems_dir = self._maybe_separate(skip_separation, audio_path, session_dir)

            result = PipelineResult(
                title=title,
                thumbnail_url=thumb_url,
                session_dir=session_dir,
                audio_path=audio_path,
                stems_dir=stems_dir,
                song_key_text=song_key_text,
            )
            self._set_status("Done")
            return result
        except Exception:
            self._set_status("Error")
            raise

    def _fetch_video_info(self, url: str) -> dict:
        info = downloader.get_video_info(url, log_callback=self.log_callback)
        title = info.get("title", "Unknown title")
        self._log(f"Video title: {title}")
        return info

    def _download_audio(self, url: str, session_dir: str) -> str:
        audio_path = downloader.download_audio(
            url, session_dir, log_callback=self.log_callback
        )
        self._log(f"Downloaded audio to: {audio_path}")
        return audio_path

    def _detect_song_key(self, audio_path: str) -> Optional[str]:
        return detect_key_string(audio_path, log_callback=self.log_callback)

    def _maybe_separate(
        self, skip_separation: bool, audio_path: str, session_dir: str
    ) -> Optional[str]:
        if skip_separation:
            self._log("Skipping Demucs separation (user selected).")
            return None

        stems_dir = demucs_runner.run_demucs(
            audio_path, session_dir, log_callback=self.log_callback
        )
        self._log("Demucs separation complete.")
        self._log(f"Separated stems folder: {stems_dir}")
        return stems_dir

    def _log(self, message: str):
        self.log_callback(message)

    def _set_status(self, message: str):
        self.status_callback(message)

    @staticmethod
    def _create_unique_cache_dir() -> str:
        home = os.path.expanduser("~")
        base_cache = os.path.join(home, ".cache", "yt_demucs")
        os.makedirs(base_cache, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:8]
        session_dir = os.path.join(base_cache, f"session_{timestamp}_{unique_id}")
        os.makedirs(session_dir, exist_ok=True)
        return session_dir
