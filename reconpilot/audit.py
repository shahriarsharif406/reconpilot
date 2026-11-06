"""Tamper-evident audit trail: every entry's hash covers the previous entry's hash."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading

GENESIS = "0" * 64


def _digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | None = None):
        self.entries: list[dict] = []
        self.path = path
        self._lock = threading.Lock()

    def append(self, actor: str, action: str, target: str, detail: dict | None = None) -> dict:
        with self._lock:
            prev = self.entries[-1]["hash"] if self.entries else GENESIS
            body = {"seq": len(self.entries) + 1,
                    "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "actor": actor, "action": action, "target": target,
                    "detail": detail or {}, "prev_hash": prev}
            entry = {**body, "hash": _digest(body)}
            self.entries.append(entry)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry) + "\n")
                except OSError:
                    pass  # audit stays in memory if the disk is read-only (e.g. some hosts)
            return entry

    def verify(self) -> dict:
        prev = GENESIS
        for e in self.entries:
            body = {k: v for k, v in e.items() if k != "hash"}
            if body["prev_hash"] != prev or _digest(body) != e["hash"]:
                return {"ok": False, "checked": len(self.entries), "broken_at": e["seq"]}
            prev = e["hash"]
        return {"ok": True, "checked": len(self.entries), "broken_at": None}

    def reset(self) -> None:
        with self._lock:
            self.entries.clear()
