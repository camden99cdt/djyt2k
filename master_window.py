from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import TYPE_CHECKING

from audio_player import StemAudioPlayer

if TYPE_CHECKING:  # pragma: no cover
    from gui import YTDemucsApp


class MasterWindow:
    def __init__(self, owner: "YTDemucsApp"):
        self.owner = owner
        self.window = tk.Toplevel(owner.root)
        self.window.title("Master")
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        base_font = tkfont.nametofont("TkDefaultFont")
        self.title_font = base_font.copy()
        self.title_font.configure(underline=True)

        for seq in ("<Control-m>", "<Control-M>", "<Control-w>", "<Control-W>"):
            self.window.bind(seq, self.on_shortcut)

        content = ttk.Frame(self.window)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(2, weight=0)
        content.rowconfigure(0, weight=1)

        self.table_frame = ttk.Frame(content, padding=10)
        self.table_frame.grid(row=0, column=0, sticky="nsew")

        ttk.Separator(content, orient="vertical").grid(
            row=0, column=1, sticky="ns", padx=(0, 6)
        )

        self.master_frame = ttk.Frame(content, padding=(0, 10, 10, 10))
        self.master_frame.grid(row=0, column=2, sticky="ns")
        self.master_frame.columnconfigure(0, weight=1)
        self.master_frame.columnconfigure(1, weight=1)

        ttk.Label(self.master_frame, text="Master", font=self.title_font).grid(
            row=0, column=0, columnspan=2, pady=(0, 2)
        )

        self.master_volume_label = ttk.Label(self.master_frame, text="100%")
        self.master_volume_label.grid(row=1, column=0, columnspan=2, pady=(0, 6))

        self.master_meter = ttk.Progressbar(
            self.master_frame,
            orient="vertical",
            mode="determinate",
            maximum=1.0,
            value=0.0,
            length=160,
        )
        self.master_meter.grid(row=2, column=0, sticky="ns", padx=(10, 3))

        self.master_volume_var = tk.DoubleVar(value=StemAudioPlayer.get_global_master_volume())
        self.master_volume_slider = ttk.Scale(
            self.master_frame,
            from_=1.0,
            to=0.0,
            orient="vertical",
            variable=self.master_volume_var,
            command=self.on_global_volume_change,
            length=180,
        )
        self.master_volume_slider.grid(row=2, column=1, sticky="ns")

        self.master_play_button = ttk.Button(
            self.master_frame, text="Play All", width=13, command=self.on_master_play_pause
        )
        self.master_play_button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        self.master_mute_button = ttk.Button(
            self.master_frame, text="Mute All", width=13, command=self.on_master_mute_all
        )
        self.master_mute_button.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        self.master_stop_button = ttk.Button(
            self.master_frame, text="Stop All", width=13, command=self.on_master_stop_all
        )
        self.master_stop_button.grid(row=5, column=0, columnspan=2, sticky="ew")

        self.update_master_volume_label()

        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)

        self.session_states: dict[YTDemucsApp, dict] = {}
        self.last_paused_sessions: set[YTDemucsApp] = set()
        self.pause_targets: set[YTDemucsApp] = set()
        self.solo_target: YTDemucsApp | None = None
        self.master_muted_sessions: set[YTDemucsApp] = set()

        self.refresh_sessions()
        self.update_loop()

    def on_shortcut(self, event=None):
        if event and event.keysym.lower() == "w":
            self.close()
        else:
            self.owner.toggle_master_window()
        return "break"

    def close(self):
        try:
            self.window.destroy()
        finally:
            if self.owner.master_window is self:
                self.owner.master_window = None

    def refresh_sessions(self) -> list["YTDemucsApp"]:
        active_apps = [app for app in self.owner.instances if app.has_active_session()]

        for app in list(self.session_states.keys()):
            if app not in active_apps:
                state = self.session_states.pop(app)
                state["frame"].destroy()
                self.pause_targets.discard(app)
                self.master_muted_sessions.discard(app)

        for idx, app in enumerate(active_apps):
            state = self.session_states.get(app)
            if state is None:
                state = self.build_session_column(app)
                self.session_states[app] = state
            state["frame"].grid(row=0, column=idx, padx=8, sticky="n")

        return active_apps

    def build_session_column(self, app: "YTDemucsApp") -> dict:
        frame = ttk.Frame(self.table_frame, padding=5)
        name_label = ttk.Label(
            frame,
            text=self.format_session_name(app.get_session_display_name()),
            font=self.title_font,
        )
        name_label.grid(row=0, column=0, columnspan=2, pady=(0, 2))

        time_label = ttk.Label(frame, text="00:00 / 00:00")
        time_label.grid(row=1, column=0, columnspan=2, pady=(0, 6))

        meter = ttk.Progressbar(
            frame, orient="vertical", mode="determinate", maximum=1.0, value=0.0, length=120
        )
        meter.grid(row=2, column=0, sticky="ns", padx=(0, 6))

        volume_var = tk.DoubleVar(value=app.get_master_volume())
        slider = ttk.Scale(
            frame,
            from_=1.0,
            to=0.0,
            orient="vertical",
            variable=volume_var,
            command=lambda v, target=app: self.on_volume_slider(target, v),
            length=140,
        )
        slider.grid(row=2, column=1, sticky="ns")

        mute_btn = ttk.Button(frame, text="M", width=2, command=lambda a=app: self.toggle_mute(a))
        mute_btn.grid(row=3, column=0, sticky="ew", pady=(8, 2))

        solo_btn = ttk.Button(frame, text="S", width=2, command=lambda a=app: self.toggle_solo(a))
        solo_btn.grid(row=3, column=1, sticky="ew", pady=(8, 2))

        play_btn = ttk.Button(frame, text="Play", command=lambda a=app: self.toggle_session_play(a))
        play_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        stop_btn = ttk.Button(frame, text="Stop", command=lambda a=app: self.stop_session(a))
        stop_btn.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        reverb_btn = ttk.Button(
            frame,
            text="Reverb",
            command=lambda a=app: self.toggle_reverb(a),
        )
        reverb_btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        return {
            "frame": frame,
            "name_label": name_label,
            "time_label": time_label,
            "meter": meter,
            "volume_var": volume_var,
            "mute_btn": mute_btn,
            "solo_btn": solo_btn,
            "play_btn": play_btn,
            "stop_btn": stop_btn,
            "reverb_btn": reverb_btn,
            "muted": False,
            "saved_volume": None,
            "solo_restore_volume": None,
            "updating_volume": False,
        }

    def set_session_volume(self, app: "YTDemucsApp", volume: float):
        state = self.session_states.get(app)
        if not state:
            return
        state["updating_volume"] = True
        state["volume_var"].set(volume)
        state["updating_volume"] = False
        app.set_master_volume_from_master(volume)

    def on_volume_slider(self, app: "YTDemucsApp", value: str):
        state = self.session_states.get(app)
        if not state or state.get("updating_volume"):
            return
        try:
            volume = float(value)
        except ValueError:
            volume = 0.0

        if volume > 0.0 and state.get("muted"):
            state["muted"] = False
            state["mute_btn"].config(text="M")
            state["saved_volume"] = None
            self.master_muted_sessions.discard(app)
            self.enforce_solo_rules()

        if self.solo_target and app is not self.solo_target and volume > 0.0:
            self.clear_solo()

        self.set_session_volume(app, max(0.0, min(volume, 1.0)))

    def set_muted_state(self, app: "YTDemucsApp", muted: bool):
        state = self.session_states.get(app)
        if not state:
            return

        if muted:
            if state.get("muted"):
                return
            state["saved_volume"] = app.get_master_volume() or 1.0
            state["muted"] = True
            state["mute_btn"].config(text="Unmute")
            app.set_master_volume_from_master(0.0)
        else:
            if not state.get("muted"):
                return
            saved_volume = state.get("saved_volume") or 1.0
            state["muted"] = False
            state["mute_btn"].config(text="M")
            state["saved_volume"] = None
            app.set_master_volume_from_master(saved_volume)

    def toggle_mute(self, app: "YTDemucsApp"):
        state = self.session_states.get(app)
        if not state:
            return
        muted = not state.get("muted")
        self.set_muted_state(app, muted)
        if muted:
            self.master_muted_sessions.add(app)
        else:
            self.master_muted_sessions.discard(app)
        self.update_master_mute_button()
        self.enforce_solo_rules()

    def toggle_solo(self, app: "YTDemucsApp"):
        if self.solo_target is app:
            self.clear_solo()
            return
        self.solo_target = app
        for other, state in self.session_states.items():
            if other is app:
                continue
            state["solo_restore_volume"] = other.get_master_volume()
            other.set_master_volume_from_master(0.0)
        self.update_master_mute_button()
        self.update_master_play_button()

    def clear_solo(self):
        for app, state in self.session_states.items():
            restore = state.get("solo_restore_volume")
            if restore is not None:
                app.set_master_volume_from_master(restore)
            state["solo_restore_volume"] = None
        self.solo_target = None
        self.update_master_mute_button()
        self.update_master_play_button()

    def toggle_session_play(self, app: "YTDemucsApp"):
        if app.get_playback_state() == "playing":
            app.pause_playback()
        elif app.get_playback_state() == "paused":
            app.start_playback()
        else:
            app.start_playback()
        self.update_master_play_button()

    def stop_session(self, app: "YTDemucsApp"):
        app.on_stop()
        self.update_master_play_button()

    def toggle_reverb(self, app: "YTDemucsApp"):
        app.toggle_reverb_from_master()
        self.update_reverb_button(app)

    def update_reverb_button(self, app: "YTDemucsApp"):
        state = self.session_states.get(app)
        if not state:
            return
        enabled = app.get_reverb_enabled()
        try:
            if app.playback_enabled:
                state["reverb_btn"].state(["!disabled"])
            else:
                state["reverb_btn"].state(["disabled"])
        except Exception:
            try:
                state["reverb_btn"].configure(
                    state="normal" if app.playback_enabled else "disabled"
                )
            except Exception:
                pass
        state["reverb_btn"].config(text=f"Reverb ({'On' if enabled else 'Off'})")

    def on_global_volume_change(self, value: str):
        try:
            volume = float(value)
        except ValueError:
            volume = 1.0
        volume = max(0.0, min(volume, 1.0))
        StemAudioPlayer.set_global_master_volume(volume)
        self.update_master_volume_label()

    def on_master_play_pause(self):
        any_playing = any(
            app.has_active_session() and app.get_playback_state() == "playing"
            for app in self.owner.instances
        )
        if any_playing:
            self.pause_targets = {
                app for app in self.owner.instances
                if app.has_active_session() and app.get_playback_state() == "playing"
            }
            for app in self.pause_targets:
                app.pause_playback()
        elif self.pause_targets:
            for app in list(self.pause_targets):
                app.start_playback()
            self.pause_targets.clear()
        else:
            for app in self.owner.instances:
                if app.has_active_session():
                    app.start_playback()
        self.update_master_play_button()

    def on_master_stop_all(self):
        for app in self.owner.instances:
            if app.has_active_session():
                app.on_stop()
        self.pause_targets.clear()
        self.update_master_play_button()

    def on_master_mute_all(self):
        target_set = self.master_muted_sessions or set(self.session_states.keys())
        mute = not self.master_muted_sessions
        for app in target_set:
            self.set_muted_state(app, mute)
            if mute:
                self.master_muted_sessions.add(app)
            else:
                self.master_muted_sessions.discard(app)
        self.update_master_mute_button()

    def enforce_solo_rules(self):
        if self.solo_target:
            for app in self.session_states:
                if app is self.solo_target:
                    continue
                app.set_master_volume_from_master(0.0)
        elif self.master_muted_sessions:
            for app in self.master_muted_sessions:
                app.set_master_volume_from_master(0.0)

    def update_master_play_button(self):
        any_active = {
            app for app in self.owner.instances
            if app.has_active_session() and app.player.audio_ok
        }
        if any(app.get_playback_state() == "playing" for app in self.pause_targets):
            self.pause_targets.clear()

        if self.pause_targets:
            text = "Resume Paused"
        else:
            any_playing = any(
                app.has_active_session() and app.get_playback_state() == "playing"
                for app in self.owner.instances
            )
            text = "Pause Playing" if any_playing else "Play All"

        self.master_play_button.config(text=text)

    def update_master_mute_button(self):
        any_active = any(app.has_active_session() and app.player.audio_ok for app in self.owner.instances)
        if not any_active:
            self.master_mute_button.state(["disabled"])
            return

        self.master_mute_button.state(["!disabled"])
        any_muted = any(state.get("muted") for state in self.session_states.values())

        if self.master_muted_sessions:
            text = "Unmute Muted"
        elif any_muted:
            text = "Mute Unmuted"
        else:
            text = "Mute All"

        self.master_mute_button.config(text=text)

    def update_master_volume_label(self):
        pct = int(max(0.0, min(self.master_volume_var.get(), 1.0)) * 100)
        self.master_volume_label.config(text=f"{pct}%")

    @staticmethod
    def compute_master_level(levels: list[float]) -> float:
        power = sum(max(0.0, l) ** 2 for l in levels)
        return max(0.0, min(math.sqrt(power), 1.0))

    def update_loop(self):
        if not self.window.winfo_exists():
            return

        active_apps = self.refresh_sessions()
        levels: list[float] = []
        for app in active_apps:
            state = self.session_states.get(app)
            if not state:
                continue

            state["name_label"].config(
                text=self.format_session_name(app.get_session_display_name())
            )

            try:
                level = max(0.0, min(app.player.get_output_level(), 1.0))
            except Exception:
                level = 0.0
            levels.append(level)
            state["meter"].configure(value=level)

            if not state.get("updating_volume"):
                current_volume = app.get_master_volume()
                state["updating_volume"] = True
                state["volume_var"].set(current_volume)
                state["updating_volume"] = False

            try:
                duration = app.player.get_duration()
                pos = max(0.0, min(app.player.get_position(), duration))
                elapsed_str = self.owner.format_time(pos)
                total_str = self.owner.format_time(duration)
                state["time_label"].config(text=f"{elapsed_str} / {total_str}")
            except Exception:
                state["time_label"].config(text="00:00 / 00:00")

            playback_state = app.get_playback_state()
            if playback_state == "playing":
                state["play_btn"].config(text="Pause")
            elif playback_state == "paused":
                state["play_btn"].config(text="Resume")
            else:
                state["play_btn"].config(text="Play")

            self.update_reverb_button(app)

        self.enforce_solo_rules()
        self.update_master_mute_button()
        self.update_master_play_button()
        if self.master_meter is not None:
            master_level = self.compute_master_level(levels) * StemAudioPlayer.get_global_master_volume()
            self.master_meter.configure(value=master_level)
        self.update_master_volume_label()
        self.window.after(200, self.update_loop)

    @staticmethod
    def format_session_name(name: str, max_len: int = 12) -> str:
        if len(name) <= max_len:
            return name
        return name[: max_len - 3] + "..."
