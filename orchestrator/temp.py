"""
temp.py — Test researcher and trader agents using API key auth.
Place in orchestrator/ directory and run:
    python temp.py
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# Load .env from parent directory (same as orchestrator.py)
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=ENV_PATH)

API_KEY      = os.getenv("AZURE_API_KEY")
API_VERSION  = "2025-11-15-preview"

RESEARCH_BASE = (
    "https://hubassist-us2.services.ai.azure.com/"
    "api/projects/proj-default/applications/autotrad-researcher/protocols/openai"
)

TRADER_BASE = (
    "https://hubassist-us2.services.ai.azure.com/"
    "api/projects/proj-default/applications/trader/protocols/openai"
)

if not API_KEY:
    raise RuntimeError("AZURE_API_KEY not found in .env")

research_client = OpenAI(
    api_key=API_KEY,
    base_url=RESEARCH_BASE,
    default_query={"api-version": API_VERSION},
)

trader_client = OpenAI(
    api_key=API_KEY,
    base_url=TRADER_BASE,
    default_query={"api-version": API_VERSION},
)

# ── Test researcher ──────────────────────────────────────────
print("Testing RESEARCHER agent...")
try:
    r = research_client.responses.create(
        model="autotrad-researcher",
        input="Say 'researcher OK' and nothing else.",
        max_output_tokens=20,
    )
    print(f"  Researcher response: {r.output_text.strip()}")
except Exception as e:
    print(f"  Researcher FAILED: {e}")

# ── Test trader ──────────────────────────────────────────────
print("Testing TRADER agent...")
try:
    t = trader_client.responses.create(
        model="trader",
        input="Say 'trader OK' and nothing else.",
        max_output_tokens=20,
    )
    print(f"  Trader response: {t.output_text.strip()}")
except Exception as e:
    print(f"  Trader FAILED: {e}")

print("Done.")