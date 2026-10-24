"""
Poll Manager - posts the weekly availability polls for the kicker group

Each run makes sure the `weeks_ahead` weeks after the current one have a poll.
Run daily, that posts one new week every Monday, and a missed Monday is caught
up by the next run.

Delivery is at-most-once. A "sending" line is fsynced to the state log before a
poll goes out, and the outcome is appended after it. A week that was attempted
is never sent again automatically, unless the attempt provably sent nothing
(the daemon was unreachable): a send that errored or timed out may still have
reached the group, and retrying it is how duplicate polls happen. Such weeks
are reported as problems instead, for a human to resolve.
"""

import fcntl
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .signal_client import DaemonUnreachable, RpcError, SignalClient, SignalError

logger = logging.getLogger(__name__)

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

DEFAULT_ANNOUNCEMENT = (
    "Bitte stimmt ab ob ihr für ein Kickerligamatch abends (ab 19-20 Uhr) "
    "Zeit habt. Wir brauchen mindestens 4 Leute, ab 5 Personen würde ich "
    "nach einem Gegnerteam suchen."
)

# State log statuses. Only POSTED counts as done; only UNSENT may be retried.
POSTED, SENDING, UNSENT, ERROR = "posted", "sending", "unsent", "error"


def week_key(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def week_label(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    return f"KW{monday.isocalendar()[1]} ({monday:%d.%m.}–{sunday:%d.%m.})"


def format_date(day: date) -> str:
    """Format date as DD.MM.YYYY Weekday"""
    return f"{day:%d.%m.%Y} {WEEKDAYS[day.weekday()]}"


def upcoming_mondays(today: date, weeks_ahead: int) -> List[date]:
    """Mondays of the `weeks_ahead` weeks after the current one"""
    this_monday = today - timedelta(days=today.weekday())
    return [this_monday + timedelta(weeks=i) for i in range(1, weeks_ahead + 1)]


def recipient_results(result) -> List[dict]:
    """Per-recipient outcomes in a send result, e.g. [{"type": "SUCCESS", ...}]"""
    results = result.get("results") if isinstance(result, dict) else None
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def undelivered(results: List[dict]) -> str:
    """Summarise failed recipients, e.g. '1/14 (UNREGISTERED_FAILURE)'"""
    failed = [str(r.get("type")) for r in results if r.get("type") != "SUCCESS"]
    if not failed:
        return ""
    return f"{len(failed)}/{len(results)} ({', '.join(sorted(set(failed)))})"


STATE_LINE = re.compile(r"(\d{4}-W\d{2})\s+(posted|sending|unsent|error)(?:\s+(.*))?")


class StateLog:
    """Append-only log with one '<week> <status> <timestamp> [detail]' per line.

    The last line for a week wins, so it can be fixed by hand: delete a week's
    lines to have it posted again, or append '<week> posted' to mark it done.
    """

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Tuple[str, str]]:
        """Return {week: (status, rest of line)}.

        Raises on anything it does not understand, rather than skip it: a
        week whose line was skipped would look unposted and be posted again.
        """
        state = {}
        if self.path.exists():
            for number, line in enumerate(self.path.read_text().splitlines(), 1):
                if not line.strip() or line.startswith("#"):
                    continue
                match = STATE_LINE.fullmatch(line.strip())
                if not match:
                    raise ValueError(f"{self.path}:{number}: cannot parse {line!r}")
                week, status, detail = match.groups()
                state[week] = (status, detail or "")
        return state

    def append(self, week: str, status: str, detail: str = ""):
        line = f"{week} {status} {datetime.now().astimezone().isoformat(timespec='seconds')}"
        if detail:
            line += " " + " ".join(detail.split())
        with open(self.path, "ab+") as f:
            end = f.seek(0, os.SEEK_END)
            if end:
                f.seek(end - 1)
                if f.read(1) != b"\n":
                    # A hand edit without a final newline would otherwise
                    # swallow this line into its own.
                    line = "\n" + line
            f.write(line.encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialise runs, so two at once cannot both see a week as due"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


class PreflightError(Exception):
    """Posting is pointless, e.g. the bot is not in the group"""


class NotReady(Exception):
    """signal-cli cannot be used right now, and nothing was sent: retry"""


@dataclass
class PollReport:
    posted: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class PollManager:
    """Manages weekly poll creation for Signal groups"""

    def __init__(
        self,
        signal_client: SignalClient,
        config: dict,
        sleep: Callable[[float], None] = time.sleep,
    ):
        poll_config = config.get("poll") or {}
        if not poll_config.get("state_file"):
            # No default: a relative or per-user fallback would be an empty
            # log the moment the bot runs from elsewhere, and repost everything.
            raise ValueError("poll.state_file must be set")
        self.client = signal_client
        self.group_id = config["group_id"]
        self.weeks_ahead = poll_config.get("weeks_ahead", 2)
        self.announcement = poll_config.get("announcement", DEFAULT_ANNOUNCEMENT)
        self.attempts = poll_config.get("connect_attempts", 5)
        self.retry_delay = poll_config.get("retry_delay", 60)
        self.state = StateLog(Path(poll_config["state_file"]).expanduser())
        self.sleep = sleep

    def plan(self, today: date) -> Tuple[List[date], List[str]]:
        """Return the weeks that need a poll, and descriptions of weeks whose
        last attempt needs a human to look at it"""
        state = self.state.load()
        due, stuck = [], []
        for monday in upcoming_mondays(today, self.weeks_ahead):
            status, detail = state.get(week_key(monday), (None, ""))
            if status in (None, UNSENT):
                due.append(monday)
            elif status != POSTED:
                stuck.append(f"{week_label(monday)}: {status} {detail}".strip())
        return due, stuck

    def run(self, today: Optional[date] = None) -> PollReport:
        """Post every due poll, then report what happened"""
        today = today or date.today()
        report = PollReport()

        with self.state.lock():
            for attempt in range(1, self.attempts + 1):
                try:
                    self._post_due(today, report)
                    break
                except (DaemonUnreachable, NotReady) as e:
                    logger.warning(f"signal-cli not ready (attempt {attempt}/{self.attempts}): {e}")
                    if attempt == self.attempts:
                        report.problems.append(f"signal-cli nicht bereit: {e}")
                    else:
                        self.sleep(self.retry_delay)
                except PreflightError as e:
                    report.problems.append(str(e))
                    break

            # Judge the outcome from the state log, not from what this run
            # thinks happened, so a week stuck from an earlier run keeps being
            # reported until someone resolves it.
            due, stuck = self.plan(today)
            report.problems += [f"{week_label(monday)}: nicht gepostet" for monday in due]
            report.problems += stuck

            # After the polls rather than before: if they fail, the group
            # should not be told to vote on nothing.
            if report.posted and self.announcement:
                try:
                    self.client.send_message(self.group_id, self.announcement)
                except SignalError as e:
                    report.problems.append(f"Ankündigung evtl. nicht gesendet: {e}")

        return report

    def _post_due(self, today: date, report: PollReport):
        due, _ = self.plan(today)
        if not due:
            logger.info("All upcoming weeks already have a poll")
            return
        logger.info(f"Due: {', '.join(week_key(monday) for monday in due)}")
        self._check_membership()
        for monday in due:
            self._post_week(monday, report)

    def _check_membership(self):
        try:
            groups = self.client.list_groups()
        except DaemonUnreachable:
            raise
        except SignalError as e:
            # Read-only, so any failure is safe to retry
            raise NotReady(f"Gruppenliste nicht abrufbar: {e}") from e

        if not isinstance(groups, list):
            raise NotReady(f"Gruppenliste unlesbar: {str(groups)[:100]}")
        group = next((g for g in groups if isinstance(g, dict) and g.get("id") == self.group_id), None)
        if group is None:
            raise PreflightError("Bot kennt die Gruppe nicht (group_id falsch?)")
        if not group.get("isMember", True):
            raise PreflightError(f"Bot ist kein Mitglied mehr von „{group.get('name')}“")

    def _post_week(self, monday: date, report: PollReport):
        key = week_key(monday)
        options = [format_date(monday + timedelta(days=i)) for i in range(7)]
        options.append("Keine Zeit :(")

        self.state.append(key, SENDING)
        error = None
        try:
            result = self.client.send_poll(self.group_id, f"KW{monday.isocalendar()[1]}", options)
        except DaemonUnreachable:
            self.state.append(key, UNSENT)
            raise
        except RpcError as e:
            # signal-cli can fail a send that still reached part of the group,
            # in which case the poll is there and must count as posted
            result = e.data.get("response") if isinstance(e.data, dict) else None
            error = str(e)
        except SignalError as e:
            result, error = None, str(e)

        results = recipient_results(result)
        failed = undelivered(results)
        delivered = any(r.get("type") == "SUCCESS" for r in results)
        if not delivered and (error or results):
            logger.error(f"Poll for {key} may or may not have been sent: {error or failed}")
            self.state.append(key, ERROR, error or f"an niemanden zugestellt: {failed}")
            return

        self.state.append(key, POSTED, failed)
        report.posted.append(week_label(monday))
        logger.info(f"Posted poll for {key}")
        if failed:
            report.notes.append(f"{week_label(monday)} nicht zugestellt an {failed}")

    def summary(self, report: PollReport) -> Optional[str]:
        """Message for the admin group, or None if there is nothing to say"""
        lines = []
        if report.posted:
            lines.append("✅ Umfragen gepostet: " + ", ".join(report.posted))
        lines += [f"ℹ️ {note}" for note in report.notes]
        if report.problems:
            lines.append("⚠️ Marvin braucht Hilfe:")
            lines += [f"• {problem}" for problem in report.problems]
            lines.append(
                f"Unklare Wochen werden nie automatisch wiederholt (könnten doch "
                f"angekommen sein). Im Chat prüfen, dann in {self.state.path} die "
                f"Zeilen der Woche löschen (→ neu posten) oder '<Woche> posted' "
                f"anhängen (→ erledigt). Sofort ausführen: systemctl start marvin-poll"
            )
        return "\n".join(lines) or None
