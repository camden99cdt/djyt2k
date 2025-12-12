import numpy as np
import soundfile as sf

from playback_engine import PlaybackEngine


class SamplePlayer:
    def __init__(self):
        self.engine: PlaybackEngine | None = None
        self.sample_rate: int | None = None
        self.data: np.ndarray = np.zeros(0, dtype="float32")
        self.play_index = 0
        self.is_playing = False

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
        if not self.is_playing or self.sample_rate is None:
            return np.zeros(frames, dtype="float32")

        if self.data.size == 0:
            self.is_playing = False
            return np.zeros(frames, dtype="float32")

        start = self.play_index
        end = min(start + frames, self.data.size)
        chunk = self.data[start:end]
        self.play_index = end

        if end >= self.data.size:
            self.is_playing = False

        if chunk.size < frames:
            padded = np.zeros(frames, dtype="float32")
            padded[: chunk.size] = chunk
            return padded

        return chunk

    def play_clip(self, data: np.ndarray, sample_rate: int) -> bool:
        if data.size == 0 or sample_rate <= 0:
            return False

        self.data = np.asarray(data, dtype="float32")
        self.sample_rate = sample_rate
        self.play_index = 0
        self.is_playing = True
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
        self.is_playing = False
        self.play_index = 0
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
