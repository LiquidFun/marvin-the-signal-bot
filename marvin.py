#!/usr/bin/env python3
"""
Marvin - Signal Bot Orchestrator
Coordinates chat responses and poll scheduling
"""

import os
import time
import threading
import logging
from datetime import datetime

import yaml
import schedule

from modules import SignalClient, ChatHandler, PollManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load configuration from YAML file"""
    config_path = os.environ.get("MARVIN_CONFIG", "config.yaml")

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    if os.environ.get("MARVIN_SIGNAL_GROUP_ID"):
        config["group_id"] = os.environ.get("MARVIN_SIGNAL_GROUP_ID")
    if os.environ.get("MARVIN_BOT_NUMBER"):
        config["bot_number"] = os.environ.get("MARVIN_BOT_NUMBER")

    return config


def setup_scheduler(poll_manager: PollManager, config: dict):
    """Configure poll scheduling based on config"""
    poll_config = config.get("poll", {})

    if not poll_config.get("enabled", True):
        logger.info("Poll scheduling disabled")
        return

    schedule_time = poll_config.get("schedule", "12:30")
    schedule_days = poll_config.get("schedule_days", [0, 1, 2, 3, 4])

    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    for day_num in schedule_days:
        if 0 <= day_num <= 6:
            day_name = day_names[day_num]
            job = getattr(schedule.every(), day_name)
            job.at(schedule_time).do(poll_manager.check_and_post_polls)
            logger.info(f"Scheduled poll check: {day_name.capitalize()} at {schedule_time}")


def run_scheduler():
    """Background thread to run scheduled tasks"""
    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    """Main entry point"""
    config = load_config()

    if not config.get("group_id"):
        logger.error("Please set group_id in config.yaml or MARVIN_SIGNAL_GROUP_ID env var")
        return 1

    if not config.get("bot_number"):
        logger.error("Please set bot_number in config.yaml or MARVIN_BOT_NUMBER env var")
        return 1

    signal_client = SignalClient(config)
    chat_handler = ChatHandler(signal_client, config)
    poll_manager = PollManager(signal_client, config, chat_handler)

    setup_scheduler(poll_manager, config)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("Scheduler thread started")

    poll_config = config.get("poll", {})
    if poll_config.get("check_on_startup", True):
        logger.info("Running initial poll check...")
        poll_manager.check_and_post_polls()

    chat_handler.run(scheduler_callback=schedule.run_pending)

    return 0


if __name__ == "__main__":
    exit(main())
