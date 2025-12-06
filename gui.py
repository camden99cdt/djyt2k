# gui.py
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO

import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk

from audio_player import StemAudioPlayer
from master_window import MasterWindow
from pipeline import PipelineResult, PipelineRunner
from player_ui import PlayerUIMixin
from saved_sessions import SavedSession, SavedSessionStore
from search_ui import SearchSuggestionsMixin

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

class YTDemucsApp(PlayerUIMixin, SearchSuggestionsMixin):
    instances: list["YTDemucsApp"] = []
    master_window: "MasterWindow | None" = None

    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_title = "YouTube \u2192 Demucs Stems"
        self.root.title(self.base_title)

        self.root.protocol("WM_DELETE_WINDOW", self.close_window)
        self.setup_menubar()

        self.style = ttk.Style(self.root)
        self.style.configure("DisabledPlayback.TFrame", background="#e6e6e6")
        self.style.configure("DisabledPlayback.TLabel", foreground="#777777")

        # ---------- layout ----------
        container = ttk.Frame(root)
        container.grid(row=0, column=0, sticky="nsew")

        main_frame = ttk.Frame(container, padding=10)
        main_frame.grid(row=0, column=0, sticky="nsew")

        self.notebook = ttk.Notebook(main_frame)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.youtube_tab = ttk.Frame(self.notebook, padding=10)
        self.playback_tab = ttk.Frame(self.notebook, padding=10)
        self.harmonics_tab = ttk.Frame(self.notebook, padding=10)
        self.sessions_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.youtube_tab, text="YouTube")
        self.notebook.add(self.playback_tab, text="Playback")
        self.notebook.add(self.harmonics_tab, text="Harmonics")
        self.notebook.add(self.sessions_tab, text="Sessions")

        ttk.Label(self.youtube_tab, text="YouTube URL:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self.youtube_tab, textvariable=self.url_var, width=60)
        self.url_entry.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 5))

        self.start_button = ttk.Button(
            self.youtube_tab, text="Download & Separate", command=self.on_start
        )
        self.start_button.grid(row=2, column=0, sticky="w")

        self.skip_sep_var = tk.BooleanVar(value=False)
        self.skip_sep_cb = ttk.Checkbutton(
            self.youtube_tab,
            text="Skip separation",
            variable=self.skip_sep_var,
        )
        self.skip_sep_cb.grid(row=2, column=1, sticky="w")

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(self.youtube_tab, textvariable=self.status_var)
        self.status_label.grid(row=2, column=2, sticky="e")

        ttk.Label(self.youtube_tab, text="Log:").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.log_text = tk.Text(self.youtube_tab, height=15, width=80, state="disabled")
        self.log_text.grid(row=4, column=0, columnspan=3, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            self.youtube_tab, orient="vertical", command=self.log_text.yview
        )
        scrollbar.grid(row=4, column=3, sticky="ns")
        self.log_text["yscrollcommand"] = scrollbar.set

        playback_top = ttk.Frame(self.playback_tab)
        playback_top.grid(row=0, column=0, sticky="nsew")
        playback_top.columnconfigure(1, weight=1)

        self.thumbnail_label = ttk.Label(
            playback_top,
            text="No\nthumbnail",
            justify="center",
            style="DisabledPlayback.TLabel",
        )
        self.thumbnail_label.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))

        meter_frame = ttk.Frame(playback_top)
        meter_frame.grid(row=0, column=1, sticky="ew")
        meter_frame.columnconfigure(1, weight=1)
        meter_frame.columnconfigure(2, weight=1)

        self.audio_meter_label = ttk.Label(meter_frame, text="-∞ dB", style="DisabledPlayback.TLabel")
        self.audio_meter_label.grid(row=0, column=0, pady=(8, 0))

        self.audio_meter = ttk.Progressbar(
            meter_frame,
            mode="determinate",
            maximum=1.0,
            value=0.0,
            length=260,
        )
        self.audio_meter.grid(row=0, column=1, sticky="ew", columnspan=2, pady=(8, 0))

        self.gain_var = tk.DoubleVar(value=0.0)
        self.gain_label = ttk.Label(meter_frame, text="+0.0 dB", style="DisabledPlayback.TLabel")
        self.gain_label.grid(row=1, column=0, pady=(8, 0))
        self.gain_slider = ttk.Scale(
            meter_frame,
            from_=-10.0,
            to=10.0,
            orient="horizontal",
            variable=self.gain_var,
            command=self.on_gain_change,
            length=280,
        )
        self.gain_slider.grid(row=1, column=1,  sticky="ew", columnspan=2, pady=(8, 0))
        self.gain_slider.bind("<ButtonRelease-1>", self.on_gain_release)

        self.reverb_enabled_var = tk.BooleanVar(value=False)
        self.reverb_mix_var = tk.DoubleVar(value=0.45)
        self.reverb_checkbox = ttk.Checkbutton(
            meter_frame,
            text="Reverb",
            variable=self.reverb_enabled_var,
            command=self.on_reverb_toggle,
        )
        self.reverb_checkbox.grid(row=2, column=0, pady=(10, 0), sticky="w")

        self.reverb_mix_label = ttk.Label(meter_frame, text="45% wet")
        self.reverb_mix_label.grid(row=2, column=2, pady=(10, 0), sticky="e")

        self.reverb_mix_slider = ttk.Scale(
            meter_frame,
            from_=0.0,
            to=1.0,
            orient="horizontal",
            variable=self.reverb_mix_var,
            command=self.on_reverb_mix_change,
            length=280,
        )
        self.reverb_mix_slider.grid(row=2, column=1, sticky="ew", pady=(10, 0))

        self.harmonics_tab.columnconfigure(0, weight=1)
        self.harmonics_tab.rowconfigure(0, weight=1)
        harmonics_frame = ttk.Frame(self.harmonics_tab)
        harmonics_frame.grid(row=0, column=0, sticky="nsew")
        for col in range(6):
            harmonics_frame.columnconfigure(col, weight=1)

        self.key_table_headers: list[ttk.Label] = []
        self.key_table_value_labels: dict[str, ttk.Label] = {}

        headers = ["Key", "+1", "-1", "Rel", "Sub", "Dom"]
        value_keys = [
            "current",
            "plus_one",
            "minus_one",
            "relative",
            "subdominant",
            "dominant",
        ]
        for idx, text in enumerate(headers):
            lbl = ttk.Label(harmonics_frame, text=text, anchor="center", justify="center")
            lbl.grid(row=0, column=idx, sticky="ew")
            self.key_table_headers.append(lbl)

        for idx, key in enumerate(value_keys):
            lbl = ttk.Label(harmonics_frame, text="N/A", anchor="center", justify="center")
            lbl.grid(row=1, column=idx, sticky="ew", pady=(4, 0))
            self.key_table_value_labels[key] = lbl

        self.sessions_tab.columnconfigure(0, weight=7, uniform="sessions")
        self.sessions_tab.columnconfigure(1, weight=3, uniform="sessions")
        self.sessions_tab.rowconfigure(1, weight=1)

        sessions_list_frame = ttk.Frame(self.sessions_tab)
        sessions_list_frame.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        sessions_list_frame.rowconfigure(1, weight=1)
        sessions_list_frame.columnconfigure(0, weight=1)

        ttk.Label(sessions_list_frame, text="Saved Sessions").grid(
            row=0, column=0, sticky="w"
        )
        self.saved_sessions_listbox = tk.Listbox(
            sessions_list_frame,
            height=20,
            exportselection=False,
        )
        saved_scrollbar = ttk.Scrollbar(
            sessions_list_frame, orient="vertical", command=self.saved_sessions_listbox.yview
        )
        self.saved_sessions_listbox.configure(yscrollcommand=saved_scrollbar.set)
        self.saved_sessions_listbox.grid(row=1, column=0, sticky="nsew")
        saved_scrollbar.grid(row=1, column=1, sticky="ns")

        self.session_loading_var = tk.StringVar(value="")
        self.session_loading_frame = ttk.Frame(sessions_list_frame)
        self.session_loading_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.session_loading_frame.columnconfigure(1, weight=1)

        self.session_loading_label = ttk.Label(
            self.session_loading_frame,
            textvariable=self.session_loading_var,
            foreground="#555555",
        )
        self.session_loading_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.session_loading_bar = ttk.Progressbar(
            self.session_loading_frame, mode="indeterminate", length=140
        )
        self.session_loading_bar.grid(row=0, column=1, sticky="ew")

        controls_frame = ttk.Frame(self.sessions_tab)
        controls_frame.grid(row=0, column=1, rowspan=3, sticky="nsew")
        controls_frame.columnconfigure(0, weight=1)
        controls_frame.rowconfigure(11, weight=1)

        ttk.Label(controls_frame, text="Sort by:").grid(row=0, column=0, sticky="w")
        self.sort_var = tk.StringVar(value="newest")
        self.sort_options = [
            ("Oldest first", "oldest"),
            ("Newest first", "newest"),
            ("A -> Z", "a_to_z"),
            ("Z -> A", "z_to_a"),
            ("By key", "by_key"),
        ]
        self.sort_dropdown_var = tk.StringVar(value=self.sort_options[1][0])
        self.sort_dropdown = ttk.Combobox(
            controls_frame,
            state="readonly",
            values=[label for label, _ in self.sort_options],
            textvariable=self.sort_dropdown_var,
        )
        self.sort_dropdown.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.sort_dropdown.bind("<<ComboboxSelected>>", lambda _e: self.on_sort_selection())

        ttk.Separator(controls_frame, orient="horizontal").grid(
            row=2, column=0, sticky="ew", pady=10
        )

        ttk.Label(controls_frame, text="Filters").grid(row=3, column=0, sticky="w")

        ttk.Label(controls_frame, text="Search:").grid(row=4, column=0, sticky="w")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(controls_frame, textvariable=self.search_var)
        search_entry.grid(row=5, column=0, sticky="ew")
        self.search_var.trace_add("write", lambda *_: self.refresh_saved_sessions_list())

        self.mixable_var = tk.BooleanVar(value=False)
        mixable_cb = ttk.Checkbutton(
            controls_frame,
            text="Mixable from...",
            variable=self.mixable_var,
            command=self.refresh_saved_sessions_list,
        )
        mixable_cb.grid(row=6, column=0, sticky="w", pady=(8, 2))

        mixable_key_row = ttk.Frame(controls_frame)
        mixable_key_row.grid(row=7, column=0, sticky="ew")
        mixable_key_row.columnconfigure(0, weight=1)
        mixable_key_row.columnconfigure(1, weight=1)
        self.mixable_key_var = tk.StringVar(value=CHROMA_LABELS[0])
        self.mixable_mode_var = tk.StringVar(value="Maj")
        key_dropdown = ttk.Combobox(
            mixable_key_row,
            state="readonly",
            values=CHROMA_LABELS,
            textvariable=self.mixable_key_var,
        )
        key_dropdown.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        key_dropdown.bind("<<ComboboxSelected>>", lambda _e: self.refresh_saved_sessions_list())

        mode_dropdown = ttk.Combobox(
            mixable_key_row,
            state="readonly",
            values=["Maj", "min"],
            textvariable=self.mixable_mode_var,
        )
        mode_dropdown.grid(row=0, column=1, sticky="ew")
        mode_dropdown.bind("<<ComboboxSelected>>", lambda _e: self.refresh_saved_sessions_list())

        self.show_sep_var = tk.BooleanVar(value=True)
        self.show_ns_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls_frame,
            text="[sep]",
            variable=self.show_sep_var,
            command=self.refresh_saved_sessions_list,
        ).grid(row=8, column=0, sticky="w", pady=(10, 0))
        ttk.Checkbutton(
            controls_frame,
            text="[ns]",
            variable=self.show_ns_var,
            command=self.refresh_saved_sessions_list,
        ).grid(row=9, column=0, sticky="w")

        ttk.Button(
            controls_frame, text="Clear", command=self.reset_session_filters
        ).grid(row=10, column=0, sticky="ew", pady=(10, 0))

        self.save_delete_button = ttk.Button(
            controls_frame,
            text="Save Session",
            command=self.on_save_or_delete,
            state="disabled",
        )
        self.save_delete_button.grid(row=12, column=0, sticky="ew", pady=(10, 0))

        self.player_frame = ttk.Frame(main_frame)
        self.player_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        self.youtube_tab.rowconfigure(4, weight=1)
        for c in range(3):
            self.youtube_tab.columnconfigure(c, weight=1)
        self.playback_tab.columnconfigure(0, weight=1)
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

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
        self.loop_button: ttk.Button | None = None
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
        self.loop_start_line_id: int | None = None
        self.loop_end_line_id: int | None = None
        self.playback_control_widgets: list[tk.Widget] = [
            self.audio_meter,
            self.gain_slider,
            self.reverb_checkbox,
            self.reverb_mix_slider,
        ]
        self.playback_label_widgets: list[ttk.Label] = [
            self.audio_meter_label,
            self.gain_label,
            self.thumbnail_label,
            self.reverb_mix_label,
        ]
        self.playback_label_widgets.extend(self.key_table_headers)
        self.playback_label_widgets.extend(self.key_table_value_labels.values())
        self.playback_enabled = False

        self.waveform_points: list[float] = []
        self.waveform_duration: float = 0.0
        self.stem_vars: dict[str, tk.BooleanVar] = {}

        self.full_mix_path: str | None = None  # path to original yt-dlp wav
        self.current_title: str | None = None
        self.song_key_text: str | None = None  # detected key, e.g. "F major"

        # search suggestions
        self.search_debounce_id: str | None = None
        self.search_request_counter = 0
        self.search_executor = ThreadPoolExecutor(max_workers=2)
        self.search_dropdown: tk.Toplevel | None = None
        self.search_result_frames: list[tk.Widget] = []
        self.search_result_images: list[ImageTk.PhotoImage] = []
        self.search_results: list[SearchResult] = []
        self.highlight_index: int = -1
        self.search_loading: bool = False
        self.search_row_height_estimate: int = 64

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

        self.set_playback_controls_state(False)
        YTDemucsApp.instances.append(self)

        self.update_key_table()

        # saved sessions UI wiring
        self.saved_sessions_listbox.bind("<<ListboxSelect>>", self.on_saved_session_select)
        self.saved_sessions_listbox.bind("<Control-n>", self.on_saved_sessions_ctrl_n)
        self.refresh_saved_sessions_list()
        self.update_save_button_state()
        self.hide_session_loading()

        # url entry bindings
        self.url_var.trace_add("write", self.on_url_text_change)
        self.url_entry.bind("<KeyRelease>", self.on_url_keypress)
        self.url_entry.bind("<FocusOut>", self.on_url_focus_out)

        self.update_player_frame_visibility()

    # ---------- session metadata ----------

    def has_active_session(self) -> bool:
        return self.full_mix_path is not None

    def get_session_display_name(self) -> str:
        window_title = self.root.wm_title()
        if window_title:
            return window_title
        if self.current_title:
            return self.current_title
        return self.base_title

    # ---------- menu + window management ----------

    def setup_menubar(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="New Window",
            accelerator="Ctrl+N",
            command=self.create_new_window,
        )
        file_menu.add_command(
            label="Close Window",
            accelerator="Ctrl+W",
            command=self.close_window,
        )
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.exit_application)

        menubar.add_cascade(label="File", menu=file_menu)
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(
            label="Master",
            accelerator="Ctrl+M",
            command=self.open_master_window,
        )
        menubar.add_cascade(label="View", menu=view_menu)
        self.root.config(menu=menubar)
        for sequence, handler in (
            ("<Control-n>", self.on_new_window_shortcut),
            ("<Control-w>", self.on_close_window_shortcut),
            ("<Control-m>", self.on_master_shortcut),
            ("<Control-M>", self.on_master_shortcut),
        ):
            self.root.bind(sequence, handler)

    def on_new_window_shortcut(self, event=None):
        self.create_new_window()

    def on_close_window_shortcut(self, event=None):
        self.close_window()

    def on_master_shortcut(self, event=None):
        self.toggle_master_window()

    def create_new_window(self):
        master = self.root if isinstance(self.root, tk.Tk) else (self.root.master or self.root)
        new_root = tk.Toplevel(master)
        YTDemucsApp(new_root)

    def close_window(self, event=None):
        self.destroy_window()
        if not YTDemucsApp.instances:
            try:
                self.root.quit()
            except Exception:
                pass

    def destroy_window(self):
        try:
            self.player.stop()
            self.player.stop_stream()
        except Exception:
            pass

        try:
            self.search_executor.shutdown(wait=False)
        except Exception:
            pass

        if self in YTDemucsApp.instances:
            YTDemucsApp.instances.remove(self)

        if not YTDemucsApp.instances:
            YTDemucsApp.close_master_window()

        if self.root.winfo_exists():
            self.root.destroy()

    def exit_application(self):
        YTDemucsApp.close_master_window()
        for instance in list(YTDemucsApp.instances):
            instance.destroy_window()
        try:
            self.root.quit()
        except Exception:
            pass

    def open_master_window(self):
        existing = YTDemucsApp.master_window
        if existing and existing.window.winfo_exists():
            existing.window.lift()
            existing.window.focus_force()
            return

        YTDemucsApp.master_window = MasterWindow(self)

    def toggle_master_window(self):
        existing = YTDemucsApp.master_window
        if existing and existing.window.winfo_exists():
            YTDemucsApp.close_master_window()
        else:
            self.open_master_window()

    @classmethod
    def close_master_window(cls):
        if cls.master_window and cls.master_window.window.winfo_exists():
            try:
                cls.master_window.window.destroy()
            except Exception:
                pass
        cls.master_window = None

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

    def set_playback_controls_state(self, enabled: bool):
        self.playback_enabled = enabled
        state = "normal" if enabled else "disabled"
        label_style = "TLabel" if enabled else "DisabledPlayback.TLabel"
        frame_style = "TFrame" if enabled else "DisabledPlayback.TFrame"

        try:
            self.playback_tab.configure(style=frame_style)
        except tk.TclError:
            pass

        for widget in self.playback_control_widgets:
            if widget is None:
                continue
            try:
                widget.state(["!disabled"] if enabled else ["disabled"])
            except Exception:
                try:
                    widget.configure(state=state)
                except Exception:
                    pass

        for label in self.playback_label_widgets:
            if label is None:
                continue
            try:
                label.configure(style=label_style)
            except Exception:
                try:
                    label.configure(state=state)
                except Exception:
                    pass

        self.update_reverb_controls_state()

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
            image.thumbnail((240, 135))
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
        self.root.after(0, lambda: self.notebook.select(self.playback_tab))
        self.root.after(0, self.update_key_table)
        self.update_save_button_state()

    # ---------- saved sessions ----------

    def refresh_saved_sessions_list(self):
        self.saved_sessions_listbox.delete(0, tk.END)
        self.displayed_sessions = self.get_filtered_sorted_sessions()

        for session in self.displayed_sessions:
            self.saved_sessions_listbox.insert(tk.END, session.display_name)

        if self.selected_saved_session_id:
            for idx, session in enumerate(self.displayed_sessions):
                if session.session_id == self.selected_saved_session_id:
                    self.saved_sessions_listbox.selection_set(idx)
                    self.saved_sessions_listbox.see(idx)
                    break
            else:
                self.selected_saved_session_id = None
        else:
            self.saved_sessions_listbox.selection_clear(0, tk.END)

        self.update_save_button_state()

    def on_sort_selection(self):
        selected_label = self.sort_dropdown_var.get()
        for label, value in self.sort_options:
            if label == selected_label:
                self.sort_var.set(value)
                break
        self.refresh_saved_sessions_list()

    def reset_session_filters(self):
        self.sort_var.set("newest")
        self.sort_dropdown_var.set(self.sort_options[1][0])
        self.search_var.set("")
        self.mixable_var.set(False)
        self.mixable_key_var.set(CHROMA_LABELS[0])
        self.mixable_mode_var.set("Maj")
        self.show_sep_var.set(True)
        self.show_ns_var.set(True)
        self.refresh_saved_sessions_list()

    @staticmethod
    def parse_created_at(created_at: str) -> datetime:
        try:
            return datetime.fromisoformat(created_at)
        except Exception:
            return datetime.min

    def normalize_key_text(self, key_text: str | None) -> str | None:
        if not key_text:
            return None
        parsed = self.parse_key_text(key_text)
        if not parsed:
            return None
        tonic_index, mode_raw = parsed
        normalized_mode = self.normalize_mode(mode_raw)
        return f"{CHROMA_LABELS[tonic_index]} {normalized_mode}"

    def key_sort_value(self, session: SavedSession):
        normalized_key = self.normalize_key_text(session.song_key_text)
        if not normalized_key:
            return (len(CHROMA_LABELS) * 2, session.title.lower())

        parsed = self.parse_key_text(normalized_key)
        if not parsed:
            return (len(CHROMA_LABELS) * 2, session.title.lower())

        tonic_index, mode_raw = parsed
        mode_norm = self.normalize_mode(mode_raw)
        mode_offset = 0 if "maj" in mode_norm else 1
        return (tonic_index * 2 + mode_offset, session.title.lower())

    def compute_mixable_keys(self, tonic_index: int, mode_raw: str) -> set[str]:
        normalized_mode = self.normalize_mode(mode_raw)
        keys = set()

        base_key = f"{CHROMA_LABELS[tonic_index]} {normalized_mode}"
        keys.add(base_key)
        keys.add(self.transpose_parsed_key(tonic_index, normalized_mode, 7))
        keys.add(self.transpose_parsed_key(tonic_index, normalized_mode, -7))

        relative_key = self.compute_relative_key(tonic_index, normalized_mode)
        keys.add(relative_key)

        rel_parsed = self.parse_key_text(relative_key)
        if rel_parsed:
            rel_tonic_index, rel_mode = rel_parsed
            keys.add(self.transpose_parsed_key(rel_tonic_index, rel_mode, 5))
            keys.add(self.transpose_parsed_key(rel_tonic_index, rel_mode, 7))

        normalized_keys = set()
        for key in keys:
            normalized = self.normalize_key_text(key)
            if normalized:
                normalized_keys.add(normalized)
        return normalized_keys

    def get_mixable_keys_from_selection(self) -> set[str]:
        try:
            tonic_index = CHROMA_LABELS.index(self.mixable_key_var.get())
        except ValueError:
            return set()
        return self.compute_mixable_keys(tonic_index, self.mixable_mode_var.get())

    def sort_sessions(self, sessions: list[SavedSession]) -> list[SavedSession]:
        sort_mode = self.sort_var.get()
        if sort_mode == "oldest":
            return sorted(sessions, key=lambda s: self.parse_created_at(s.created_at))
        if sort_mode == "newest":
            return sorted(
                sessions, key=lambda s: self.parse_created_at(s.created_at), reverse=True
            )
        if sort_mode == "a_to_z":
            return sorted(sessions, key=lambda s: s.title.lower())
        if sort_mode == "z_to_a":
            return sorted(sessions, key=lambda s: s.title.lower(), reverse=True)
        if sort_mode == "by_key":
            return sorted(sessions, key=self.key_sort_value)
        return sessions

    def get_filtered_sorted_sessions(self) -> list[SavedSession]:
        sessions = self.sort_sessions(self.saved_session_store.list_sessions())
        search_text = self.search_var.get().strip().lower()
        mixable_enabled = self.mixable_var.get()
        mixable_keys = self.get_mixable_keys_from_selection() if mixable_enabled else set()

        filtered: list[SavedSession] = []
        for session in sessions:
            has_stems = session.stems_dir is not None
            if not self.show_sep_var.get() and has_stems:
                continue
            if not self.show_ns_var.get() and not has_stems:
                continue

            if search_text:
                searchable = [session.display_name.lower()]
                if session.song_key_text:
                    searchable.append(session.song_key_text.lower())
                if not any(search_text in value for value in searchable):
                    continue

            if mixable_enabled:
                normalized_key = self.normalize_key_text(session.song_key_text)
                if not normalized_key or normalized_key not in mixable_keys:
                    continue

            filtered.append(session)

        return filtered

    def on_tab_changed(self, event=None):
        self.update_player_frame_visibility()

    def update_player_frame_visibility(self):
        should_show_player = (
            self.has_active_session()
            and self.notebook.select() == str(self.playback_tab)
        )

        if should_show_player:
            if not self.player_frame.winfo_manager():
                self.player_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        else:
            if self.player_frame.winfo_manager():
                self.player_frame.grid_remove()

    def update_save_button_state(self):
        if self.selected_saved_session_id:
            self.save_delete_button.config(text="Delete Session", state="normal")
        elif self.current_pipeline_result:
            self.save_delete_button.config(text="Save Session", state="normal")
        else:
            self.save_delete_button.config(text="Save Session", state="disabled")

    def show_session_loading(self, message: str = "Loading session..."):
        self.session_loading_var.set(message)
        self.session_loading_frame.grid()
        self.session_loading_bar.start(10)
        self.saved_sessions_listbox.configure(state="disabled")
        self.save_delete_button.configure(state="disabled")

    def hide_session_loading(self):
        self.session_loading_var.set("")
        self.session_loading_bar.stop()
        self.session_loading_frame.grid_remove()
        self.saved_sessions_listbox.configure(state="normal")
        self.update_save_button_state()

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

    def on_saved_sessions_ctrl_n(self, event=None):
        self.on_new_window_shortcut()
        return "break"

    def on_save_or_delete(self):
        if self.selected_saved_session_id:
            self.delete_selected_session()
        else:
            self.save_current_session()

    def save_current_session(self):
        result = self.current_pipeline_result
        if not result:
            messagebox.showinfo("Save Session", "No session available to save.")
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
        self.show_session_loading(f"Loading {session.title}...")
        self.clear_current_session()
        self.full_mix_path = session.audio_path
        self.current_title = session.title
        self.song_key_text = session.song_key_text
        self.current_pipeline_result = None

        if session.thumbnail_path:
            self.set_thumbnail_from_file(session.thumbnail_path)
        else:
            self.thumbnail_label.configure(image="", text="No\nthumbnail")

        def worker():
            try:
                if session.stems_dir is None:
                    preloaded = self.player.load_mix_only(session.audio_path)
                else:
                    preloaded = self.player.load_audio(session.stems_dir, session.audio_path)
            except Exception as e:
                self.append_log(f"Failed to load saved session: {e}")
                self.root.after(0, self.hide_session_loading)
                return

            def _finish():
                window_title = session.title
                self.root.title(window_title)
                self.notebook.select(self.playback_tab)
                self.setup_player(session.stems_dir, preloaded=preloaded)
                self.update_key_table()
                self.hide_session_loading()

            self.root.after(0, _finish)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- player UI ----------

    # Implemented in PlayerUIMixin.

    # ---------- waveform logic ----------

    # Waveform rendering implemented in PlayerUIMixin.
    # ---------- transport ----------

    # Transport controls implemented in PlayerUIMixin.
    def get_playback_state(self) -> str:
        if not self.player.is_playing:
            return "stopped"
        if self.player.is_paused:
            return "paused"
        return "playing"

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
        self.waveform_points = []
        self.loop_start_line_id = None
        self.loop_end_line_id = None
        self.waveform_duration = 0.0
        self.stem_vars.clear()
        self.full_mix_path = None
        self.current_title = None
        self.song_key_text = None
        self.current_pipeline_result = None
        self.current_thumbnail_bytes = None

        self.thumbnail_image = None
        self.thumbnail_label.configure(image="", text="No\nthumbnail")
        self.gain_var.set(0.0)
        self.gain_label.config(text="+0.0 dB")
        self.audio_meter.configure(value=0.0)
        self.audio_meter_label.config(text="-∞ dB")
        self.player.set_gain_db(0.0)
        self.set_playback_controls_state(False)
        self.update_key_table()
        self.update_save_button_state()
        self.update_player_frame_visibility()

    def on_reset_playback(self):
        """
        Reset speed to 1x, pitch to +0.0 st, volume to 100%.
        Update both sliders/labels and underlying audio.
        """
        self.player.set_loop_enabled(False)
        self.player.reset_loop_points()
        self.update_loop_button()

        # sliders
        if self.volume_var is not None:
            self.volume_var.set(1.0)
        if self.speed_var is not None:
            self.speed_var.set(1.0)
        if self.pitch_var is not None:
            self.pitch_var.set(0.0)
        if self.gain_var is not None:
            self.gain_var.set(0.0)
        if self.reverb_enabled_var is not None:
            self.reverb_enabled_var.set(False)
        if self.reverb_mix_var is not None:
            self.reverb_mix_var.set(0.45)

        # labels
        if self.volume_label is not None:
            self.volume_label.config(text="100%")
        if self.speed_label is not None:
            self.speed_label.config(text="1.00x")
        if self.pitch_label is not None:
            self.pitch_label.config(text=self.format_pitch_label(0.0))
        if self.gain_label is not None:
            self.gain_label.config(text="+0 dB")
        if self.reverb_mix_label is not None:
            self.reverb_mix_label.config(text="45% wet")

        # audio engine
        self.player.set_master_volume(1.0)
        self.player.set_tempo_and_pitch(1.0, 0.0)
        self.player.set_gain_db(0.0)
        self.player.set_reverb_enabled(False)
        self.player.set_reverb_wet(0.45)
        self.update_reverb_controls_state()

        self.update_key_table(0.0)

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
        Quantize to 1.0 semitone steps between -6 and +6.
        """
        snapped = round(v)
        return max(-6.0, min(6.0, snapped))

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
        self.update_key_table(snapped)

    def on_pitch_release(self, event):
        if self.pitch_var is None:
            return
        semitones = self.snap_pitch(float(self.pitch_var.get()))
        self.pitch_var.set(semitones)
        self.player.set_pitch_semitones(semitones)

        if self.pitch_label is not None:
            self.pitch_label.config(text=self.format_pitch_label(semitones))

        self.update_key_table(semitones)

        self.waveform_duration = self.player.get_duration()
        self.update_waveform_from_selection()
        self.draw_waveform()

    @staticmethod
    def snap_gain(value: float) -> float:
        if abs(value) < 0.25:
            return 0.0
        return max(-10.0, min(10.0, value))

    def on_gain_change(self, value: str):
        try:
            raw = float(value)
        except ValueError:
            raw = 0.0

        snapped = self.snap_gain(raw)
        if abs(snapped - raw) <= 0.15:
            self.gain_var.set(snapped)
            gain = snapped
        else:
            gain = raw

        self.player.set_gain_db(gain)
        if self.gain_label is not None:
            self.gain_label.config(text=f"{gain:+.1f} dB")

    def on_gain_release(self, event):
        if self.gain_var is None:
            return
        value = float(self.gain_var.get())
        snapped = self.snap_gain(value)
        self.gain_var.set(snapped)
        self.player.set_gain_db(snapped)
        if self.gain_label is not None:
            self.gain_label.config(text=f"{snapped:+.1f} dB")

    def on_reverb_toggle(self):
        enabled = bool(self.reverb_enabled_var.get()) if self.reverb_enabled_var else False
        self.player.set_reverb_enabled(enabled)
        self.update_reverb_controls_state()

    def on_reverb_mix_change(self, value: str):
        try:
            wet = float(value)
        except ValueError:
            wet = 0.0

        wet = max(0.0, min(1.0, wet))
        if self.reverb_mix_var is not None:
            self.reverb_mix_var.set(wet)
        self.player.set_reverb_wet(wet)
        if self.reverb_mix_label is not None:
            pct = int(round(wet * 100))
            self.reverb_mix_label.config(text=f"{pct}% wet")

    def update_reverb_controls_state(self):
        slider_state = "disabled"
        if self.reverb_enabled_var is not None and self.playback_enabled:
            slider_state = "normal" if self.reverb_enabled_var.get() else "disabled"
        if self.reverb_mix_slider is not None:
            try:
                self.reverb_mix_slider.state(["!disabled"] if slider_state == "normal" else ["disabled"])
            except Exception:
                try:
                    self.reverb_mix_slider.configure(state=slider_state)
                except Exception:
                    pass

    def get_reverb_enabled(self) -> bool:
        if self.reverb_enabled_var is not None:
            try:
                return bool(self.reverb_enabled_var.get())
            except Exception:
                return False
        return False

    def set_reverb_enabled_from_master(self, enabled: bool):
        if self.reverb_enabled_var is not None:
            self.reverb_enabled_var.set(enabled)
        self.on_reverb_toggle()

    def toggle_reverb_from_master(self):
        self.set_reverb_enabled_from_master(not self.get_reverb_enabled())


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

    def get_master_volume(self) -> float:
        try:
            return float(self.volume_var.get()) if self.volume_var is not None else 0.0
        except Exception:
            return 0.0

    def set_master_volume_from_master(self, volume: float):
        if self.volume_var is not None:
            self.volume_var.set(volume)
        self.on_volume_change(str(volume))


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

            level = self.player.get_output_level()
            if self.audio_meter is not None:
                self.audio_meter.configure(value=max(0.0, min(level, 1.0)))
            if self.audio_meter_label is not None:
                if level <= 1e-6:
                    db_text = "-∞ dB"
                else:
                    db = max(-60.0, 20 * math.log10(level))
                    db_text = f"{db:.1f} dB"
                self.audio_meter_label.config(text=db_text)

            if (
                self.play_pause_button is not None
                and not self.player.is_playing
                and not self.player.is_paused
            ):
                self.play_pause_button.config(text="Play")

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
        pitch_part = f"{sign}{semitones}"

        current_key = self.get_current_key_text(semitones)
        if not current_key:
            return pitch_part

        return f"{pitch_part} |  {current_key}"

    @staticmethod
    def normalize_mode(mode_raw: str) -> str:
        mode_lower = mode_raw.lower()
        if "min" in mode_lower:
            return "minor"
        if "maj" in mode_lower:
            return "major"
        return mode_raw

    def parse_key_text(self, key_text: str) -> tuple[int, str] | None:
        parts = key_text.split()
        if len(parts) < 2:
            return None

        tonic_raw = parts[0]
        mode_raw = " ".join(parts[1:])
        tonic = FLAT_TO_SHARP.get(tonic_raw, tonic_raw)

        try:
            tonic_index = CHROMA_LABELS.index(tonic)
        except ValueError:
            return None

        return tonic_index, self.normalize_mode(mode_raw)

    def get_current_key_text(self, semitones: float | None = None) -> str | None:
        base_key = self.song_key_text
        if not base_key:
            return None

        parsed = self.parse_key_text(base_key)
        if not parsed:
            return base_key

        tonic_index, mode_raw = parsed
        key_steps = 0
        if semitones is None:
            if self.pitch_var is not None:
                try:
                    key_steps = int(round(float(self.pitch_var.get())))
                except (TypeError, ValueError):
                    key_steps = 0
        else:
            key_steps = int(round(semitones))

        return self.transpose_parsed_key(tonic_index, mode_raw, key_steps)

    @staticmethod
    def transpose_parsed_key(tonic_index: int, mode_raw: str, semitone_steps: int) -> str:
        new_index = (tonic_index + semitone_steps) % 12
        new_tonic = CHROMA_LABELS[new_index]
        return f"{new_tonic} {mode_raw}"

    def compute_relative_key(self, tonic_index: int, mode_raw: str) -> str:
        mode_lower = mode_raw.lower()
        if "minor" in mode_lower:
            # Relative major is a minor third up
            rel_index = (tonic_index + 3) % 12
            rel_mode = "major"
        else:
            # Relative minor is a minor third down
            rel_index = (tonic_index + 9) % 12
            rel_mode = "minor"
        return f"{CHROMA_LABELS[rel_index]} {rel_mode}"

    def compute_key_table_values(self, semitones: float | None = None) -> dict[str, str]:
        default_text = "N/A"
        values = {
            "current": default_text,
            "plus_one": default_text,
            "minus_one": default_text,
            "relative": default_text,
            "subdominant": default_text,
            "dominant": default_text,
        }

        current_key = self.get_current_key_text(semitones)
        if not current_key:
            return values

        values["current"] = current_key

        parsed = self.parse_key_text(current_key)
        if not parsed:
            return values

        tonic_index, mode_raw = parsed

        values["plus_one"] = self.transpose_parsed_key(tonic_index, mode_raw, 7)
        values["minus_one"] = self.transpose_parsed_key(tonic_index, mode_raw, -7)

        relative_key_text = self.compute_relative_key(tonic_index, mode_raw)
        values["relative"] = relative_key_text

        rel_parsed = self.parse_key_text(relative_key_text)
        if rel_parsed:
            rel_tonic_index, rel_mode = rel_parsed
            values["subdominant"] = self.transpose_parsed_key(rel_tonic_index, rel_mode, 5)
            values["dominant"] = self.transpose_parsed_key(rel_tonic_index, rel_mode, 7)

        return values

    def update_key_table(self, semitones: float | None = None):
        if not self.key_table_value_labels:
            return

        values = self.compute_key_table_values(semitones)
        for key, lbl in self.key_table_value_labels.items():
            lbl.config(text=values.get(key, "N/A"))


