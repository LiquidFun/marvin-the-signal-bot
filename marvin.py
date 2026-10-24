#!/usr/bin/env -S uv run
"""
Marvin - Signal bot for the kicker group

    marvin.py chat             answer @-mentions (long-running; marvin.service)
    marvin.py poll             post due availability polls, then exit
                               (oneshot; marvin-poll.timer)
    marvin.py poll --dry-run   show what is due, send nothing

Polls run as a separate short-lived process on purpose: a restart or crash of
the chat loop can never cause a poll to be posted.
"""

import argparse
import logging
import os
import sys
from datetime import date

import requests
import yaml

from modules import ChatHandler, PollManager, SignalClient
from modules.poll import PollReport, week_label
from modules.signal_client import SignalError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load configuration from YAML file: $MARVIN_CONFIG, else the systemd
    credential `config.yaml` (LoadCredential=), else ./config.yaml"""
    config_path = os.environ.get("MARVIN_CONFIG")
    if not config_path and os.environ.get("CREDENTIALS_DIRECTORY"):
        config_path = os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "config.yaml")

    with open(config_path or "config.yaml", 'r') as f:
        config = yaml.safe_load(f)

    if os.environ.get("MARVIN_SIGNAL_GROUP_ID"):
        config["group_id"] = os.environ.get("MARVIN_SIGNAL_GROUP_ID")
    if os.environ.get("MARVIN_BOT_NUMBER"):
        config["bot_number"] = os.environ.get("MARVIN_BOT_NUMBER")

    return config


def notify_admin(client: SignalClient, config: dict, text: str):
    """Tell the admin group what happened. Never raises."""
    logger.info(f"Report:\n{text}")
    admin_group_id = config.get("admin_group_id")
    if not admin_group_id:
        logger.warning("No admin_group_id configured, report not sent")
        return
    try:
        client.send_message(admin_group_id, text)
    except SignalError as e:
        logger.error(f"Could not send report to admin group: {e}")


def ping_healthcheck(config: dict, ok: bool, text: str):
    """Ping a dead man's switch (e.g. healthchecks.io), if configured. It
    raises the alarm when pings stop, which covers the cases Signal cannot
    report itself: signal-cli broken, server down. Never raises."""
    url = config.get("healthcheck_url")
    if not url:
        return
    try:
        requests.post(url if ok else url.rstrip("/") + "/fail", data=text.encode(), timeout=10)
    except requests.RequestException as e:
        logger.warning(f"Healthcheck ping failed: {e}")


def run_poll(config: dict, dry_run: bool) -> int:
    client = SignalClient(config)
    manager = PollManager(client, config)

    if dry_run:
        due, stuck = manager.plan(date.today())
        print("Due:", ", ".join(week_label(monday) for monday in due) or "nothing")
        for problem in stuck:
            print("Needs attention:", problem)
        return 0

    try:
        report = manager.run()
    except Exception as e:
        logger.exception("Poll run crashed:")
        report = PollReport(problems=[f"Absturz: {e!r} (journalctl -u marvin-poll)"])

    text = manager.summary(report)
    if text:
        notify_admin(client, config, text)
    ping_healthcheck(config, report.ok, text or "nothing due")
    return 0 if report.ok else 1


def main():
    parser = argparse.ArgumentParser(description="Marvin, the Signal kicker bot")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("chat", help="answer @-mentions (long-running)")
    poll_parser = commands.add_parser("poll", help="post due polls, then exit")
    poll_parser.add_argument("--dry-run", action="store_true", help="show what is due, send nothing")
    args = parser.parse_args()

    config = load_config()

    if not config.get("group_id"):
        logger.error("Please set group_id in config.yaml or MARVIN_SIGNAL_GROUP_ID env var")
        return 1

    if args.command == "poll":
        return run_poll(config, args.dry_run)

    if not config.get("bot_number"):
        logger.error("Please set bot_number in config.yaml or MARVIN_BOT_NUMBER env var")
        return 1

    ChatHandler(SignalClient(config), config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
