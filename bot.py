#!/usr/bin/env python3
"""
Signal Poll Bot - Posts weekly polls with dates
Usage: signal_poll_bot.py [--self | -g GROUP_ID]
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

SIGNAL_CLI = str(Path.home() / "bin" / "signal-cli")
STATE_FILE = Path.home() / ".config/signal-cli/signal_poll_weeks.json"

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

def load_state():
    """Load posted weeks from state file"""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    """Save posted weeks to state file"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_week_key(date):
    """Get week key as 'YYYY-WW'"""
    year, week, _ = date.isocalendar()
    return f"{year}-{week:02d}"

def get_monday_of_week(year, week):
    """Get Monday date for a given ISO year and week"""
    jan4 = datetime(year, 1, 4)
    week_start = jan4 + timedelta(days=-jan4.weekday(), weeks=week-1)
    return week_start

def format_date(date):
    """Format date as DD.MM.YYYY Weekday"""
    weekday = WEEKDAYS[date.weekday()]
    return f"{date.strftime('%d.%m.%Y')} {weekday}"

def create_poll(week_year, week_num, target):
    """Create a poll for the given week"""
    monday = get_monday_of_week(week_year, week_num)
    
    # Generate 7 day options
    options = [format_date(monday + timedelta(days=i)) for i in range(7)]
    
    # Build command
    cmd = [SIGNAL_CLI, "sendPollCreate"]
    
    if target == "self":
        cmd.append("--note-to-self")
    else:
        cmd.extend(["-g", target])
    
    cmd.extend(["-q", f"KW{week_num}"])
    cmd.append("-o")
    cmd.extend(options)
    
    # Execute
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode == 0:
        print(f"✓ Posted KW{week_num} ({week_year})")
        return True
    else:
        print(f"✗ Failed to post KW{week_num}: {result.stderr.strip()}")
        return False

def main():
    # Parse arguments
    if "--self" in sys.argv:
        target = "self"
    elif "-g" in sys.argv:
        idx = sys.argv.index("-g")
        if idx + 1 >= len(sys.argv):
            print("Error: -g requires GROUP_ID")
            sys.exit(1)
        target = sys.argv[idx + 1]
    else:
        print("Usage: signal_poll_bot.py [--self | -g GROUP_ID]")
        sys.exit(1)
    
    # Receive messages first
    print("Receiving messages...")
    subprocess.run([SIGNAL_CLI, "receive"], capture_output=True, timeout=30)
    
    # Load state
    state = load_state()
    
    # Get current date and next two weeks
    today = datetime.now()
    weeks_to_check = []
    
    for i in range(2):
        check_date = today + timedelta(weeks=i)
        year, week, _ = check_date.isocalendar()
        weeks_to_check.append((year, week))
    
    print(f"Checking weeks: {', '.join([f'KW{w} ({y})' for y, w in weeks_to_check])}")
    
    # Check which weeks need to be posted
    weeks_to_post = []
    for year, week in weeks_to_check:
        key = f"{year}-{week:02d}"
        if key not in state:
            weeks_to_post.append((year, week))
    
    if not weeks_to_post:
        print("→ Next 2 weeks already covered")
        return
    
    print(f"→ Need to post: {', '.join([f'KW{w} ({y})' for y, w in weeks_to_post])}")
    print(f"→ Posting 2 weeks to cover gaps...")
    
    # Post two weeks starting from the first missing week
    first_year, first_week = weeks_to_post[0]
    start_date = get_monday_of_week(first_year, first_week)
    
    posted = []
    for i in range(2):
        week_date = start_date + timedelta(weeks=i)
        year, week, _ = week_date.isocalendar()
        
        if create_poll(year, week, target):
            key = f"{year}-{week:02d}"
            state[key] = datetime.now().strftime("%Y-%m-%d")
            posted.append(f"KW{week}")
    
    if posted:
        save_state(state)
        print(f"✓ Successfully posted: {', '.join(posted)}")
    else:
        print("✗ No polls posted")

if __name__ == "__main__":
    main()
