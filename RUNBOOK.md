# AutoTrader Runbook

Setup and run guide for AutoTrader — locally on Mac or deployed on a Linux VM.

**Repo:** https://github.com/daniyalshahzad/automated-trading

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Running Locally (Mac)](#2-running-locally-mac)
3. [Deploying on a VM](#3-deploying-on-a-vm)

---

## 1. Prerequisites

### Accounts & Services

- **Interactive Brokers** account with paper trading enabled
- **Azure AI Foundry v2** project with the following agents deployed:
  - **Researcher** (`autotrad-researcher`) — research pipeline agent with web search tool enabled
  - **Trader** (`trader`) — portfolio decision agent (HOLD / REBALANCE / EXIT_TO_CASH)

### Environment Variables

Copy `.env.sample` to `.env` and fill in all values:

```bash
cp .env.sample .env
```

| Variable | Description |
|---|---|
| `IB_HOST` | IB Gateway host — always `127.0.0.1` |
| `IB_PORT` | `7497` for TWS (local), `4002` for IB Gateway (VM) |
| `IB_CLIENT_ID` | Client ID for ib_insync connection — any unique integer |
| `BRIDGE_TOKEN` | Secret token for the broker bridge API — set to any strong random string |
| `ENABLE_TRADING` | Set to `true` to allow real order placement, `false` for dry run |
| `AZURE_RESEARCHER_URL` | Full endpoint URL for the researcher agent in Azure AI Foundry v2 |
| `AZURE_TRADER_URL` | Full endpoint URL for the trader agent in Azure AI Foundry v2 |
| `AZURE_API_VERSION` | Azure AI API version (default: `2025-11-15-preview`) |
| `DEFAULT_CURRENCY` | Base currency for cash checks and display (e.g. `USD`) |
| `DEFAULT_EXCHANGE` | Order routing exchange (default: `SMART`) |
| `DEFAULT_PRIMARY_EXCHANGE` | Primary exchange for contracts (default: `NASDAQ`) |
| `SNAPSHOT_WAIT_SECS` | Timeout for market data snapshots in seconds (default: `2.0`) |
| `QUOTE_WAIT_SECS` | Timeout for live quotes in seconds (default: `5`) |
| `MARKET_DATA_TYPE` | IB market data type — `1` for live, `3` for delayed |
| `FORCE_RUN` | Set to `true` to bypass market hours check (testing only) |

---

## 2. Running Locally (Mac)

### Step 1 — Clone the repo

```bash
git clone https://github.com/daniyalshahzad/automated-trading.git
cd automated-trading
```

### Step 2 — Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure environment

```bash
cp .env.sample .env
nano .env
```

Set `IB_PORT=7497` (TWS default).

### Step 5 — Start TWS or IB Gateway

Open TWS or IB Gateway on your Mac and log in to your paper account.

Enable the API:
- Configure → API → Settings
- Check **Enable ActiveX and Socket Clients**
- Set socket port to **7497**
- Check **Allow connections from localhost only**

### Step 6 — Start the broker bridge

```bash
source .venv/bin/activate
uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787
```

Verify it is working:

```bash
curl -s -H "Authorization: Bearer $(grep BRIDGE_TOKEN .env | cut -d= -f2)" http://127.0.0.1:8787/broker/account
```

Should return JSON with your NLV and account details.

### Step 7 — Authenticate with Azure

```bash
az login
```

### Step 8 — Test the orchestrator

```bash
cd orchestrator
FORCE_RUN=true python orchestrator.py
```

Review the output and logs to confirm research and trading agents respond correctly.

### Step 9 — Start the scheduler

```bash
cd orchestrator
python scheduler.py
```

Runs the orchestrator at **9:35, 11:35, 13:35, 15:35 ET** on weekdays. Keep the terminal open and your laptop awake.

---

## 3. Deploying on a VM

> Assumes you have a Linux VM (Ubuntu 24.04 recommended) with SSH access. Steps are cloud-agnostic.

### Step 1 — SSH into the VM

```bash
ssh -i ~/path/to/your_key.pem user@<VM_IP>
```

### Step 2 — Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git openjdk-11-jdk
sudo apt install -y xfce4 xfce4-goodies tightvncserver
```

Java is required for IB Gateway. VNC is required for the IB Gateway GUI.

### Step 3 — Set up VNC

```bash
vncserver :1
```

Set a VNC password when prompted. Answer `n` to the view-only password prompt.

Open port **5901** in your VM firewall or security group (TCP, restricted to your IP).

Connect from your Mac: `Cmd+K` in Finder → `vnc://<VM_IP>:5901`

### Step 4 — Clone the repo

```bash
cd ~ && git clone https://github.com/daniyalshahzad/automated-trading.git automatedtrading
cd automatedtrading
```

### Step 5 — Create virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 6 — Configure environment

```bash
cp .env.sample .env
nano .env
```

Set `IB_PORT=4002` (IB Gateway default).

### Step 7 — Install Azure CLI and authenticate

```bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
az login
```

Follow the device code prompt in your browser.

### Step 8 — Install IB Gateway

```bash
cd ~
wget https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
chmod +x ibgateway-stable-standalone-linux-x64.sh
DISPLAY=:1 ./ibgateway-stable-standalone-linux-x64.sh
```

Connect via VNC to click through the installer GUI.

### Step 9 — Configure IB Gateway

Open IB Gateway via VNC and set:

- Login type: **IB API**
- Account: **Paper trading**
- Port: **4002**
- Allow connections from localhost only: **checked**
- Auto-restart: **enabled**

Login with your IBKR credentials and approve 2FA on your phone.

### Step 10 — Start the broker bridge

```bash
cd ~/automatedtrading
source .venv/bin/activate
uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787
```

Verify:

```bash
curl -s -H "Authorization: Bearer $(grep BRIDGE_TOKEN ~/automatedtrading/.env | cut -d= -f2)" http://127.0.0.1:8787/broker/account
```

### Step 11 — Test the orchestrator

```bash
cd ~/automatedtrading/orchestrator
source ../.venv/bin/activate
FORCE_RUN=true python orchestrator.py
```

### Step 12 — Start the scheduler

```bash
cd ~/automatedtrading/orchestrator
source ../.venv/bin/activate
python scheduler.py
```

Leave running. The VM will trade autonomously during market hours.

---

See [OPERATIONS.md](./OPERATIONS.md) for day-to-day commands, monitoring, and troubleshooting.

---
