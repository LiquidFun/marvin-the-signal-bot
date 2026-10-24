"""
Offer - drafts the "we have time" message for the league captains

On the Monday a week before a week starts, Marvin tallies that week's poll.
If any day has at least `min_votes` people, it sends the admin a message
listing those days, written so it can be passed on to the other teams as is.
Nothing is sent when no day qualifies.

Each week is handled once, with the same write-ahead state log as the polls:
a week with any line in it is never sent again automatically.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from .poll import (
    POSTED, SENDING, SKIPPED, UNSENT, ERROR, WEEKDAYS,
    PollManager, PollReport, StateLog, week_key, week_label,
)
from .signal_client import DaemonUnreachable, SignalClient, SignalError
from .votes import VoteLog

logger = logging.getLogger(__name__)


# Written the way the captain writes in the captains group. {days} becomes
# e.g. "3.11 (Dienstag) und 5.11 (Donnerstag)".
DEFAULT_TEMPLATE = 'Moin! Team "Da war offen" hat am {days} Zeit für ein Match. Wer hat interesse?'


def day_label(day: date) -> str:
    return f"{day.day}.{day.month} ({WEEKDAYS[day.weekday()]})"


def offer_text(days: List[date], template: str = DEFAULT_TEMPLATE) -> str:
    """The message for the captains: the template with the days filled in"""
    labels = [day_label(day) for day in days]
    listed = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " und " + labels[-1]
    return template.format(days=listed)


class OfferManager:
    def __init__(self, signal_client: SignalClient, config: dict, polls: PollManager):
        offer_config = config.get("offer") or {}
        if not offer_config.get("state_file"):
            raise ValueError("offer.state_file must be set")
        if not (config.get("poll") or {}).get("votes_file"):
            raise ValueError("poll.votes_file must be set")
        if not config.get("admin_group_id"):
            raise ValueError("admin_group_id must be set: the offer goes there")
        self.client = signal_client
        self.admin_group_id = config["admin_group_id"]
        self.min_votes = offer_config.get("min_votes", 5)
        self.template = offer_config.get("template") or DEFAULT_TEMPLATE
        self.template.format(days="")  # a broken template fails here, not on the Monday
        self.state = StateLog(Path(offer_config["state_file"]).expanduser())
        self.polls = polls
        self.votes = VoteLog(Path(config["poll"]["votes_file"]).expanduser())

    def qualifying_days(self, monday: date) -> Tuple[Optional[List[date]], int, int]:
        """Days of the week with enough votes (None if the week has no known
        poll), the highest vote count of any day, and how many people voted"""
        sent = self.polls.sent_timestamp(monday)
        if sent is None:
            return None, 0, 0
        counts = self.votes.tally(sent)
        per_day = [counts.get(i, 0) for i in range(7)]
        days = [monday + timedelta(days=i) for i, n in enumerate(per_day) if n >= self.min_votes]
        return days, max(per_day), len(self.votes.latest(sent))

    def run(self, today: Optional[date] = None) -> PollReport:
        """Send the offer for next week, if it is due and any day qualifies"""
        today = today or date.today()
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        key = week_key(monday)
        report = PollReport()

        with self.state.lock():
            status, detail = self.state.load().get(key, (None, ""))
            if status in (POSTED, SKIPPED):
                return report
            if status not in (None, UNSENT):
                report.problems.append(f"Captains-Nachricht {week_label(monday)}: {status} {detail}".strip())
                return report

            with self.polls.state.lock():
                days, best, voters = self.qualifying_days(monday)
            if days is None:
                self.state.append(key, SKIPPED, "keine Umfrage")
                report.problems.append(f"Captains-Nachricht {week_label(monday)}: keine Umfrage gefunden")
                return report
            if not voters:
                # "Nobody voted at all" looks exactly like a listener that
                # stopped recording, so it is worth a look, unlike a quiet week.
                self.state.append(key, SKIPPED, "keine einzige Stimme")
                report.problems.append(
                    f"Captains-Nachricht {week_label(monday)}: keine einzige Stimme aufgezeichnet. "
                    f"Läuft marvin.service? (journalctl -u marvin)"
                )
                return report
            if not days:
                logger.info(f"{key}: no day has {self.min_votes} votes (best: {best}), nothing to offer")
                self.state.append(key, SKIPPED, f"höchstens {best} Stimmen")
                return report

            self.state.append(key, SENDING)
            try:
                self.client.send_message(self.admin_group_id, offer_text(days, self.template))
            except DaemonUnreachable as e:
                self.state.append(key, UNSENT)
                report.problems.append(f"Captains-Nachricht {week_label(monday)} nicht gesendet: {e}")
                return report
            except SignalError as e:
                self.state.append(key, ERROR, str(e))
                report.problems.append(f"Captains-Nachricht {week_label(monday)} evtl. nicht gesendet: {e}")
                return report

            self.state.append(key, POSTED, ", ".join(f"{d:%d.%m.}" for d in days))
            report.posted.append(week_label(monday))
            logger.info(f"Sent captains offer for {key}: {[str(d) for d in days]}")
        return report
