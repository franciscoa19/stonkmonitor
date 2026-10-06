"""Daily-report stages retry independently, and the UW quota pause can clear.

All local: generation, export, git publish and Pushover are faked, and report
files go to a temporary directory.
"""
from datetime import date
from types import SimpleNamespace as NS

import pytest

DAY = "2026-10-06"


@pytest.fixture
def report(tmp_path, monkeypatch):
    """main.generate_daily_report wired to fakes; returns (main, calls, reports_dir)."""
    import daily_report
    import main
    import market_time
    (tmp_path / "backend").mkdir()
    monkeypatch.setattr(main, "__file__", str(tmp_path / "backend" / "main.py"))
    calls = NS(build=0, export=0, push=[], notify=[], export_error=None, push_ok=True)

    async def build(db, trader, thresholds=None, settings=None):
        calls.build += 1
        return {"generated": f"{DAY}T12:00:05+00:00", "proposals": [],
                "account": {"equity": 50000.0, "total_pnl_pct": 0.0, "open_positions": 0, "error": None},
                "metrics": {"closed_trades": 0, "win_rate": 0.0}}

    async def export(db, reports_dir):
        calls.export += 1
        if calls.export_error:
            raise calls.export_error
        return {}

    async def send_alert(title, message):
        calls.notify.append(title)
        return True

    monkeypatch.setattr(daily_report, "build_report_data", build)
    monkeypatch.setattr(daily_report, "export_history", export)
    monkeypatch.setattr(daily_report, "render_html", lambda data: "<html>report</html>")
    monkeypatch.setattr(main, "_git_push_eval_data", lambda day: calls.push.append(day) or calls.push_ok)
    monkeypatch.setattr(main, "pushover", NS(enabled=True, send_alert=send_alert))
    monkeypatch.setattr(main, "settings", NS(report_git_push=True, auto_trade_score_threshold=9.0,
                                             auto_trade_pattern_threshold=9.5))
    monkeypatch.setattr(market_time, "et_today", lambda *a: date.fromisoformat(DAY))
    return main, calls, tmp_path / "backend" / "reports"


async def test_export_failure_leaves_the_day_unfinished_and_is_retried(report):
    """The HTML is written before the export. As the completion marker it made
    the scheduler skip the rest of the day after a failed export."""
    main, calls, reports = report
    calls.export_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError):
        await main.generate_daily_report(scheduled=True)
    assert (reports / f"daily_{DAY}.html").exists()            # the old "done" marker is there…
    assert not main._report_complete(reports, DAY)             # …but the day is not done
    assert calls.push == [] and calls.notify == []

    calls.export_error = None
    await main.generate_daily_report(scheduled=True)           # next scheduler tick
    assert main._report_complete(reports, DAY)
    assert (calls.build, calls.export, len(calls.push), len(calls.notify)) == (2, 2, 1, 1)


async def test_failed_publish_is_retried_without_rebuilding_or_renotifying(report):
    main, calls, reports = report
    calls.push_ok = False
    await main.generate_daily_report(scheduled=True)
    assert not main._report_complete(reports, DAY)
    assert (calls.build, len(calls.push), len(calls.notify)) == (1, 1, 1)

    calls.push_ok = True
    result = await main.generate_daily_report(scheduled=True)  # next scheduler tick
    assert result == {"resumed": True, "day": DAY, "complete": True}
    assert main._report_complete(reports, DAY)
    # Only the publish stage ran again: no rebuild, no re-export, no second notification.
    assert (calls.build, calls.export, len(calls.push), len(calls.notify)) == (1, 1, 2, 1)


async def test_a_manual_run_rebuilds_and_notifies_even_after_completion(report):
    main, calls, reports = report
    await main.generate_daily_report(scheduled=True)
    assert main._report_complete(reports, DAY)
    data = await main.generate_daily_report()                  # the /api/report/generate path
    assert data["generated"].startswith(DAY)
    assert (calls.build, len(calls.notify)) == (2, 2) and main._report_complete(reports, DAY)


async def test_publish_stage_is_skipped_when_git_push_is_off(report, monkeypatch):
    main, calls, reports = report
    monkeypatch.setattr(main, "settings", NS(report_git_push=False, auto_trade_score_threshold=9.0,
                                             auto_trade_pattern_threshold=9.5))
    await main.generate_daily_report(scheduled=True)
    assert calls.push == [] and main._report_complete(reports, DAY)


@pytest.mark.parametrize("failure", ["transport", "http", "rejected", "invalid_json"])
async def test_real_notifier_failure_retries_only_notification(report, monkeypatch, failure):
    """Exercise the real adapter; a mock that raises misses swallowed failures."""
    from notifications import pushover as module
    main, calls, reports = report
    requests, closed, timeouts = [], [], []
    failing = True

    class Response:
        status = 200

        async def __aenter__(self):
            self.status = 503 if failing and failure == "http" else 200
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def json(self):
            if failing and failure == "invalid_json":
                raise ValueError("bad JSON")
            return {"status": 0 if failing and failure == "rejected" else 1}

    class Session:
        def __init__(self, *, timeout):
            timeouts.append(timeout.total)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, data):
            requests.append(data["title"])
            if failing and failure == "transport":
                raise OSError("offline")
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(main, "pushover", module.PushoverNotifier("local-fake-token", "local-fake-user"))
    await main.generate_daily_report(scheduled=True)
    state = main._load_report_state(reports, DAY)
    assert state["exported"] and state["published"]
    assert not state["notified"] and not state["complete"]

    failing = False
    assert await main.generate_daily_report(scheduled=True) == {"resumed": True, "day": DAY, "complete": True}
    assert main._report_complete(reports, DAY)
    # The scheduler stops retrying once the durable completion check passes.
    assert (calls.build, calls.export, len(calls.push), len(requests)) == (1, 1, 1, 2)
    assert timeouts == [10, 10]
    assert len(closed) == (1 if failure == "transport" else 2)


async def test_disabled_pushover_completes_without_attempting_a_send(report, monkeypatch):
    main, calls, reports = report
    monkeypatch.setattr(main.pushover, "enabled", False)
    await main.generate_daily_report(scheduled=True)
    assert main._report_complete(reports, DAY) and calls.notify == []


async def test_notification_exception_clears_previous_manual_success(report, monkeypatch):
    main, calls, reports = report
    await main.generate_daily_report(scheduled=True)

    async def failed(*args):
        raise OSError("offline")

    monkeypatch.setattr(main.pushover, "send_alert", failed)
    state = main._load_report_state(reports, DAY)
    await main._finish_report(reports, DAY, state, always_notify=True)
    assert not state["notified"] and not state["complete"]


def test_a_report_from_before_stage_tracking_counts_as_complete(tmp_path):
    """No state file + an HTML: written by the old code, where the HTML was the
    marker. Without this rule the first deploy would regenerate and re-push
    today's report."""
    import main
    assert not main._report_complete(tmp_path, DAY)
    (tmp_path / f"daily_{DAY}.html").write_text("<html></html>")
    assert main._report_complete(tmp_path, DAY)
    main._save_report_state(tmp_path, DAY, {"complete": False})
    assert not main._report_complete(tmp_path, DAY)            # a state file always wins
    assert not list(tmp_path.glob("*.tmp"))                    # atomic write left nothing behind


def test_report_push_reports_failure(tmp_path, monkeypatch):
    import main
    (tmp_path / "backend").mkdir()
    monkeypatch.setattr(main, "__file__", str(tmp_path / "backend" / "main.py"))
    assert main._git_push_eval_data(DAY) is False              # no report files, no repo


def test_uw_pause_lets_one_refresh_probe_through_and_then_clears():
    """Usage only updates from response headers. Paused, the client sent nothing,
    so it never learned the quota had reset and stayed blocked until restart."""
    from feeds.uw_budget import UWBudget
    t0 = 1_000_000.0
    budget = UWBudget(daily_count=14_900, daily_limit=15_000, last_update_ts=t0)
    assert budget.should_pause()
    assert not budget.probe_due(t0 + 60) and not budget.allow_request(t0 + 60)   # fresh reading

    two_days = t0 + 2 * 86_400
    assert budget.should_pause()                       # the cached count alone never clears
    assert budget.probe_due(two_days) and budget.allow_request(two_days)         # one probe
    assert not budget.probe_due(two_days + 5) and not budget.allow_request(two_days + 5)
    assert budget.allow_request(two_days + 1_800)      # and one more per interval

    # The probe's response shows the reset quota: the pause lifts for everyone.
    budget.update_from_headers("/api/x", {"x-uw-daily-req-count": "12", "x-uw-token-req-limit": "15000"})
    assert not budget.should_pause() and not budget.probe_due() and budget.allow_request()


def test_uw_budget_below_the_pause_level_never_blocks():
    from feeds.uw_budget import UWBudget
    budget = UWBudget(daily_count=100, daily_limit=15_000)
    assert budget.allow_request(0.0) and not budget.probe_due(10 ** 9) and budget.last_probe_ts == 0.0
