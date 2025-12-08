"""Search suggestion dropdown handling for the YouTube URL input."""

import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Optional

from PIL import Image, ImageTk
from tkinter import ttk

from youtube_search import SearchResult, fetch_search_results


class SearchSuggestionController:
    """Encapsulates the autocomplete dropdown used for search suggestions."""

    def __init__(self, root: tk.Tk, url_entry: tk.Entry, url_var: tk.StringVar):
        self.root = root
        self.url_entry = url_entry
        self.url_var = url_var

        self.search_debounce_id: Optional[str] = None
        self.search_request_counter = 0
        self.search_executor = ThreadPoolExecutor(max_workers=2)
        self.search_dropdown: tk.Toplevel | None = None
        self.search_result_frames: list[tk.Widget] = []
        self.search_result_images: list[ImageTk.PhotoImage] = []
        self.search_results: list[SearchResult] = []
        self.highlight_index: int = -1
        self.search_loading: bool = False
        self.search_row_height_estimate: int = 64

    def bind_events(self):
        self.url_var.trace_add("write", self.on_url_text_change)
        self.url_entry.bind("<KeyRelease>", self.on_url_keypress)
        self.url_entry.bind("<FocusOut>", self.on_url_focus_out)

    def shutdown(self):
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

        self.search_debounce_id = self.root.after(1000, lambda t=text: self.trigger_search(t))

    def on_url_keypress(self, event):
        if event.keysym in {"Up", "Down", "Return", "Escape"}:
            handled = self.handle_search_navigation(event.keysym)
            if handled:
                return "break"

    def on_url_focus_out(self, _event):
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

        future = self.search_executor.submit(fetch_search_results, query)
        future.add_done_callback(callback)

    def on_search_results(self, request_id: int, _query: str, results: list[SearchResult]):
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
            lambda _e, i=index: self.apply_search_selection(i),
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

