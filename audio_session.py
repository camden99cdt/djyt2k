# audio_session.py
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Set, Optional

import numpy as np
import soundfile as sf
import librosa


@dataclass
class RenderPlan:
    tempo_rate: float
    pitch_semitones: float
    include_mix: bool
    mark_missing: bool
    target_play_all: bool
    target_active_stems: Set[str]
    requested_stems: Set[str] = field(default_factory=set)
    remaining_stems: List[str] = field(default_factory=list)
    completed_cache: Dict[str, np.ndarray] = field(default_factory=dict)
    mix_data: Optional[np.ndarray] = None
    mix_pending: bool = False
    cancelled: bool = False
    current_task: Optional[str] = None

    def total_items(self) -> int:
        total = len(self.requested_stems)
        if self.include_mix:
            total += 1
        return max(total, 0)

    def completed_items(self) -> int:
        completed = len(self.requested_stems & set(self.completed_cache.keys()))
        if self.include_mix and self.mix_data is not None:
            completed += 1
        return completed


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
        self._pending_condition = threading.Condition(self._pending_lock)
        self._render_plan: Optional["RenderPlan"] = None
        self._render_thread: Optional[threading.Thread] = None

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

        with self._pending_lock:
            self.pending_stem_data = {}
            self.pending_mix_data = None
            self.pending_missing_stems = set()
            self.pending_total_samples = 0
            self.pending_ready = False
            self._pending_generation = 0
            self._render_plan = None
            self._render_thread = None

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

        stems_to_build = {name for name in stems_to_build if name in self.original_stem_data}
        include_mix = bool(include_mix) and self.original_mix is not None

        if not stems_to_build and not include_mix:
            return

        base_completed = dict(base_stems)
        base_mix_value = base_mix

        with self._pending_condition:
            self._pending_generation += 1
            generation = self._pending_generation
            target_play_all = self.play_all if target_play_all is None else bool(target_play_all)
            target_active = (
                set(self.active_stems)
                if target_active_stems is None
                else set(target_active_stems)
            )

            compatible_plan = (
                self._render_plan
                and not self._render_plan.cancelled
                and abs(self._render_plan.tempo_rate - tempo_rate) < 1e-6
                and abs(self._render_plan.pitch_semitones - pitch_semitones) < 1e-6
            )

            if compatible_plan:
                dropping_current = (
                    self._render_plan.current_task
                    and self._render_plan.current_task != "__mix__"
                    and self._render_plan.current_task not in stems_to_build
                )
                switching_to_mix_only = (
                    include_mix and not stems_to_build and not self._render_plan.include_mix
                )
                if dropping_current or switching_to_mix_only:
                    base_completed.update(self._render_plan.completed_cache)
                    if self._render_plan.mix_data is not None:
                        base_mix_value = self._render_plan.mix_data
                    self._render_plan.cancelled = True
                    compatible_plan = False

            if compatible_plan:
                plan = self._render_plan
                plan.target_play_all = target_play_all
                plan.target_active_stems = target_active
                plan.include_mix = include_mix
                plan.mark_missing = mark_missing
                plan.requested_stems = set(stems_to_build)
                plan.remaining_stems = [
                    name for name in plan.remaining_stems if name in plan.requested_stems
                ]
                known = set(plan.completed_cache.keys()) | set(plan.remaining_stems)
                for name in stems_to_build:
                    if name not in known:
                        plan.remaining_stems.append(name)
                if include_mix:
                    plan.mix_pending = plan.mix_data is None
                else:
                    plan.mix_pending = False
                self.pending_target_play_all = target_play_all
                self.pending_target_active_stems = set(target_active)
                self.pending_tempo_rate = tempo_rate
                self.pending_pitch_semitones = pitch_semitones
                if progress_callback and plan.current_task:
                    progress_callback(
                        plan.completed_items() / float(max(plan.total_items(), 1)),
                        f"{plan.current_task}, {tempo_rate:.2f}x, {pitch_semitones:+.1f} st",
                        plan.total_items(),
                    )
                self._pending_condition.notify_all()
                return

            if self._render_plan is not None:
                self._render_plan.cancelled = True

            self.pending_ready = False
            self.pending_tempo_rate = tempo_rate
            self.pending_pitch_semitones = pitch_semitones
            self.pending_target_play_all = target_play_all
            self.pending_target_active_stems = target_active

            plan = RenderPlan(
                tempo_rate=tempo_rate,
                pitch_semitones=pitch_semitones,
                include_mix=include_mix,
                mark_missing=mark_missing,
                target_play_all=target_play_all,
                target_active_stems=target_active,
                requested_stems=set(stems_to_build),
                remaining_stems=[name for name in stems_to_build if name not in base_completed],
                completed_cache=base_completed,
                mix_data=base_mix_value,
                mix_pending=include_mix and base_mix_value is None,
            )
            self._render_plan = plan

            if log_callback:
                log_callback(
                    f"Rebuilding audio for tempo={tempo_rate:.2f}x, "
                    f"pitch={pitch_semitones:+.1f} st..."
                )

            self._render_thread = threading.Thread(
                target=self._run_render_plan,
                args=(plan, progress_callback, log_callback),
                daemon=True,
            )
            self._render_thread.start()

    def cancel_pending_render(self):
        """Abort any in-flight render tasks and clear pending buffers."""
        with self._pending_condition:
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
            if self._render_plan is not None:
                self._render_plan.cancelled = True
            self._render_plan = None
            self._render_thread = None
            self._pending_condition.notify_all()

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
    ):
        stems = {name for name in active_stems if name in self.original_stem_data}
        if not stems and self.original_stem_data:
            stems = set(self.original_stem_data.keys())

        self.play_all = bool(play_all)
        self.active_stems = stems
        self._sync_reverb_states()

        self.ensure_selection_ready(
            log_callback=log_callback, progress_callback=progress_callback
        )

    def set_active_stems(self, names: Set[str], log_callback=None, progress_callback=None):
        self.set_selection(
            play_all=False,
            active_stems=set(names),
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def set_play_all(self, value: bool, log_callback=None, progress_callback=None):
        self.set_selection(
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

    def _emit_plan_progress(
        self,
        plan: RenderPlan,
        progress_callback,
        label_name: str,
    ):
        if not progress_callback:
            return

        total_items = max(plan.total_items(), 1)
        progress_callback(
            plan.completed_items() / float(total_items),
            f"{label_name}, {plan.tempo_rate:.2f}x, {plan.pitch_semitones:+.1f} st",
            total_items,
        )

    def _run_render_plan(self, plan: RenderPlan, progress_callback, log_callback):
        while True:
            with self._pending_condition:
                if plan.cancelled or self._render_plan is not plan:
                    return

                total_items = plan.total_items()
                completed_items = plan.completed_items()

                if plan.remaining_stems:
                    task = plan.remaining_stems.pop(0)
                    plan.current_task = task
                elif plan.mix_pending and plan.include_mix:
                    plan.current_task = "__mix__"
                    plan.mix_pending = False
                else:
                    if completed_items >= total_items or total_items == 0:
                        self._finalize_render_plan(plan, progress_callback, log_callback)
                        return
                    plan.current_task = None
                    self._pending_condition.wait()
                    continue

                current_label = "mix" if plan.current_task == "__mix__" else plan.current_task
                self._emit_plan_progress(plan, progress_callback, current_label)

            sr = self.sample_rate
            if sr is None:
                return

            if plan.current_task == "__mix__":
                processed = None
                if self.original_mix is not None:
                    processed = self._apply_tempo_pitch(
                        data=self.original_mix,
                        tempo_rate=plan.tempo_rate,
                        pitch_semitones=plan.pitch_semitones,
                        sr=sr,
                    )
            else:
                orig = self.original_stem_data.get(plan.current_task)
                processed = None
                if orig is not None:
                    processed = self._apply_tempo_pitch(
                        data=orig,
                        tempo_rate=plan.tempo_rate,
                        pitch_semitones=plan.pitch_semitones,
                        sr=sr,
                    )

            with self._pending_condition:
                if plan.cancelled or self._render_plan is not plan:
                    return

                if plan.current_task == "__mix__":
                    plan.mix_data = processed
                elif plan.current_task:
                    if processed is not None:
                        plan.completed_cache[plan.current_task] = processed
                plan.current_task = None

                if plan.completed_items() >= plan.total_items():
                    self._finalize_render_plan(plan, progress_callback, log_callback)
                    return

                self._pending_condition.notify_all()

    def _finalize_render_plan(
        self, plan: RenderPlan, progress_callback, log_callback
    ):
        new_stems = {
            name: data
            for name, data in plan.completed_cache.items()
            if name in plan.requested_stems
        }
        new_mix = plan.mix_data if plan.include_mix else None

        missing = (
            plan.requested_stems - set(new_stems.keys()) if plan.mark_missing else set()
        )
        if plan.include_mix and new_mix is None and plan.mark_missing:
            missing = set(missing)

        new_total_samples = self._compute_total_samples(new_stems, new_mix)

        self.pending_stem_data = new_stems
        self.pending_mix_data = new_mix
        self.pending_tempo_rate = plan.tempo_rate
        self.pending_pitch_semitones = plan.pitch_semitones
        self.pending_total_samples = new_total_samples
        self.pending_missing_stems = missing
        self.pending_target_play_all = plan.target_play_all
        self.pending_target_active_stems = set(plan.target_active_stems)
        self.pending_ready = True

        if log_callback:
            log_callback("New tempo/pitch configuration ready.")

        if progress_callback:
            progress_callback(1.0, "", plan.total_items())

        plan.cancelled = True
        self._render_plan = None
        self._render_thread = None

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
            target_play_all=self.play_all,
            target_active_stems=set(self.active_stems),
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
            return mixed

        return dry_mix

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
