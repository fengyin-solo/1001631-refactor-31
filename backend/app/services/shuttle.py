"""摆渡接送业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any

from app.store import store

MODULE = "shuttle"
REQUIRED_FIELDS = ["任务编号", "关联航班", "车辆编号"]
SHUTTLE_FIELDS = REQUIRED_FIELDS + ["乘客人数", "出发时刻", "到达时刻"]
STATUS_ORDER = ["待发车", "行驶中", "已送达", "已取消"]
ACTION_RULES = {"安排发车": "行驶中", "确认送达": "已送达", "取消任务": "已取消"}
NEGATIVE_ACTIONS = []
ACTIVE_STATUSES = {"待发车", "行驶中"}
TIME_MESSAGE = "出发时刻必须早于到达时刻"
PASSENGER_MESSAGE = "乘客人数必须为正整数"
VEHICLE_MESSAGE = "同一车辆不能同时挂在两条摆渡任务上"


def _parse_schedule(value: Any) -> datetime | time | date | None:
    if isinstance(value, (datetime, time, date)) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for parser in (datetime.fromisoformat, time.fromisoformat, date.fromisoformat):
        try:
            return parser(text)
        except ValueError:
            pass
    return None


def _is_positive_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[0-9]+", text) is None:
            return False
        return int(text) > 0
    return False


def _schedules_overlap(current: dict[str, Any], other: dict[str, Any]) -> bool:
    current_departure = _parse_schedule(current.get("出发时刻"))
    current_arrival = _parse_schedule(current.get("到达时刻"))
    other_departure = _parse_schedule(other.get("出发时刻"))
    other_arrival = _parse_schedule(other.get("到达时刻"))
    if None in (current_departure, current_arrival, other_departure, other_arrival):
        return True
    if type(current_departure) is not type(other_departure) or type(current_arrival) is not type(other_arrival):
        return True
    try:
        return current_departure < other_arrival and other_departure < current_arrival
    except TypeError:
        return True


def validate_shuttle_entry(
    values: dict[str, Any],
    *,
    exclude_id: int | None = None,
) -> list[str]:
    """登记、动作和派车共用的摆渡任务校验，返回固定顺序的错误说明。"""
    errors: list[str] = []

    departure = _parse_schedule(values.get("出发时刻"))
    arrival = _parse_schedule(values.get("到达时刻"))
    schedule_valid = (
        departure is not None
        and arrival is not None
        and type(departure) is type(arrival)
    )
    if schedule_valid:
        try:
            schedule_valid = departure < arrival  # type: ignore[operator]
        except TypeError:
            schedule_valid = False
    if not schedule_valid:
        errors.append(TIME_MESSAGE)

    if not _is_positive_integer(values.get("乘客人数")):
        errors.append(PASSENGER_MESSAGE)

    vehicle = str(values.get("车辆编号") or "").strip()
    if vehicle:
        for other in store.rows(MODULE):
            if exclude_id is not None and other.get("id") == exclude_id:
                continue
            if other.get("status") not in ACTIVE_STATUSES:
                continue
            if str(other.get("车辆编号") or "").strip() != vehicle:
                continue
            if _schedules_overlap(values, other):
                errors.append(VEHICLE_MESSAGE)
                break

    return errors


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
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, [f"缺少必填字段：{'、'.join(missing)}"]

        errors = validate_shuttle_entry(values)
        if errors:
            return None, errors

        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in SHUTTLE_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def dispatch_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"摆渡任务 {entry_id} 不存在或已归档"
        errors = validate_shuttle_entry(entry, exclude_id=entry_id)
        if errors:
            return None, "；".join(errors)
        return self._apply_action(entry, "安排发车")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"摆渡任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于摆渡接送可执行范围"
        if action == "安排发车":
            return self.dispatch_entry(entry_id)

        errors = validate_shuttle_entry(entry, exclude_id=entry_id)
        if errors:
            return None, "；".join(errors)
        return self._apply_action(entry, action)

    def _apply_action(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"摆渡任务已{action}"
