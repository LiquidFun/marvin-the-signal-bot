"""
Vote Log - remembers how people voted on the polls

Signal keeps no poll results anywhere. Every vote is a message to the group
that names the poll by its sent timestamp, lists the selected options, and
carries a counter that goes up each time that person changes their vote. The
listener appends every vote it receives here; tallying replays the log.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def vote_from_envelope(envelope: dict) -> Optional[dict]:
    """The vote in a received envelope, as a log record, or None"""
    data_message = envelope.get("dataMessage") or {}
    vote = data_message.get("pollVote")
    if not isinstance(vote, dict):
        return None
    voter = envelope.get("sourceUuid") or envelope.get("sourceNumber") or envelope.get("source")
    options = vote.get("optionIndexes")
    if not voter or not isinstance(vote.get("targetSentTimestamp"), int) or not isinstance(options, list):
        logger.warning(f"Ignoring malformed poll vote: {vote}")
        return None
    return {
        "poll": vote["targetSentTimestamp"],
        "voter": voter,
        "options": [i for i in options if isinstance(i, int)],
        "count": vote.get("voteCount") or 0,
    }


class VoteLog:
    """Append-only JSON lines, one per received vote"""

    def __init__(self, path: Path):
        self.path = path

    def append(self, record: dict):
        record = {**record, "received": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def latest(self, poll: int) -> Dict[str, List[int]]:
        """Each person's latest vote on one poll: {voter: options}. A vote
        with no options (withdrawn) still counts as having voted."""
        latest: Dict[str, tuple] = {}
        for record in self._records():
            if record.get("poll") != poll:
                continue
            count = record.get("count", 0)
            voter = record.get("voter")
            # >=: of two votes with the same counter, the later one wins
            if voter not in latest or count >= latest[voter][0]:
                latest[voter] = (count, record.get("options", []))
        return {voter: options for voter, (_, options) in latest.items()}

    def tally(self, poll: int) -> Dict[int, int]:
        """Number of people per option of one poll"""
        counts: Dict[int, int] = {}
        for options in self.latest(poll).values():
            for option in set(options):
                counts[option] = counts.get(option, 0) + 1
        return counts

    def _records(self) -> List[dict]:
        if not self.path.exists():
            return []
        records = []
        for number, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                # At worst the last line, cut short by a crash mid-write.
                # Losing one vote beats refusing to count all the others.
                logger.warning(f"{self.path}:{number}: skipping unreadable line")
        return records
