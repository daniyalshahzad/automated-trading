"""
watchdog.py — Auto-restarts broker bridge and scheduler if they go down.
Run with: nohup python ~/automatedtrading/watchdog.py > /dev/null 2>&1 &
"""

import subprocess
import time
import os
from datetime import datetime

BASE_DIR = os.path.expanduser("~/automatedtrading")
VENV_PYTHON = os.path.join(BASE_DIR, ".venv/bin/python")
VENV_UVICORN = os.path.join(BASE_DIR, ".venv/bin/uvicorn")
LOG_PATH = os.path.expanduser("~/watchdog.log")

BRIDGE_CMD = [
    VENV_UVICORN,
    "broker_bridge.app:app",
    "--host", "127.0.0.1",
    "--port", "8787"
]

SCHEDULER_CMD = [
    VENV_PYTHON,
    "scheduler.py"
]

SCHEDULER_DIR = os.path.join(BASE_DIR, "orchestrator")


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def is_running(name):
    result = subprocess.run(["pgrep", "-f", name], capture_output=True)
    return result.returncode == 0


def start_bridge():
    subprocess.Popen(
        BRIDGE_CMD,
        cwd=BASE_DIR,
        stdout=open(os.path.expanduser("~/bridge.log"), "a"),
        stderr=subprocess.STDOUT
    )
    log("Bridge restarted.")


def start_scheduler():
    subprocess.Popen(
        SCHEDULER_CMD,
        cwd=SCHEDULER_DIR,
        stdout=open(os.path.expanduser("~/scheduler.log"), "a"),
        stderr=subprocess.STDOUT
    )
    log("Scheduler restarted.")


log("Watchdog started.")

while True:
    if not is_running("uvicorn"):
        log("Bridge is down — restarting...")
        start_bridge()

    if not is_running("scheduler.py"):
        log("Scheduler is down — restarting...")
        start_scheduler()

    time.sleep(30)