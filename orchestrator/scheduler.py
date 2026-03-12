"""
scheduler.py
Runs orchestrator.py at 9:31, 11:30, 13:30, 15:30 ET on weekdays.
Run once and leave it: python scheduler.py
"""

import subprocess
import sys
import time
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

RUN_TIMES = [
    (9, 35),
    (11, 35),
    (13, 35),
    (15, 35),
]

# Resolve orchestrator.py relative to this file
ORCHESTRATOR = Path(__file__).resolve().parent / "orchestrator.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("scheduler")


def next_run_today(now: datetime) -> tuple[int, int] | None:
    """Return the next (hour, minute) run time that hasn't passed yet today."""
    for h, m in RUN_TIMES:
        if (now.hour, now.minute) < (h, m):
            return h, m
    return None


def seconds_until(now: datetime, hour: int, minute: int) -> float:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (target - now).total_seconds()


def run_orchestrator():
    log.info("Firing orchestrator.py ...")
    result = subprocess.run(
        [sys.executable, str(ORCHESTRATOR)],
        capture_output=False,   # orchestrator logs itself
    )
    if result.returncode != 0:
        log.error(f"orchestrator.py exited with code {result.returncode}")
    else:
        log.info("orchestrator.py completed OK")


def main():
    log.info(f"Scheduler started. Runs at {RUN_TIMES} ET on weekdays.")
    log.info(f"Orchestrator: {ORCHESTRATOR}")

    while True:
        now = datetime.now(MARKET_TZ)

        # Skip weekends
        if now.weekday() >= 5:
            # Sleep until Monday 9:00 ET
            log.info("Weekend — sleeping 1 hour then re-checking.")
            time.sleep(3600)
            continue

        slot = next_run_today(now)

        if slot is None:
            # All runs done for today — sleep until next morning
            log.info("All runs done for today. Sleeping 1 hour then re-checking.")
            time.sleep(3600)
            continue

        wait = seconds_until(now, *slot)
        log.info(f"Next run at {slot[0]:02d}:{slot[1]:02d} ET — sleeping {wait/60:.1f} min")
        time.sleep(max(0, wait))

        # Double-check it's still a weekday (edge case: slept over midnight)
        now = datetime.now(MARKET_TZ)
        if now.weekday() < 5:
            run_orchestrator()

        # Small sleep to avoid re-firing the same slot
        time.sleep(60)


if __name__ == "__main__":
    main()