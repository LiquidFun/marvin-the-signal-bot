"""PollManager: which weeks get posted when, and that no failure mode can post
a week twice."""

import threading
from datetime import date, timedelta

import pytest

from modules.poll import PollManager, upcoming_mondays, week_key
from modules.signal_client import DaemonUnreachable, NoResponse, RpcError

from .conftest import GROUP

SAT = date(2026, 10, 24)  # KW43
MON = date(2026, 10, 26)  # KW44


def questions(client):
    return [question for _, question, _ in client.polls]


def state_lines(pm):
    return pm.state.path.read_text().splitlines()


# ── which weeks ──────────────────────────────────────────────────


class TestSchedule:
    def test_upcoming_mondays_skips_current_week(self):
        assert upcoming_mondays(SAT, 2) == [date(2026, 10, 26), date(2026, 11, 2)]
        assert upcoming_mondays(MON, 2) == [date(2026, 11, 2), date(2026, 11, 9)]
        assert upcoming_mondays(date(2026, 11, 1), 2) == [date(2026, 11, 2), date(2026, 11, 9)]

    def test_first_run_posts_next_two_weeks(self, pm, client):
        report = pm.run(SAT)
        assert questions(client) == ["KW44", "KW45"]
        assert report.ok
        assert report.posted == ["KW44 (26.10.–01.11.)", "KW45 (02.11.–08.11.)"]

    def test_following_monday_posts_only_the_new_week(self, pm, client):
        pm.run(SAT)
        pm.run(MON)
        assert questions(client) == ["KW44", "KW45", "KW46"]

    def test_daily_runs_post_each_week_exactly_once_on_mondays(self, pm, client):
        pm.run(SAT)
        posted_on = {}
        for offset in range(1, 36):
            today = SAT + timedelta(days=offset)
            before = len(client.polls)
            pm.run(today)
            for _, question, _ in client.polls[before:]:
                posted_on[question] = today

        assert questions(client) == [f"KW{week}" for week in range(44, 51)]
        assert all(day.weekday() == 0 for day in posted_on.values())

    def test_rerun_same_day_is_a_noop(self, pm, client):
        pm.run(MON)
        calls = len(client.calls)
        report = pm.run(MON)
        assert report.ok and not report.posted
        assert len(client.calls) == calls  # not even the membership check

    def test_restart_with_fresh_manager_does_not_repost(self, make_pm, client):
        make_pm().run(MON)
        make_pm().run(MON)
        assert questions(client) == ["KW45", "KW46"]

    def test_missed_mondays_are_caught_up(self, pm, client):
        pm.run(SAT)
        pm.run(MON + timedelta(weeks=2, days=3))  # Thursday of KW46
        # KW46 has started by now, so a poll for it is pointless
        assert questions(client) == ["KW44", "KW45", "KW47", "KW48"]

    def test_year_boundary(self, pm, client):
        pm.run(date(2026, 12, 26))
        assert [week_key(m) for m in upcoming_mondays(date(2026, 12, 26), 2)] == ["2026-W53", "2027-W01"]
        assert questions(client) == ["KW53", "KW1"]
        assert client.polls[1][2][0] == "04.01.2027 Montag"


class TestContent:
    def test_poll_options(self, pm, client):
        pm.run(SAT)
        group_id, question, options = client.polls[0]
        assert group_id == GROUP
        assert question == "KW44"
        assert options == [
            "26.10.2026 Montag", "27.10.2026 Dienstag", "28.10.2026 Mittwoch",
            "29.10.2026 Donnerstag", "30.10.2026 Freitag", "31.10.2026 Samstag",
            "01.11.2026 Sonntag", "Keine Zeit :(",
        ]

    def test_announcement_sent_once_after_the_polls(self, pm, client):
        pm.run(SAT)
        assert client.calls == ["list_groups", "send_poll", "send_poll", "send_message"]
        assert client.messages[0][0] == GROUP
        assert "Kickerligamatch" in client.messages[0][1]

    def test_no_announcement_when_nothing_was_posted(self, pm, client):
        client.fail["send_poll"] = [RpcError("sendPollCreate", {"message": "x"})] * 2
        pm.run(SAT)
        assert client.messages == []

    def test_announcement_can_be_disabled(self, make_pm, client):
        make_pm(announcement="").run(SAT)
        assert client.messages == []


# ── at-most-once ─────────────────────────────────────────────────


class TestUncertainSendsAreNeverRetried:
    @pytest.mark.parametrize("error", [
        RpcError("sendPollCreate", {"code": -3, "message": "Failed to send to 1 recipient"}),
        NoResponse("sendPollCreate: timed out"),
    ])
    def test_failed_send_is_reported_and_not_retried(self, pm, client, error):
        client.fail["send_poll"] = [error]

        report = pm.run(SAT)
        assert questions(client) == ["KW44", "KW45"]
        assert not report.ok
        assert report.posted == ["KW45 (02.11.–08.11.)"]
        assert any(p.startswith("KW44") and "error" in p for p in report.problems)

        # every later run keeps reporting it, but never sends it again
        for today in (SAT, MON, MON + timedelta(days=1)):
            report = pm.run(today)
            assert any(p.startswith("KW44") for p in report.problems) == (today < MON)
        assert questions(client) == ["KW44", "KW45", "KW46"]

    def test_crash_between_sending_and_outcome_is_not_retried(self, pm, client):
        pm.state.append("2026-W44", "sending")
        report = pm.run(SAT)
        assert questions(client) == ["KW45"]
        assert any(p.startswith("KW44") and "sending" in p for p in report.problems)

    def test_write_ahead_line_precedes_the_send(self, pm, client):
        seen = []
        original = client.send_poll

        def send_poll(*args):
            seen.append(state_lines(pm)[-1].split()[:2])
            return original(*args)

        client.send_poll = send_poll
        pm.run(SAT)
        assert seen == [["2026-W44", "sending"], ["2026-W45", "sending"]]

    def test_unreadable_state_is_fatal_not_empty(self, pm, client):
        pm.state.path.mkdir()
        with pytest.raises(OSError):
            pm.run(SAT)
        assert client.polls == []

    def test_concurrent_runs_post_each_week_once(self, pm, client):
        entered, release = threading.Event(), threading.Event()
        original = client.send_poll

        def slow_send_poll(*args):
            entered.set()
            release.wait(5)
            return original(*args)

        client.send_poll = slow_send_poll
        other = PollManager(client, {"group_id": GROUP, "poll": {"state_file": str(pm.state.path)}})

        first = threading.Thread(target=pm.run, args=(SAT,))
        first.start()
        entered.wait(5)
        second = threading.Thread(target=other.run, args=(SAT,))
        second.start()
        release.set()
        first.join(5)
        second.join(5)

        assert questions(client) == ["KW44", "KW45"]


class TestUnreachableDaemon:
    def test_retries_then_posts(self, pm, client, sleeps):
        client.fail["list_groups"] = [DaemonUnreachable("refused"), DaemonUnreachable("refused")]
        report = pm.run(SAT)
        assert report.ok
        assert questions(client) == ["KW44", "KW45"]
        assert len(sleeps) == 2

    def test_unreachable_mid_run_resumes_without_duplicates(self, pm, client):
        client.fail["send_poll"] = [None, DaemonUnreachable("refused")]
        report = pm.run(SAT)
        assert report.ok
        assert questions(client) == ["KW44", "KW45"]
        assert report.posted == ["KW44 (26.10.–01.11.)", "KW45 (02.11.–08.11.)"]
        assert [line.split()[1] for line in state_lines(pm)] == [
            "sending", "posted", "sending", "unsent", "sending", "posted",
        ]

    def test_gives_up_after_all_attempts_and_next_run_posts(self, make_pm, client, sleeps):
        pm = make_pm(connect_attempts=3)
        client.fail["list_groups"] = [DaemonUnreachable("refused")] * 3
        report = pm.run(SAT)
        assert client.polls == []
        assert len(sleeps) == 2
        assert any("nicht bereit" in p for p in report.problems)
        assert any(p.startswith("KW44") and "nicht gepostet" in p for p in report.problems)

        assert pm.run(SAT).ok
        assert questions(client) == ["KW44", "KW45"]


class TestPreflight:
    def test_not_a_member_posts_nothing(self, pm, client):
        client.groups[0]["isMember"] = False
        report = pm.run(SAT)
        assert client.polls == []
        assert any("kein Mitglied" in p for p in report.problems)

    def test_unknown_group_posts_nothing(self, pm, client):
        client.groups = [{"id": "other", "name": "Other", "isMember": True}]
        report = pm.run(SAT)
        assert client.polls == []
        assert any("kennt die Gruppe nicht" in p for p in report.problems)

    @pytest.mark.parametrize("error", [RpcError("listGroups", {"message": "boom"}), NoResponse("closed")])
    def test_list_groups_failure_is_retried(self, pm, client, sleeps, error):
        """Read-only, so safe to retry; giving up would lose the whole day"""
        client.fail["list_groups"] = [error]
        assert pm.run(SAT).ok
        assert questions(client) == ["KW44", "KW45"]
        assert len(sleeps) == 1

    def test_garbage_group_list_posts_nothing(self, make_pm, client):
        client.groups = None
        report = make_pm(connect_attempts=2).run(SAT)
        assert client.polls == []
        assert any("unlesbar" in p for p in report.problems)


class TestManualResolution:
    def test_deleting_the_lines_reposts(self, pm, client):
        client.fail["send_poll"] = [NoResponse("timeout")]
        pm.run(SAT)
        pm.state.path.write_text(
            "\n".join(line for line in state_lines(pm) if not line.startswith("2026-W44")) + "\n"
        )
        assert pm.run(SAT).ok
        assert questions(client) == ["KW44", "KW45", "KW44"]

    def test_appending_posted_resolves(self, pm, client):
        client.fail["send_poll"] = [NoResponse("timeout")]
        pm.run(SAT)
        with open(pm.state.path, "a") as f:
            f.write("2026-W44 posted manual\n")
        assert pm.run(SAT).ok
        assert questions(client) == ["KW44", "KW45"]


class TestReporting:
    def test_partial_delivery_is_noted_but_counts_as_posted(self, pm, client):
        client.poll_result = {"results": [{"type": "SUCCESS"}, {"type": "UNREGISTERED_FAILURE"}]}
        report = pm.run(SAT)
        assert report.ok
        assert report.notes[0] == "KW44 (26.10.–01.11.) nicht zugestellt an 1/2 (UNREGISTERED_FAILURE)"

    def test_failed_announcement_is_a_problem(self, pm, client):
        client.fail["send_message"] = [NoResponse("timeout")]
        report = pm.run(SAT)
        assert len(report.posted) == 2
        assert any("Ankündigung" in p for p in report.problems)

    def test_summary(self, pm, client):
        assert pm.summary(pm.run(SAT)) == "✅ Umfragen gepostet: KW44 (26.10.–01.11.), KW45 (02.11.–08.11.)"
        assert pm.summary(pm.run(SAT)) is None

        client.fail["send_poll"] = [NoResponse("timeout")]
        text = pm.summary(pm.run(MON))
        assert "⚠️" in text and "KW46" in text and str(pm.state.path) in text


class TestStateLog:
    def test_unparseable_line_is_fatal_not_skipped(self, pm, client):
        """A skipped line would make its week look unposted"""
        pm.state.path.write_text("# comment\n\n2026-W44 posted 2026-10-24T14:00:00+02:00\n2026-45 2026-01-01\n")
        with pytest.raises(ValueError, match="polls.log:4"):
            pm.run(SAT)
        assert client.polls == []

    def test_typo_in_status_is_fatal(self, pm):
        pm.state.path.write_text("2026-W44 psoted\n")
        with pytest.raises(ValueError):
            pm.state.load()

    def test_append_after_hand_edit_without_final_newline(self, pm):
        pm.state.path.write_text("2026-W44 posted manual")
        pm.state.append("2026-W45", "sending")
        state = pm.state.load()
        assert state["2026-W44"] == ("posted", "manual")
        assert state["2026-W45"][0] == "sending"

    def test_state_file_is_required(self, client):
        for poll_config in ({}, None, {"weeks_ahead": 2}):
            with pytest.raises(ValueError, match="state_file"):
                PollManager(client, {"group_id": GROUP, "poll": poll_config})


class TestDeliveryResults:
    def test_failed_send_that_reached_part_of_the_group_counts_as_posted(self, pm, client):
        response = {"timestamp": 1, "results": [{"type": "SUCCESS"}, {"type": "NETWORK_FAILURE"}]}
        client.fail["send_poll"] = [RpcError("sendPollCreate", {"code": -3, "message": "x", "data": {"response": response}})]
        report = pm.run(SAT)
        assert report.ok
        assert len(report.posted) == 2
        assert "1/2 (NETWORK_FAILURE)" in report.notes[0]

    def test_failed_send_without_any_success_is_an_error(self, pm, client):
        response = {"results": [{"type": "NETWORK_FAILURE"}]}
        client.fail["send_poll"] = [RpcError("sendPollCreate", {"message": "x", "data": {"response": response}})]
        report = pm.run(SAT)
        assert any(p.startswith("KW44") and "error" in p for p in report.problems)

    def test_success_that_reached_nobody_is_an_error(self, pm, client):
        client.poll_result = {"results": [{"type": "NETWORK_FAILURE"}, {"type": "UNREGISTERED_FAILURE"}]}
        report = pm.run(SAT)
        assert report.posted == []
        assert any("niemanden" in p for p in report.problems)
        pm.run(SAT)
        assert len(client.polls) == 2  # still not retried

    @pytest.mark.parametrize("result", [None, {}, {"results": None}, {"results": ["x", None]}, []])
    def test_odd_result_shapes_count_as_posted(self, pm, client, result):
        client.poll_result = result
        assert pm.run(SAT).ok
        assert [line.split()[1] for line in state_lines(pm)][-1] == "posted"


def test_hand_edited_lines_may_use_any_whitespace(pm):
    pm.state.path.write_text("2026-W44\tposted\n2026-W45  posted   by hand  \n")
    assert pm.state.load() == {"2026-W44": ("posted", ""), "2026-W45": ("posted", "by hand")}
