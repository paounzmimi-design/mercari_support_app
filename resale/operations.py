"""One operations tick: recover test payments, then check local health."""
import json
import os

from resale.monitor import inspect
from resale.reconcile import reconcile


def main():
    try:
        database=os.environ["RESALE_DATABASE"]
        payments=reconcile(database,os.environ.get("RESALE_STRIPE_TEST_KEY",""))
        health=inspect(database,"http://127.0.0.1:5200/healthz")
        result={"ok":payments["ok"] and health["ok"],"payments":payments,"health":health}
    except Exception:
        result={"ok":False,"state":"configuration_or_database_error"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__=="__main__":
    raise SystemExit(main())
