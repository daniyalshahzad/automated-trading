from ib_insync import IB
from datetime import datetime, timezone

HOST = "127.0.0.1"
PORT = 7497          # TWS paper default (often 7497). If using IB Gateway paper, often 4002.
CLIENT_ID = 11       # change if “clientId already in use”

def main():
    ib = IB()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Connecting to {HOST}:{PORT} (clientId={CLIENT_ID})...")

    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=5)
    except Exception as e:
        print("❌ connect() failed:", repr(e))
        print("\nChecklist:")
        print("- Is TWS open and logged into PAPER?")
        print("- TWS → Global Configuration → API → Settings:")
        print("  - Enable ActiveX and Socket Clients = ON")
        print("  - Socket port matches PORT")
        print("- If 'localhost only' enabled, HOST must be 127.0.0.1")
        return

    print("✅ Connected:", ib.isConnected())
    print("Managed accounts:", ib.managedAccounts())

    summary = ib.accountSummary()
    # Print a few key tags to confirm data flow
    wanted = {"NetLiquidation", "AvailableFunds", "BuyingPower"}
    for row in summary:
        if row.tag in wanted and row.currency in ("USD", ""):
            print(f"{row.tag} [{row.currency}]: {row.value}")

    ib.disconnect()
    print("Disconnected.")

if __name__ == "__main__":
    main()