"""
Poll Manager - Creates weekly polls for the kicker group
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .chat import ChatHandler

from .signal_client import SignalClient

logger = logging.getLogger(__name__)

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


class PollManager:
    """Manages weekly poll creation for Signal groups"""

    def __init__(
        self,
        signal_client: SignalClient,
        config: dict,
        chat_handler: Optional["ChatHandler"] = None
    ):
        self.client = signal_client
        self.config = config
        self.group_id = config.get("group_id")
        self.chat_handler = chat_handler

        poll_config = config.get("poll", {})
        state_file = poll_config.get(
            "state_file",
            "~/.config/signal-cli/signal_poll_weeks.json"
        )
        self.state_file = Path(state_file).expanduser()
        self.weeks_ahead = poll_config.get("weeks_ahead", 3)
        self.post_count = poll_config.get("post_count", 2)
        self.poll_prompt = poll_config.get("poll_prompt")

    def load_state(self) -> dict:
        """Load posted weeks from state file"""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception:
                logger.exception("Failed to load poll state:")
        return {}

    def save_state(self, state: dict):
        """Save posted weeks to state file"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception:
            logger.exception("Failed to save poll state:")

    @staticmethod
    def get_week_key(date: datetime) -> str:
        """Get week key as 'YYYY-WW'"""
        year, week, _ = date.isocalendar()
        return f"{year}-{week:02d}"

    @staticmethod
    def get_monday_of_week(year: int, week: int) -> datetime:
        """Get Monday date for a given ISO year and week"""
        jan4 = datetime(year, 1, 4)
        week_start = jan4 + timedelta(days=-jan4.weekday(), weeks=week - 1)
        return week_start

    @staticmethod
    def format_date(date: datetime) -> str:
        """Format date as DD.MM.YYYY Weekday"""
        weekday = WEEKDAYS[date.weekday()]
        return f"{date.strftime('%d.%m.%Y')} {weekday}"

    def get_weeks_to_post(self) -> List[Tuple[int, int]]:
        """Determine which weeks need polls posted"""
        state = self.load_state()
        today = datetime.now()
        weeks_to_check = []

        for i in range(self.weeks_ahead):
            check_date = today + timedelta(weeks=i)
            year, week, _ = check_date.isocalendar()
            weeks_to_check.append((year, week))

        weeks_to_post = []
        for year, week in weeks_to_check:
            key = f"{year}-{week:02d}"
            if key not in state:
                weeks_to_post.append((year, week))

        return weeks_to_post

    def generate_poll_message(self, week: int) -> Optional[str]:
        """Generate a poll announcement message using the LLM"""
        if not self.chat_handler or not self.poll_prompt:
            return None

        prompt = self.poll_prompt.format(week=week)
        try:
            return self.chat_handler.get_llm_response(prompt, context=[])
        except Exception:
            logger.exception("Failed to generate poll message:")
            return None

    def create_weekly_poll(self, year: int, week: int) -> bool:
        """Create a poll for the given week"""
        monday = self.get_monday_of_week(year, week)
        options = [self.format_date(monday + timedelta(days=i)) for i in range(7)]
        question = f"KW{week}"

        success = self.client.send_poll(self.group_id, question, options)
        if success:
            logger.info(f"Posted poll for KW{week} ({year})")
        else:
            logger.error(f"Failed to post poll for KW{week} ({year})")
        return success

    def check_and_post_polls(self) -> List[str]:
        """Check and post polls for the next weeks if needed"""
        weeks_to_post = self.get_weeks_to_post()

        if not weeks_to_post:
            logger.info(f"Next {self.weeks_ahead} weeks already covered")
            return []

        logger.info(f"Need to post: {', '.join([f'KW{w} ({y})' for y, w in weeks_to_post])}")

        first_year, first_week = weeks_to_post[0]

        # Send LLM-generated announcement before the first poll
        poll_message = self.generate_poll_message(first_week)
        if poll_message:
            self.client.send_message(self.group_id, poll_message)

        state = self.load_state()
        posted = []

        for year, week in weeks_to_post[:self.post_count]:
            if self.create_weekly_poll(year, week):
                key = f"{year}-{week:02d}"
                state[key] = datetime.now().strftime("%Y-%m-%d")
                posted.append(f"KW{week}")

        if posted:
            self.save_state(state)
            logger.info(f"Successfully posted: {', '.join(posted)}")

        return posted
