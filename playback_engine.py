# playback_engine.py
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import sounddevice as sd


class PlaybackEngine:
    """
    Thin wrapper around sounddevice.OutputStream.
    It pulls audio from a callback that returns a mono float32 numpy array.

    Parameters
    ----------
    blocksize: Optional[int]
        Frames per callback. Larger values lower CPU overhead but increase
        latency; set to None to let sounddevice choose automatically.
    """

    def __init__(
        self,
        sample_rate: int,
        pull_callback: Callable[[int], np.ndarray],
        blocksize: Optional[int] = 0,
        output_channels: int = 2,
        route_to_headphones: bool = False,
    ):
        self.sample_rate = sample_rate
        self.pull_callback = pull_callback
        self.blocksize = blocksize
        self.stream: Optional[sd.OutputStream] = None
        self.output_channels = max(1, int(output_channels))
        self.actual_channels = 1
        self.route_to_headphones = bool(route_to_headphones)

    def start(self):
        if self.stream is not None:
            return

        channels = self.output_channels
        try:
            default_output = sd.query_devices(kind="output")
            max_channels = int(default_output.get("max_output_channels", channels))
            if max_channels > 0:
                channels = min(channels, max_channels)
        except Exception:
            pass

        self.actual_channels = max(1, channels)

        self.stream = sd.OutputStream(
            samplerate=self.sample_rate,
            latency='low',
            channels=self.actual_channels,
            callback=self._audio_callback,
            blocksize=self.blocksize,
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

    def set_route_to_headphones(self, enabled: bool):
        self.route_to_headphones = bool(enabled)

    def _audio_callback(self, outdata, frames, time_info, status):
        samples = self.pull_callback(frames)
        if samples is None or samples.size == 0:
            outdata.fill(0)
            return

        samples = samples.astype("float32")
        n = min(frames, samples.size)

        outdata.fill(0)

        if self.route_to_headphones and outdata.shape[1] >= 2:
            outdata[:n, 1] = samples[:n]
        else:
            outdata[:n, 0] = samples[:n]
            if outdata.shape[1] > 1:
                outdata[:n, 1] = samples[:n]

        if n < outdata.shape[0]:
            outdata[n:, :].fill(0)
