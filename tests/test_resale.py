import hashlib
import hmac
import json
import sqlite3

import pytest

from resale.pricing import MAX_PRICE, minimum_price, parse_batch, proceeds
from resale.web import PERIOD, create_app

CSV = "商品名,状態,売値,送料,梱包費,仕入れ値,希望手残り,補足\n本,傷あり,1200,230,30,,500,角に傷\n"


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "x"*40,
        "DATABASE": str(tmp_path/"db.sqlite3"), "SESSION_COOKIE_SECURE": False,
        "NOW": lambda: 1_000_000, "WEBHOOK_SECRET": "whsec_fixture"})


def token(client):
    client.get("/account")
    with client.session_transaction() as session:
        return session["csrf"]


def register(client, name="tester"):
    response = client.post("/account", data={"csrf": token(client), "name": name,
        "password": "long-test-password", "action": "register"})
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session["uid"]


def sql(app, statement, values=()):
    with sqlite3.connect(app.config["DATABASE"]) as db:
        return db.execute(statement, values).fetchall()


def order(app, uid, checkout="cs_test_fixture", order_id="order1"):
    sql(app, "INSERT INTO orders(id,owner,checkout,amount) VALUES (?,?,?,980)", (order_id, uid, checkout))


def event(client, app, event_type="checkout.session.completed", event_id="evt1", live=False, **overrides):
    obj = dict(id="cs_test_fixture", client_reference_id="order1", amount_total=980,
               currency="jpy", mode="payment", payment_status="paid", payment_intent="pi_fixture")
    obj.update(overrides)
    payload = json.dumps(dict(id=event_id, type=event_type, created=1_000_000,
                              livemode=live, data={"object": obj})).encode()
    import time
    timestamp = int(time.time())
    signature = hmac.new(app.config["WEBHOOK_SECRET"].encode(), str(timestamp).encode()+b"."+payload, hashlib.sha256).hexdigest()
    return client.post("/stripe/webhook", data=payload,
        headers={"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"})


@pytest.mark.parametrize("shipping,packing,cost,target", [(230,30,0,500),(800,100,4000,1000),(0,0,0,0),(1,0,0,300)])
def test_minimum_is_exact(shipping, packing, cost, target):
    minimum = minimum_price(shipping, packing, cost, target)
    assert proceeds(minimum, shipping, packing, cost) >= target
    if minimum > 300:
        assert proceeds(minimum-1, shipping, packing, cost) < target


def test_unreachable_and_unknown_cost():
    assert minimum_price(MAX_PRICE,0,0,MAX_PRICE) is None
    item = parse_batch(CSV)[0]
    assert item["net"] == 820 and item["profit"] is None
    assert "匿名" not in item["description"] and "発送" not in item["description"]


@pytest.mark.parametrize("csv", [CSV.replace("1200", "-1"), CSV.replace("1200", "nan"),
    CSV.replace("1200", "1e9"), CSV.replace("1200", "１２３４"), CSV.replace("1200", "9999999999999999999999"),
    "商品名,状態,売値\n本,傷あり,1200", CSV.replace("本,傷あり", ",傷あり")])
def test_bad_batch_rejected(csv):
    with pytest.raises(ValueError):
        parse_batch(csv)


def test_batch_limit():
    with pytest.raises(ValueError):
        parse_batch(CSV+CSV.splitlines()[1]+"\n"+ (CSV.splitlines()[1]+"\n")*49)


def test_requires_paid_access_and_csrf(app):
    client = app.test_client()
    assert client.get("/workbench").status_code == 401
    uid = register(client)
    assert client.post("/batch", data={"csv": CSV}).status_code == 400
    assert client.post("/batch", data={"csrf": token(client), "csv": CSV}).status_code == 403
    assert sql(app,"SELECT COUNT(*) FROM items")[0][0] == 0
    assert client.post("/checkout", data={"csrf": token(client)}).status_code == 503


def test_purchase_batch_sale_export_and_expiry(app):
    client = app.test_client()
    uid = register(client)
    order(app,uid)
    assert event(client,app).status_code == 200
    assert client.post("/batch", data={"csrf": token(client),"csv":CSV}).status_code == 302
    item = sql(app,"SELECT id FROM items")[0][0]
    assert client.post(f"/items/{item}", data={"csrf":token(client),"status":"売却済み","sold_price":"1000"}).status_code == 302
    assert "640円" in client.get("/workbench").get_data(as_text=True)
    assert client.get("/export").json[0]["sold_price"] == 1000
    app.config["NOW"] = lambda: 1_000_000+PERIOD
    assert client.post("/batch",data={"csrf":token(client),"csv":CSV}).status_code == 403
    assert client.get("/export").status_code == 200
    assert client.post(f"/items/{item}/delete",data={"csrf":token(client)}).status_code == 302


def test_duplicate_event_and_duplicate_session_never_extend(app):
    client=app.test_client(); uid=register(client); order(app,uid)
    for event_id in ["evt1","evt1","evt2"]:
        assert event(client,app,event_id=event_id).status_code == 200
    assert sql(app,"SELECT expires FROM orders")[0][0] == 1_000_000+PERIOD


@pytest.mark.parametrize("overrides", [{"amount_total":1},{"currency":"usd"},{"id":"wrong_session"},{"mode":"subscription"}])
def test_mismatched_payment_never_grants(app,overrides):
    client=app.test_client(); uid=register(client); order(app,uid)
    assert event(client,app,**overrides).status_code == 400
    assert sql(app,"SELECT expires FROM orders")[0][0] is None


def test_unpaid_and_bad_signature(app):
    client=app.test_client(); uid=register(client); order(app,uid)
    assert event(client,app,payment_status="unpaid").status_code == 200
    assert sql(app,"SELECT expires FROM orders")[0][0] is None
    assert client.post("/stripe/webhook",data=b"{}",headers={"Stripe-Signature":"bad"}).status_code == 400


@pytest.mark.parametrize("reverse", [True,False])
def test_refund_before_or_after_payment_holds_access(app,reverse):
    client=app.test_client(); uid=register(client); order(app,uid)
    refund=lambda: event(client,app,"charge.refunded","evt_refund")
    payment=lambda: event(client,app)
    for operation in ([refund,payment] if reverse else [payment,refund]):
        assert operation().status_code == 200
    assert sql(app,"SELECT revoked FROM orders")[0][0] == 1
    assert client.post("/batch",data={"csrf":token(client),"csv":CSV}).status_code == 403


def test_checkout_event_race_is_retryable(app):
    client=app.test_client(); uid=register(client); order(app,uid,checkout=None)
    assert event(client,app).status_code == 503
    assert sql(app,"SELECT COUNT(*) FROM events")[0][0] == 0


def test_other_user_cannot_read_modify_or_delete_items(app):
    first=app.test_client(); uid=register(first); order(app,uid); event(first,app)
    first.post("/batch",data={"csrf":token(first),"csv":CSV})
    item=sql(app,"SELECT id FROM items")[0][0]
    second=app.test_client(); other=register(second,"other")
    sql(app,"INSERT INTO orders(id,owner,expires,amount) VALUES ('other',?,?,980)",(other,1_000_000+PERIOD))
    assert second.get("/export").json == []
    assert second.post(f"/items/{item}",data={"csrf":token(second),"status":"出品中"}).status_code == 404
    assert second.post(f"/items/{item}/delete",data={"csrf":token(second)}).status_code == 404


def test_xss_is_escaped_and_invalid_batch_is_atomic(app):
    client=app.test_client(); uid=register(client); order(app,uid); event(client,app)
    client.post("/batch",data={"csrf":token(client),"csv":CSV.replace("本,", "<script>alert(1)</script>,")})
    html=client.get("/workbench").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    client.post("/batch",data={"csrf":token(client),"csv":CSV+"bad,row,-1,0"})
    assert sql(app,"SELECT COUNT(*) FROM items")[0][0] == 1


def test_live_keys_and_weak_secret_refused(tmp_path):
    with pytest.raises(RuntimeError):
        create_app({"SECRET_KEY":"short","DATABASE":str(tmp_path/"no.db")})
    with pytest.raises(RuntimeError):
        create_app({"SECRET_KEY":"x"*40,"STRIPE_KEY":"sk_live_anything","DATABASE":str(tmp_path/"no.db")})


def test_live_event_refused(app):
    client=app.test_client(); uid=register(client); order(app,uid)
    assert event(client,app,live=True).status_code == 400
    assert sql(app,"SELECT expires FROM orders")[0][0] is None


def test_checkout_creates_trusted_order_and_return_does_not_grant(app,monkeypatch):
    from types import SimpleNamespace
    import stripe
    captured={}
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="cs_test_new",url="https://checkout.stripe.com/test",livemode=False)
    monkeypatch.setattr(stripe.checkout.Session,"create",create)
    app.config.update(STRIPE_KEY="sk_test_fixture",PUBLIC_URL="https://example.test")
    client=app.test_client(); register(client)
    assert client.post("/checkout",data={"csrf":token(client)}).status_code == 303
    assert "payment_method_types" not in captured
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 980
    assert sql(app,"SELECT checkout,expires FROM orders")[0] == ("cs_test_new",None)
    response = client.get("/workbench?session_id=cs_test_new")
    assert response.status_code == 200
    policy = response.headers["Content-Security-Policy"]
    form_action = next(part.strip() for part in policy.split(";") if part.strip().startswith("form-action "))
    assert form_action == "form-action 'self' https://checkout.stripe.com"
    assert client.post("/batch",data={"csrf":token(client),"csv":CSV}).status_code == 403


def test_checkout_failure_is_safe(app,monkeypatch):
    import stripe
    def create(**kwargs):
        raise stripe.APIConnectionError("test")
    monkeypatch.setattr(stripe.checkout.Session,"create",create)
    app.config.update(STRIPE_KEY="sk_test_fixture",PUBLIC_URL="https://example.test")
    client=app.test_client(); register(client)
    assert client.post("/checkout",data={"csrf":token(client)}).status_code == 503
    assert sql(app,"SELECT expires FROM orders")[0][0] is None


def test_login_throttle(app):
    client=app.test_client()
    csrf=token(client)
    for index in range(10):
        assert client.post("/account",data={"csrf":csrf,"action":"login","name":"tester","password":"wrong-password"}).status_code == 400
    assert client.post("/account",data={"csrf":csrf,"action":"login","name":"tester","password":"wrong-password"}).status_code == 429


def test_single_and_edit_recalculate_without_losing_sale(app):
    client=app.test_client(); uid=register(client); order(app,uid); event(client,app)
    fields={"商品名":"本","状態":"傷あり","売値":"1200","送料":"230","梱包費":"30","仕入れ値":"","希望手残り":"500","補足":"角に傷"}
    assert client.post("/single",data=dict(fields,csrf=token(client))).status_code == 302
    item=sql(app,"SELECT id FROM items")[0][0]
    client.post(f"/items/{item}",data={"csrf":token(client),"status":"売却済み","sold_price":"1000"})
    assert client.get(f"/items/{item}/edit").status_code == 200
    assert client.post(f"/items/{item}/edit",data=dict(fields,csrf=token(client),送料="330")).status_code == 302
    record=client.get("/export").json[0]
    assert record["net"] == 720 and record["status"] == "売却済み" and record["sold_price"] == 1000
    assert "540円" in client.get("/workbench").get_data(as_text=True)
    assert client.post(f"/items/{item}/edit",data=dict(fields,csrf=token(client),売値="1")).status_code == 400
    assert client.get("/export").json[0]["net"] == 720


def test_recovery_rotates_code_and_invalidates_old_sessions(app):
    import re
    client=app.test_client()
    response=client.post("/account",data={"csrf":token(client),"name":"tester","password":"long-test-password","action":"register"})
    code=re.search(r'id="recovery-code" value="([^"]+)"',response.get_data(as_text=True)).group(1)
    assert len(code)==43
    assert code not in str(sql(app,"SELECT * FROM users"))
    with client.session_transaction() as saved:
        old_session=dict(saved)
    stale=app.test_client()
    with stale.session_transaction() as saved:
        saved.update(old_session)
    recovery=app.test_client()
    response=recovery.post("/recover",data={"csrf":token(recovery),"name":"tester","recovery":code,"password":"new-long-password","password_confirm":"new-long-password"})
    assert response.status_code==200
    new_code=re.search(r'id="recovery-code" value="([^"]+)"',response.get_data(as_text=True)).group(1)
    assert new_code != code
    assert stale.get("/workbench").status_code==401
    assert recovery.get("/workbench").status_code==200
    assert recovery.post("/recover",data={"csrf":token(recovery),"name":"tester","recovery":code,"password":"another-long-password","password_confirm":"another-long-password"}).status_code==400
    other=app.test_client()
    assert other.post("/account",data={"csrf":token(other),"name":"tester","password":"long-test-password","action":"login"}).status_code==400
    assert other.post("/account",data={"csrf":token(other),"name":"tester","password":"new-long-password","password_confirm":"new-long-password","action":"login"}).status_code==302


def test_invalid_recovery_never_changes_account(app):
    client=app.test_client(); register(client)
    original=sql(app,"SELECT password,recovery_hash,auth_version FROM users")
    assert client.post("/recover",data={"csrf":token(client),"name":"tester","recovery":"a"*43,"password":"new-long-password","password_confirm":"new-long-password"}).status_code==400
    assert sql(app,"SELECT password,recovery_hash,auth_version FROM users")==original
    assert client.post("/recover",data={"name":"tester","recovery":"a"*43,"password":"new-long-password","password_confirm":"new-long-password"}).status_code==400


def test_edit_is_owner_scoped_and_expiry_blocks_new_mutations(app):
    first=app.test_client(); uid=register(first); order(app,uid); event(first,app)
    first.post("/batch",data={"csrf":token(first),"csv":CSV})
    item=sql(app,"SELECT id FROM items")[0][0]
    second=app.test_client(); other=register(second,"other")
    sql(app,"INSERT INTO orders(id,owner,expires,amount) VALUES ('other',?,?,980)",(other,1_000_000+PERIOD))
    assert second.get(f"/items/{item}/edit").status_code==404
    assert second.post(f"/items/{item}/edit",data={"csrf":token(second)}).status_code==404
    app.config["NOW"]=lambda:1_000_000+PERIOD
    assert first.get(f"/items/{item}/edit").status_code==403
    assert first.post("/single",data={"csrf":token(first)}).status_code==403


def test_legacy_prototype_database_migrates_without_reset(tmp_path):
    db=tmp_path/"legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT UNIQUE, password TEXT)")
        conn.execute("INSERT INTO users VALUES ('u','name','hash')")
    config={"SECRET_KEY":"x"*40,"DATABASE":str(db)}
    create_app(config); app=create_app(config)
    assert sql(app,"SELECT id,name,password,auth_version FROM users")==[("u","name","hash",0)]


def test_repeated_checkout_reuses_session(app,monkeypatch):
    import stripe
    from types import SimpleNamespace
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="cs_same",url="https://checkout.stripe.com/same",livemode=False)
    monkeypatch.setattr(stripe.checkout.Session,"create",create)
    app.config.update(STRIPE_KEY="sk_test_fixture",PUBLIC_URL="https://example.test")
    client=app.test_client(); register(client)
    for unused in range(3):
        response=client.post("/checkout",data={"csrf":token(client)})
        assert response.status_code==303 and response.location=="https://checkout.stripe.com/same"
    assert len(calls)==1 and sql(app,"SELECT COUNT(*) FROM orders")[0][0]==1


def test_failed_checkout_retry_reuses_idempotency_key(app,monkeypatch):
    import stripe
    from types import SimpleNamespace
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls)==1:
            raise stripe.APIConnectionError("lost response")
        return SimpleNamespace(id="cs_retry",url="https://checkout.stripe.com/retry",livemode=False)
    monkeypatch.setattr(stripe.checkout.Session,"create",create)
    app.config.update(STRIPE_KEY="sk_test_fixture",PUBLIC_URL="https://example.test")
    client=app.test_client(); register(client)
    assert client.post("/checkout",data={"csrf":token(client)}).status_code==503
    assert client.post("/checkout",data={"csrf":token(client)}).status_code==303
    assert calls[0]==calls[1]
    assert sql(app,"SELECT COUNT(*) FROM orders")[0][0]==1


def test_monitor_read_only_and_no_private_details(app,tmp_path):
    import io
    from resale.monitor import inspect
    class Response(io.BytesIO):
        status=200
    def healthy(*args,**kwargs):
        return Response(b'{"ok":true}')
    client=app.test_client(); uid=register(client)
    sql(app,"INSERT INTO orders(id,owner,amount,created) VALUES ('private-order',?,980,1)",(uid,))
    before=sql(app,"SELECT * FROM orders")
    result=inspect(app.config["DATABASE"],"http://localhost/healthz",now=1_000_000,opener=healthy)
    assert result["problems"]==["old_pending_orders"]
    assert "private-order" not in json.dumps(result) and uid not in json.dumps(result)
    assert sql(app,"SELECT * FROM orders")==before
    missing=tmp_path/"missing.db"
    result=inspect(missing,"http://localhost/healthz",opener=healthy)
    assert "database_unavailable" in result["problems"] and not missing.exists()


def test_health_and_monitor_failure(app):
    from resale.monitor import inspect
    assert app.test_client().get("/healthz").json=={"ok":True}
    def fail(*args,**kwargs):
        raise OSError("secret should not be logged")
    result=inspect(app.config["DATABASE"],"http://localhost/healthz",opener=fail)
    assert result["problems"]==["app_unreachable"]
    assert "secret" not in json.dumps(result)


def test_stale_checkout_does_not_allow_new_purchase(app,monkeypatch):
    import stripe
    from types import SimpleNamespace
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="cs_old_"+str(len(calls)),url="https://checkout.stripe.com/old",livemode=False)
    monkeypatch.setattr(stripe.checkout.Session,"create",create)
    app.config.update(STRIPE_KEY="sk_test_fixture",PUBLIC_URL="https://example.test")
    client=app.test_client(); register(client)
    assert client.post("/checkout",data={"csrf":token(client)}).status_code==303
    app.config["NOW"]=lambda:1_010_000
    assert client.post("/checkout",data={"csrf":token(client)}).status_code==409
    assert len(calls)==1
    sql(app,"UPDATE orders SET closed=1")
    assert client.post("/checkout",data={"csrf":token(client)}).status_code==303
    assert len(calls)==2


def test_recovery_confirmation_and_static_script(app):
    client = app.test_client()
    register(client)
    original = sql(app, "SELECT password,recovery_hash,auth_version FROM users")
    response = client.post("/recover", data={"csrf":token(client), "name":"tester", "recovery":"a"*43, "password":"new-long-password", "password_confirm":"different-password"})
    assert response.status_code == 400
    assert "一致しません" in response.get_data(as_text=True)
    assert sql(app, "SELECT password,recovery_hash,auth_version FROM users") == original
    assert b'new-long-password' not in response.data
    script = client.get("/static/copy.js")
    assert script.status_code == 200
    assert b'execCommand' in script.data
    assert b'data-save' in script.data
