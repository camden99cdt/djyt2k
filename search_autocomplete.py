import json
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import urllib.parse
import urllib.request
from typing import Callable

import tkinter as tk

from PIL import Image, ImageTk

try:
    from yt_dlp import YoutubeDL
    USE_YTDLP_PYTHON = True
except ImportError:
    YoutubeDL = None
    USE_YTDLP_PYTHON = False


@dataclass
class SearchResult:
    title: str
    url: str
    duration: str
    time_ago: str
    thumb_bytes: bytes | None


class SearchAutocomplete:
    def __init__(
        self,
        root: tk.Misc,
        entry: tk.Entry,
        url_var: tk.StringVar,
        log_callback: Callable[[str], None],
        on_url_selected: Callable[[str], None],
    ) -> None:
        self.root = root
        self.entry = entry
        self.url_var = url_var
        self.log = log_callback
        self.on_url_selected = on_url_selected

        self.dropdown: tk.Toplevel | None = None
        self.results: list[SearchResult] = []
        self.result_frames: list[tk.Frame] = []
        self.result_images: list[ImageTk.PhotoImage] = []
        self.active_index: int = -1
        self.debounce_id: str | None = None
        self.query_token: int = 0

        self.url_var.trace_add("write", self.on_url_change)
        self.entry.bind("<Down>", self.on_nav_down)
        self.entry.bind("<Up>", self.on_nav_up)
        self.entry.bind("<Return>", self.on_enter)
        self.entry.bind("<Escape>", self.on_escape)
        self.entry.bind("<FocusOut>", self.on_focus_out)

    # ---------- formatting helpers ----------
    @staticmethod
    def is_probable_url(text: str) -> bool:
        if not text:
            return False
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme and parsed.netloc:
            return True
        lowered = text.lower()
        return lowered.startswith("www.") or "youtube.com" in lowered or "youtu.be" in lowered

    @staticmethod
    def format_duration(seconds: int | float | None) -> str:
        if seconds is None:
            return "--:--"
        try:
            total = int(seconds)
        except (TypeError, ValueError):
            return "--:--"
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def format_time_ago(upload_date: str | int | None) -> str:
        if upload_date is None:
            return "Unknown"
        try:
            if isinstance(upload_date, str) and len(upload_date) == 8:
                dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromtimestamp(int(upload_date), tz=timezone.utc)
        except Exception:
            return "Unknown"

        delta = datetime.now(timezone.utc) - dt
        days = delta.days
        if days >= 365:
            years = days // 365
            return f"{years} year{'s' if years != 1 else ''} ago"
        if days >= 30:
            months = days // 30
            return f"{months} month{'s' if months != 1 else ''} ago"
        if days >= 7:
            weeks = days // 7
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        if days > 0:
            return f"{days} day{'s' if days != 1 else ''} ago"
        hours = delta.seconds // 3600
        if hours:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        minutes = (delta.seconds % 3600) // 60
        if minutes:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        return "Just now"

    # ---------- search orchestration ----------
    def on_url_change(self, *_):
        if self.debounce_id:
            try:
                self.root.after_cancel(self.debounce_id)
            except Exception:
                pass
            self.debounce_id = None

        text = self.url_var.get().strip()
        if not text or self.is_probable_url(text):
            self.hide_dropdown()
            return

        self.debounce_id = self.root.after(350, lambda q=text: self.start_search(q))

    def start_search(self, query: str):
        if not query:
            self.hide_dropdown()
            return

        self.query_token += 1
        token = self.query_token
        self.active_index = -1
        self.show_dropdown([])

        def worker():
            results = self.fetch_search_results(query)

            def _update():
                if token != self.query_token:
                    return
                self.show_dropdown(results)

            self.root.after(0, _update)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_search_results(self, query: str) -> list[SearchResult]:
        search_query = f"ytsearch5:{query}"
        entries = []
        if USE_YTDLP_PYTHON and YoutubeDL is not None:
            ydl_opts = {
                "quiet": True,
                "skip_download": True,
                "noplaylist": True,
            }
            try:
                with YoutubeDL(ydl_opts) as ydl:  # type: ignore
                    info = ydl.extract_info(search_query, download=False)
                    entries = info.get("entries") or []
            except Exception as e:
                self.log(f"Search failed: {e}")
        else:
            cmd = ["yt-dlp", "-J", "--skip-download", "--no-playlist", search_query]
            try:
                result = subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                parsed = json.loads(result.stdout)
                entries = parsed.get("entries") or []
            except FileNotFoundError:
                self.log("yt-dlp CLI not found for search suggestions.")
            except subprocess.CalledProcessError as e:
                self.log(f"yt-dlp search failed (exit {e.returncode}).")
            except json.JSONDecodeError as e:
                self.log(f"Failed to parse yt-dlp search output: {e}")

        results: list[SearchResult] = []
        for entry in entries:
            url = entry.get("webpage_url") or entry.get("url")
            if not url:
                video_id = entry.get("id")
                if video_id:
                    url = f"https://www.youtube.com/watch?v={video_id}"
            if not url:
                continue

            duration_text = self.format_duration(entry.get("duration"))
            time_ago = self.format_time_ago(entry.get("upload_date") or entry.get("timestamp"))

            thumb_url = entry.get("thumbnail")
            if not thumb_url:
                thumbs = entry.get("thumbnails") or []
                if thumbs:
                    thumb_url = thumbs[-1].get("url")

            thumb_bytes = None
            if thumb_url:
                try:
                    req = urllib.request.Request(
                        thumb_url,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        thumb_bytes = resp.read()
                except Exception:
                    thumb_bytes = None

            results.append(
                SearchResult(
                    title=entry.get("title", "(no title)"),
                    url=url,
                    duration=duration_text,
                    time_ago=time_ago,
                    thumb_bytes=thumb_bytes,
                )
            )

        return results

    # ---------- dropdown rendering ----------
    def ensure_dropdown(self):
        if self.dropdown is None or not self.dropdown.winfo_exists():
            self.dropdown = tk.Toplevel(self.root)
            self.dropdown.overrideredirect(True)
            self.dropdown.attributes("-topmost", True)
            self.dropdown.configure(bg="#1f1f1f")

        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        width = self.entry.winfo_width()
        self.dropdown.geometry(f"{width}x1+{x}+{y}")

    def hide_dropdown(self):
        if self.dropdown and self.dropdown.winfo_exists():
            self.dropdown.destroy()
        self.dropdown = None
        self.results = []
        self.result_frames = []
        self.result_images = []
        self.active_index = -1

    def show_dropdown(self, results: list[SearchResult]):
        self.ensure_dropdown()
        if not self.dropdown:
            return

        for child in self.dropdown.winfo_children():
            child.destroy()

        container = tk.Frame(self.dropdown, bg="#1f1f1f")
        container.pack(fill="both", expand=True, padx=2, pady=2)

        self.results = results
        self.result_frames = []
        self.result_images = []

        if not results:
            label = tk.Label(
                container,
                text="Searching..." if self.query_token else "No results",
                fg="white",
                bg="#1f1f1f",
                anchor="w",
                padx=8,
                pady=6,
            )
            label.pack(fill="x")
            self.dropdown.update_idletasks()
            width = self.entry.winfo_width()
            height = label.winfo_height() + 4
            x = self.entry.winfo_rootx()
            y = self.entry.winfo_rooty() + self.entry.winfo_height()
            self.dropdown.geometry(f"{width}x{height}+{x}+{y}")
            return

        for idx, result in enumerate(results):
            frame = tk.Frame(container, bg="#1f1f1f", bd=0, highlightthickness=0)
            frame.pack(fill="x", padx=1, pady=1)
            frame.bind("<Button-1>", lambda e, i=idx: self.apply_result(i))
            frame.bind("<Enter>", lambda e, i=idx: self.set_selection(i))

            thumb_label = tk.Label(frame, bg="#1f1f1f")
            thumb_label.pack(side="left", padx=6, pady=4)
            if result.thumb_bytes:
                try:
                    image = Image.open(BytesIO(result.thumb_bytes))
                    image.thumbnail((80, 45))
                    photo = ImageTk.PhotoImage(image)
                    thumb_label.configure(image=photo)
                    self.result_images.append(photo)
                except Exception:
                    thumb_label.configure(text="No\nimage", fg="#aaaaaa")
            else:
                thumb_label.configure(text="No\nimage", fg="#aaaaaa")

            text_frame = tk.Frame(frame, bg="#1f1f1f")
            text_frame.pack(side="left", fill="x", expand=True, padx=(6, 4), pady=4)

            title_label = tk.Label(
                text_frame,
                text=result.title,
                bg="#1f1f1f",
                fg="white",
                anchor="w",
                justify="left",
                wraplength=360,
            )
            title_label.pack(fill="x")
            title_label.bind("<Button-1>", lambda e, i=idx: self.apply_result(i))

            meta_label = tk.Label(
                text_frame,
                text=f"{result.duration} • {result.time_ago}",
                bg="#1f1f1f",
                fg="#bbbbbb",
                anchor="w",
            )
            meta_label.pack(fill="x")
            meta_label.bind("<Button-1>", lambda e, i=idx: self.apply_result(i))

            self.result_frames.append(frame)

        self.set_selection(0)
        self.dropdown.update_idletasks()
        height = sum(f.winfo_height() for f in self.result_frames) + 4
        # If Tk hasn't fully realized the widgets yet, fall back to a
        # generous per-row estimate so the dropdown isn't clipped.
        if height <= 5:
            estimated_row_height = 78
            height = len(self.result_frames) * estimated_row_height + 8
        width = self.entry.winfo_width()
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        self.dropdown.geometry(f"{width}x{height}+{x}+{y}")

    # ---------- selection + navigation ----------
    def set_selection(self, index: int):
        if not self.result_frames:
            self.active_index = -1
            return
        index = max(0, min(index, len(self.result_frames) - 1))
        self.active_index = index
        for idx, frame in enumerate(self.result_frames):
            bg = "#2f2f2f" if idx == index else "#1f1f1f"
            frame.configure(bg=bg)
            for child in frame.winfo_children():
                if isinstance(child, tk.Label):
                    child.configure(bg=bg)
                for grandchild in child.winfo_children():
                    if isinstance(grandchild, tk.Label):
                        grandchild.configure(bg=bg)

    def apply_result(self, index: int):
        if index < 0 or index >= len(self.results):
            return
        result = self.results[index]
        self.on_url_selected(result.url)
        self.hide_dropdown()

    def on_nav_down(self, _event):
        if not self.results:
            return None
        new_index = 0 if self.active_index < 0 else self.active_index + 1
        if new_index >= len(self.results):
            new_index = len(self.results) - 1
        self.set_selection(new_index)
        return "break"

    def on_nav_up(self, _event):
        if not self.results:
            return None
        new_index = 0 if self.active_index <= 0 else self.active_index - 1
        self.set_selection(new_index)
        return "break"

    def on_enter(self, _event):
        if self.active_index >= 0:
            self.apply_result(self.active_index)
            return "break"
        return None

    def on_escape(self, _event):
        self.hide_dropdown()
        return "break"

    def on_focus_out(self, _event):
        self.hide_dropdown()
