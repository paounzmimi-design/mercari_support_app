"""Recover missing test payment notifications using authenticated Stripe reads."""
import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import stripe

PERIOD = 30 * 86400


def as_dict(value):
    return value if isinstance(value, dict) else value.to_dict()


def reconcile(database, key, now=None, api=stripe):
    if not key.startswith("sk_test_"):
        raise ValueError("Only a Stripe test secret key is supported")
    if not Path(database).is_file():
        raise ValueError("Existing prototype database required")
    checked = int(time.time() if now is None else now)
    result = {"recovered":0,"held":0,"closed":0,"pending":0,"failed":0}
    with sqlite3.connect(database,timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        orders = conn.execute("SELECT * FROM orders WHERE closed=0 AND (expires IS NULL OR (expires>? AND revoked=0)) LIMIT 100",(checked,)).fetchall()
    for order in orders:
        try:
            checkout = order["checkout"]
            if not checkout:
                if not order["created"]:
                    raise ValueError("Creation time unknown")
                # The create API may have succeeded before its response was lost.
                listing = as_dict(api.checkout.Session.list(api_key=key,limit=100,
                    created={"gte":order["created"]-60,"lte":order["created"]+3600}))
                candidates = [as_dict(candidate) for candidate in listing.get("data",[])]
                matches = [candidate for candidate in candidates if candidate.get("client_reference_id")==order["id"]]
                if listing.get("has_more") is not False or len(matches)!=1:
                    # Missing/ambiguous result is not proof of an unpaid purchase.
                    raise ValueError("Checkout cannot be identified")
                checkout = matches[0]["id"]
            remote = as_dict(api.checkout.Session.retrieve(checkout,api_key=key))
            if (remote.get("livemode") is not False or remote.get("id") != checkout
                or remote.get("client_reference_id") != order["id"] or remote.get("mode") != "payment"
                or remote.get("amount_total") != order["amount"] or remote.get("currency") != "jpy"):
                raise ValueError("Checkout mismatch")
            action, intent, expiry = "pending", None, None
            if remote.get("status") == "expired" and remote.get("payment_status") == "unpaid":
                action = "closed"
            elif remote.get("status") == "complete" and remote.get("payment_status") == "paid":
                intent = remote.get("payment_intent")
                if not isinstance(intent,str):
                    raise ValueError("Missing payment")
                payment = as_dict(api.PaymentIntent.retrieve(intent,api_key=key,expand=["latest_charge"]))
                charge = as_dict(payment["latest_charge"])
                if (payment.get("id") != intent or payment.get("livemode") is not False
                    or payment.get("status") != "succeeded" or payment.get("amount_received") != order["amount"]
                    or payment.get("currency") != "jpy" or charge.get("payment_intent") != intent
                    or charge.get("livemode") is not False or charge.get("paid") is not True
                    or charge.get("amount") != order["amount"] or charge.get("currency") != "jpy"
                    or type(charge.get("created")) is not int or charge["created"] > checked
                    or charge.get("created",0) <= 0 or type(charge.get("amount_refunded")) is not int
                    or type(charge.get("disputed")) is not bool):
                    raise ValueError("Payment mismatch")
                action = "held" if charge["amount_refunded"]>0 or charge["disputed"] else "recovered"
                expiry = charge["created"]+PERIOD
            with sqlite3.connect(database,timeout=15) as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute("SELECT expires,revoked,closed FROM orders WHERE id=?",(order["id"],)).fetchone()
                if current is None:
                    raise ValueError("Order missing")
                conn.execute("UPDATE orders SET checkout=COALESCE(checkout,?) WHERE id=?",(checkout,order["id"]))
                if action == "closed" and current[0] is None:
                    conn.execute("UPDATE orders SET closed=1 WHERE id=?",(order["id"],))
                    result["closed"] += 1
                elif action in {"held","recovered"}:
                    blocked = conn.execute("SELECT 1 FROM blocked_intents WHERE intent=?",(intent,)).fetchone()
                    held = action=="held" or bool(blocked) or bool(current[1])
                    if held:
                        conn.execute("INSERT OR IGNORE INTO blocked_intents VALUES (?)",(intent,))
                    # COALESCE never extends an already-granted right. A refund wins concurrent races.
                    conn.execute("UPDATE orders SET intent=?,expires=COALESCE(expires,?),revoked=MAX(revoked,?) WHERE id=?",(intent,expiry,int(held),order["id"]))
                    if held:
                        result["held"] += 1
                    elif current[0] is None:
                        result["recovered"] += 1
                else:
                    result["pending"] += 1
        except Exception:
            # Do not print provider exception bodies (may contain identifiers/secrets).
            result["failed"] += 1
    result["ok"] = result["failed"]==0
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--database",required=True)
    args=parser.parse_args()
    try:
        result=reconcile(args.database,os.environ.get("RESALE_STRIPE_TEST_KEY",""))
    except Exception:
        result={"ok":False,"state":"configuration_or_database_error"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__=="__main__":
    raise SystemExit(main())
