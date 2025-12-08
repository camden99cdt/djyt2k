import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class SavedSession:
    session_id: str
    title: str
    song_key_text: Optional[str]
    session_dir: str
    audio_rel_path: str
    stems_rel_dir: Optional[str]
    thumbnail_rel_path: Optional[str]
    created_at: str

    @property
    def display_name(self) -> str:
        if self.song_key_text:
            base = f"{self.title} ({self.song_key_text})"
        else:
            base = self.title

        if not self.stems_rel_dir:
            return f"{base} [ns]"

        return base

    @property
    def audio_path(self) -> str:
        return os.path.join(self.session_dir, self.audio_rel_path)

    @property
    def stems_dir(self) -> Optional[str]:
        if self.stems_rel_dir:
            return os.path.join(self.session_dir, self.stems_rel_dir)
        return None

    @property
    def thumbnail_path(self) -> Optional[str]:
        if self.thumbnail_rel_path:
            return os.path.join(self.session_dir, self.thumbnail_rel_path)
        return None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "song_key_text": self.song_key_text,
            "session_dir": self.session_dir,
            "audio_rel_path": self.audio_rel_path,
            "stems_rel_dir": self.stems_rel_dir,
            "thumbnail_rel_path": self.thumbnail_rel_path,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "SavedSession":
        return SavedSession(
            session_id=data["session_id"],
            title=data["title"],
            song_key_text=data.get("song_key_text"),
            session_dir=data["session_dir"],
            audio_rel_path=data["audio_rel_path"],
            stems_rel_dir=data.get("stems_rel_dir"),
            thumbnail_rel_path=data.get("thumbnail_rel_path"),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


class SavedSessionStore:
    def __init__(self):
        home = os.path.expanduser("~")
        self.base_dir = os.path.join(home, ".djyt")
        self.sessions_dir = os.path.join(self.base_dir, "sessions")
        self.index_path = os.path.join(self.base_dir, "sessions.json")

        os.makedirs(self.sessions_dir, exist_ok=True)
        self.sessions: List[SavedSession] = []
        self._load_sessions()

    def _load_sessions(self):
        if not os.path.exists(self.index_path):
            self.sessions = []
            return

        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self.sessions = []
            return

        sessions: List[SavedSession] = []
        for raw in data:
            try:
                session = SavedSession.from_dict(raw)
            except Exception:
                continue
            if not os.path.exists(session.audio_path):
                continue
            if session.stems_dir and not os.path.isdir(session.stems_dir):
                continue
            sessions.append(session)
        self.sessions = sessions

    def _write_sessions(self):
        data = [s.to_dict() for s in self.sessions]
        os.makedirs(self.base_dir, exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def list_sessions(self) -> List[SavedSession]:
        return list(self.sessions)

    def add_session(
        self,
        title: str,
        song_key_text: Optional[str],
        session_dir: str,
        audio_path: str,
        stems_dir: Optional[str],
        thumbnail_bytes: Optional[bytes] = None,
    ) -> SavedSession:
        session_id = uuid.uuid4().hex
        dest_dir = os.path.join(self.sessions_dir, session_id)

        os.makedirs(self.sessions_dir, exist_ok=True)

        audio_rel = os.path.relpath(audio_path, session_dir)
        stems_rel = os.path.relpath(stems_dir, session_dir) if stems_dir else None

        shutil.move(session_dir, dest_dir)

        thumb_rel = None
        if thumbnail_bytes:
            thumb_rel = "thumbnail.jpg"
            thumb_path = os.path.join(dest_dir, thumb_rel)
            os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
            with open(thumb_path, "wb") as f:
                f.write(thumbnail_bytes)

        session = SavedSession(
            session_id=session_id,
            title=title,
            song_key_text=song_key_text,
            session_dir=dest_dir,
            audio_rel_path=audio_rel,
            stems_rel_dir=stems_rel,
            thumbnail_rel_path=thumb_rel,
            created_at=datetime.now().isoformat(),
        )
        self.sessions.append(session)
        self._write_sessions()
        return session

    def delete_session(self, session_id: str) -> bool:
        session = next((s for s in self.sessions if s.session_id == session_id), None)
        if not session:
            return False

        if os.path.isdir(session.session_dir):
            shutil.rmtree(session.session_dir, ignore_errors=True)

        self.sessions = [s for s in self.sessions if s.session_id != session_id]
        self._write_sessions()
        return True

    def get_session(self, session_id: str) -> Optional[SavedSession]:
        return next((s for s in self.sessions if s.session_id == session_id), None)

    def rename_session(self, session_id: str, new_title: str) -> Optional[SavedSession]:
        session = self.get_session(session_id)
        if not session:
            return None

        session.title = new_title
        self._write_sessions()
        return session
