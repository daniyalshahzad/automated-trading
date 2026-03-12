# AutoTrader

An AI-driven swing trading orchestrator that autonomously manages a portfolio of NASDAQ equities using Azure AI agents and Interactive Brokers.

## How It Works

AutoTrader runs every 2 hours during market hours. Each cycle it:

1. Fetches live portfolio state and enriches positions with real-time prices
2. Runs a 4-call AI research pipeline (initial scan → deep dive → devil's advocate → synthesis)
3. Passes research and portfolio context to a trader agent that decides to HOLD, REBALANCE, or EXIT_TO_CASH
4. Validates the decision against hard rules (exchange, position limits, allocation caps)
5. Executes orders via Interactive Brokers — sells first, then buys with available cash
6. Updates conviction memory with actual fills and P&L

## Stack

- **Azure AI Foundry** — researcher and trader agents (GPT-based)
- **Interactive Brokers** — order execution via IB Gateway + ib_insync
- **FastAPI** — broker bridge REST API
- **Python** — orchestrator, scheduler, memory management

## Features

- Swing trading focus — target 7-day holds, max 14 days
- Max 3 concurrent positions, NASDAQ only
- Conviction tracking with thesis history and P&L per position
- Anti-churn logic — suppresses noise rebalances under 2% of NLV
- Cash buffer on every order to absorb slippage
- Email notifications on every decision cycle
- PDF executive report generation

## Project Structure

```
├── broker_bridge/        # FastAPI wrapper over IB Gateway
├── orchestrator/
│   ├── orchestrator.py   # Main trading loop
│   ├── scheduler.py      # Fires orchestrator at market hours
│   ├── report.py         # PDF report generator
│   ├── logs/             # Daily log files
│   ├── research_logs/    # Per-run research JSON
│   └── reports/          # Generated PDF reports
├── docs/                 # Documentation
├── .env.sample           # Environment variable template
└── RUNBOOK.md            # Operations guide
```

## Setup

See [RUNBOOK.md](./RUNBOOK.md) for full setup and deployment instructions.