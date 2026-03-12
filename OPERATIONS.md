# AutoTrader Operations

Day-to-day commands, monitoring, and troubleshooting for AutoTrader.

---

## Table of Contents

1. [Daily Startup (VM)](#1-daily-startup-vm)
2. [Monitoring](#2-monitoring)
3. [Deploying Code Updates](#3-deploying-code-updates)
4. [Troubleshooting](#4-troubleshooting)
5. [VS Code Remote SSH](#5-vs-code-remote-ssh)
6. [Quick Reference](#6-quick-reference)

---

## 1. Daily Startup (VM)

Follow this sequence each trading day if services are not already running.

**1. SSH into VM**
```bash
ssh -i ~/path/to/your_key.pem user@<VM_IP>
```

**2. Start VNC (if not running)**
```bash
vncserver :1
```

**3. Open IB Gateway via VNC**

Connect from Mac: `Cmd+K` → `vnc://<VM_IP>:5901`

Launch IB Gateway from the desktop. Login with IBKR credentials → IB API → Paper account → port 4002. Approve 2FA on phone.

**4. Start the broker bridge**
```bash
cd ~/automatedtrading && source .venv/bin/activate
nohup uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787 > ~/bridge.log 2>&1 &
```

**5. Verify bridge is working**
```bash
curl -s -H "Authorization: Bearer $(grep BRIDGE_TOKEN ~/automatedtrading/.env | cut -d= -f2)" http://127.0.0.1:8787/broker/account
```

Should return JSON with NLV and account details.

**6. Start scheduler (new terminal tab)**
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
nohup python scheduler.py > ~/scheduler.log 2>&1 &
```

Leave running. Fires at **9:35, 11:35, 13:35, 15:35 ET** on weekdays.

---

## 2. Monitoring

**Watch live logs**
```bash
tail -f ~/automatedtrading/orchestrator/logs/orchestrator_$(date +%Y%m%d).log
```

**Force run orchestrator manually**
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
FORCE_RUN=true python orchestrator.py
```

**Generate PDF report**
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
python report.py
```

**Check scheduler is running**
```bash
ps aux | grep scheduler.py
```

**Check bridge is alive**
```bash
curl -s -H "Authorization: Bearer $(grep BRIDGE_TOKEN ~/automatedtrading/.env | cut -d= -f2)" http://127.0.0.1:8787/broker/account
```

**Inspect trading memory**
```bash
cat ~/automatedtrading/orchestrator/trading_memory.json | python3 -m json.tool | head -100
```

---

## 3. Deploying Code Updates

**On your Mac — commit and push**
```bash
cd ~/path/to/automated-trading
git add .
git commit -m "your message"
git push
```

**On the VM — pull latest**
```bash
cd ~/automatedtrading
git pull
```

> `trading_memory.json`, `.env`, `logs/`, `research_logs/`, and `reports/` are gitignored and will not be affected by `git pull`.

After pulling, restart the bridge and scheduler if orchestrator.py or broker_bridge changed.

---

## 4. Troubleshooting

### IB Gateway Lost Session / Needs Re-Auth

1. Connect via VNC: `vnc://<VM_IP>:5901`
2. Re-login to IB Gateway with IBKR credentials
3. Approve 2FA on phone
4. Restart bridge:
```bash
cd ~/automatedtrading && source .venv/bin/activate
nohup uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787 > ~/bridge.log 2>&1 &
```

> IB Gateway is configured to auto-restart daily but may occasionally need manual intervention.

### Bridge Not Responding

1. Check IB Gateway is running and connected via VNC
2. Restart bridge:
```bash
cd ~/automatedtrading && source .venv/bin/activate
nohup uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787 > ~/bridge.log 2>&1 &
```

### Scheduler Crashed
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
nohup python scheduler.py > ~/scheduler.log 2>&1 &
```

### Azure Auth Expired
```bash
az login
```

Token lasts approximately 90 days. Re-run if the orchestrator fails with 401 or 403 errors.

### VNC Not Connecting

- Check port 5901 is open in your VM firewall / security group
- Restart VNC if not running: `vncserver :1`

### Orchestrator Sold Everything Unexpectedly

Check logs immediately for validation warnings:
```bash
grep "validate" ~/automatedtrading/orchestrator/logs/orchestrator_$(date +%Y%m%d).log
```

Look for lines like `[validate] zeroing target` which indicate a hard rule triggered and zeroed a position target.

---

## 5. VS Code Remote SSH

Connect to the VM directly in VS Code to browse files and edit code without SCP.

**SSH config** (`~/.ssh/config`):
```
Host autotrader-vm
    HostName <VM_IP>
    User <VM_USER>
    IdentityFile ~/path/to/your_key.pem
```

`Cmd+Shift+P` → **Remote-SSH: Connect to Host** → `autotrader-vm`

---

## 6. Quick Reference

### Scheduler Times (ET)

| Run | Time |
|---|---|
| Morning | 9:35 AM |
| Midday | 11:35 AM |
| Afternoon | 1:35 PM |
| Late afternoon | 3:35 PM |

### Key Config (orchestrator.py)

| Constant | Value | Purpose |
|---|---|---|
| MAX_POSITIONS | 3 | Max simultaneous stock positions |
| MIN_NEW_POSITION_PCT | 10% | Minimum size for new position |
| MAX_SINGLE_NAME_PCT | 80% | Max allocation to one stock |
| CASH_BUFFER_PCT | 2% | Slippage buffer on market orders |
| MIN_REBALANCE_DELTA_PCT | 2% | Min delta to place an order |
| ALLOWED_EXCHANGE | NASDAQ | Only NASDAQ stocks allowed |
| SWING_TARGET_DAYS | 7 | Target holding period |
| SWING_MAX_DAYS | 14 | Max holding period guidance |

### File Locations (VM)

| File | Path |
|---|---|
| Orchestrator | `~/automatedtrading/orchestrator/orchestrator.py` |
| Scheduler | `~/automatedtrading/orchestrator/scheduler.py` |
| Report generator | `~/automatedtrading/orchestrator/report.py` |
| Broker bridge | `~/automatedtrading/broker_bridge/app.py` |
| Config / secrets | `~/automatedtrading/.env` |
| Trading memory | `~/automatedtrading/orchestrator/trading_memory.json` |
| Daily logs | `~/automatedtrading/orchestrator/logs/orchestrator_YYYYMMDD.log` |
| Research logs | `~/automatedtrading/orchestrator/research_logs/` |
| PDF reports | `~/automatedtrading/orchestrator/reports/` |

---