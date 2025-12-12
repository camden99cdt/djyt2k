"""Pipeline orchestration for download, separation, and key detection.

This module also exposes a process-based worker that isolates the heavy
pipeline from the GUI and audio threads.
"""
from __future__ import annotations

import os
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Process, Queue, get_context
from queue import Empty
from typing import Any, Callable, Optional

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


@dataclass
class PipelineMessage:
    """Cross-process messages emitted by the pipeline worker."""

    kind: str
    payload: Any = None


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

            with ThreadPoolExecutor(max_workers=2) as executor:
                key_future = executor.submit(self._detect_song_key, audio_path)
                stems_future = None

                if not skip_separation:
                    stems_future = executor.submit(
                        self._maybe_separate, skip_separation, audio_path, session_dir
                    )

                song_key_text = key_future.result()
                stems_dir = stems_future.result() if stems_future else None

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


def _run_pipeline_subprocess(
    url: str,
    skip_separation: bool,
    cache_dir_factory: Optional[Callable[[], str]],
    queue: Queue,
):
    """Entry point for executing the pipeline inside a worker process."""

    runner = PipelineRunner(
        log_callback=lambda message: queue.put(PipelineMessage("log", message)),
        status_callback=lambda status: queue.put(PipelineMessage("status", status)),
        cache_dir_factory=cache_dir_factory,
    )

    try:
        result = runner.process(url, skip_separation)
        queue.put(PipelineMessage("result", result))
    except Exception:
        queue.put(PipelineMessage("error", traceback.format_exc()))
    finally:
        queue.put(PipelineMessage("complete"))


class PipelineProcessWorker:
    """Runs the pipeline in an isolated background process.

    Logs, status updates, and results are emitted via a queue so callers can
    update the GUI thread without blocking it or the audio callback.
    """

    def __init__(self, cache_dir_factory: Optional[Callable[[], str]] = None):
        self.cache_dir_factory = cache_dir_factory
        self.process: Process | None = None
        self.queue: Queue | None = None

    def start(self, url: str, skip_separation: bool):
        if self.process and self.process.is_alive():
            raise RuntimeError("Pipeline worker already running")

        ctx = get_context("spawn")
        self.queue = ctx.Queue()
        self.process = ctx.Process(
            target=_run_pipeline_subprocess,
            args=(url, skip_separation, self.cache_dir_factory, self.queue),
            daemon=True,
        )
        self.process.start()

    def poll_messages(self) -> list[PipelineMessage]:
        if not self.queue:
            return []

        messages: list[PipelineMessage] = []
        while True:
            try:
                messages.append(self.queue.get_nowait())
            except Empty:
                break
        return messages

    def join(self, timeout: Optional[float] = None):
        if self.process:
            self.process.join(timeout=timeout)
