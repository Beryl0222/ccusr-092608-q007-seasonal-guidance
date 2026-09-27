"""可控时钟：预警生效、过期与纠正期限都以注入的时钟为准。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("时间必须携带时区")
    return moment


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    """真实时钟，统一使用 UTC。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """演练与测试用时钟，只能显式推进，保证期限处理可复现。"""

    def __init__(self, start: datetime):
        self._current = require_aware(start)

    def now(self) -> datetime:
        return self._current

    def set(self, moment: datetime) -> None:
        self._current = require_aware(moment)

    def advance(self, **kwargs: float) -> datetime:
        self._current = self._current + timedelta(**kwargs)
        return self._current
