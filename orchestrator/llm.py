"""
llm.py — LLM interface.
 
The orchestrator calls two functions:
    call_researcher(prompt: str) -> str
    call_trader(prompt: str) -> str
 
That's the entire contract. Any code that accepts a string and returns a string works.
 
To swap providers: replace the two functions below with anything you like.
The rest of the codebase never changes.
"""
 
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
 
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
 
# ── Azure AI Foundry setup ────────────────────────────────────
_token = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")
 
_researcher = OpenAI(
    api_key=_token,
    base_url=os.getenv("AZURE_RESEARCHER_URL"),
    default_query={"api-version": os.getenv("AZURE_API_VERSION", "2025-11-15-preview")},
)
_trader = OpenAI(
    api_key=_token,
    base_url=os.getenv("AZURE_TRADER_URL"),
    default_query={"api-version": os.getenv("AZURE_API_VERSION", "2025-11-15-preview")},
)
 
 
# ── Public interface (replace these two functions to swap providers) ──
 
def call_researcher(prompt: str) -> str:
    """Send a prompt to the researcher. Returns the response as a string."""
    return _researcher.responses.create(input=prompt).output_text
 
 
def call_trader(prompt: str) -> str:
    """Send a prompt to the trader. Returns the response as a string."""
    return _trader.responses.create(input=prompt).output_text
 
 
# ── Examples of alternative implementations ───────────────────
#
# OpenAI:
#   from openai import OpenAI
#   _client = OpenAI(api_key="sk-...")
#   def call_researcher(prompt): return _client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":prompt}]).choices[0].message.content
#   def call_trader(prompt):     return _client.chat.completions.create(model="gpt-4o", messages=[{"role":"user","content":prompt}]).choices[0].message.content
#
# Anthropic:
#   from anthropic import Anthropic
#   _client = Anthropic(api_key="...")
#   def call_researcher(prompt): return _client.messages.create(model="claude-opus-4-5", max_tokens=4096, messages=[{"role":"user","content":prompt}]).content[0].text
#   def call_trader(prompt):     return _client.messages.create(model="claude-opus-4-5", max_tokens=4096, messages=[{"role":"user","content":prompt}]).content[0].text
#
# Ollama (local):
#   from openai import OpenAI
#   _client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
#   def call_researcher(prompt): return _client.chat.completions.create(model="llama3", messages=[{"role":"user","content":prompt}]).choices[0].message.content
#   def call_trader(prompt):     return _client.chat.completions.create(model="llama3", messages=[{"role":"user","content":prompt}]).choices[0].message.content