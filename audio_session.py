# audio_session.py
from __future__ import annotations

import os
import threading
from typing import Dict, List, Tuple, Set, Optional

import numpy as np
import soundfile as sf
import librosa


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
        self.play_all = False

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
        y = data
        if tempo_rate != 1.0 and y.size > 0:
            y = librosa.effects.time_stretch(y=y, rate=tempo_rate)
        if abs(pitch_semitones) > 1e-3 and y.size > 0:
            y = librosa.effects.pitch_shift(
                y=y, sr=sr, n_steps=pitch_semitones
            )
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
                    orig = self.original_stem_data.get(name)
                    if orig is None:
                        continue
                    if progress_callback:
                        progress_callback(
                            completed / float(max(total_items, 1)),
                            f"{name}, {tempo_rate:.2f}x",
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
                    if progress_callback:
                        progress_callback(
                            completed / float(max(total_items, 1)),
                            f"mix, {tempo_rate:.2f}x",
                        )
                    new_mix = self._apply_tempo_pitch(
                        data=self.original_mix,
                        tempo_rate=tempo_rate,
                        pitch_semitones=pitch_semitones,
                        sr=sr,
                    )
                    completed += 1

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
                    progress_callback(1.0, "")

            except Exception as e:
                if log_callback:
                    log_callback(f"Error rebuilding audio for new tempo/pitch: {e}")

        with self._pending_lock:
            self._pending_generation += 1
            generation = self._pending_generation
            self.pending_ready = False
            self.pending_tempo_rate = tempo_rate
            self.pending_pitch_semitones = pitch_semitones

        thread = threading.Thread(target=worker, args=(generation,), daemon=True)
        thread.start()

    def set_active_stems(self, names: Set[str]):
        self.active_stems = set(names)

    def set_play_all(self, value: bool):
        self.play_all = bool(value)

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
        new_pitch_semitones = max(-3.0, min(3.0, float(new_pitch_semitones)))

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

        self._queue_build(
            tempo_rate=new_tempo_rate,
            pitch_semitones=new_pitch_semitones,
            stems_to_build=stems_to_process,
            include_mix=bool(include_mix),
            base_stems={},
            base_mix=None,
            mark_missing=True,
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def ensure_selection_ready(self, log_callback=None, progress_callback=None):
        """
        Ensure the currently selected playback source (mix or active stems)
        has buffers rendered for the *current* tempo/pitch. Missing stems/mix
        are rendered asynchronously and swapped in when ready.
        """
        if self.sample_rate is None:
            return

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
            return

        self._queue_build(
            tempo_rate=self.tempo_rate,
            pitch_semitones=self.pitch_semitones,
            stems_to_build=missing_stems,
            include_mix=mix_missing,
            base_stems=dict(self.current_stem_data),
            base_mix=self.current_mix_data,
            mark_missing=True,
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

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

            # Clear pending
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_missing_stems = set()
            self.pending_total_samples = 0
            self.pending_ready = False

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
        mix = np.zeros(frames, dtype="float32")

        if self.play_all and self.current_mix_data is not None:
            segment = self.current_mix_data[start:start + frames]
            if segment.size > 0:
                mix[:segment.size] += segment
        else:
            for name in list(self.active_stems):
                data = self.current_stem_data.get(name)
                if data is None:
                    continue
                segment = data[start:start + frames]
                if segment.size == 0:
                    continue
                mix[:segment.size] += segment

        return mix

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
