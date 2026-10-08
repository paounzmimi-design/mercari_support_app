"""Read-only local status check. Output contains no usernames, IDs or secrets."""
import argparse
import json
import sqlite3
import time
from pathlib import Path
from urllib.request import urlopen


def inspect(database, health_url, now=None, opener=urlopen):
    checked = int(time.time() if now is None else now)
    problems = []
    try:
        with opener(health_url, timeout=5) as response:
            if response.status != 200 or json.load(response).get("ok") is not True:
                problems.append("app_unhealthy")
    except Exception:
        problems.append("app_unreachable")
    pending = held = 0
    try:
        # Read-only URI: monitoring must never create or migrate a database.
        uri = Path(database).resolve().as_uri()+"?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                problems.append("database_check_failed")
            pending = conn.execute("SELECT COUNT(*) FROM orders WHERE expires IS NULL AND created<?", (checked-7200,)).fetchone()[0]
            held = conn.execute("SELECT COUNT(*) FROM orders WHERE revoked=1").fetchone()[0]
    except sqlite3.Error:
        problems.append("database_unavailable")
    if pending:
        # Could be abandoned carts, not proof of failed payments. Needs reconciliation.
        problems.append("old_pending_orders")
    return {"ok":not problems, "problems":problems,"old_pending_orders":pending,"held_orders":held}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--health-url", default="http://127.0.0.1:5200/healthz")
    args = parser.parse_args()
    result = inspect(args.database,args.health_url)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
