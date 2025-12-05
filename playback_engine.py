# playback_engine.py
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import sounddevice as sd


class PlaybackEngine:
    """
    Thin wrapper around sounddevice.OutputStream.
    It pulls audio from a callback that returns a mono float32 numpy array.
    """

    def __init__(self, sample_rate: int, pull_callback: Callable[[int], np.ndarray]):
        self.sample_rate = sample_rate
        self.pull_callback = pull_callback
        self.stream: Optional[sd.OutputStream] = None

    def start(self):
        if self.stream is not None:
            return

        self.stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            callback=self._audio_callback,
            blocksize=1024,
        )
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # internal

    def _audio_callback(self, outdata, frames, time_info, status):
        samples = self.pull_callback(frames)
        if samples is None or samples.size == 0:
            outdata.fill(0)
            return

        samples = samples.astype("float32")
        n = min(frames, samples.size)

        outdata[:n, 0] = samples[:n]
        if outdata.shape[1] > 1:
            outdata[:n, 1] = samples[:n]
        if n < outdata.shape[0]:
            outdata[n:, :].fill(0)
