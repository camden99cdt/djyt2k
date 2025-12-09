import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


@dataclass
class Jam:
    jam_id: str
    title: str
    session_ids: List[str]
    created_at: str

    def to_dict(self) -> dict:
        return {
            "jam_id": self.jam_id,
            "title": self.title,
            "session_ids": list(self.session_ids),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "Jam":
        return Jam(
            jam_id=data["jam_id"],
            title=data.get("title", "Untitled Jam"),
            session_ids=list(data.get("session_ids", [])),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


class JamStore:
    def __init__(self):
        home = os.path.expanduser("~")
        self.base_dir = os.path.join(home, ".djyt")
        self.index_path = os.path.join(self.base_dir, "jams.json")
        os.makedirs(self.base_dir, exist_ok=True)
        self.jams: List[Jam] = []
        self._load_jams()

    def _load_jams(self):
        if not os.path.exists(self.index_path):
            self.jams = []
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self.jams = []
            return

        jams: List[Jam] = []
        for raw in data:
            try:
                jam = Jam.from_dict(raw)
            except Exception:
                continue
            jams.append(jam)
        self.jams = jams

    def _write_jams(self):
        data = [j.to_dict() for j in self.jams]
        os.makedirs(self.base_dir, exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def list_jams(self) -> List[Jam]:
        return list(self.jams)

    def get_jam(self, jam_id: str) -> Optional[Jam]:
        return next((j for j in self.jams if j.jam_id == jam_id), None)

    def add_jam(
        self, title: str, session_ids: Optional[List[str]] = None, jam_id: str | None = None
    ) -> Jam:
        jam = Jam(
            jam_id=jam_id or uuid.uuid4().hex,
            title=title or "Untitled Jam",
            session_ids=list(session_ids or []),
            created_at=datetime.now().isoformat(),
        )
        self.jams.append(jam)
        self._write_jams()
        return jam

    def update_jam(self, jam_id: str, title: Optional[str], session_ids: List[str]) -> Optional[Jam]:
        jam = self.get_jam(jam_id)
        if not jam:
            return None
        jam.title = title or jam.title
        jam.session_ids = list(session_ids)
        self._write_jams()
        return jam

    def delete_jam(self, jam_id: str) -> bool:
        existing = any(j.jam_id == jam_id for j in self.jams)
        if not existing:
            return False
        self.jams = [j for j in self.jams if j.jam_id != jam_id]
        self._write_jams()
        return True
