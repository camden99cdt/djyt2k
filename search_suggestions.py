from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import subprocess
import urllib.request

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk


@dataclass
class SearchResult:
    title: str
    url: str
    duration: str
    published: str
    thumbnail_bytes: bytes | None


class SearchDropdownController:
    def __init__(self, root: tk.Misc, url_var: tk.StringVar, url_entry: ttk.Entry):
        self.root = root
        self.url_var = url_var
        self.url_entry = url_entry

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

        self.url_var.trace_add("write", self.on_url_text_change)
        self.url_entry.bind("<KeyRelease>", self.on_url_keypress)
        self.url_entry.bind("<FocusOut>", self.on_url_focus_out)

    def shutdown(self):
        if self.search_debounce_id:
            self.root.after_cancel(self.search_debounce_id)
            self.search_debounce_id = None
        try:
            self.search_executor.shutdown(wait=False)
        except Exception:
            pass
        self.hide_search_dropdown()

    @staticmethod
    def is_probable_url(text: str) -> bool:
        lower = text.lower()
        return lower.startswith(("http://", "https://", "www.")) or "youtu" in lower

    def on_url_text_change(self, *_):
        if self.search_debounce_id:
            self.root.after_cancel(self.search_debounce_id)
            self.search_debounce_id = None

        text = self.url_var.get().strip()
        if not text or self.is_probable_url(text):
            self.hide_search_dropdown()
            return

        self.search_debounce_id = self.root.after(400, lambda t=text: self.trigger_search(t))

    def on_url_keypress(self, event):
        if event.keysym in {"Up", "Down", "Return", "Escape"}:
            handled = self.handle_search_navigation(event.keysym)
            if handled:
                return "break"

    def on_url_focus_out(self, event):
        self.root.after(150, self.hide_search_dropdown_if_unfocused)

    def hide_search_dropdown_if_unfocused(self):
        focus_widget = self.root.focus_get()
        if focus_widget is None:
            self.hide_search_dropdown()
            return
        if self.search_dropdown and str(focus_widget).startswith(str(self.search_dropdown)):
            return
        if focus_widget == self.url_entry:
            return
        self.hide_search_dropdown()

    def trigger_search(self, query: str):
        self.search_debounce_id = None
        self.search_request_counter += 1
        request_id = self.search_request_counter
        self.search_loading = True
        self.search_results = []
        self.show_search_dropdown(loading=True)

        def callback(future):
            try:
                results = future.result()
            except Exception:
                results = []
            self.root.after(0, lambda: self.on_search_results(request_id, query, results))

        future = self.search_executor.submit(self.fetch_search_results, query)
        future.add_done_callback(callback)

    def fetch_search_results(self, query: str) -> list[SearchResult]:
        cmd = [
            "yt-dlp",
            "--dump-json",
            "ytsearch5:" + query,
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        results: list[SearchResult] = []

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            title = data.get("title") or "(untitled)"
            url = data.get("original_url") or data.get("webpage_url") or ""
            duration = self.format_duration_from_seconds(data.get("duration"))
            published = self.format_time_ago(data)
            thumb_bytes = None
            thumb_url = data.get("thumbnail")
            if thumb_url:
                thumb_bytes = self.fetch_thumbnail_bytes(thumb_url)

            if title and url:
                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        duration=duration,
                        published=published,
                        thumbnail_bytes=thumb_bytes,
                    )
                )

            if len(results) >= 5:
                break

        return results

    @staticmethod
    def format_duration_from_seconds(seconds) -> str:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "--:--"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @staticmethod
    def format_time_ago(data: dict) -> str:
        upload_date = data.get("upload_date") or data.get("release_date")
        timestamp = data.get("timestamp") or data.get("release_timestamp")
        dt: datetime | None = None
        if upload_date:
            try:
                dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
        elif timestamp:
            try:
                dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                dt = None

        if not dt:
            return ""

        delta = datetime.now(tz=timezone.utc) - dt
        days = delta.days
        if days < 1:
            hours = delta.seconds // 3600
            if hours:
                return f"{hours}h ago"
            minutes = max(1, delta.seconds // 60)
            return f"{minutes}m ago"
        if days < 30:
            return f"{days}d ago"
        months = days // 30
        if months < 12:
            return f"{months}mo ago"
        years = months // 12
        return f"{years}y ago"

    @staticmethod
    def fetch_thumbnail_bytes(url: str) -> bytes | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read()
        except Exception:
            return None

    def on_search_results(self, request_id: int, query: str, results: list[SearchResult]):
        if request_id != self.search_request_counter:
            return
        if self.is_probable_url(self.url_var.get().strip()):
            self.hide_search_dropdown()
            return
        if not results:
            results = [SearchResult("No results", "", "", "", None)]
        self.search_results = results
        self.search_loading = False
        self.show_search_dropdown()

    def show_search_dropdown(self, loading: bool = False):
        if not self.search_results and not loading:
            self.hide_search_dropdown()
            return

        if self.search_dropdown is None or not self.search_dropdown.winfo_exists():
            self.search_dropdown = tk.Toplevel(self.root)
            self.search_dropdown.overrideredirect(True)
            self.search_dropdown.attributes("-topmost", True)

        x = self.url_entry.winfo_rootx()
        y = self.url_entry.winfo_rooty() + self.url_entry.winfo_height()
        width = self.url_entry.winfo_width()
        estimated_height = self.search_row_height_estimate * 5
        self.search_dropdown.geometry(f"{width}x{estimated_height}+{x}+{y}")

        for child in self.search_dropdown.winfo_children():
            child.destroy()

        container = ttk.Frame(self.search_dropdown, relief="solid", borderwidth=1)
        container.pack(fill="both", expand=True)
        list_frame = ttk.Frame(container)
        list_frame.pack(fill="both", expand=True)

        self.search_result_frames.clear()
        self.search_result_images.clear()
        self.highlight_index = -1

        if loading:
            loading_row = tk.Frame(list_frame, bg="#ffffff", padx=8, pady=8)
            loading_row.pack(fill="x", expand=True)
            spinner = ttk.Progressbar(loading_row, mode="indeterminate", length=80)
            spinner.pack(side="left", padx=(0, 8))
            spinner.start(10)
            tk.Label(loading_row, text="Searching...", bg="#ffffff").pack(side="left", anchor="w")
            self.search_result_frames.append(loading_row)

        for idx, result in enumerate(self.search_results):
            row = tk.Frame(list_frame, bg="#ffffff", bd=0, relief="flat", padx=4, pady=4)
            row.pack(fill="x", expand=True)

            thumb_label = tk.Label(row, bg="#ffffff")
            thumb_label.pack(side="left", padx=(0, 6))
            if result.thumbnail_bytes:
                try:
                    image = Image.open(BytesIO(result.thumbnail_bytes))
                    image.thumbnail((80, 45))
                    photo = ImageTk.PhotoImage(image)
                    thumb_label.configure(image=photo)
                    self.search_result_images.append(photo)
                except Exception:
                    thumb_label.configure(text="No\nthumb", bg="#ffffff")
            else:
                thumb_label.configure(text="No\nthumb", bg="#ffffff")

            text_frame = tk.Frame(row, bg="#ffffff")
            text_frame.pack(side="left", fill="x", expand=True)

            wrap_len = max(120, width - 140)
            title_label = tk.Label(
                text_frame,
                text=result.title,
                wraplength=wrap_len,
                justify="left",
                anchor="w",
                bg="#ffffff",
            )
            title_label.pack(anchor="w")

            meta_text = " ".join(filter(None, [result.duration, result.published]))
            meta_label = tk.Label(text_frame, text=meta_text, fg="#666666", bg="#ffffff", anchor="w")
            meta_label.pack(anchor="w")

            row.bind("<Enter>", lambda e, i=idx: self.set_highlight(i))

            self.bind_search_row_click(row, idx)
            self.bind_search_row_click(text_frame, idx)
            self.bind_search_row_click(title_label, idx)
            self.bind_search_row_click(meta_label, idx)
            self.bind_search_row_click(thumb_label, idx)

            self.search_result_frames.append(row)

        self.search_dropdown.deiconify()
        self.search_dropdown.lift(self.root)
        self.search_dropdown.update_idletasks()

        row_heights = [frame.winfo_height() or frame.winfo_reqheight() for frame in self.search_result_frames]
        if row_heights:
            self.search_row_height_estimate = max(self.search_row_height_estimate, max(row_heights))
        row_height = self.search_row_height_estimate
        visible_rows = max(5, len(self.search_result_frames))
        height = row_height * visible_rows
        self.search_dropdown.geometry(f"{width}x{height}+{x}+{y}")

    def bind_search_row_click(self, widget: tk.Widget, index: int):
        widget.bind(
            "<Button-1>",
            lambda e, i=index: self.apply_search_selection(i),
        )

    def set_highlight(self, index: int):
        if not self.search_result_frames:
            return
        index = max(0, min(index, len(self.search_result_frames) - 1))
        for i, frame in enumerate(self.search_result_frames):
            bg = "#e6edff" if i == index else "#ffffff"
            frame.configure(bg=bg)
            for child in frame.winfo_children():
                try:
                    child.configure(bg=bg)
                except tk.TclError:
                    pass
        self.highlight_index = index

    def handle_search_navigation(self, keysym: str) -> bool:
        if not self.search_dropdown or not self.search_dropdown.winfo_ismapped():
            return False
        if keysym == "Down":
            new_index = 0 if self.highlight_index < 0 else self.highlight_index + 1
            self.set_highlight(new_index)
            return True
        if keysym == "Up":
            new_index = len(self.search_result_frames) - 1 if self.highlight_index <= 0 else self.highlight_index - 1
            self.set_highlight(new_index)
            return True
        if keysym == "Return":
            if self.highlight_index >= 0:
                self.apply_search_selection(self.highlight_index)
                return True
            return False
        if keysym == "Escape":
            self.hide_search_dropdown()
            return True
        return False

    def apply_search_selection(self, index: int):
        if not self.search_results:
            return
        index = max(0, min(index, len(self.search_results) - 1))
        selection = self.search_results[index]
        if not selection.url:
            return
        self.url_var.set(selection.url)
        self.hide_search_dropdown()
        self.url_entry.icursor("end")
        self.url_entry.focus_set()

    def hide_search_dropdown(self):
        if self.search_dropdown and self.search_dropdown.winfo_exists():
            self.search_dropdown.withdraw()
        self.search_results = []
        self.search_result_frames.clear()
        self.search_result_images.clear()
        self.highlight_index = -1
