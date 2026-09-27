"""可控时钟：预警生效、过期与纠正期限一律经此取时，不直接读系统时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时间。"""


class SystemClock:
    """生产环境时钟，固定使用东八区（广西所在时区）。"""

    def __init__(self, tz=timezone(timedelta(hours=8))) -> None:
        self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock:
    """测试/重放用时钟：时间只能显式推进，绝不随系统时钟漂移。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("固定时钟的初始时间必须携带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("设定时间必须携带时区")
        self._now = value
