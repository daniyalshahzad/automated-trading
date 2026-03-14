"""
scheduler.py — Fires the orchestrator at market hours.
Runs every day, Monday-Friday at 9:35, 11:35, 13:35, 15:35 ET.

Run:
    cd ~/automatedtrading
    source .venv/bin/activate
    nohup python scheduler.py &
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from orchestrator_v2.main import run

RUN_TIMES = [(9, 35), (11, 35), (13, 35), (15, 35)]
MARKET_TZ = ZoneInfo("America/New_York")

print("Scheduler started. Waiting for next run time ...")

last_run_minute = None

while True:
    now = datetime.now(MARKET_TZ)
    key = (now.date(), now.hour, now.minute)

    if now.weekday() < 5 and (now.hour, now.minute) in RUN_TIMES and key != last_run_minute:
        last_run_minute = key
        print(f"[{now.strftime('%Y-%m-%d %H:%M')} ET] Running orchestrator ...")
        try:
            run()
        except Exception as e:
            print(f"Orchestrator run failed: {e}")

    time.sleep(30)