"""Local user data: per-mod notes, folders and folder assignments."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path


class UserData:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "userdata.json"
        self._lock = threading.RLock()
        self.notes = {}
        self.folders = []
        self.assignments = {}
        self._load()

    def _load(self):
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        notes = payload.get("notes")
        folders = payload.get("folders")
        assignments = payload.get("assignments")
        self.notes = notes if isinstance(notes, dict) else {}
        self.folders = folders if isinstance(folders, list) else []
        self.assignments = assignments if isinstance(assignments, dict) else {}

    def _save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + f".{uuid.uuid4().hex}.tmp")
            payload = {
                "notes": dict(self.notes),
                "folders": list(self.folders),
                "assignments": dict(self.assignments),
            }
            try:
                temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(temp, self.path)
            except OSError as exc:
                try:
                    temp.unlink()
                except OSError:
                    pass
                raise ValueError(
                    "备注与文件夹无法保存，userdata.json 可能被占用或本身是目录。") from exc

    def note(self, mod_id, text):
        mod_id = str(mod_id)
        text = (text or "").strip()
        with self._lock:
            if text:
                self.notes[mod_id] = text[:2000]
            else:
                self.notes.pop(mod_id, None)
            self._save()
            return self.notes.get(mod_id, "")

    def folder_create(self, name):
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("文件夹名称不能为空。")
        folder = {"id": str(uuid.uuid4()), "name": name, "created": int(time.time() * 1000)}
        with self._lock:
            self.folders.append(folder)
            self._save()
            return folder

    def folder_rename(self, folder_id, name):
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("文件夹名称不能为空。")
        with self._lock:
            for folder in self.folders:
                if folder["id"] == folder_id:
                    folder["name"] = name
                    self._save()
                    return folder
        raise ValueError("文件夹不存在。")

    def folder_delete(self, folder_id):
        with self._lock:
            self.folders = [folder for folder in self.folders if folder["id"] != folder_id]
            self.assignments = {key: value for key, value in self.assignments.items() if value != folder_id}
            self._save()
            return True

    def folder_assign(self, mod_id, folder_id):
        mod_id = str(mod_id)
        with self._lock:
            if folder_id:
                if not any(folder["id"] == folder_id for folder in self.folders):
                    raise ValueError("文件夹不存在。")
                self.assignments[mod_id] = folder_id
            else:
                self.assignments.pop(mod_id, None)
            self._save()
            return self.assignments.get(mod_id)

    def folder_reorder(self, ids):
        """Persist the user's folder order. Unknown ids are ignored."""
        wanted = [str(item) for item in (ids or []) if str(item).strip()]
        with self._lock:
            by_id = {folder["id"]: folder for folder in self.folders}
            ordered, seen = [], set()
            for fid in wanted:
                folder = by_id.get(fid)
                if folder is not None and fid not in seen:
                    ordered.append(folder)
                    seen.add(fid)
            ordered += [folder for folder in self.folders if folder["id"] not in seen]
            self.folders = ordered
            self._save()
            return [dict(item) for item in self.folders]

    def snapshot(self):
        with self._lock:
            return {
                "notes": dict(self.notes),
                "folders": [dict(item) for item in self.folders],
                "assignments": dict(self.assignments),
            }
