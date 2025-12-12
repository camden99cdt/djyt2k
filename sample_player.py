import numpy as np
import soundfile as sf

from playback_engine import PlaybackEngine


class SamplePlayer:
    def __init__(self):
        self.engine: PlaybackEngine | None = None
        self.sample_rate: int | None = None
        self.active_clips: list[dict] = []

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
            return np.zeros(frames, dtype="float32")

        buffer = np.zeros(frames, dtype="float32")
        remaining: list[dict] = []

        for clip in self.active_clips:
            data = clip["data"]
            start = clip["index"]
            end = min(start + frames, data.size)
            chunk = data[start:end]
            clip["index"] = end

            if chunk.size < frames:
                padded = np.zeros(frames, dtype="float32")
                padded[: chunk.size] = chunk
                chunk = padded

            buffer[: chunk.size] += chunk

            if end < data.size:
                remaining.append(clip)

        self.active_clips = remaining
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
        if data.size == 0 or sample_rate <= 0:
            return False

        if self.sample_rate is None:
            self.sample_rate = sample_rate
        elif sample_rate != self.sample_rate:
            data = self._resample_to_match(data, sample_rate, self.sample_rate)

        self.active_clips.append({"data": np.asarray(data, dtype="float32"), "index": 0})
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

    def stop(self):
        self.active_clips = []
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        self.sample_rate = None
