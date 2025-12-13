import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class SampleEntry:
    sample_id: str
    session_title: str
    created_at: str
    duration_seconds: float
    file_path: str

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "session_title": self.session_title,
            "created_at": self.created_at,
            "duration_seconds": self.duration_seconds,
            "file_path": self.file_path,
        }

    @staticmethod
    def from_dict(data: dict) -> "SampleEntry":
        return SampleEntry(
            sample_id=data.get("sample_id") or uuid.uuid4().hex,
            session_title=data.get("session_title", "Untitled Session"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            file_path=data["file_path"],
        )


class SampleStore:
    def __init__(self):
        home = os.path.expanduser("~")
        self.base_dir = os.path.join(home, ".djyt")
        self.samples_dir = os.path.join(self.base_dir, "samples")
        self.index_path = os.path.join(self.base_dir, "samples.json")
        os.makedirs(self.samples_dir, exist_ok=True)
        self.samples: List[SampleEntry] = []
        self._last_mtime: float | None = None
        self._load_samples()

    def _load_samples(self):
        if not os.path.exists(self.index_path):
            self.samples = []
            self._last_mtime = None
            return

        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self.samples = []
            self._last_mtime = None
            return

        loaded: List[SampleEntry] = []
        for raw in data:
            try:
                entry = SampleEntry.from_dict(raw)
            except Exception:
                continue
            if not os.path.exists(entry.file_path):
                continue
            loaded.append(entry)
        self.samples = loaded
        try:
            self._last_mtime = os.path.getmtime(self.index_path)
        except Exception:
            self._last_mtime = None

    def _write_samples(self):
        data = [s.to_dict() for s in self.samples]
        os.makedirs(self.base_dir, exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            self._last_mtime = os.path.getmtime(self.index_path)
        except Exception:
            self._last_mtime = None

    def list_samples(self) -> List[SampleEntry]:
        return list(self.samples)

    def add_sample(
        self,
        session_title: str,
        duration_seconds: float,
        file_path: str,
        created_at: Optional[str] = None,
    ) -> SampleEntry:
        entry = SampleEntry(
            sample_id=uuid.uuid4().hex,
            session_title=session_title or "Untitled Session",
            created_at=created_at or datetime.now().isoformat(),
            duration_seconds=duration_seconds,
            file_path=file_path,
        )
        self.samples.append(entry)
        self._write_samples()
        return entry

    def reload_if_changed(self) -> bool:
        try:
            mtime = os.path.getmtime(self.index_path)
        except Exception:
            mtime = None

        if mtime is None:
            if self.samples:
                self.samples = []
                self._last_mtime = None
                return True
            return False

        if self._last_mtime is None or mtime > self._last_mtime:
            self._load_samples()
            return True
        return False
