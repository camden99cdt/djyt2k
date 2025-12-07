# audio_player.py
from __future__ import annotations

from typing import Dict, List, Tuple, Set, Optional

import numpy as np
import sounddevice as sd  # only to probe devices at init

from audio_session import AudioSession
from loop_controller import LoopController
from playback_engine import PlaybackEngine


class StemAudioPlayer:
    global_master_volume: float = 1.0
    """
    High-level interface used by the GUI.

    Responsibilities:
      - Own an AudioSession (audio data + DSP)
      - Own a PlaybackEngine (sounddevice stream)
      - Manage play/pause/stop/seek
      - Expose envelopes & selection controls
    """

    def __init__(self, blocksize: Optional[int] = 1024):
        self.audio_ok: bool = False
        self.error_message: Optional[str] = None

        try:
            sd.query_devices()
            self.audio_ok = True
        except Exception as e:
            self.audio_ok = False
            self.error_message = str(e)

        self.session = AudioSession()
        self.engine: Optional[PlaybackEngine] = None
        # Frames per callback; larger buffers reduce CPU load at the cost of latency
        self.blocksize = blocksize

        self.render_progress_callback = None

        self.master_volume: float = 1.0
        self.gain_db: float = 0.0
        self.output_level: float = 0.0
        self.clipping: bool = False

        self.play_index: int = 0
        self.is_playing: bool = False
        self.is_paused: bool = False

        self.loop_controller = LoopController()

    # ---------- global master volume ----------

    @classmethod
    def set_global_master_volume(cls, volume: float):
        cls.global_master_volume = max(0.0, min(float(volume), 1.0))

    @classmethod
    def get_global_master_volume(cls) -> float:
        return cls.global_master_volume

    # ---------- loading wrappers ----------

    def load_audio(self, stems_dir: str, full_mix_path: str) -> Tuple[List[str], Dict[str, List[float]]]:
        """
        Load full mix + stems. Returns (stem_names, stem_envelopes).
        """
        self.stop()
        if not self.audio_ok:
            raise RuntimeError(f"Audio engine not available: {self.error_message}")

        stem_names, envelopes = self.session.load_audio(stems_dir, full_mix_path)
        self._reset_transport()
        self._ensure_engine()
        return stem_names, envelopes

    def load_mix_only(self, full_mix_path: str) -> Tuple[List[str], Dict[str, List[float]]]:
        """
        Load only full mix (skip separation). Returns ([], {}).
        """
        self.stop()
        if not self.audio_ok:
            raise RuntimeError(f"Audio engine not available: {self.error_message}")

        stem_names, envelopes = self.session.load_mix_only(full_mix_path)
        self._reset_transport()
        self._ensure_engine()
        return stem_names, envelopes

    def _reset_transport(self):
        self.play_index = 0
        self.is_playing = False
        self.is_paused = False
        self.loop_controller.reset_bounds()
        self.loop_controller.set_enabled(False)

    # ---------- envelopes / selection ----------

    def mix_envelopes(self, active_names: Set[str]) -> List[float]:
        return self.session.mix_envelopes(active_names)

    def get_mix_envelope(self) -> List[float]:
        return self.session.get_mix_envelope()

    def set_active_stems(self, names: Set[str]):
        self.session.set_active_stems(names)
        self.session.ensure_selection_ready(
            log_callback=getattr(self, "log_callback", None),
            progress_callback=self.render_progress_callback,
        )

    def set_play_all(self, value: bool):
        self.session.set_play_all(value)
        self.session.ensure_selection_ready(
            log_callback=getattr(self, "log_callback", None),
            progress_callback=self.render_progress_callback,
        )

    # ---------- tempo & pitch & volume ----------

    # audio_player.py

    def set_tempo_rate(self, rate: float):
        """
        Request a new tempo (0.25x–2.0x). The old audio keeps playing
        until the background rebuild is ready, then the session will
        swap in the new config (see _pull_audio).
        """
        if self.session.sample_rate is None:
            return

        rate = max(0.25, min(float(rate), 2.0))

        # Use current pitch, only tempo changes
        self.session.request_tempo_pitch_change(
            new_tempo_rate=rate,
            new_pitch_semitones=self.session.pitch_semitones,
            target_stems=set(self.session.active_stems),
            include_mix=self.session.play_all,
            # optionally pass a logger if you have one on the player:
            log_callback=getattr(self, "log_callback", None),
            progress_callback=self.render_progress_callback,
        )

    def set_tempo_and_pitch(self, rate: float, semitones: float):
        """Request a combined tempo/pitch change as a single rebuild."""
        if self.session.sample_rate is None:
            return

        rate = max(0.25, min(float(rate), 2.0))
        semitones = max(-6.0, min(float(semitones), 6.0))

        self.session.request_tempo_pitch_change(
            new_tempo_rate=rate,
            new_pitch_semitones=semitones,
            target_stems=set(self.session.active_stems),
            include_mix=self.session.play_all,
            log_callback=getattr(self, "log_callback", None),
            progress_callback=self.render_progress_callback,
        )


    def set_pitch_semitones(self, semitones: float):
        """
        Request a new pitch (-6..+6 st). Tempo stays the same.
        """
        if self.session.sample_rate is None:
            return

        semitones = max(-6.0, min(float(semitones), 6.0))

        self.session.request_tempo_pitch_change(
            new_tempo_rate=self.session.tempo_rate,
            new_pitch_semitones=semitones,
            target_stems=set(self.session.active_stems),
            include_mix=self.session.play_all,
            log_callback=getattr(self, "log_callback", None),
            progress_callback=self.render_progress_callback,
        )

    def set_render_progress_callback(self, callback):
        """Optional UI hook to receive render progress updates."""
        self.render_progress_callback = callback

    def cancel_pending_render(self):
        self.session.cancel_pending_render()


    def set_reverb_enabled(self, enabled: bool):
        self.session.set_reverb_enabled(enabled)

    def set_reverb_wet(self, wet: float):
        self.session.set_reverb_wet(wet)


    def set_master_volume(self, volume: float):
        self.master_volume = max(0.0, min(float(volume), 1.0))

    def set_gain_db(self, gain_db: float):
        self.gain_db = max(-10.0, min(float(gain_db), 10.0))

    def get_output_level(self) -> float:
        return self.output_level

    def is_clipping(self) -> bool:
        return self.clipping

    # ---------- playback engine ----------

    def _ensure_engine(self):
        if self.engine is None:
            if self.session.sample_rate is None:
                return
            self.engine = PlaybackEngine(
                sample_rate=self.session.sample_rate,
                pull_callback=self._pull_audio,
                blocksize=self.blocksize,
            )
            self.engine.start()

    def _pull_audio(self, frames: int) -> np.ndarray:
        """
        Called by the PlaybackEngine (sounddevice callback).
        """
        if not self.is_playing or self.is_paused:
            self.output_level = 0.0
            self.clipping = False
            return np.zeros(frames, dtype="float32")

        # 1) If a pending tempo/pitch config is ready, swap it in
        pos_seconds = self.get_position()  # play_index / sample_rate
        new_index = self.session.maybe_swap_pending(pos_seconds)
        if new_index is not None:
            self.play_index = new_index  # keep time continuous

        loop_bounds = self.loop_controller.get_bounds_samples(self.session.total_samples)
        loop_active = (
            self.loop_controller.enabled
            and loop_bounds is not None
            and self.play_index <= loop_bounds[1]
        )

        # 2) Now pull from the *current* config
        if loop_active and loop_bounds is not None:
            chunk = self._get_looping_chunk(loop_bounds[0], loop_bounds[1], frames)
            n = chunk.size
        else:
            chunk = self.session.get_chunk(self.play_index, frames)
            n = chunk.size
            if n == 0:
                self.is_playing = False
                self.is_paused = False
                self.play_index = 0
                self.output_level = 0.0
                self.clipping = False
                return np.zeros(frames, dtype="float32")

            self.play_index += n
            if (
                self.session.total_samples > 0
                and self.play_index >= self.session.total_samples
            ):
                self.is_playing = False
                self.is_paused = False
                self.play_index = 0

        # Apply master volume and clip
        gain = 10 ** (self.gain_db / 20.0)
        chunk = chunk * self.master_volume * gain * self.global_master_volume
        try:
            self.clipping = bool(np.any(np.abs(chunk) > 1.0))
        except Exception:
            self.clipping = False
        try:
            self.output_level = float(np.sqrt(np.mean(np.square(chunk))))
        except Exception:
            self.output_level = 0.0
        np.clip(chunk, -1.0, 1.0, out=chunk)

        # Pad if shorter than requested
        if n < frames:
            padded = np.zeros(frames, dtype="float32")
            padded[:n] = chunk
            return padded

        return chunk

    def _get_looping_chunk(self, loop_start: int, loop_end: int, frames: int) -> np.ndarray:
        """
        Build a chunk that respects loop boundaries [loop_start, loop_end).
        """
        total_samples = self.session.total_samples
        if total_samples <= 0 or loop_end <= loop_start:
            return np.zeros(frames, dtype="float32")

        chunk = np.zeros(frames, dtype="float32")
        filled = 0
        current_index = min(self.play_index, loop_end)

        while filled < frames:
            if current_index >= loop_end:
                current_index = loop_start

            remaining_loop = loop_end - current_index
            if remaining_loop <= 0:
                break

            to_copy = min(frames - filled, remaining_loop)
            segment = self.session.get_chunk(current_index, to_copy)
            n = segment.size

            if n == 0:
                break

            chunk[filled : filled + n] = segment
            filled += n
            current_index += n

            if current_index >= total_samples:
                current_index = loop_start

        if current_index >= loop_end:
            current_index = loop_start

        self.play_index = current_index
        return chunk

    def stop_stream(self):
        if self.engine is not None:
            self.engine.stop()
            self.engine = None

    # ---------- transport ----------

    def play(self):
        """
        Start or resume playback from current play_index.
        """
        if not self.audio_ok:
            return
        if self.session.sample_rate is None:
            return

        self._ensure_engine()
        self.is_playing = True
        self.is_paused = False

    def pause(self):
        if not self.audio_ok:
            return
        self.is_paused = True

    def stop(self):
        if not self.audio_ok:
            return
        self.is_playing = False
        self.is_paused = False
        self.play_index = 0

    def seek(self, pos_seconds: float):
        """
        Seek to pos_seconds in the stretched audio and start playback.
        """
        if not self.audio_ok or self.session.sample_rate is None:
            return

        duration = self.get_duration()
        pos_seconds = max(0.0, min(pos_seconds, duration))
        self.play_index = int(pos_seconds * self.session.sample_rate)
        self.is_playing = True
        self.is_paused = False
        self._ensure_engine()

    # ---------- looping ----------

    def set_loop_enabled(self, enabled: bool):
        self.loop_controller.set_enabled(enabled)

    def toggle_loop_enabled(self) -> bool:
        return self.loop_controller.toggle()

    def set_loop_start(self, position_seconds: float) -> bool:
        return self.loop_controller.set_start(position_seconds, self.get_duration())

    def set_loop_end(self, position_seconds: float) -> bool:
        return self.loop_controller.set_end(position_seconds, self.get_duration())

    def reset_loop_points(self):
        self.loop_controller.reset_bounds()

    def get_loop_bounds_seconds(self) -> tuple[float, float]:
        return self.loop_controller.get_bounds_seconds(self.get_duration())

    # ---------- query ----------

    def get_position(self) -> float:
        if self.session.sample_rate is None:
            return 0.0
        return self.play_index / float(self.session.sample_rate)

    def get_duration(self) -> float:
        return self.session.get_duration()

    # ---------- convenience for GUI ----------

    @property
    def stem_data(self) -> Dict[str, np.ndarray]:
        return self.session.stem_data

    @property
    def mix_data(self) -> Optional[np.ndarray]:
        return self.session.mix_data
