from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from gui import YTDemucsApp


class PlayerUIMixin:
    """Player UI construction and interactions extracted from gui.py."""

    def setup_player(
        self: "YTDemucsApp",
        stems_dir: str | None,
        preloaded: tuple[list[str], dict[str, list[float]]] | None = None,
    ):
        if not self.player.audio_ok:
            self.append_log("Audio playback not available (sounddevice init failed).")
            return
        if not self.full_mix_path:
            self.append_log("No full mix path available.")
            return

        for widget in self.player_frame.winfo_children():
            widget.destroy()

        self.wave_canvas = None
        self.wave_cursor_id = None
        self.time_label = None
        self.play_pause_button = None
        self.stop_button = None
        self.loop_button = None
        self.volume_var = None
        self.volume_label = None
        self.speed_var = None
        self.speed_label = None
        self.pitch_var = None
        self.pitch_label = None
        self.all_var = None
        self.render_progress_var = None
        self.render_progress_label_var = None
        self.render_progress_bar = None
        self.render_progress_label = None
        self.playback_control_widgets = [
            self.audio_meter,
            self.gain_slider,
            self.reverb_checkbox,
            self.reverb_mix_slider,
        ]
        self.playback_label_widgets = [
            self.audio_meter_label,
            self.gain_label,
            self.thumbnail_label,
            self.reverb_mix_label,
        ]
        self.playback_label_widgets.extend(self.key_table_headers)
        self.playback_label_widgets.extend(self.key_table_value_labels.values())
        self.waveform_points = []
        self.waveform_duration = 0.0
        self.loop_start_line_id = None
        self.loop_end_line_id = None
        self.stem_vars.clear()

        try:
            if preloaded is None:
                if stems_dir is None:
                    stem_names, envelopes = self.player.load_mix_only(self.full_mix_path)
                else:
                    stem_names, envelopes = self.player.load_audio(
                        stems_dir, self.full_mix_path
                    )
            else:
                stem_names, envelopes = preloaded
        except Exception as exc:  # pragma: no cover - UI logging
            self.append_log(f"Failed to load audio: {exc}")
            return

        self.waveform_duration = self.player.get_duration()

        self.set_playback_controls_state(True)

        self.wave_canvas = tk.Canvas(
            self.player_frame,
            height=80,
            bg="#202020",
            highlightthickness=1,
            highlightbackground="#444444",
        )
        self.wave_canvas.grid(row=0, column=0, columnspan=6, sticky="ew")
        self.wave_canvas.bind("<Configure>", self.on_waveform_configure)
        self.wave_canvas.bind("<Button-1>", self.on_waveform_click)

        self.player_frame.columnconfigure(0, weight=0)
        for col in range(1, 6):
            self.player_frame.columnconfigure(
                col,
                weight=1,
                minsize=40,
                uniform="buttons",
            )

        stem_frame = ttk.Frame(self.player_frame)
        stem_frame.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 4))
        cb_all = ttk.Checkbutton(
            stem_frame,
            text="All",
            variable=self.all_var,
            command=self.on_all_toggle,
        )
        cb_all.grid(row=0, column=0, padx=(0, 10))

        for idx, name in enumerate(stem_names):
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(
                stem_frame,
                text=name,
                variable=var,
                command=self.on_stem_toggle,
            )
            cb.grid(row=0, column=idx + 1)
            self.stem_vars[name] = var

            env_points = envelopes.get(name, [])
            if env_points:
                label = ttk.Label(stem_frame, text=f"{name}: {len(env_points)} points")
                label.grid(row=1, column=idx + 1)

        if not stem_names:
            self.player.set_play_all(True)

        self.time_label = ttk.Label(self.player_frame, text="00:00 / 00:00")
        self.time_label.grid(row=2, column=0, pady=(5, 0))

        self.play_pause_button = ttk.Button(
            self.player_frame, text="Play", command=self.on_play_pause
        )
        self.play_pause_button.grid(row=2, column=1, pady=(5, 0), sticky="nsew")

        self.stop_button = ttk.Button(
            self.player_frame, text="Stop", command=self.on_stop
        )
        self.stop_button.grid(row=2, column=2, pady=(5, 0), sticky="nsew")

        self.loop_button = ttk.Button(
            self.player_frame, text="Loop", command=self.on_toggle_loop
        )
        self.loop_button.grid(row=2, column=3, pady=(5, 0), sticky="nsew")

        reset_button = ttk.Button(
            self.player_frame, text="Reset", command=self.on_reset_playback
        )
        reset_button.grid(row=2, column=4, pady=(5, 0), sticky="nsew")

        clear_button = ttk.Button(
            self.player_frame, text="Clear", command=self.on_clear_app
        )
        clear_button.grid(row=2, column=5, pady=(5, 0), sticky="nsew")

        self.update_loop_button()

        self.volume_label = ttk.Label(self.player_frame, text="100%")
        self.volume_label.grid(
            row=3, column=0, pady=(5, 0)
        )

        self.volume_var = tk.DoubleVar(value=1.0)
        vol_slider = ttk.Scale(
            self.player_frame,
            from_=0.0,
            to=1.0,
            orient="horizontal",
            variable=self.volume_var,
            command=self.on_volume_change,
            length=500,
        )
        vol_slider.grid(row=3, column=1, columnspan=5, sticky="ew", pady=(5, 0))

        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_label = ttk.Label(self.player_frame, text="1.00x")
        self.speed_label.grid(row=4, column=0, pady=(5, 0))

        speed_slider = ttk.Scale(
            self.player_frame,
            from_=0.25,
            to=2.0,
            orient="horizontal",
            variable=self.speed_var,
            command=self.on_speed_drag,
            length=500,
        )
        speed_slider.grid(row=4, column=1, columnspan=5, sticky="ew", pady=(5, 0))
        speed_slider.bind("<ButtonRelease-1>", self.on_speed_release)

        self.pitch_var = tk.DoubleVar(value=0)
        initial_pitch = 0
        self.pitch_label = ttk.Label(
            self.player_frame,
            width=12,
            text=self.format_pitch_label(initial_pitch)
        )
        self.pitch_label.grid(row=5, column=0, pady=(5, 0))

        pitch_slider = ttk.Scale(
            self.player_frame,
            from_=-6.0,
            to=6.0,
            orient="horizontal",
            variable=self.pitch_var,
            command=self.on_pitch_drag,
            length=500,
        )
        pitch_slider.grid(row=5, column=1, columnspan=5, sticky="ew", pady=(5, 0))
        pitch_slider.bind("<ButtonRelease-1>", self.on_pitch_release)

        self.update_key_table(self.pitch_var.get())

        if self.reverb_enabled_var is not None:
            self.reverb_enabled_var.set(False)
        if self.reverb_mix_var is not None:
            self.reverb_mix_var.set(0.45)
        self.player.set_reverb_enabled(bool(self.reverb_enabled_var.get()))
        self.on_reverb_mix_change(str(self.reverb_mix_var.get()))
        self.update_reverb_controls_state()

        ttk.Separator(self.player_frame, orient="horizontal").grid(
            row=6,
            column=0,
            columnspan=max(6, len(stem_names) + 1),
            sticky="ew",
            pady=(10, 5),
        )
        self.render_progress_var = tk.DoubleVar(value=0.0)
        self.render_progress_label_var = tk.StringVar(value="Rendering: Ready")
        self.render_progress_bar = ttk.Progressbar(
            self.player_frame,
            variable=self.render_progress_var,
            maximum=100,
            mode="determinate",
        )
        self.render_progress_bar.grid(
            row=7,
            column=0,
            columnspan=7,
            sticky="ew",
            pady=(5, 0),
        )
        self.render_progress_label = ttk.Label(
            self.player_frame,
            textvariable=self.render_progress_label_var,
            width=28,
        )
        self.render_progress_label.grid(
            row=7,
            column=5,
            pady=(5, 0),
        )

        self.update_waveform_from_selection()
        self.draw_waveform()
        self.update_player_frame_visibility()

    def on_waveform_configure(self: "YTDemucsApp", _event):
        self.draw_waveform()

    def update_waveform_from_selection(self: "YTDemucsApp"):
        if self.all_var is not None and self.all_var.get():
            self.player.set_play_all(True)
            active = set()
            self.player.set_active_stems(active)
            self.waveform_points = self.player.get_mix_envelope()
        else:
            active = {
                name for name, var in self.stem_vars.items() if var.get()
            }
            self.player.set_play_all(False)
            self.player.set_active_stems(active)
            self.waveform_points = self.player.mix_envelopes(active)

    def draw_waveform(self: "YTDemucsApp"):
        if self.wave_canvas is None:
            return

        self.wave_canvas.delete("wave")
        self.wave_canvas.delete("loop_marker")
        w = self.wave_canvas.winfo_width()
        h = self.wave_canvas.winfo_height()
        if w <= 2 or h <= 2 or not self.waveform_points:
            return

        mid_y = h / 2
        n = len(self.waveform_points)
        if n < 2:
            return

        x_step = w / float(n - 1)
        max_amp = h / 2 - 2

        for i, amp in enumerate(self.waveform_points):
            x = i * x_step
            y = amp * max_amp
            self.wave_canvas.create_line(
                x, mid_y - y, x, mid_y + y,
                fill="#808080",
                tags="wave",
            )

        self.draw_loop_markers()
        self.draw_cursor()

    def draw_loop_markers(self: "YTDemucsApp"):
        if self.wave_canvas is None or self.waveform_duration <= 0:
            return

        w = self.wave_canvas.winfo_width()
        h = self.wave_canvas.winfo_height()
        if w <= 2 or h <= 2:
            return

        start_sec, end_sec = self.player.get_loop_bounds_seconds()
        start_x = (start_sec / self.waveform_duration) * w
        end_x = (end_sec / self.waveform_duration) * w

        self.loop_start_line_id = self.wave_canvas.create_line(
            start_x,
            0,
            start_x,
            h,
            fill="#00cc66",
            width=2,
            tags="loop_marker",
        )

        self.loop_end_line_id = self.wave_canvas.create_line(
            end_x,
            0,
            end_x,
            h,
            fill="#cc0000",
            width=2,
            tags="loop_marker",
        )

    def draw_cursor(self: "YTDemucsApp"):
        if self.wave_canvas is None or self.waveform_duration <= 0:
            return

        pos = self.player.get_position()
        pos = max(0.0, min(pos, self.waveform_duration))

        w = self.wave_canvas.winfo_width()
        h = self.wave_canvas.winfo_height()
        if w <= 2 or h <= 2:
            return
        x = (pos / self.waveform_duration) * w

        if self.wave_cursor_id is not None:
            self.wave_canvas.coords(self.wave_cursor_id, x, 0, x, h)
        else:
            self.wave_cursor_id = self.wave_canvas.create_line(
                x, 0, x, h,
                fill="#ffcc00",
                width=2,
                tags="cursor",
            )

    def on_waveform_click(self: "YTDemucsApp", event):
        if self.wave_canvas is None or self.waveform_duration <= 0:
            return

        w = self.wave_canvas.winfo_width()
        if w <= 1:
            return
        frac = event.x / float(w)
        frac = max(0.0, min(frac, 1.0))
        new_pos = frac * self.waveform_duration
        ctrl_pressed = bool(event.state & 0x0004)
        alt_pressed = bool(event.state & 0x0008 or event.state & 0x20000)

        if ctrl_pressed and alt_pressed:
            self.player.reset_loop_points()
            self.draw_waveform()
            return

        if ctrl_pressed:
            if self.player.set_loop_start(new_pos):
                self.draw_waveform()
            return

        if alt_pressed:
            if self.player.set_loop_end(new_pos):
                self.draw_waveform()
            return
        self.append_log(f"Seeking to {new_pos:.2f} seconds")
        self.player.seek(new_pos)
        if self.play_pause_button is not None:
            self.play_pause_button.config(text="Pause")

    def on_play_pause(self: "YTDemucsApp"):
        if not self.player.audio_ok:
            self.append_log("Audio engine not available.")
            return

        if self.full_mix_path is None:
            self.append_log("No audio loaded for playback.")
            return
        if not self.player.is_playing or self.player.is_paused:
            self.start_playback()
        else:
            self.pause_playback()

    def on_stop(self: "YTDemucsApp"):
        self.player.stop()
        self.update_play_pause_button()

    def start_playback(self: "YTDemucsApp") -> bool:
        if not self.player.audio_ok or self.full_mix_path is None:
            return False
        self.player.play()
        self.update_play_pause_button()
        return True

    def pause_playback(self: "YTDemucsApp") -> bool:
        if not self.player.audio_ok or self.full_mix_path is None:
            return False
        self.player.pause()
        self.update_play_pause_button()
        return True

    def update_play_pause_button(self: "YTDemucsApp"):
        if self.play_pause_button is None:
            return
        if not self.player.is_playing:
            self.play_pause_button.config(text="Play")
        elif self.player.is_paused:
            self.play_pause_button.config(text="Resume")
        else:
            self.play_pause_button.config(text="Pause")

    def update_loop_button(self: "YTDemucsApp"):
        if self.loop_button is None:
            return
        if self.player.loop_controller.enabled:
            self.loop_button.config(text="Linear")
        else:
            self.loop_button.config(text="Loop")

    def on_toggle_loop(self: "YTDemucsApp"):
        if not self.player.audio_ok or self.waveform_duration <= 0:
            return
        enabled = self.player.toggle_loop_enabled()
        self.update_loop_button()
        status = "enabled" if enabled else "disabled"
        self.append_log(f"Looping {status}.")
        self.draw_waveform()
