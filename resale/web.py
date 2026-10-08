"""Separate test-only storefront. No production sales or legacy data migration."""
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import stripe
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from resale.pricing import parse_batch, proceeds, yen

PERIOD = 30 * 86400


def create_app(config=None):
    app = Flask(__name__, template_folder="templates")
    app.config.update(
        SECRET_KEY=os.getenv("RESALE_SECRET_KEY"),
        DATABASE=os.getenv("RESALE_DATABASE", str(Path.cwd()/"resale-private"/"workbench.sqlite3")),
        SESSION_COOKIE_NAME="resale_session", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=100_000,
        STRIPE_KEY=os.getenv("RESALE_STRIPE_TEST_KEY", ""),
        WEBHOOK_SECRET=os.getenv("RESALE_STRIPE_TEST_WEBHOOK_SECRET", ""),
        PUBLIC_URL=os.getenv("RESALE_PUBLIC_URL", ""),
        PRICE_YEN=980, NOW=time.time,
    )
    app.config.update(config or {})
    if not app.secret_key or len(app.secret_key) < 32:
        raise RuntimeError("RESALE_SECRET_KEY must contain at least 32 characters")
    # Hard guard: this prototype cannot initiate live payments.
    if app.config["STRIPE_KEY"] and not app.config["STRIPE_KEY"].startswith("sk_test_"):
        raise RuntimeError("Only Stripe test keys are supported")
    database = Path(app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if database.is_symlink():
        raise RuntimeError("Database cannot be a symlink")

    @contextmanager
    def db():
        conn = sqlite3.connect(database, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT UNIQUE, password TEXT);
        CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, owner TEXT, payload TEXT,
          status TEXT NOT NULL DEFAULT '準備中', sold_price INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, owner TEXT, checkout TEXT UNIQUE,
          amount INTEGER, intent TEXT, expires INTEGER, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS blocked_intents(intent TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS attempts(key TEXT, created INTEGER);
        """)
    os.chmod(database, 0o600)

    def now():
        return int(app.config["NOW"]())

    def user_id():
        uid = session.get("uid")
        with db() as conn:
            if not uid or not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                abort(401)
        return uid

    def access(uid):
        with db() as conn:
            return conn.execute("SELECT MAX(expires) FROM orders WHERE owner=? AND revoked=0", (uid,)).fetchone()[0] or 0

    def paid_user():
        uid = user_id()
        if access(uid) <= now():
            abort(403, description="利用期間が終了しています。記録の閲覧・書き出しは可能です。")
        return uid

    @app.before_request
    def csrf():
        if request.method == "POST" and request.endpoint != "webhook":
            supplied = request.form.get("csrf", "")
            if not supplied or not secrets.compare_digest(supplied, session.get("csrf", "")):
                abort(400, description="画面を開き直してから操作してください")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.context_processor
    def globals_():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf": session["csrf"], "signed_in": bool(session.get("uid"))}

    @app.route("/")
    def home():
        return render_template("home.html")

    @app.route("/account", methods=["GET", "POST"])
    def account():
        if request.method == "POST":
            key = request.remote_addr or "unknown"
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM attempts WHERE created<?", (now()-900,))
                if conn.execute("SELECT COUNT(*) FROM attempts WHERE key=?", (key,)).fetchone()[0] >= 10:
                    abort(429)
                conn.execute("INSERT INTO attempts VALUES (?,?)", (key, now()))
            name, password = request.form.get("name", ""), request.form.get("password", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", name) or not 12 <= len(password) <= 128:
                flash("ユーザー名は半角英数字等3〜40文字、パスワードは12〜128文字で入力してください")
                return render_template("account.html"), 400
            with db() as conn:
                row = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
                if request.form.get("action") == "register" and row is None:
                    uid = secrets.token_hex(16)
                    conn.execute("INSERT INTO users VALUES (?,?,?)", (uid, name, generate_password_hash(password)))
                elif request.form.get("action") == "login" and row and check_password_hash(row["password"], password):
                    uid = row["id"]
                else:
                    flash("登録またはログインできませんでした。入力内容を確認してください")
                    return render_template("account.html"), 400
            session.clear()
            session["uid"] = uid
            return redirect(url_for("workbench"))
        return render_template("account.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/workbench")
    def workbench():
        uid = user_id()
        with db() as conn:
            records = conn.execute("SELECT * FROM items WHERE owner=? ORDER BY created DESC, id", (uid,)).fetchall()
        items, actual_total = [], 0
        for row in records:
            item = json.loads(row["payload"])
            item.update(id=row["id"], status=row["status"], sold_price=row["sold_price"])
            item["actual_net"] = None if row["sold_price"] is None else proceeds(row["sold_price"], item["shipping"], item["packing"])
            if item["actual_net"] is not None:
                actual_total += item["actual_net"]
            items.append(item)
        return render_template("workbench.html", items=items, active=access(uid)>now(), actual_total=actual_total)

    @app.post("/batch")
    def batch():
        uid = paid_user()
        try:
            items = parse_batch(request.form.get("csv", ""))
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("workbench"))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM items WHERE owner=?", (uid,)).fetchone()[0] + len(items) > 500:
                abort(400, description="保存上限は500商品です")
            conn.executemany("INSERT INTO items(id,owner,payload,created) VALUES (?,?,?,?)", [
                (secrets.token_hex(16), uid, json.dumps(item, ensure_ascii=False), now()) for item in items])
        flash(f"{len(items)}商品を登録しました")
        return redirect(url_for("workbench"))

    @app.post("/items/<item_id>")
    def update_item(item_id):
        uid = paid_user()
        status = request.form.get("status", "")
        if status not in {"準備中", "出品中", "売却済み"}:
            abort(400)
        try:
            sold = yen(request.form.get("sold_price"), "売却額") if status == "売却済み" else None
            if sold is not None and sold < 300:
                raise ValueError("売却額は300円以上です")
        except ValueError as exc:
            abort(400, description=str(exc))
        with db() as conn:
            result = conn.execute("UPDATE items SET status=?,sold_price=? WHERE id=? AND owner=?", (status, sold, item_id, uid))
            if not result.rowcount:
                abort(404)
        flash("売却状況を保存しました")
        return redirect(url_for("workbench"))

    @app.post("/items/<item_id>/delete")
    def delete_item(item_id):
        uid = user_id()
        with db() as conn:
            if not conn.execute("DELETE FROM items WHERE id=? AND owner=?", (item_id, uid)).rowcount:
                abort(404)
        flash("商品を削除しました")
        return redirect(url_for("workbench"))

    @app.get("/export")
    def export():
        uid = user_id()
        with db() as conn:
            records = conn.execute("SELECT payload,status,sold_price FROM items WHERE owner=?", (uid,)).fetchall()
        # JSON avoids spreadsheet formula injection and preserves multiline text.
        data = [dict(json.loads(row["payload"]), status=row["status"], sold_price=row["sold_price"]) for row in records]
        return app.response_class(json.dumps(data, ensure_ascii=False), mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="my-products.json"'})

    @app.post("/checkout")
    def checkout():
        uid = user_id()
        base = urlsplit(app.config["PUBLIC_URL"])
        if not app.config["STRIPE_KEY"] or not app.config["WEBHOOK_SECRET"] or base.scheme != "https" or not base.netloc or base.path not in {"", "/"} or base.username or base.query or base.fragment:
            abort(503, description="試作版です。決済テストはまだ設定されていません")
        if access(uid)>now():
            abort(409, description="利用期間中です。期限後に延長できます")
        order = secrets.token_hex(16)
        with db() as conn:
            conn.execute("INSERT INTO orders(id,owner,amount) VALUES (?,?,?)", (order, uid, app.config["PRICE_YEN"]))
        try:
            result = stripe.checkout.Session.create(
                api_key=app.config["STRIPE_KEY"], idempotency_key=order,
                mode="payment", payment_method_types=["card"],
                client_reference_id=order, metadata={"order": order},
                line_items=[{"price_data": {"currency": "jpy", "unit_amount": app.config["PRICE_YEN"],
                    "product_data": {"name": "出品・手残り管理 30日間（テスト）"}}, "quantity": 1}],
                success_url=app.config["PUBLIC_URL"].rstrip("/")+"/workbench",
                cancel_url=app.config["PUBLIC_URL"].rstrip("/")+"/workbench")
        except stripe.StripeError:
            abort(503, description="決済準備に失敗しました。利用開始はしていません")
        checkout_url = urlsplit(result.url or "")
        if result.livemode or checkout_url.scheme != "https" or checkout_url.hostname != "checkout.stripe.com":
            abort(503)
        with db() as conn:
            conn.execute("UPDATE orders SET checkout=? WHERE id=?", (result.id, order))
        return redirect(result.url, code=303)

    @app.post("/stripe/webhook")
    def webhook():
        if not app.config["WEBHOOK_SECRET"]:
            abort(503)
        try:
            stripe.Webhook.construct_event(request.get_data(), request.headers.get("Stripe-Signature", ""), app.config["WEBHOOK_SECRET"])
            event = json.loads(request.get_data())
        except (ValueError, stripe.SignatureVerificationError):
            abort(400)
        if event.get("livemode") is not False:
            abort(400)
        obj = event["data"]["object"]
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM events WHERE id=?", (event["id"],)).fetchone():
                return {"ok": True}
            if event["type"] in {"charge.refunded", "charge.dispute.created"}:
                intent = obj.get("payment_intent")
                # Conservative hold also applies to partial refunds and disputes.
                if isinstance(intent, str):
                    conn.execute("INSERT OR IGNORE INTO blocked_intents VALUES (?)", (intent,))
                    conn.execute("UPDATE orders SET revoked=1 WHERE intent=?", (intent,))
            elif event["type"] == "checkout.session.completed" and obj.get("payment_status") == "paid":
                order = conn.execute("SELECT * FROM orders WHERE id=?", (obj.get("client_reference_id"),)).fetchone()
                if order:
                    if order["checkout"] is None:
                        # Checkout event can race with the API response; ask Stripe to retry.
                        abort(503)
                    intent = obj.get("payment_intent")
                    if obj["id"] != order["checkout"] or obj.get("mode") != "payment" or obj.get("currency") != "jpy" or obj.get("amount_total") != order["amount"] or not isinstance(intent, str):
                        abort(400)
                    blocked = bool(conn.execute("SELECT 1 FROM blocked_intents WHERE intent=?", (intent,)).fetchone())
                    # Session id is independently deduplicated; multiple event IDs never extend access.
                    if order["expires"] is None:
                        conn.execute("UPDATE orders SET intent=?,expires=?,revoked=? WHERE id=?", (intent, int(event["created"])+PERIOD, int(blocked), order["id"]))
            conn.execute("INSERT INTO events VALUES (?)", (event["id"],))
        return {"ok": True}

    return app
