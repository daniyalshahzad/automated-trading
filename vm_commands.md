# Azure VM — Command Cheat Sheet

## VM Details
- **IP**: 4.172.254.168
- **User**: azureuser
- **SSH Key**: `~/Desktop/azureuser_key.pem`

---

## Connect to VM

**SSH (terminal)**
```bash
ssh -i ~/Desktop/azureuser_key.pem azureuser@4.172.254.168
```

**VS Code Remote SSH**
`Cmd+Shift+P` → Remote-SSH: Connect to Host → `azure-vm`

**VNC (for IB Gateway UI)**
In Finder: `Cmd+K` → `vnc://4.172.254.168:5901`

---

## Startup Sequence (run in this order)

**1 — Start VNC server (if not running)**
```bash
vncserver :1
```

**2 — Start IB Gateway**
Connect via VNC and launch IB Gateway from the desktop.
Login with IBKR credentials → IB API → Paper account → port 4002.
Approve 2FA on your phone.

**3 — Start bridge**
```bash
cd ~/automatedtrading && source .venv/bin/activate
uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787
```

**4 — Start scheduler**
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
python scheduler.py
```

---

## Day-to-Day Commands

**Force run orchestrator manually**
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
FORCE_RUN=true python orchestrator.py
```

**Check live logs**
```bash
tail -f ~/automatedtrading/orchestrator/logs/orchestrator_$(date +%Y%m%d).log
```

**Check bridge is alive**
```bash
curl -s -H "Authorization: Bearer $(grep BRIDGE_TOKEN ~/automatedtrading/.env | cut -d= -f2)" http://127.0.0.1:8787/broker/account
```

**Check scheduler is running**
```bash
ps aux | grep scheduler.py
```

---

## If Things Go Wrong

**IB Gateway lost session / needs re-auth**
1. Connect via VNC
2. Re-login to IB Gateway
3. Approve 2FA on phone
4. Restart bridge (see above)

**Bridge crashed**
```bash
cd ~/automatedtrading && source .venv/bin/activate
uvicorn broker_bridge.app:app --host 127.0.0.1 --port 8787
```

**Scheduler crashed**
```bash
cd ~/automatedtrading/orchestrator && source ../.venv/bin/activate
python scheduler.py
```

**VNC not connecting**
Check Azure Portal → VM → Networking → inbound rule for port 5901 exists.

---

## File Locations
| File | Path |
|------|------|
| Orchestrator | `~/automatedtrading/orchestrator/orchestrator.py` |
| Scheduler | `~/automatedtrading/orchestrator/scheduler.py` |
| Memory | `~/automatedtrading/orchestrator/trading_memory.json` |
| Logs | `~/automatedtrading/orchestrator/logs/` |
| Research logs | `~/automatedtrading/orchestrator/research_logs/` |
| Reports | `~/automatedtrading/orchestrator/reports/` |
| Bridge | `~/automatedtrading/broker_bridge/app.py` |
| Config | `~/automatedtrading/.env` |