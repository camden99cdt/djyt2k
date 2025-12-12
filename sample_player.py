import numpy as np
import soundfile as sf

from audio_player import StemAudioPlayer
from playback_engine import PlaybackEngine


class SamplePlayer:
    def __init__(self):
        self.engine: PlaybackEngine | None = None
        self.sample_rate: int | None = None
        self.active_clips: list[dict] = []
        self._clip_counter: int = 0
        self.volume: float = 1.0
        self.output_level: float = 0.0
        self.clipping: bool = False

    def _ensure_engine(self):
        if self.sample_rate is None:
            return
        if self.engine is not None and self.engine.sample_rate != self.sample_rate:
            self.engine.stop()
            self.engine = None
        if self.engine is None:
            self.engine = PlaybackEngine(
                sample_rate=self.sample_rate,
                pull_callback=self._pull_audio,
                blocksize=1024,
            )
            self.engine.start()

    def _pull_audio(self, frames: int) -> np.ndarray:
        if not self.active_clips or self.sample_rate is None:
            self.output_level = 0.0
            self.clipping = False
            return np.zeros(frames, dtype="float32")

        buffer = np.zeros(frames, dtype="float32")
        remaining: list[dict] = []

        for clip in self.active_clips:
            data = clip["data"]
            start = clip["index"]
            chunk = np.zeros(frames, dtype="float32")
            write_cursor = 0
            remaining_frames = frames

            while remaining_frames > 0:
                if data.size == 0:
                    break
                end = min(start + remaining_frames, data.size)
                piece = data[start:end]
                take = piece.size
                if take:
                    chunk[write_cursor : write_cursor + take] = piece
                    write_cursor += take
                    remaining_frames -= take
                start = end

                if remaining_frames > 0:
                    if clip.get("loop"):
                        start = 0
                    else:
                        break

            clip["index"] = start % data.size if data.size else 0
            buffer[: chunk.size] += chunk

            if clip.get("loop") and data.size > 0:
                remaining.append(clip)
            elif write_cursor >= frames and start < data.size:
                remaining.append(clip)

        self.active_clips = remaining

        buffer *= self.volume * StemAudioPlayer.get_global_master_volume()
        try:
            self.clipping = bool(np.any(np.abs(buffer) > 1.0))
        except Exception:
            self.clipping = False
        try:
            self.output_level = float(np.sqrt(np.mean(np.square(buffer))))
        except Exception:
            self.output_level = 0.0
        np.clip(buffer, -1.0, 1.0, out=buffer)
        return buffer

    @staticmethod
    def _resample_to_match(data: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
        if data.size == 0 or source_rate == target_rate:
            return data

        duration = data.size / float(source_rate)
        target_len = max(1, int(round(duration * target_rate)))
        x_old = np.linspace(0.0, duration, num=data.size, endpoint=False)
        x_new = np.linspace(0.0, duration, num=target_len, endpoint=False)
        return np.interp(x_new, x_old, data).astype("float32")

    def play_clip(self, data: np.ndarray, sample_rate: int) -> bool:
        clip_id = f"anon_{self._clip_counter}"
        self._clip_counter += 1
        return self.start_clip(clip_id, data, sample_rate, start_index=0, loop=False)

    def start_clip(
        self,
        clip_id: str,
        data: np.ndarray,
        sample_rate: int,
        *,
        start_index: int = 0,
        loop: bool = False,
    ) -> bool:
        if data.size == 0 or sample_rate <= 0:
            return False

        if self.sample_rate is None:
            self.sample_rate = sample_rate
        elif sample_rate != self.sample_rate:
            data = self._resample_to_match(data, sample_rate, self.sample_rate)

        start_index = int(max(0, min(start_index, max(0, data.size - 1)))) if data.size else 0
        self.active_clips.append(
            {
                "id": clip_id,
                "data": np.asarray(data, dtype="float32"),
                "index": start_index,
                "loop": bool(loop),
            }
        )
        self._ensure_engine()
        return self.engine is not None

    def play_file(self, file_path: str) -> bool:
        try:
            data, sr = sf.read(file_path, dtype="float32")
        except Exception:
            return False

        if data.ndim > 1:
            data = data.mean(axis=1)
        return self.play_clip(data, sr)

    def set_volume(self, volume: float):
        self.volume = max(0.0, min(float(volume), 1.0))

    def get_volume(self) -> float:
        return self.volume

    def get_output_level(self) -> float:
        return self.output_level

    def is_clipping(self) -> bool:
        return self.clipping

    def get_active_clip_ids(self) -> set[str]:
        return {
            clip_id
            for clip_id in (clip.get("id") for clip in self.active_clips)
            if clip_id
        }

    def get_active_clip_positions(self) -> dict[str, int]:
        positions: dict[str, int] = {}
        for clip in self.active_clips:
            clip_id = clip.get("id")
            if not clip_id:
                continue
            try:
                positions[clip_id] = int(clip.get("index", 0))
            except Exception:
                positions[clip_id] = 0
        return positions

    def stop_clip(self, clip_id: str) -> int:
        if not clip_id:
            return -1
        remaining: list[dict] = []
        stopped_index = -1
        for clip in self.active_clips:
            if clip.get("id") == clip_id:
                stopped_index = int(clip.get("index", -1))
                continue
            remaining.append(clip)
        self.active_clips = remaining
        if not self.active_clips and self.engine is not None:
            try:
                self.engine.stop()
            finally:
                self.engine = None
                self.sample_rate = None
        return stopped_index

    def stop(self):
        self.active_clips = []
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        self.sample_rate = None
        self.output_level = 0.0
        self.clipping = False
