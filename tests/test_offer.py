"""Vote tallying and the weekly offer for the captains."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from modules.offer import OfferManager, offer_text
from modules.signal_client import DaemonUnreachable, RpcError
from modules.votes import VoteLog, vote_from_envelope

SAT = date(2026, 10, 24)  # KW43; polls for KW44 + KW45 go out
MON = date(2026, 10, 26)  # KW44; the offer for KW45 is due
TUE, THU = 1, 3           # option indexes


def envelope(voter, poll, options, count=1):
    return {
        "sourceUuid": voter,
        "dataMessage": {
            "groupInfo": {"groupId": "kicker-group"},
            "pollVote": {"authorNumber": "+49bot", "targetSentTimestamp": poll,
                         "optionIndexes": options, "voteCount": count},
        },
    }


@pytest.fixture
def config(config, tmp_path):
    config["poll"]["votes_file"] = str(tmp_path / "votes.jsonl")
    config["offer"] = {"state_file": str(tmp_path / "offers.log"), "min_votes": 5}
    return config


@pytest.fixture
def votes(config):
    return VoteLog(Path(config["poll"]["votes_file"]))


@pytest.fixture
def offers(pm, client, config):
    pm.run(SAT)  # posts KW44 + KW45
    client.messages.clear()
    return OfferManager(client, config, pm)


def kw45_poll(pm):
    return pm.sent_timestamp(date(2026, 11, 2))


def vote(votes, pm, voters, options):
    for voter in voters:
        votes.append(vote_from_envelope(envelope(voter, kw45_poll(pm), options)))


def sent(client):
    return [text for group, text in client.messages if group == "admin-group"]


# ── votes ────────────────────────────────────────────────────────


class TestVotes:
    def test_envelope_without_vote(self):
        assert vote_from_envelope({"dataMessage": {"message": "hi"}}) is None
        assert vote_from_envelope({"sourceUuid": "a", "dataMessage": {"pollVote": {"optionIndexes": "x"}}}) is None

    def test_counts_each_persons_latest_vote(self, votes):
        votes.append(vote_from_envelope(envelope("a", 7, [0, 1], count=1)))
        votes.append(vote_from_envelope(envelope("b", 7, [1], count=1)))
        votes.append(vote_from_envelope(envelope("a", 7, [2], count=2)))   # a changed their mind
        votes.append(vote_from_envelope(envelope("b", 7, [], count=2)))    # b withdrew
        votes.append(vote_from_envelope(envelope("c", 8, [1], count=1)))   # another poll
        assert votes.tally(7) == {2: 1}
        assert votes.tally(8) == {1: 1}

    def test_late_arriving_older_vote_does_not_win(self, votes):
        votes.append(vote_from_envelope(envelope("a", 7, [2], count=2)))
        votes.append(vote_from_envelope(envelope("a", 7, [0], count=1)))
        assert votes.tally(7) == {2: 1}

    def test_unreadable_line_is_skipped(self, votes):
        votes.append(vote_from_envelope(envelope("a", 7, [1])))
        with open(votes.path, "a") as f:
            f.write('{"poll": 7, "vot')
        assert votes.tally(7) == {1: 1}


# ── offer ────────────────────────────────────────────────────────


class TestOffer:
    def test_poll_timestamps_are_recorded(self, offers, pm):
        assert pm.sent_timestamp(date(2026, 10, 26)) == 1001
        assert kw45_poll(pm) == 1002

    def test_lists_every_day_with_enough_votes(self, offers, client, votes, pm):
        vote(votes, pm, "abcdef", [TUE, THU])
        vote(votes, pm, "ghij", [THU])
        report = offers.run(MON)
        assert report.ok and report.posted == ["KW45 (02.11.–08.11.)"]
        assert sent(client) == [
            'Moin! Team "Da war offen" hat am 3.11 (Dienstag) und 5.11 (Donnerstag) '
            'Zeit für ein Match. Wer hat interesse?'
        ]

    def test_single_day(self, offers, client, votes, pm):
        vote(votes, pm, "abcde", [THU])
        offers.run(MON)
        assert sent(client) == ['Moin! Team "Da war offen" hat am 5.11 (Donnerstag) Zeit für ein Match. Wer hat interesse?']

    def test_four_votes_are_not_enough_and_nothing_is_sent(self, offers, client, votes, pm):
        vote(votes, pm, "abcd", [TUE, THU])
        report = offers.run(MON)
        assert report.ok and sent(client) == []
        assert "skipped" in offers.state.path.read_text()

    def test_only_once_per_week(self, offers, client, votes, pm):
        vote(votes, pm, "abcde", [TUE])
        offers.run(MON)
        vote(votes, pm, "fghij", [THU])  # more votes later in the week change nothing
        for day in range(7):
            offers.run(MON + timedelta(days=day))
        assert len(sent(client)) == 1

    def test_a_skipped_week_stays_skipped(self, offers, client, votes, pm):
        vote(votes, pm, "ab", [TUE])
        offers.run(MON)
        vote(votes, pm, "cdefg", [TUE])
        offers.run(MON + timedelta(days=1))
        assert sent(client) == []

    def test_due_from_the_monday_a_week_before(self, offers, client, votes, pm):
        vote(votes, pm, "abcde", [TUE])
        offers.run(SAT)  # still KW43: next week is KW44, whose poll has no votes
        assert sent(client) == []
        offers.run(MON)
        assert "3.11 (Dienstag)" in sent(client)[0]

    def test_missing_poll_is_reported_once(self, pm, client, config):
        offers = OfferManager(client, config, pm)  # no polls posted
        report = offers.run(MON)
        assert any("keine Umfrage" in p for p in report.problems)
        assert offers.run(MON).ok
        assert sent(client) == []

    def test_unreachable_daemon_retries_next_run(self, offers, client, votes, pm):
        vote(votes, pm, "abcde", [TUE])
        client.fail["send_message"] = [DaemonUnreachable("refused")]
        assert not offers.run(MON).ok
        assert offers.run(MON + timedelta(days=1)).ok
        assert len(sent(client)) == 1

    def test_uncertain_send_is_not_retried(self, offers, client, votes, pm):
        vote(votes, pm, "abcde", [TUE])
        client.fail["send_message"] = [RpcError("send", {"message": "boom"})]
        offers.run(MON)
        report = offers.run(MON + timedelta(days=1))
        assert any("error" in p for p in report.problems)
        assert sent(client) == []

    def test_requires_admin_group(self, pm, client, config):
        del config["admin_group_id"]
        with pytest.raises(ValueError, match="admin_group_id"):
            OfferManager(client, config, pm)

    def test_three_days_and_month_boundary(self):
        days = [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)]
        assert offer_text(days, "{days}") == "30.10 (Freitag), 31.10 (Samstag) und 1.11 (Sonntag)"

    def test_custom_template(self, pm, client, config):
        config["offer"]["template"] = "Wir können am {days}!"
        assert OfferManager(client, config, pm).template == "Wir können am {days}!"
        config["offer"]["template"] = "Kaputt {tage}"
        with pytest.raises(KeyError):
            OfferManager(client, config, pm)


def test_nobody_voting_at_all_is_reported(offers, client):
    report = offers.run(MON)
    assert any("keine einzige Stimme" in p for p in report.problems)
    assert offers.run(MON).ok  # once
    assert sent(client) == []


def test_withdrawn_votes_still_count_as_voting(offers, client, votes, pm):
    vote(votes, pm, "ab", [])
    assert offers.run(MON).ok
    assert sent(client) == []


def test_timestamp_survives_a_hand_resolved_week(offers, pm):
    with open(pm.state.path, "a") as f:
        f.write("2026-W45 error 2026-10-24T17:00:00+02:00 boom\n2026-W45 posted manual\n")
    assert kw45_poll(pm) == 1002
