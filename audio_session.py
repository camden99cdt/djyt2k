# audio_session.py
from __future__ import annotations

import os
import threading
from typing import Dict, List, Tuple, Set, Optional

import numpy as np
import soundfile as sf
import librosa
from scipy.signal import butter, sosfilt, sosfilt_zi


class SimpleReverb:
    """
    Lightweight Schroeder-style reverb tuned for small block sizes.

    The design keeps processing cheap enough for audio callbacks while still
    giving a noticeably deep tail by combining a few comb filters with a short
    all-pass cascade. Each stem/mix keeps its own instance so the wet path only
    contains sources that are currently audible.
    """

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        comb_times = (0.0297, 0.0371, 0.0411, 0.0437)
        allpass_times = (0.005, 0.0017)

        self.comb_feedback = 0.78
        self.allpass_gain = 0.7

        self.comb_buffers = [
            np.zeros(max(1, int(sample_rate * t)), dtype="float32")
            for t in comb_times
        ]
        self.comb_indices = [0 for _ in self.comb_buffers]

        self.allpass_buffers = [
            np.zeros(max(1, int(sample_rate * t)), dtype="float32")
            for t in allpass_times
        ]
        self.allpass_indices = [0 for _ in self.allpass_buffers]

    def reset(self):
        for buf in self.comb_buffers + self.allpass_buffers:
            buf.fill(0.0)
        self.comb_indices = [0 for _ in self.comb_buffers]
        self.allpass_indices = [0 for _ in self.allpass_buffers]

    def process(self, input_chunk: np.ndarray) -> np.ndarray:
        x = np.asarray(input_chunk, dtype="float32")
        if x.size == 0:
            return np.zeros_like(x)

        y = np.zeros_like(x)

        for i, sample in enumerate(x):
            comb_sum = 0.0
            for idx, buf in enumerate(self.comb_buffers):
                tap = buf[self.comb_indices[idx]]
                comb_sum += tap
                buf[self.comb_indices[idx]] = sample + tap * self.comb_feedback
                self.comb_indices[idx] = (self.comb_indices[idx] + 1) % buf.size

            comb_avg = comb_sum / float(len(self.comb_buffers))

            ap_out = comb_avg
            for ap_idx, buf in enumerate(self.allpass_buffers):
                buf_index = self.allpass_indices[ap_idx]
                buf_out = buf[buf_index]
                buf[buf_index] = ap_out + buf_out * self.allpass_gain
                ap_out = -ap_out * self.allpass_gain + buf_out
                self.allpass_indices[ap_idx] = (buf_index + 1) % buf.size

            y[i] = ap_out

        np.clip(y, -1.0, 1.0, out=y)
        return y


class AudioSession:
    """
    Audio/DSP core, no sounddevice.

    Design:
      - original_*: immutable audio as loaded from disk
      - current_*:  processed audio for the *active* tempo/pitch
      - pending_*:  processed audio for a *new* tempo/pitch, built in a worker thread

    The audio callback should:
      1) Ask maybe_swap_pending(current_time) to atomically swap in any ready config
      2) Read from current_* only (no librosa, no heavy work)
    """

    LOW_CROSSOVER_HZ = 200.0
    HIGH_CROSSOVER_HZ = 2000.0

    def __init__(self):
        # sample rate
        self.sample_rate: Optional[int] = None

        # Original audio (never modified)
        self.original_stem_data: Dict[str, np.ndarray] = {}
        self.original_mix: Optional[np.ndarray] = None

        # CURRENT config (what playback actually uses right now)
        self.current_stem_data: Dict[str, np.ndarray] = {}
        self.current_mix_data: Optional[np.ndarray] = None
        self.tempo_rate: float = 1.0
        self.pitch_semitones: float = 0.0
        self.current_missing_stems: Set[str] = set()
        self.total_samples: int = 0  # for current config

        # Reverb
        self.reverb_enabled: bool = False
        self.reverb_wet: float = 0.45
        self.reverb_states: Dict[str, "SimpleReverb"] = {}

        # Frequency filtering
        self.frequency_bands_enabled: Dict[str, bool] = {
            "low": True,
            "mid": True,
            "high": True,
        }
        self._frequency_filters: Dict[str, np.ndarray] = {}
        self._frequency_filter_states: Dict[str, np.ndarray] = {}
        self._frequency_filter_lock = threading.Lock()
        self._frequency_band_gain: Dict[str, float] = {"low": 1.0, "mid": 1.0, "high": 1.0}
        self._frequency_band_gain_target: Dict[str, float] = {
            "low": 1.0,
            "mid": 1.0,
            "high": 1.0,
        }
        self._frequency_gain_ramp_samples = 1024

        # PENDING config (being built in the background for a new tempo/pitch)
        self.pending_stem_data: Dict[str, np.ndarray] = {}
        self.pending_mix_data: Optional[np.ndarray] = None
        self.pending_tempo_rate: float = 1.0
        self.pending_pitch_semitones: float = 0.0
        self.pending_missing_stems: Set[str] = set()
        self.pending_total_samples: int = 0
        self.pending_ready: bool = False
        self._pending_generation: int = 0  # to discard stale builds
        self._pending_lock = threading.Lock()

        # Envelopes for UI (built from ORIGINAL audio only)
        self.stem_envelopes: Dict[str, List[float]] = {}
        self.mix_envelope: List[float] = []

        # Playback configuration
        self.active_stems: Set[str] = set()
        self.play_all: bool = False  # True -> play full mix only
        # Track the target selection even while renders are pending.
        self.pending_target_play_all: bool = False
        self.pending_target_active_stems: Set[str] = set()

    # -------------------------------------------------------------------------
    # LOADING
    # -------------------------------------------------------------------------

    def load_audio(self, stems_dir: str, full_mix_path: str) -> Tuple[List[str], Dict[str, List[float]]]:
        """
        Load:
          - full mix from full_mix_path
          - all WAV stems in stems_dir
        into memory as mono float32.

        CURRENT config is initially just the original audio (tempo=1, pitch=0),
        with no librosa processing. Envelopes are built from ORIGINAL audio.
        """
        self._reset_state()

        # Load full mix
        mix_data, sr_mix = sf.read(full_mix_path, dtype="float32")
        if mix_data.ndim > 1:
            mix_data = mix_data.mean(axis=1)
        self.original_mix = mix_data
        self.sample_rate = sr_mix

        # Load stems
        wav_files = [
            f for f in os.listdir(stems_dir)
            if f.lower().endswith(".wav")
        ]
        wav_files.sort()
        if not wav_files:
            raise FileNotFoundError(f"No WAV files found in stems directory {stems_dir}")

        for filename in wav_files:
            stem_name = os.path.splitext(filename)[0]
            full_path = os.path.join(stems_dir, filename)

            data, sr = sf.read(full_path, dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)

            # Resample stems to match full-mix sample rate if needed
            if sr != self.sample_rate:
                data = librosa.resample(y=data, orig_sr=sr, target_sr=self.sample_rate)

            self.original_stem_data[stem_name] = data

        # INITIAL current config: identity (tempo=1, pitch=0, use originals)
        self.tempo_rate = 1.0
        self.pitch_semitones = 0.0
        self.current_stem_data = dict(self.original_stem_data)
        self.current_mix_data = self.original_mix
        self.total_samples = self._compute_total_samples(self.current_stem_data, self.current_mix_data)

        # Envelopes from original audio only
        self.stem_envelopes.clear()
        for stem_name, data in self.original_stem_data.items():
            self.stem_envelopes[stem_name] = self._build_envelope(data)
        self.mix_envelope = self._build_envelope(self.original_mix)

        self.active_stems = set(self.original_stem_data.keys())
        self.play_all = True
        self.pending_target_play_all = True
        self.pending_target_active_stems = set(self.active_stems)

        self._build_frequency_filters()

        # Pending config is empty
        with self._pending_lock:
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_total_samples = 0
            self.pending_ready = False
            self._pending_generation = 0

        return list(self.original_stem_data.keys()), dict(self.stem_envelopes)

    def load_mix_only(self, full_mix_path: str) -> Tuple[List[str], Dict[str, List[float]]]:
        """
        Load only the full mix (no stems).
        Used when 'skip separation' is enabled.
        Returns empty stem_names and empty envelopes dict.
        """
        self._reset_state()

        mix_data, sr_mix = sf.read(full_mix_path, dtype="float32")
        if mix_data.ndim > 1:
            mix_data = mix_data.mean(axis=1)
        self.original_mix = mix_data
        self.sample_rate = sr_mix

        # No stems
        self.original_stem_data.clear()
        self.stem_envelopes.clear()

        # INITIAL current config: identity (tempo=1, pitch=0)
        self.tempo_rate = 1.0
        self.pitch_semitones = 0.0
        self.current_stem_data = {}
        self.current_mix_data = self.original_mix
        self.total_samples = self._compute_total_samples(self.current_stem_data, self.current_mix_data)

        self.mix_envelope = self._build_envelope(self.original_mix)
        self.play_all = True
        self.active_stems = set()
        self.pending_target_play_all = True
        self.pending_target_active_stems = set()

        self._build_frequency_filters()

        # Pending config
        with self._pending_lock:
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_total_samples = 0
            self.pending_ready = False
            self._pending_generation = 0

        return [], {}

    def _reset_state(self):
        self.sample_rate = None

        self.original_stem_data.clear()
        self.original_mix = None

        self.current_stem_data.clear()
        self.current_mix_data = None
        self.tempo_rate = 1.0
        self.pitch_semitones = 0.0
        self.current_missing_stems = set()
        self.total_samples = 0

        self.stem_envelopes.clear()
        self.mix_envelope = []

        self.active_stems.clear()
        self.play_all = False
        self.pending_target_play_all = False
        self.pending_target_active_stems = set()

        self.reverb_states.clear()
        self.reverb_enabled = False
        self.reverb_wet = 0.45

        with self._frequency_filter_lock:
            self.frequency_bands_enabled = {"low": True, "mid": True, "high": True}
            self._frequency_filters = {}
            self._frequency_filter_states = {}
            self._frequency_band_gain = {"low": 1.0, "mid": 1.0, "high": 1.0}
            self._frequency_band_gain_target = {"low": 1.0, "mid": 1.0, "high": 1.0}
            self._frequency_gain_ramp_samples = 1024

        with self._pending_lock:
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_missing_stems = set()
            self.pending_total_samples = 0
            self.pending_ready = False
            self._pending_generation = 0

    # -------------------------------------------------------------------------
    # ENVELOPES (from ORIGINAL audio)
    # -------------------------------------------------------------------------

    @staticmethod
    def _build_envelope(data: Optional[np.ndarray], max_points: int = 1000) -> List[float]:
        """
        Build a normalized amplitude envelope from raw data, downsampled
        to at most max_points. Used only for drawing waveforms.
        """
        if data is None or data.size == 0:
            return []
        step = max(1, data.size // max_points)
        env = np.abs(data[::step])
        if env.size == 0:
            return []
        max_val = float(env.max() or 1.0)
        env = env / max_val
        return env.tolist()

    def get_mix_envelope(self) -> List[float]:
        return list(self.mix_envelope)

    def mix_envelopes(self, active_names: Set[str]) -> List[float]:
        """
        Mix envelopes of the given active stems (all built from original audio).
        Used for drawing waveforms when multiple stems are selected.
        """
        if not self.stem_envelopes:
            return []

        selected = [
            self.stem_envelopes[name]
            for name in active_names
            if name in self.stem_envelopes
        ]
        if not selected:
            any_env = next(iter(self.stem_envelopes.values()))
            return [0.0 for _ in any_env]

        length = min(len(env) for env in selected)
        if length == 0:
            return []

        mixed = np.zeros(length, dtype="float32")
        for env in selected:
            mixed += np.array(env[:length], dtype="float32")

        max_val = float(mixed.max() or 1.0)
        mixed /= max_val
        return mixed.tolist()

    # -------------------------------------------------------------------------
    # CONFIGURATION & REQUESTING NEW TEMPO/PITCH
    # -------------------------------------------------------------------------

    @staticmethod
    def _apply_tempo_pitch(
        data: np.ndarray, tempo_rate: float, pitch_semitones: float, sr: int
    ) -> np.ndarray:
        """
        Apply tempo and pitch changes while preserving perceived loudness.

        Librosa's phase-vocoder-based pitch shifting can introduce a gain drop
        and a slightly duller sound because of the internal resampling filter.
        To counteract that, we normalize the processed buffer back to the
        original RMS and clip to a safe range. Using the higher-quality SOXR
        resampler further reduces high-frequency loss.
        """

        if data.size == 0:
            return np.asarray(data, dtype="float32")

        y = np.asarray(data, dtype="float32")
        original_rms = float(np.sqrt(np.mean(np.square(y)))) or 1e-12

        if tempo_rate != 1.0:
            y = librosa.effects.time_stretch(y=y, rate=tempo_rate)
        if abs(pitch_semitones) > 1e-3:
            y = librosa.effects.pitch_shift(
                y=y,
                sr=sr,
                n_steps=pitch_semitones,
                res_type="soxr_hq",
            )

        processed_rms = float(np.sqrt(np.mean(np.square(y)))) or 1e-12
        gain = original_rms / processed_rms
        y = y * gain
        np.clip(y, -1.0, 1.0, out=y)
        return np.asarray(y, dtype="float32")

    def _queue_build(
        self,
        tempo_rate: float,
        pitch_semitones: float,
        stems_to_build: Set[str],
        include_mix: bool,
        base_stems: Dict[str, np.ndarray],
        base_mix: Optional[np.ndarray],
        mark_missing: bool,
        target_play_all: Optional[bool] = None,
        target_active_stems: Optional[Set[str]] = None,
        log_callback=None,
        progress_callback=None,
    ):
        if self.sample_rate is None:
            return

        stems_to_build = set(stems_to_build)
        include_mix = bool(include_mix) and self.original_mix is not None

        if not stems_to_build and not include_mix:
            return

        def worker(generation: int):
            try:
                def is_stale() -> bool:
                    with self._pending_lock:
                        return generation != self._pending_generation

                def abort_if_stale() -> bool:
                    if is_stale():
                        if log_callback:
                            log_callback("Tempo/pitch rebuild superseded; aborting current job.")
                        return True
                    return False

                if log_callback:
                    log_callback(
                        f"Rebuilding audio for tempo={tempo_rate:.2f}x, "
                        f"pitch={pitch_semitones:+.1f} st..."
                    )

                total_items = len(stems_to_build) + (1 if include_mix else 0)
                completed = 0

                sr = self.sample_rate
                if sr is None:
                    return

                new_stems: Dict[str, np.ndarray] = dict(base_stems)
                for name in stems_to_build:
                    if abort_if_stale():
                        return
                    orig = self.original_stem_data.get(name)
                    if orig is None:
                        continue
                    if progress_callback:
                        progress_callback(
                            completed / float(max(total_items, 1)),
                            f"{name}, {tempo_rate:.2f}x, {pitch_semitones:+.1f} st",
                            total_items,
                        )
                    new_stems[name] = self._apply_tempo_pitch(
                        data=orig,
                        tempo_rate=tempo_rate,
                        pitch_semitones=pitch_semitones,
                        sr=sr,
                    )
                    completed += 1

                new_mix = base_mix
                if include_mix and self.original_mix is not None:
                    if abort_if_stale():
                        return
                    if progress_callback:
                        progress_callback(
                            completed / float(max(total_items, 1)),
                            f"mix, {tempo_rate:.2f}x, {pitch_semitones:+.1f} st",
                            total_items,
                        )
                    new_mix = self._apply_tempo_pitch(
                        data=self.original_mix,
                        tempo_rate=tempo_rate,
                        pitch_semitones=pitch_semitones,
                        sr=sr,
                    )
                    completed += 1

                if abort_if_stale():
                    return

                new_total_samples = self._compute_total_samples(new_stems, new_mix)
                new_missing = (
                    set(self.original_stem_data.keys()) - set(new_stems.keys())
                    if mark_missing
                    else set()
                )

                with self._pending_lock:
                    if generation != self._pending_generation:
                        return

                    self.pending_stem_data = new_stems
                    self.pending_mix_data = new_mix
                    self.pending_tempo_rate = tempo_rate
                    self.pending_pitch_semitones = pitch_semitones
                    self.pending_total_samples = new_total_samples
                    self.pending_missing_stems = new_missing
                    self.pending_ready = True

                if log_callback:
                    log_callback("New tempo/pitch configuration ready.")

                if progress_callback:
                    progress_callback(1.0, "", total_items)

            except Exception as e:
                if log_callback:
                    log_callback(f"Error rebuilding audio for new tempo/pitch: {e}")

        with self._pending_lock:
            self._pending_generation += 1
            generation = self._pending_generation
            self.pending_ready = False
            self.pending_tempo_rate = tempo_rate
            self.pending_pitch_semitones = pitch_semitones
            self.pending_target_play_all = (
                self.play_all if target_play_all is None else bool(target_play_all)
            )
            self.pending_target_active_stems = set(
                self.active_stems
                if target_active_stems is None
                else target_active_stems
            )

        thread = threading.Thread(target=worker, args=(generation,), daemon=True)
        thread.start()

    def cancel_pending_render(self):
        """Abort any in-flight render tasks and clear pending buffers."""
        with self._pending_lock:
            self._pending_generation += 1
            self.pending_ready = False
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_missing_stems = set()
            self.pending_total_samples = 0
            self.pending_tempo_rate = self.tempo_rate
            self.pending_pitch_semitones = self.pitch_semitones
            self.pending_target_play_all = self.play_all
            self.pending_target_active_stems = set(self.active_stems)

    def reset_to_original_mix(self, current_position_seconds: Optional[float] = None) -> Optional[int]:
        """
        Immediately revert playback buffers to the original full mix at 1x tempo
        and 0 st pitch. This bypasses any stretched renders and clears pending
        work so the "Reset" control restores the true original audio.

        Returns a play_index (in samples) that preserves the fractional position
        through the track when possible.
        """
        sr = self.sample_rate
        if sr is None:
            return None

        old_total_samples = self.total_samples

        self.tempo_rate = 1.0
        self.pitch_semitones = 0.0
        self.current_stem_data = dict(self.original_stem_data)
        self.current_mix_data = self.original_mix
        self.current_missing_stems = set()
        self.total_samples = self._compute_total_samples(
            self.current_stem_data, self.current_mix_data
        )

        self.play_all = True
        self.active_stems = set(self.original_stem_data.keys())
        self.pending_target_play_all = True
        self.pending_target_active_stems = set(self.active_stems)

        with self._pending_lock:
            self._pending_generation += 1
            self.pending_ready = False
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_missing_stems = set()
            self.pending_total_samples = 0
            self.pending_tempo_rate = self.tempo_rate
            self.pending_pitch_semitones = self.pitch_semitones
            self.pending_target_play_all = self.play_all
            self.pending_target_active_stems = set(self.active_stems)

        self._sync_reverb_states(reset=True)

        if current_position_seconds is None:
            return 0

        if old_total_samples <= 0 or self.total_samples <= 0:
            return 0

        old_duration = old_total_samples / float(sr)
        new_duration = self.total_samples / float(sr)
        if old_duration <= 0.0 or new_duration <= 0.0:
            return 0

        progress = max(0.0, min(current_position_seconds / old_duration, 1.0))
        new_pos_seconds = progress * new_duration
        new_index = int(new_pos_seconds * sr)
        return min(new_index, self.total_samples - 1)

    def set_selection(
        self,
        play_all: bool,
        active_stems: Set[str],
        log_callback=None,
        progress_callback=None,
    ) -> bool:
        stems = {name for name in active_stems if name in self.original_stem_data}
        if not stems and self.original_stem_data:
            stems = set(self.original_stem_data.keys())

        self.play_all = bool(play_all)
        self.active_stems = stems
        self._sync_reverb_states()

        return self.ensure_selection_ready(
            log_callback=log_callback, progress_callback=progress_callback
        )

    def set_active_stems(
        self, names: Set[str], log_callback=None, progress_callback=None
    ) -> bool:
        return self.set_selection(
            play_all=False,
            active_stems=set(names),
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def set_play_all(self, value: bool, log_callback=None, progress_callback=None) -> bool:
        return self.set_selection(
            play_all=bool(value),
            active_stems=set(self.active_stems),
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def set_reverb_enabled(self, enabled: bool):
        self.reverb_enabled = bool(enabled)
        if not self.reverb_enabled:
            for state in self.reverb_states.values():
                state.reset()

    def set_reverb_wet(self, wet: float):
        self.reverb_wet = max(0.0, min(float(wet), 1.0))

    def _sync_reverb_states(self, reset: bool = False):
        targets = self._reverb_targets()
        for name in list(self.reverb_states.keys()):
            if name not in targets:
                self.reverb_states[name].reset()
                del self.reverb_states[name]

        if reset:
            for state in self.reverb_states.values():
                state.reset()

    def _reverb_targets(self) -> Set[str]:
        if self.play_all:
            return {"__mix__"} if self.current_mix_data is not None else set()
        return {name for name in self.active_stems if name in self.current_stem_data}

    def _get_reverb(self, name: str) -> SimpleReverb:
        if name not in self.reverb_states:
            if self.sample_rate is None:
                raise RuntimeError("Cannot build reverb without sample rate")
            self.reverb_states[name] = SimpleReverb(self.sample_rate)
        return self.reverb_states[name]

    # -------------------------------------------------------------------------
    # FREQUENCY FILTERING
    # -------------------------------------------------------------------------

    def _build_frequency_filters(self):
        if self.sample_rate is None:
            return

        nyquist = max(1.0, self.sample_rate / 2.0)
        low_norm = min(max(self.LOW_CROSSOVER_HZ / nyquist, 1e-4), 0.99)
        high_norm = min(max(self.HIGH_CROSSOVER_HZ / nyquist, low_norm + 1e-4), 0.99)

        self._frequency_gain_ramp_samples = max(1, int(0.01 * self.sample_rate))

        if high_norm <= low_norm:
            high_norm = min(0.99, low_norm + 0.05)
            low_norm = max(1e-4, high_norm - 0.05)

        filters = {
            "low": butter(4, low_norm, btype="lowpass", output="sos"),
            "mid": butter(4, [low_norm, high_norm], btype="bandpass", output="sos"),
            "high": butter(4, high_norm, btype="highpass", output="sos"),
        }

        with self._frequency_filter_lock:
            self._frequency_filters = filters
            self._reset_filter_state_locked()

    def _reset_filter_state_locked(self):
        self._frequency_filter_states = {
            name: sosfilt_zi(sos).astype("float32")
            for name, sos in self._frequency_filters.items()
        }

    def set_frequency_bands(self, low: bool, mid: bool, high: bool):
        with self._frequency_filter_lock:
            self.frequency_bands_enabled = {
                "low": bool(low),
                "mid": bool(mid),
                "high": bool(high),
            }
            self._frequency_band_gain_target = {
                "low": 1.0 if low else 0.0,
                "mid": 1.0 if mid else 0.0,
                "high": 1.0 if high else 0.0,
            }

    def _apply_frequency_filters(self, chunk: np.ndarray) -> np.ndarray:
        if chunk.size == 0:
            return chunk

        if all(self.frequency_bands_enabled.values()):
            return chunk

        if self.sample_rate is None:
            return chunk

        if not self._frequency_filters:
            self._build_frequency_filters()

        output = np.zeros_like(chunk, dtype="float32")

        with self._frequency_filter_lock:
            if self._frequency_filters and not self._frequency_filter_states:
                self._reset_filter_state_locked()

            chunk_len = chunk.shape[0]
            ramp_samples = max(1, int(self._frequency_gain_ramp_samples))

            for band in ("low", "mid", "high"):
                sos = self._frequency_filters.get(band)
                if sos is None:
                    continue

                filtered, self._frequency_filter_states[band] = sosfilt(
                    sos, chunk, zi=self._frequency_filter_states[band]
                )

                current_gain = float(self._frequency_band_gain.get(band, 1.0))
                target_gain = float(self._frequency_band_gain_target.get(band, 1.0))

                if current_gain != target_gain:
                    step = min(1.0, chunk_len / float(ramp_samples))
                    next_gain = current_gain + (target_gain - current_gain) * step
                    ramp = np.linspace(
                        current_gain,
                        next_gain,
                        num=chunk_len,
                        endpoint=True,
                        dtype="float32",
                    )
                    band_output = (
                        filtered.astype("float32") * ramp
                        if filtered.ndim == 1
                        else filtered.astype("float32") * ramp[:, None]
                    )
                    self._frequency_band_gain[band] = next_gain
                else:
                    band_output = filtered.astype("float32") * target_gain

                output += band_output

        np.clip(output, -1.0, 1.0, out=output)
        return output

    def request_tempo_pitch_change(
        self,
        new_tempo_rate: float,
        new_pitch_semitones: float,
        target_stems: Optional[Set[str]] = None,
        include_mix: Optional[bool] = None,
        log_callback=None,
        progress_callback=None,
    ):
        """
        Request a change in tempo/pitch.

        This does NOT block, and does NOT affect the audio currently playing.
        Instead, it spawns a background thread that:
          - processes all originals with librosa
          - stores them into pending_* buffers
          - marks pending_ready=True

        At playback time, maybe_swap_pending() will atomically swap current_* with
        pending_* at a safe boundary.
        """
        if self.sample_rate is None or (not self.original_stem_data and self.original_mix is None):
            return

        # Clamp / normalize
        new_tempo_rate = max(0.25, min(float(new_tempo_rate), 2.0))
        new_pitch_semitones = max(-6.0, min(6.0, float(new_pitch_semitones)))

        # If exactly the same as current, do nothing
        if (
            abs(new_tempo_rate - self.tempo_rate) < 1e-3
            and abs(new_pitch_semitones - self.pitch_semitones) < 1e-3
        ):
            return

        if include_mix is None:
            include_mix = self.play_all

        stems_to_process: Set[str]
        if target_stems is None:
            stems_to_process = set(self.original_stem_data.keys())
        else:
            stems_to_process = {
                name for name in target_stems if name in self.original_stem_data
            }

        # When the "All" mix is active, only rebuild the mix—even if active_stems
        # was populated earlier for fallback purposes. Rendering stems in this
        # mode is unnecessary and caused extra background work when changing
        # tempo/pitch while "All" was selected.
        if include_mix and self.play_all:
            stems_to_process.clear()

        self._queue_build(
            tempo_rate=new_tempo_rate,
            pitch_semitones=new_pitch_semitones,
            stems_to_build=stems_to_process,
            include_mix=bool(include_mix),
            base_stems={},
            base_mix=None,
            mark_missing=True,
            target_play_all=self.play_all,
            target_active_stems=set(self.active_stems),
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def ensure_selection_ready(self, log_callback=None, progress_callback=None) -> bool:
        """
        Ensure the currently selected playback source (mix or active stems)
        has buffers rendered for the *current* tempo/pitch. Missing stems/mix
        are rendered asynchronously and swapped in when ready.
        """
        if self.sample_rate is None:
            return False

        required_stems: Set[str] = set()
        include_mix = False

        if self.play_all:
            include_mix = self.original_mix is not None and self.original_mix.size > 0
        else:
            required_stems = {
                name for name in self.active_stems if name in self.original_stem_data
            }

        missing_stems = required_stems - set(self.current_stem_data.keys())
        mix_missing = include_mix and self.current_mix_data is None

        if not missing_stems and not mix_missing:
            return False

        self._queue_build(
            tempo_rate=self.tempo_rate,
            pitch_semitones=self.pitch_semitones,
            stems_to_build=missing_stems,
            include_mix=mix_missing,
            base_stems=dict(self.current_stem_data),
            base_mix=self.current_mix_data,
            mark_missing=True,
            target_play_all=self.play_all,
            target_active_stems=set(self.active_stems),
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

        return True

    def maybe_swap_pending(self, current_position_seconds: float) -> Optional[int]:
        """
        If a pending config is ready, atomically swap it into current_* and
        return a new play_index (in samples) that keeps the *fractional*
        position in the track the same.

        Example:
        old duration = 120s, position = 30s  -> progress = 0.25
        new duration = 240s                  -> new position = 60s
        """
        with self._pending_lock:
            if not self.pending_ready:
                return None

            sr = self.sample_rate
            if sr is None:
                return None

            # Capture old & new total samples before swapping
            old_total_samples = self.total_samples
            new_total_samples = self.pending_total_samples

            # Swap to new config
            self.current_stem_data = dict(self.pending_stem_data)
            self.current_mix_data = self.pending_mix_data
            self.tempo_rate = self.pending_tempo_rate
            self.pitch_semitones = self.pending_pitch_semitones
            self.total_samples = new_total_samples
            self.current_missing_stems = set(self.pending_missing_stems)
            self.play_all = self.pending_target_play_all
            self.active_stems = set(self.pending_target_active_stems)

            # Clear pending
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_missing_stems = set()
            self.pending_total_samples = 0
            self.pending_ready = False

        self._sync_reverb_states()

        # Compute new play index based on FRACTION through the old track
        if old_total_samples <= 0 or new_total_samples <= 0:
            return None

        old_duration = old_total_samples / float(sr)
        new_duration = new_total_samples / float(sr)

        if old_duration <= 0.0 or new_duration <= 0.0:
            return None

        # Fraction of track completed
        progress = current_position_seconds / old_duration
        progress = max(0.0, min(progress, 1.0))

        # New time position in seconds & samples
        new_pos_seconds = progress * new_duration
        new_index = int(new_pos_seconds * sr)

        # Clamp to end
        new_index = min(new_index, new_total_samples - 1)
        return new_index


    # -------------------------------------------------------------------------
    # MIXING FOR PLAYBACK (reads CURRENT config only)
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_total_samples(stems: Dict[str, np.ndarray], mix: Optional[np.ndarray]) -> int:
        total = 0
        candidates = list(stems.values())
        if mix is not None:
            candidates.append(mix)
        for arr in candidates:
            n = len(arr)
            if total == 0:
                total = n
            else:
                total = min(total, n)
        return total

    def get_chunk(self, start: int, frames: int) -> np.ndarray:
        """
        Return a mono chunk of length `frames` from either:
          - full mix (if play_all=True),
          - or selected stems.

        Reads only from current_* arrays (which should be prebuilt).
        """
        if self.sample_rate is None or self.total_samples <= 0:
            return np.zeros(frames, dtype="float32")

        if start >= self.total_samples:
            return np.zeros(frames, dtype="float32")

        frames = min(frames, self.total_samples - start)
        dry_mix = np.zeros(frames, dtype="float32")

        wet_amount = self.reverb_wet if self.reverb_enabled else 0.0
        wet_amount = max(0.0, min(wet_amount, 1.0))
        wet_mix = np.zeros(frames, dtype="float32") if wet_amount > 0 else None

        self._sync_reverb_states()

        played_any = False

        if self.play_all and self.current_mix_data is not None:
            segment = self.current_mix_data[start:start + frames]
            if segment.size > 0:
                dry_mix[:segment.size] += segment
                if wet_mix is not None:
                    wet_mix[:segment.size] += self._get_reverb("__mix__").process(segment)
                played_any = segment.size > 0
        else:
            for name in list(self.active_stems):
                data = self.current_stem_data.get(name)
                if data is None:
                    continue
                segment = data[start:start + frames]
                if segment.size == 0:
                    continue
                dry_mix[:segment.size] += segment
                if wet_mix is not None:
                    wet_mix[:segment.size] += self._get_reverb(name).process(segment)
                played_any = True

        if (not played_any) and self.current_mix_data is not None:
            segment = self.current_mix_data[start:start + frames]
            if segment.size > 0:
                dry_mix[:segment.size] += segment
                if wet_mix is not None:
                    wet_mix[:segment.size] += self._get_reverb("__mix__").process(segment)

        if wet_mix is not None:
            mixed = dry_mix * (1.0 - wet_amount) + wet_mix * wet_amount
            np.clip(mixed, -1.0, 1.0, out=mixed)
            return self._apply_frequency_filters(mixed)

        return self._apply_frequency_filters(dry_mix)

    # -------------------------------------------------------------------------
    # DURATION
    # -------------------------------------------------------------------------

    def get_duration(self) -> float:
        """
        Duration in seconds of the *current* config (what’s actually playing).
        """
        if self.sample_rate is None:
            return 0.0
        return self.total_samples / float(self.sample_rate)
