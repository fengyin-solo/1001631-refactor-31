"""摆渡接送业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any

from app.seed import SEED_ROWS
from app.store import store

MODULE = "shuttle"
LEGACY_ENTRY_IDS = {int(row["id"]) for row in SEED_ROWS[MODULE]}
REQUIRED_FIELDS = ["任务编号", "关联航班", "车辆编号", "乘客人数", "出发时刻", "到达时刻"]
MISSING_REQUIRED_FIELDS = ["任务编号", "关联航班", "车辆编号"]
VALIDATION_ERROR_PREFIX = "摆渡任务校验失败："
STATUS_ORDER = ["待发车", "行驶中", "已送达", "已取消"]
ACTIVE_STATUSES = {"待发车", "行驶中"}
ACTION_RULES = {"安排发车": "行驶中", "确认送达": "已送达", "取消任务": "已取消"}
NEGATIVE_ACTIONS = []
DISPATCH_ACTION = "安排发车"

_PASSENGER_PATTERN = re.compile(r"^\d+$")


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _passenger_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = _text(value)
    if not _PASSENGER_PATTERN.fullmatch(text):
        return None
    count = int(text)
    return count if count > 0 else None


def _schedule_time(value: Any) -> tuple[str, Any] | None:
    if isinstance(value, datetime):
        kind = "datetime_aware" if value.tzinfo is not None else "datetime"
        return kind, value
    if isinstance(value, time):
        return "time", value
    if isinstance(value, date):
        return "datetime", datetime.combine(value, time.min)

    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        kind = "datetime_aware" if parsed.tzinfo is not None else "datetime"
        return kind, parsed
    except ValueError:
        try:
            return "time", time.fromisoformat(text)
        except ValueError:
            return None


def _entry_id(entry: dict[str, Any]) -> int | None:
    try:
        return int(entry.get("id"))
    except (TypeError, ValueError):
        return None


def validate_shuttle_entry(
    entry: dict[str, Any],
    rows: list[dict[str, Any]] | None = None,
    *,
    exclude_id: int | None = None,
    strict: bool = False,
) -> str | None:
    """校验同一条摆渡任务，登记、动作和派车流程共用这一份判断。"""
    entry_id = _entry_id(entry)
    if not strict and entry_id in LEGACY_ENTRY_IDS:
        return None

    departure = _schedule_time(entry.get("出发时刻"))
    arrival = _schedule_time(entry.get("到达时刻"))
    if (
        departure is None
        or arrival is None
        or departure[0] != arrival[0]
        or departure[1] >= arrival[1]
    ):
        return "出发时刻必须早于到达时刻"

    if _passenger_count(entry.get("乘客人数")) is None:
        return "乘客人数必须为正整数"

    vehicle = _text(entry.get("车辆编号"))
    if rows is not None and vehicle:
        for other in rows:
            if exclude_id is not None and _entry_id(other) == exclude_id:
                continue
            if other.get("status") not in ACTIVE_STATUSES:
                continue
            if _text(other.get("车辆编号")) != vehicle:
                continue

            other_departure = _schedule_time(other.get("出发时刻"))
            other_arrival = _schedule_time(other.get("到达时刻"))
            if (
                other_departure is None
                or other_arrival is None
                or other_departure[0] != departure[0]
                or other_arrival[0] != arrival[0]
            ):
                continue
            if (
                departure[1] < other_arrival[1]
                and arrival[1] > other_departure[1]
            ):
                return "同一车辆不能同时挂在两条摆渡任务上"

    return None


class ShuttleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        list_rows = list(rows)
        for row in list_rows:
            validate_shuttle_entry(row, list_rows, exclude_id=_entry_id(row))
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def validate_entry(self, entry: dict[str, Any], exclude_id: int | None = None) -> str | None:
        entry_id = _entry_id(entry)
        return validate_shuttle_entry(
            entry,
            store.rows(MODULE),
            exclude_id=exclude_id if exclude_id is not None else entry.get("id"),
            strict=entry_id not in LEGACY_ENTRY_IDS,
        )

    def dispatch_entry(self, entry: dict[str, Any]) -> str | None:
        entry_id = _entry_id(entry)
        return self.validate_entry(entry, exclude_id=entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in MISSING_REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing

        rows = store.rows(MODULE)
        candidate = {field: values.get(field) for field in REQUIRED_FIELDS}
        error = validate_shuttle_entry(candidate, rows, strict=True)
        if error:
            return None, [VALIDATION_ERROR_PREFIX + error]

        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(candidate)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"摆渡任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于摆渡接送可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == DISPATCH_ACTION:
            error = self.dispatch_entry(entry)
            if error:
                return None, error
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"摆渡任务已{action}"
