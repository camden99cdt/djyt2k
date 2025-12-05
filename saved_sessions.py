import json
import os
import shutil
import urllib.request
from dataclasses import dataclass, asdict
from typing import Callable


def _default_logger(msg: str):
    pass


@dataclass
class SavedSession:
    title: str
    session_dir: str
    audio_path: str
    stems_dir: str | None
    thumbnail_path: str | None
    song_key_text: str | None


class SavedSessionStore:
    def __init__(self, base_dir: str | None = None, log_callback: Callable[[str], None] | None = None):
        self.base_dir = base_dir or os.path.join(os.path.expanduser("~"), ".djyt")
        self.sessions_file = os.path.join(self.base_dir, "sessions.json")
        self.log = log_callback or _default_logger
        self.sessions: list[SavedSession] = []
        self.load()

    # ---------- persistence ----------
    def load(self):
        os.makedirs(self.base_dir, exist_ok=True)
        if not os.path.exists(self.sessions_file):
            self.sessions = []
            return

        try:
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions: list[SavedSession] = []
            for item in data.get("sessions", []):
                sessions.append(
                    SavedSession(
                        title=item.get("title", "Unknown"),
                        session_dir=item.get("session_dir", ""),
                        audio_path=item.get("audio_path", ""),
                        stems_dir=item.get("stems_dir"),
                        thumbnail_path=item.get("thumbnail_path"),
                        song_key_text=item.get("song_key_text"),
                    )
                )
            self.sessions = [s for s in sessions if os.path.exists(s.audio_path)]
        except Exception as exc:
            self.sessions = []
            self.log(f"Failed to load saved sessions: {exc}")

    def persist(self):
        try:
            payload = {"sessions": [asdict(s) for s in self.sessions]}
            with open(self.sessions_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            self.log(f"Failed to persist saved sessions: {exc}")

    # ---------- helpers ----------
    def find_by_dir(self, session_dir: str | None) -> SavedSession | None:
        if not session_dir:
            return None
        for sess in self.sessions:
            if sess.session_dir == session_dir:
                return sess
        return None

    def _unique_dest_dir(self, base_name: str) -> str:
        dest_dir = os.path.join(self.base_dir, base_name)
        suffix = 1
        while os.path.exists(dest_dir):
            dest_dir = os.path.join(self.base_dir, f"{base_name}_{suffix}")
            suffix += 1
        return dest_dir

    def _download_thumbnail(self, thumb_url: str | None, dest_dir: str) -> str | None:
        if not thumb_url:
            return None
        try:
            req = urllib.request.Request(
                thumb_url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
            thumb_path = os.path.join(dest_dir, "thumbnail.jpg")
            with open(thumb_path, "wb") as f:
                f.write(data)
            return thumb_path
        except Exception as exc:
            self.log(f"Could not save thumbnail: {exc}")
            return None

    def _relocate_session(self, src_dir: str) -> str:
        base_name = os.path.basename(src_dir.rstrip(os.sep))
        dest_dir = self._unique_dest_dir(base_name)
        shutil.move(src_dir, dest_dir)
        return dest_dir

    def add_session(
        self,
        *,
        title: str,
        session_dir: str,
        full_mix_path: str,
        stems_dir: str | None,
        thumbnail_url: str | None,
        song_key_text: str | None,
    ) -> SavedSession:
        dest_dir = self._relocate_session(session_dir)

        audio_path = os.path.join(dest_dir, os.path.basename(full_mix_path))
        if not os.path.exists(audio_path):
            raise FileNotFoundError("Could not locate session audio to save.")

        stems_path = None
        if stems_dir:
            rel_stems = os.path.relpath(stems_dir, session_dir)
            stems_path = os.path.join(dest_dir, rel_stems)

        thumb_path = self._download_thumbnail(thumbnail_url, dest_dir)

        saved = SavedSession(
            title=title or "Unknown",
            session_dir=dest_dir,
            audio_path=audio_path,
            stems_dir=stems_path,
            thumbnail_path=thumb_path,
            song_key_text=song_key_text,
        )
        self.sessions.append(saved)
        self.persist()
        return saved

    def delete(self, saved: SavedSession):
        if saved in self.sessions:
            self.sessions.remove(saved)
        try:
            shutil.rmtree(saved.session_dir, ignore_errors=True)
        except Exception:
            pass
        self.persist()

    def resolve_stems_dir(self, saved: SavedSession) -> str | None:
        if saved.stems_dir and os.path.exists(saved.stems_dir):
            return saved.stems_dir

        track_name = os.path.basename(saved.stems_dir or "")
        search_roots = [saved.session_dir, os.path.join(saved.session_dir, "separated")]
        for root in search_roots:
            if not os.path.isdir(root):
                continue
            for model_dir in os.listdir(root):
                model_path = os.path.join(root, model_dir)
                if not os.path.isdir(model_path):
                    continue
                candidate = os.path.join(model_path, track_name)
                if os.path.isdir(candidate):
                    return candidate
        return saved.stems_dir
