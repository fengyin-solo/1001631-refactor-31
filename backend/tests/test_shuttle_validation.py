import unittest
from unittest.mock import patch

from app.services.shuttle import (
    DISPATCH_ACTION,
    LEGACY_ENTRY_IDS,
    ShuttleService,
    validate_shuttle_entry,
)


def make_values(**overrides):
    values = {
        "任务编号": "SHUT-TEST",
        "关联航班": "CA1234",
        "车辆编号": "CAR-TEST",
        "乘客人数": 10,
        "出发时刻": "2026-09-29T12:00",
        "到达时刻": "2026-09-29T12:30",
    }
    values.update(overrides)
    return values


def make_entry(entry_id=10, **overrides):
    entry = {"id": entry_id, "status": "待发车", **make_values(**overrides)}
    return entry


class ShuttleValidationTest(unittest.TestCase):
    def setUp(self):
        self.service = ShuttleService()

    def test_time_passenger_and_vehicle_conflict_share_one_rule(self):
        self.assertIsNone(validate_shuttle_entry(make_entry()))
        self.assertEqual(
            validate_shuttle_entry(make_entry(出发时刻="2026-09-29T12:30")),
            "出发时刻必须早于到达时刻",
        )
        self.assertEqual(
            validate_shuttle_entry(make_entry(乘客人数=0)),
            "乘客人数必须为正整数",
        )

        other = make_entry(
            entry_id=11,
            车辆编号="CAR-CONFLICT",
            出发时刻="2026-09-29T12:15",
            到达时刻="2026-09-29T12:45",
        )
        entry = make_entry(车辆编号="CAR-CONFLICT")
        self.assertEqual(
            validate_shuttle_entry(entry, [other]),
            "同一车辆不能同时挂在两条摆渡任务上",
        )

    def test_creation_rejection_does_not_store_input(self):
        with patch("app.services.shuttle.store") as store:
            rows = []
            store.rows.return_value = rows
            entry, errors = self.service.create_entry(
                make_values(出发时刻="2026-09-29T13:00")
            )

        self.assertIsNone(entry)
        self.assertEqual(errors, ["摆渡任务校验失败：出发时刻必须早于到达时刻"])
        self.assertEqual(rows, [])

    def test_historical_entries_keep_existing_result(self):
        entry = {
            "id": next(iter(LEGACY_ENTRY_IDS)),
            "status": "已送达",
            "乘客人数": "摆渡接送样例",
            "出发时刻": "摆渡接送样例",
            "到达时刻": "摆渡接送样例",
        }
        self.assertIsNone(validate_shuttle_entry(entry, []))

    def test_same_rule_rejects_reversed_times_in_each_entry(self):
        reversed_entry = make_entry(出发时刻="2026-09-29T12:30")
        self.assertEqual(
            validate_shuttle_entry(reversed_entry, [], strict=True),
            "出发时刻必须早于到达时刻",
        )

        with patch("app.services.shuttle.store") as store:
            store.find.return_value = reversed_entry
            store.rows.return_value = []
            result, message = self.service.run_action(10, DISPATCH_ACTION)
            self.assertIsNone(result)
            self.assertEqual(message, "出发时刻必须早于到达时刻")

    def test_list_and_dispatch_use_shared_rule(self):
        entry = make_entry()
        with patch("app.services.shuttle.store") as store:
            store.rows.return_value = [entry]
            items, total = self.service.list_entries()
            self.assertEqual(items, [entry])
            self.assertEqual(total, 1)

            store.find.return_value = entry
            store.rows.return_value = [entry]
            result, message = self.service.run_action(10, DISPATCH_ACTION)
            self.assertIsNotNone(result)
            self.assertEqual(message, "摆渡任务已安排发车")

    def test_dispatch_uses_same_validation_rule(self):
        entry = make_entry(乘客人数=-1)
        with patch("app.services.shuttle.store") as store:
            store.find.return_value = entry
            store.rows.return_value = []
            result, message = self.service.run_action(10, DISPATCH_ACTION)

        self.assertIsNone(result)
        self.assertEqual(message, "乘客人数必须为正整数")
        self.assertEqual(entry["status"], "待发车")


if __name__ == "__main__":
    unittest.main()
