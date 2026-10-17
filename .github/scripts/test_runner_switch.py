"""test_runner_switch は、runner_switch の判定と数え方を、API を呼ばずに確かめる。"""

from __future__ import annotations

import datetime as dt
import unittest

import runner_switch as rs

UTC = dt.timezone.utc


def config(**overrides) -> rs.Config:
    """config は、テスト用の設定を作る。"""
    values = {
        "token": "",
        "org": "o",
        "state_repo": "o/state",
        "threshold_minutes": 1800,
        "free_minutes": 3000,
        "fallback_runner": "ubuntu-24.04",
        "blacksmith_label_prefix": "blacksmith-",
        "revert_not_before_hours": 12,
        "dry_run": True,
        "repos": ["o/a", "o/b"],
        "issue_repo": "o/a",
    }
    values.update(overrides)
    return rs.Config(**values)


class FakeGitHub:
    """FakeGitHub は、パスごとに決めた応答を返す GitHub の代わり。"""

    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, path: str) -> dict:
        """get は、パスの先頭が一致する応答を返す。"""
        self.calls.append(path)
        for prefix, body in self.responses.items():
            if path.startswith(prefix):
                return body
        raise AssertionError(f"想定していない呼び出し: {path}")


def job(labels: list[str], started: str | None, completed: str | None) -> dict:
    """job は、jobs の API が返すジョブの形を作る。"""
    return {"labels": labels, "started_at": started, "completed_at": completed}


class DecideTest(unittest.TestCase):
    """DecideTest は、RUNNER のあるべき値の決め方を確かめる。"""

    start = dt.datetime(2026, 11, 1, tzinfo=UTC)

    def test_switches_to_github_at_threshold(self) -> None:
        now = self.start + dt.timedelta(days=10)
        self.assertEqual(rs.decide(config(), 1800, now, self.start, None), "ubuntu-24.04")

    def test_stays_on_blacksmith_below_threshold(self) -> None:
        now = self.start + dt.timedelta(days=10)
        self.assertIsNone(rs.decide(config(), 1799, now, self.start, None))

    def test_waits_for_hours_after_month_start(self) -> None:
        now = self.start + dt.timedelta(hours=11, minutes=59)
        self.assertEqual(rs.decide(config(), 0, now, self.start, "ubuntu-24.04"), "ubuntu-24.04")

    def test_reverts_after_waiting(self) -> None:
        now = self.start + dt.timedelta(hours=12)
        self.assertIsNone(rs.decide(config(), 0, now, self.start, "ubuntu-24.04"))

    def test_reverts_manual_switch_mid_month(self) -> None:
        now = self.start + dt.timedelta(days=10)
        self.assertIsNone(rs.decide(config(), 100, now, self.start, "ubuntu-latest"))


class StateTest(unittest.TestCase):
    """StateTest は、月が替わったときの state の作り直しを確かめる。"""

    def test_period_start_is_first_day_utc(self) -> None:
        now = dt.datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
        self.assertEqual(rs.period_start(now), dt.datetime(2026, 10, 1, tzinfo=UTC))

    def test_resets_on_new_month_and_keeps_issue(self) -> None:
        start = dt.datetime(2026, 11, 1, tzinfo=UTC)
        old = {"period": "2026-10-01T00:00:00Z", "settled_until": "2026-10-29T00:00:00Z", "settled_minutes": 2500, "issue": 3}
        original = rs.get_variable
        rs.get_variable = lambda gh, repo, name: __import__("json").dumps(old)
        try:
            state = rs.load_state(None, config(), start)
        finally:
            rs.get_variable = original
        self.assertEqual(state, {"period": "2026-11-01T00:00:00Z", "settled_until": "2026-10-31T00:00:00Z", "settled_minutes": 0, "issue": 3})


class CountTest(unittest.TestCase):
    """CountTest は、ジョブの分数の数え方を確かめる。"""

    since = dt.datetime(2026, 11, 1, tzinfo=UTC)
    now = dt.datetime(2026, 11, 1, 1, 0, tzinfo=UTC)

    def test_counts_only_blacksmith_jobs_rounded_up(self) -> None:
        gh = FakeGitHub({"/repos/o/r/actions/runs/1/jobs": {"jobs": [
            job(["blacksmith-2vcpu-ubuntu-2404"], "2026-11-01T00:00:00Z", "2026-11-01T00:01:01Z"),
            job(["ubuntu-24.04"], "2026-11-01T00:00:00Z", "2026-11-01T00:10:00Z"),
            job(["blacksmith-2vcpu-ubuntu-2404"], None, None),
        ]}})
        self.assertEqual(rs.blacksmith_minutes(gh, config(), "o/r", 1, self.since, self.now), 2)

    def test_skips_jobs_before_period_and_counts_running_jobs_until_now(self) -> None:
        gh = FakeGitHub({"/repos/o/r/actions/runs/1/jobs": {"jobs": [
            job(["blacksmith-2vcpu-ubuntu-2404"], "2026-10-31T23:59:00Z", "2026-11-01T00:05:00Z"),
            job(["blacksmith-2vcpu-ubuntu-2404"], "2026-11-01T00:30:00Z", None),
        ]}})
        self.assertEqual(rs.blacksmith_minutes(gh, config(), "o/r", 1, self.since, self.now), 30)

    def test_splits_windows_over_search_cap(self) -> None:
        start = dt.datetime(2026, 11, 1, tzinfo=UTC)
        end = dt.datetime(2026, 11, 2, tzinfo=UTC)

        class Splitting(FakeGitHub):
            def get(self, path: str) -> dict:
                self.calls.append(path)
                # 1 日まるごとの区間だけを 1,000 件超えとして返し、割った後の区間は 1 件ずつ返す。
                if "2026-11-01T00%3A00%3A00Z..2026-11-02T00%3A00%3A00Z" in path:
                    return {"total_count": 1001, "workflow_runs": []}
                return {"total_count": 1, "workflow_runs": [{"id": len(self.calls)}]}

        gh = Splitting({})
        runs = rs.list_runs(gh, "o/r", start, end)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(gh.calls), 3)


class RecordingGitHub:
    """RecordingGitHub は、request の呼び出しを記録し、Issue を作ったら番号 7 を返す GitHub の代わり。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        """request は、呼び出しを記録して決まった応答を返す。"""
        self.calls.append((method, path))
        return 201, {"number": 7}


class NotifyTest(unittest.TestCase):
    """NotifyTest は、Issue を立てる先と閉じる先を確かめる。"""

    now = dt.datetime(2026, 11, 10, tzinfo=UTC)

    def test_opens_issue_in_issue_repo(self) -> None:
        gh = RecordingGitHub()
        state: dict = {"issue": None}
        rs.notify(gh, config(), state, "ubuntu-24.04", 1800, self.now)
        self.assertEqual(gh.calls, [("POST", "/repos/o/a/issues")])
        self.assertEqual(state["issue"], {"repo": "o/a", "number": 7})

    def test_closes_issue_where_it_was_opened(self) -> None:
        gh = RecordingGitHub()
        state = {"issue": {"repo": "o/old", "number": 3}}
        rs.notify(gh, config(), state, None, 0, self.now)
        self.assertEqual(gh.calls, [("POST", "/repos/o/old/issues/3/comments"), ("PATCH", "/repos/o/old/issues/3")])
        self.assertIsNone(state["issue"])

    def test_treats_legacy_issue_number_as_state_repo(self) -> None:
        gh = RecordingGitHub()
        state = {"issue": 5}
        rs.notify(gh, config(), state, None, 0, self.now)
        self.assertEqual(gh.calls[0], ("POST", "/repos/o/state/issues/5/comments"))


if __name__ == "__main__":
    unittest.main()
