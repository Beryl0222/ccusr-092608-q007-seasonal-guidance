"""JSONL 事件存储：仅做追加与整读，状态由上层重放得到。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Mapping


class EventStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, events: Iterable[Mapping[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        events: list[dict] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events
