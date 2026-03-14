"""
config.py — All configuration, constants, and environment variables.
 
To change LLM provider: update llm.py only.
To change trading rules: update constants here.
"""
 
import logging
import os
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
 
from dotenv import load_dotenv
 
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_PATH)
 
# ── Broker bridge ─────────────────────────────────────────────
BRIDGE_URL   = "http://127.0.0.1:8787"
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")
if not BRIDGE_TOKEN:
    raise RuntimeError("BRIDGE_TOKEN not set in .env")
HEADERS = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
 
# ── LLM (see llm.py to change provider) ──────────────────────
LLM_RESEARCHER_URL = os.getenv("AZURE_RESEARCHER_URL")
LLM_TRADER_URL     = os.getenv("AZURE_TRADER_URL")
LLM_API_VERSION    = os.getenv("AZURE_API_VERSION", "2025-11-15-preview")
 
if not LLM_RESEARCHER_URL or not LLM_TRADER_URL:
    raise RuntimeError("AZURE_RESEARCHER_URL and AZURE_TRADER_URL must be set in .env")
 
# ── Paths ─────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).resolve().parent
MEMORY_PATH   = BASE_DIR / "trading_memory.json"
RESEARCH_DIR  = BASE_DIR / "research_logs"
LOG_DIR       = BASE_DIR / "logs"
RESEARCH_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
 
# ── Trading rules (hard limits enforced in trader.py) ─────────
MAX_POSITIONS        = 3
MAX_SINGLE_NAME_PCT  = 0.80
MIN_NEW_POSITION_PCT = 0.10
ALLOW_FULL_CASH      = True
ALLOWED_EXCHANGE     = "NASDAQ"
CASH_BUFFER_PCT      = 0.02   # reserve 2% to absorb market order slippage
MIN_REBALANCE_DELTA_PCT = 0.02  # ignore rebalances smaller than 2% of NLV
 
# ── Swing trading guidance (biases, not hard rules) ───────────
SWING_TARGET_DAYS        = 7
SWING_MAX_DAYS           = 14
DAYS_BEFORE_PROTECTED    = 2
DAYS_FOR_HIGH_STICKINESS = 4
DAYS_FOR_MAX_STICKINESS  = 7
 
# ── Memory limits ─────────────────────────────────────────────
MAX_DECISION_HISTORY   = 10
MAX_DECISIONS_IN_PROMPT = 5
MAX_TARGET_PCT_HISTORY = 10
 
# ── Market hours ──────────────────────────────────────────────
MARKET_TZ    = ZoneInfo("America/New_York")
MARKET_OPEN  = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
 
# ── Notifications ─────────────────────────────────────────────
NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "")
NOTIFY_EMAIL_TO   = os.getenv("NOTIFY_EMAIL_TO", "")
NOTIFY_SMTP_HOST  = os.getenv("NOTIFY_SMTP_HOST", "smtp.gmail.com")
NOTIFY_SMTP_PORT  = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
NOTIFY_SMTP_USER  = os.getenv("NOTIFY_SMTP_USER", "")
NOTIFY_SMTP_PASS  = os.getenv("NOTIFY_SMTP_PASS", "")
 
# ── Runtime flags ─────────────────────────────────────────────
FORCE_RUN = os.getenv("FORCE_RUN", "false").lower() == "true"
POST_TRADE_SETTLE_SECS = 3
 
# ── Logging ───────────────────────────────────────────────────
log_file = LOG_DIR / f"orchestrator_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s|%(levelname)s| %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("orchestrator")