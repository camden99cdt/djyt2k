# gui.py
import os
import threading
from io import BytesIO
import urllib.request

import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk

from audio_player import StemAudioPlayer
from pipeline import PipelineResult, PipelineRunner
from saved_sessions import SavedSession, SavedSessionStore

CHROMA_LABELS = ['C', 'C#', 'D', 'D#', 'E', 'F',
                 'F#', 'G', 'G#', 'A', 'A#', 'B']

# Map simple flats to enharmonic sharps, in case your key detector
# ever returns something like "Bb major"
FLAT_TO_SHARP = {
    "Db": "C#",
    "Eb": "D#",
    "Gb": "F#",
    "Ab": "G#",
    "Bb": "A#",
}

class YTDemucsApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_title = "YouTube \u2192 Demucs Stems"
        self.root.title(self.base_title)

        # ---------- layout ----------
        container = ttk.Frame(root)
        container.grid(row=0, column=0, sticky="nsew")

        self.sidebar_frame = ttk.Frame(container, padding=(10, 10))
        self.sidebar_frame.grid(row=0, column=0, sticky="nsw")

        main_frame = ttk.Frame(container, padding=10)
        main_frame.grid(row=0, column=1, sticky="nsew")

        self.thumbnail_label = tk.Label(
            main_frame,
            text="No\nthumbnail",
            justify="center"
        )
        self.thumbnail_label.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))

        ttk.Label(main_frame, text="YouTube URL:").grid(row=0, column=1, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(main_frame, textvariable=self.url_var, width=60)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 5))

        # Button + skip separation checkbox
        self.start_button = ttk.Button(
            main_frame, text="Download & Separate", command=self.on_start
        )
        self.start_button.grid(row=2, column=1, sticky="w")

        self.skip_sep_var = tk.BooleanVar(value=False)
        self.skip_sep_cb = ttk.Checkbutton(
            main_frame,
            text="Skip separation",
            variable=self.skip_sep_var,
        )
        self.skip_sep_cb.grid(row=2, column=2, sticky="w")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(main_frame, textvariable=self.status_var)
        self.status_label.grid(row=2, column=3, sticky="e")

        ttk.Label(main_frame, text="Log:").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.log_text = tk.Text(main_frame, height=15, width=80, state="disabled")
        self.log_text.grid(row=4, column=0, columnspan=4, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            main_frame, orient="vertical", command=self.log_text.yview
        )
        scrollbar.grid(row=4, column=4, sticky="ns")
        self.log_text["yscrollcommand"] = scrollbar.set

        self.player_frame = ttk.Frame(main_frame)
        self.player_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))

        # Sidebar with saved sessions
        ttk.Label(self.sidebar_frame, text="Saved Sessions").grid(row=0, column=0, sticky="w")
        self.saved_sessions_listbox = tk.Listbox(
            self.sidebar_frame,
            height=20,
            exportselection=False,
        )
        saved_scrollbar = ttk.Scrollbar(
            self.sidebar_frame, orient="vertical", command=self.saved_sessions_listbox.yview
        )
        self.saved_sessions_listbox.configure(yscrollcommand=saved_scrollbar.set)
        self.saved_sessions_listbox.grid(row=1, column=0, sticky="nsew")
        saved_scrollbar.grid(row=1, column=1, sticky="ns")

        self.save_delete_button = ttk.Button(
            self.sidebar_frame,
            text="Save Session",
            command=self.on_save_or_delete,
            state="disabled",
        )
        self.save_delete_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        self.sidebar_frame.rowconfigure(1, weight=1)
        self.sidebar_frame.columnconfigure(0, weight=1)

        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)
        for c in range(4):
            main_frame.columnconfigure(c, weight=0)
        main_frame.columnconfigure(1, weight=1)

        # ---------- GUI state ----------
        self.saved_session_store = SavedSessionStore()
        self.selected_saved_session_id: str | None = None
        self.displayed_sessions: list = []
        self.current_pipeline_result: PipelineResult | None = None
        self.thumbnail_image = None
        self.current_thumbnail_bytes: bytes | None = None

        self.wave_canvas: tk.Canvas | None = None
        self.wave_cursor_id: int | None = None
        self.time_label: ttk.Label | None = None
        self.play_pause_button: ttk.Button | None = None
        self.stop_button: ttk.Button | None = None
        self.volume_label: ttk.Label | None = None
        self.volume_var: tk.DoubleVar | None = None
        self.speed_var: tk.DoubleVar | None = None
        self.speed_label: ttk.Label | None = None
        self.pitch_var: tk.DoubleVar | None = None
        self.pitch_label: ttk.Label | None = None
        self.all_var: tk.BooleanVar | None = None
        self.render_progress_var: tk.DoubleVar | None = None
        self.render_progress_label_var: tk.StringVar | None = None
        self.render_progress_bar: ttk.Progressbar | None = None
        self.render_progress_label: ttk.Label | None = None

        self.waveform_points: list[float] = []
        self.waveform_duration: float = 0.0
        self.stem_vars: dict[str, tk.BooleanVar] = {}

        self.full_mix_path: str | None = None  # path to original yt-dlp wav
        self.current_title: str | None = None
        self.song_key_text: str | None = None  # detected key, e.g. "F major"

        # audio engine
        self.player = StemAudioPlayer()
        if not self.player.audio_ok:
            self.append_log(f"Audio engine not available: {self.player.error_message}")
        else:
            self.player.set_render_progress_callback(self.on_render_progress)

        # pipeline orchestration
        self.pipeline_runner = PipelineRunner(
            log_callback=self.append_log,
            status_callback=self.set_status,
        )

        # periodic UI updates
        self.root.after(100, self.update_playback_ui)

        # saved sessions UI wiring
        self.saved_sessions_listbox.bind("<<ListboxSelect>>", self.on_saved_session_select)
        self.refresh_saved_sessions_list()
        self.update_save_button_state()

    # ---------- logging / status ----------

    def append_log(self, message: str):
        def _append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _append)

    def set_status(self, message: str):
        self.root.after(0, lambda: self.status_var.set(message))

    def set_running(self, running: bool):
        def _set():
            self.start_button.configure(state="disabled" if running else "normal")
            self.skip_sep_cb.configure(state="disabled" if running else "normal")
        self.root.after(0, _set)

    # ---------- thumbnail ----------

    def update_thumbnail(self, thumb_url: str | None):
        if not thumb_url:
            self.append_log("No thumbnail URL found.")
            return

        self.append_log(f"Fetching thumbnail: {thumb_url}")

        def worker():
            try:
                req = urllib.request.Request(
                    thumb_url,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req) as resp:
                    data = resp.read()
                self.current_thumbnail_bytes = data
                self.set_thumbnail_from_bytes(data)
            except Exception as e:
                self.append_log(f"Could not load thumbnail: {e}")
                return

        threading.Thread(target=worker, daemon=True).start()

    def set_thumbnail_from_bytes(self, data: bytes):
        try:
            image = Image.open(BytesIO(data))
            image.thumbnail((240, 240))
            photo = ImageTk.PhotoImage(image)
        except Exception as e:
            self.append_log(f"Could not process thumbnail: {e}")
            return

        def _set():
            self.thumbnail_image = photo
            self.thumbnail_label.configure(image=photo, text="")
        self.root.after(0, _set)

    def set_thumbnail_from_file(self, path: str):
        if not path or not os.path.exists(path):
            self.append_log("Thumbnail not found on disk.")
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.current_thumbnail_bytes = data
        except Exception as e:
            self.append_log(f"Failed to read thumbnail: {e}")
            return
        self.set_thumbnail_from_bytes(self.current_thumbnail_bytes)

    # ---------- main button ----------

    def on_start(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter a YouTube URL.")
            return
        t = threading.Thread(target=self.run_pipeline, args=(url,), daemon=True)
        t.start()

    # ---------- pipeline ----------

    def run_pipeline(self, url: str):
        self.set_running(True)
        try:
            result = self.pipeline_runner.process(
                url, skip_separation=self.skip_sep_var.get()
            )
            self.handle_pipeline_success(result)
        except Exception as e:
            self.append_log(f"ERROR: {e}")
            self.set_status("Error")
        finally:
            self.set_running(False)

    def handle_pipeline_success(self, result: PipelineResult):
        self.full_mix_path = result.audio_path
        self.current_title = result.title
        self.song_key_text = result.song_key_text
        self.current_pipeline_result = result
        self.selected_saved_session_id = None
        self.saved_sessions_listbox.selection_clear(0, tk.END)

        if result.thumbnail_url:
            self.update_thumbnail(result.thumbnail_url)

        window_title = result.title if not result.separated else f"{result.title} [sep]"
        self.root.after(0, lambda t=window_title: self.root.title(t))
        self.root.after(0, lambda: self.setup_player(result.stems_dir))
        self.update_save_button_state()

    # ---------- saved sessions ----------

    def refresh_saved_sessions_list(self):
        self.saved_sessions_listbox.delete(0, tk.END)
        self.displayed_sessions = self.saved_session_store.list_sessions()
        for session in self.displayed_sessions:
            self.saved_sessions_listbox.insert(tk.END, session.display_name)

        if self.selected_saved_session_id:
            for idx, session in enumerate(self.displayed_sessions):
                if session.session_id == self.selected_saved_session_id:
                    self.saved_sessions_listbox.selection_set(idx)
                    self.saved_sessions_listbox.see(idx)
                    break

    def update_save_button_state(self):
        if self.selected_saved_session_id:
            self.save_delete_button.config(text="Delete Session", state="normal")
        elif self.current_pipeline_result and self.current_pipeline_result.stems_dir:
            self.save_delete_button.config(text="Save Session", state="normal")
        else:
            self.save_delete_button.config(text="Save Session", state="disabled")

    def on_saved_session_select(self, event):
        selection = self.saved_sessions_listbox.curselection()
        if not selection:
            self.selected_saved_session_id = None
            self.update_save_button_state()
            return

        idx = selection[0]
        if idx >= len(self.displayed_sessions):
            return

        session = self.displayed_sessions[idx]
        self.selected_saved_session_id = session.session_id
        self.update_save_button_state()
        self.load_saved_session(session)

    def on_save_or_delete(self):
        if self.selected_saved_session_id:
            self.delete_selected_session()
        else:
            self.save_current_session()

    def save_current_session(self):
        result = self.current_pipeline_result
        if not result or not result.stems_dir:
            messagebox.showinfo("Save Session", "A separated session is required to save.")
            return

        def worker():
            try:
                session = self.saved_session_store.add_session(
                    title=self.current_title or "Untitled",
                    song_key_text=self.song_key_text,
                    session_dir=result.session_dir,
                    audio_path=result.audio_path,
                    stems_dir=result.stems_dir,
                    thumbnail_bytes=self.current_thumbnail_bytes,
                )
                self.append_log(f"Saved session: {session.display_name}")
            except Exception as e:
                self.append_log(f"Failed to save session: {e}")
                return

            def _after_save():
                self.current_pipeline_result = None
                self.selected_saved_session_id = session.session_id
                self.refresh_saved_sessions_list()
                self.update_save_button_state()
            self.root.after(0, _after_save)

        threading.Thread(target=worker, daemon=True).start()

    def delete_selected_session(self):
        session = self.saved_session_store.get_session(self.selected_saved_session_id)
        if not session:
            return

        if not self.saved_session_store.delete_session(session.session_id):
            return

        self.append_log(f"Deleted session: {session.display_name}")
        self.selected_saved_session_id = None
        self.refresh_saved_sessions_list()
        self.update_save_button_state()

    def load_saved_session(self, session: SavedSession):
        self.append_log(f"Loading saved session: {session.display_name}")
        self.clear_current_session()
        self.full_mix_path = session.audio_path
        self.current_title = session.title
        self.song_key_text = session.song_key_text
        self.current_pipeline_result = None

        if session.thumbnail_path:
            self.set_thumbnail_from_file(session.thumbnail_path)
        else:
            self.thumbnail_label.configure(image="", text="No\nthumbnail")

        window_title = session.title
        self.root.after(0, lambda t=window_title: self.root.title(t))
        self.root.after(0, lambda: self.setup_player(session.stems_dir))
        self.update_save_button_state()

    # ---------- player UI ----------

    def setup_player(self, stems_dir: str | None):
        if not self.player.audio_ok:
            self.append_log("Audio playback not available (sounddevice init failed).")
            return
        if not self.full_mix_path:
            self.append_log("No full mix path available.")
            return

        # clear UI
        for w in self.player_frame.winfo_children():
            w.destroy()

        self.wave_canvas = None
        self.wave_cursor_id = None
        self.time_label = None
        self.play_pause_button = None
        self.stop_button = None
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
        self.waveform_points = []
        self.waveform_duration = 0.0
        self.stem_vars.clear()

        # load audio via player
        try:
            if stems_dir is None:
                # Skip separation mode: full mix only
                stem_names, envelopes = self.player.load_mix_only(self.full_mix_path)
            else:
                stem_names, envelopes = self.player.load_audio(stems_dir, self.full_mix_path)
        except Exception as e:
            self.append_log(f"Failed to load audio: {e}")
            return

        self.waveform_duration = self.player.get_duration()

        # waveform canvas
        self.wave_canvas = tk.Canvas(
            self.player_frame,
            height=80,
            bg="#202020",
            highlightthickness=1,
            relief="sunken",
        )
        self.wave_canvas.grid(
            row=0,
            column=0,
            columnspan=max(6, len(stem_names) + 2),
            sticky="ew",
            pady=(0, 5),
        )
        self.player_frame.columnconfigure(0, weight=1)
        self.wave_canvas.bind("<Configure>", self.on_waveform_configure)
        self.wave_canvas.bind("<Button-1>", self.on_waveform_click)

        # stem checkboxes (only if we actually have stems)
        stems_frame = ttk.Frame(self.player_frame)
        stems_frame.grid(
            row=1,
            column=0,
            columnspan=max(6, len(stem_names) + 2),
        )

        for idx, stem_name in enumerate(stem_names):
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(
                stems_frame,
                text=stem_name,
                variable=var,
                command=self.on_stem_toggle,
            )
            cb.grid(row=0, column=idx + 1, padx=(0, 5))
            self.stem_vars[stem_name] = var

        # "All" checkbox (full mix)
        self.all_var = tk.BooleanVar(value=(stems_dir is None))
        cb_all = ttk.Checkbutton(
            stems_frame,
            text="All",
            variable=self.all_var,
            command=self.on_all_toggle,
        )
        cb_all.grid(row=0, column=0, padx=(0, 10))

        # If no stems at all (skip separation), force All mode in player
        if not stem_names:
            self.player.set_play_all(True)

        # time label + controls

        # Columns 1–4: buttons – equal size, minimum 160px each
        # Column 0: time label — inflexible (no stretch)
        self.player_frame.columnconfigure(0, weight=0)

        # Columns 1–4: buttons — flexible, equal width, with a min size
        for col in range(1, 5):
            self.player_frame.columnconfigure(
                col,
                weight=1,
                minsize=40,
                uniform="buttons",
            )

        self.time_label = ttk.Label(self.player_frame, text="00:00 / 00:00")
        # No sticky -> it keeps its natural (requested) size
        self.time_label.grid(row=2, column=0, pady=(5, 0))

        self.play_pause_button = ttk.Button(
            self.player_frame, text="Play", command=self.on_play_pause
        )
        self.play_pause_button.grid(row=2, column=1, pady=(5, 0), sticky="nsew")

        self.stop_button = ttk.Button(
            self.player_frame, text="Stop", command=self.on_stop
        )
        self.stop_button.grid(row=2, column=2, pady=(5, 0), sticky="nsew")

        reset_button = ttk.Button(
            self.player_frame, text="Reset", command=self.on_reset_playback
        )
        reset_button.grid(row=2, column=3, pady=(5, 0), sticky="nsew")

        clear_button = ttk.Button(
            self.player_frame, text="Clear", command=self.on_clear_app
        )
        clear_button.grid(row=2, column=4, pady=(5, 0), sticky="nsew")

        # master volume (row 3) – wider slider via length
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
            command=self.on_volume_change,  # live update on drag
            length=500,                     # keep it wide
        )
        vol_slider.grid(row=3, column=1, columnspan=4, sticky="ew", pady=(5, 0))


        # playback speed (row 4) – snapping + wider slider
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_label = ttk.Label(self.player_frame, text="1.00x")
        self.speed_label.grid(row=4, column=0, pady=(5, 0))

        speed_slider = ttk.Scale(
            self.player_frame,
            from_=0.25,
            to=2.0,
            orient="horizontal",
            variable=self.speed_var,
            command=self.on_speed_drag,   # update label while dragging
            length=500,
        )
        speed_slider.grid(row=4, column=1, columnspan=4, sticky="ew", pady=(5, 0))
        speed_slider.bind("<ButtonRelease-1>", self.on_speed_release)

        # pitch (row 5) – semitones, -3..+3, 0.5 steps
        self.pitch_var = tk.DoubleVar(value=0.0)
        initial_pitch = 0.0
        self.pitch_label = ttk.Label(
            self.player_frame,
            text=self.format_pitch_label(initial_pitch)
        )
        self.pitch_label.grid(row=5, column=0, pady=(5, 0))

        pitch_slider = ttk.Scale(
            self.player_frame,
            from_=-3.0,
            to=3.0,
            orient="horizontal",
            variable=self.pitch_var,
            command=self.on_pitch_drag,
            length=500,
        )
        pitch_slider.grid(row=5, column=1, columnspan=4, sticky="ew", pady=(5, 0))
        pitch_slider.bind("<ButtonRelease-1>", self.on_pitch_release)

        # rendering progress (bottom of player area)
        ttk.Separator(self.player_frame, orient="horizontal").grid(
            row=6,
            column=0,
            columnspan=max(5, len(stem_names) + 1),
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
            columnspan=4,
            sticky="ew",
            pady=(5, 0),
        )
        self.render_progress_label = ttk.Label(
            self.player_frame,
            textvariable=self.render_progress_label_var,
            # Fixed width prevents layout shifts when the status text changes.
            width=28,
        )
        self.render_progress_label.grid(
            row=7,
            column=4,
            columnspan=max(1, len(stem_names) - 3),
            sticky="w",
            pady=(5, 0),
        )

        # initial waveform
        self.update_waveform_from_selection()
        self.draw_waveform()

    # ---------- waveform logic ----------

    def on_waveform_configure(self, event):
        self.draw_waveform()

    def update_waveform_from_selection(self):
        """
        Update player mode + waveform_points based on the current checkbox state.
        - If "All" is checked -> play full mix, waveform = full mix envelope
        - Else -> mix selected stems
        """
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

    def draw_waveform(self):
        if self.wave_canvas is None:
            return

        self.wave_canvas.delete("wave")
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

        self.draw_cursor()

    def draw_cursor(self):
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

    def on_waveform_click(self, event):
        if self.wave_canvas is None or self.waveform_duration <= 0:
            return

        w = self.wave_canvas.winfo_width()
        if w <= 1:
            return

        frac = event.x / float(w)
        frac = max(0.0, min(frac, 1.0))
        new_pos = frac * self.waveform_duration
        self.append_log(f"Seeking to {new_pos:.2f} seconds")
        self.player.seek(new_pos)
        if self.play_pause_button is not None:
            self.play_pause_button.config(text="Pause")

    # ---------- transport ----------

    def on_play_pause(self):
        if not self.player.audio_ok:
            self.append_log("Audio engine not available.")
            return

        # OLD (remove this):
        # if not self.player.stem_data and self.player.mix_data is None:
        #     self.append_log("No audio loaded for playback.")
        #     return

        # NEW: check whether original audio is loaded instead
        if self.full_mix_path is None:
            self.append_log("No audio loaded for playback.")
            return

        if not self.player.is_playing:
            self.player.play()
            if self.play_pause_button is not None:
                self.play_pause_button.config(text="Pause")
        elif not self.player.is_paused:
            self.player.pause()
            if self.play_pause_button is not None:
                self.play_pause_button.config(text="Resume")
        else:
            self.player.play()
            if self.play_pause_button is not None:
                self.play_pause_button.config(text="Pause")


    def on_stop(self):
        self.player.stop()
        if self.play_pause_button is not None:
            self.play_pause_button.config(text="Play")

    # ---------- RESET & CLEAR ----------

    def clear_current_session(self):
        try:
            self.player.stop()
            self.player.stop_stream()
        except Exception:
            pass

        self.player = StemAudioPlayer()
        if not self.player.audio_ok:
            self.append_log(f"Audio engine not available: {self.player.error_message}")
        else:
            self.player.set_render_progress_callback(self.on_render_progress)

        for w in self.player_frame.winfo_children():
            w.destroy()

        self.wave_canvas = None
        self.wave_cursor_id = None
        self.time_label = None
        self.play_pause_button = None
        self.stop_button = None
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
        self.waveform_points = []
        self.waveform_duration = 0.0
        self.stem_vars.clear()
        self.full_mix_path = None
        self.current_title = None
        self.song_key_text = None
        self.current_pipeline_result = None
        self.current_thumbnail_bytes = None

        self.thumbnail_image = None
        self.thumbnail_label.configure(image="", text="No\nthumbnail")
        self.update_save_button_state()

    def on_reset_playback(self):
        """
        Reset speed to 1x, pitch to +0.0 st, volume to 100%.
        Update both sliders/labels and underlying audio.
        """
        # sliders
        if self.volume_var is not None:
            self.volume_var.set(1.0)
        if self.speed_var is not None:
            self.speed_var.set(1.0)
        if self.pitch_var is not None:
            self.pitch_var.set(0.0)

        # labels
        if self.volume_label is not None:
            self.volume_label.config(text="100%")
        if self.speed_label is not None:
            self.speed_label.config(text="1.00x")
        if self.pitch_label is not None:
            self.pitch_label.config(text=self.format_pitch_label(0.0))

        # audio engine
        self.player.set_master_volume(1.0)
        self.player.set_tempo_rate(1.0)
        self.player.set_pitch_semitones(0.0)

        # refresh duration & waveform
        self.waveform_duration = self.player.get_duration()
        self.update_waveform_from_selection()
        self.draw_waveform()

    def on_clear_app(self):
        """
        Reset app to a 'just launched' state:
          - stop playback
          - clear buffers / playback UI
          - clear URL, log, thumbnail
          - reset skip separation, status, window title
        """
        self.clear_current_session()

        # clear URL
        self.url_var.set("")

        # clear log
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        # reset status & controls
        self.skip_sep_var.set(False)
        self.status_var.set("Idle")

        self.saved_sessions_listbox.selection_clear(0, tk.END)
        self.selected_saved_session_id = None
        self.update_save_button_state()

        # reset window title
        self.root.title(self.base_title)

    # ---------- volume / speed / pitch / stems / "All" ----------
    @staticmethod
    def snap_speed(v: float) -> float:
        preferred = [0.5, 0.75, 1.0, 1.25, 1.5]
        threshold = 0.04
        closest = min(preferred, key=lambda p: abs(p - v))
        if abs(closest - v) <= threshold:
            return closest
        return v

    def on_speed_drag(self, value: str):
        if self.speed_label is None or self.speed_var is None:
            return
        try:
            raw_v = float(value)
        except ValueError:
            raw_v = 1.0

        snapped = self.snap_speed(raw_v)
        if abs(snapped - raw_v) <= 0.04:
            self.speed_var.set(snapped)
            v = snapped
        else:
            v = raw_v

        self.speed_label.config(text=f"{v:.2f}x")

    def on_speed_release(self, event):
        if self.speed_var is None:
            return
        raw_v = float(self.speed_var.get())
        v = self.snap_speed(raw_v)
        self.speed_var.set(v)

        # tell the player to request the new tempo
        self.player.set_tempo_rate(v)

        if self.speed_label is not None:
            self.speed_label.config(text=f"{v:.2f}x")

        # optional: redraw waveform (time axis effectively changes)
        self.draw_waveform()

    @staticmethod
    def snap_pitch(v: float) -> float:
        """
        Quantize to 0.5 semitone steps between -3 and +3.
        """
        snapped = round(v * 2.0) / 2.0
        return max(-3.0, min(3.0, snapped))

    def on_pitch_drag(self, value: str):
        if self.pitch_label is None or self.pitch_var is None:
            return
        try:
            raw_v = float(value)
        except ValueError:
            raw_v = 0.0

        snapped = self.snap_pitch(raw_v)
        self.pitch_var.set(snapped)
        if self.pitch_label is not None:
            self.pitch_label.config(text=self.format_pitch_label(snapped))

    def on_pitch_release(self, event):
        if self.pitch_var is None:
            return
        semitones = self.snap_pitch(float(self.pitch_var.get()))
        self.pitch_var.set(semitones)
        self.player.set_pitch_semitones(semitones)

        if self.pitch_label is not None:
            self.pitch_label.config(text=self.format_pitch_label(semitones))

        self.waveform_duration = self.player.get_duration()
        self.update_waveform_from_selection()
        self.draw_waveform()


    def on_stem_toggle(self):
        if self.all_var is not None:
            self.all_var.set(False)
        self.update_waveform_from_selection()
        self.draw_waveform()

    def on_all_toggle(self):
        if self.all_var is None:
            return
        if self.all_var.get():
            for var in self.stem_vars.values():
                var.set(False)
        self.update_waveform_from_selection()
        self.draw_waveform()

    def on_volume_change(self, value: str):
        """
        Live volume update while dragging the slider.
        Also keeps the 'xx%' label in sync.
        """
        try:
            v = float(value)
        except ValueError:
            v = 1.0

        # Clamp to [0, 1] just in case
        v = max(0.0, min(1.0, v))
        self.player.set_master_volume(v)

        if self.volume_label is not None:
            pct = int(v * 100)
            self.volume_label.config(text=f"{pct}%")


    # ---------- render progress ----------

    def on_render_progress(self, progress: float, label: str):
        def _update():
            if (
                self.render_progress_var is None
                or self.render_progress_label_var is None
            ):
                return

            try:
                pct = max(0.0, min(float(progress), 1.0)) * 100.0
            except (TypeError, ValueError):
                pct = 0.0
            self.render_progress_var.set(pct)

            text = label.strip() if label else "Ready"
            self.render_progress_label_var.set(f"Rendering: {text}")

        self.root.after(0, _update)


    # ---------- periodic UI ----------

    def update_playback_ui(self):
        try:
            # Always get the true duration from the audio engine
            duration = self.player.get_duration()
            self.waveform_duration = duration

            if self.time_label is not None and duration > 0:
                pos = self.player.get_position()
                pos = max(0.0, min(pos, duration))
                elapsed_str = self.format_time(pos)
                total_str = self.format_time(duration)
                self.time_label.config(text=f"{elapsed_str} / {total_str}")

            self.draw_cursor()
        finally:
            self.root.after(100, self.update_playback_ui)


    @staticmethod
    def format_time(seconds: float) -> str:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        return f"{m:02d}:{s:02d}"

    def format_pitch_label(self, semitones: float) -> str:
        """
        Build the pitch label text, AND display the musical key shifted
        according to the current pitch shift, where:

            +1.0 st = +1 semitone transpose
            -1.0 st = -1 semitone transpose

        No recomputation of the actual key — purely a musical transposition.
        """
        sign = "+" if semitones >= 0 else ""
        pitch_part = f"{sign}{semitones:.1f} st"

        base_key = self.song_key_text
        if not base_key:
            return pitch_part

        # Parse something like "F major" or "Bb minor"
        parts = base_key.split()
        if len(parts) < 2:
            return f"{pitch_part} |  {base_key}"

        tonic_raw = parts[0]              # e.g. "F", "Bb"
        mode_raw = " ".join(parts[1:])    # e.g. "major", "minor"

        # Normalize flats to sharps for indexing
        tonic = FLAT_TO_SHARP.get(tonic_raw, tonic_raw)

        # If tonic not recognized, fallback
        try:
            base_index = CHROMA_LABELS.index(tonic)
        except ValueError:
            return f"{pitch_part} |  {base_key}"

        # THE IMPORTANT FIX:
        # semitones slider value *is* the number of semitone key steps.
        key_steps = int(round(semitones))  # +1 st → +1 semitone

        new_index = (base_index + key_steps) % 12
        new_tonic = CHROMA_LABELS[new_index]

        new_key_text = f"{new_tonic} {mode_raw}"

        return f"{pitch_part} |  {new_key_text}"
