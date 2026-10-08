import sqlite3
from types import SimpleNamespace

import pytest

from resale.reconcile import PERIOD, reconcile
from resale.web import create_app


@pytest.fixture
def database(tmp_path):
    path=tmp_path/"test.sqlite3"
    create_app({"SECRET_KEY":"x"*40,"DATABASE":str(path)})
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO orders(id,owner,checkout,amount,created) VALUES ('order','owner','cs_test',980,900000)")
    return path


def remote(**changes):
    value=dict(id="cs_test",client_reference_id="order",livemode=False,mode="payment",
               amount_total=980,currency="jpy",status="complete",payment_status="paid",payment_intent="pi_test")
    value.update(changes)
    return value


def payment(**changes):
    value=dict(id="pi_test",livemode=False,status="succeeded",amount_received=980,currency="jpy",
        latest_charge=dict(payment_intent="pi_test",livemode=False,paid=True,amount=980,currency="jpy",created=910000,amount_refunded=0,disputed=False))
    value.update(changes)
    return value


def api(session=None,paid=None):
    return SimpleNamespace(checkout=SimpleNamespace(Session=SimpleNamespace(retrieve=lambda *a,**k:session or remote())),
        PaymentIntent=SimpleNamespace(retrieve=lambda *a,**k:paid or payment()))


def row(database):
    with sqlite3.connect(database) as conn:
        return conn.execute("SELECT expires,revoked,closed FROM orders").fetchone()


def test_notification_loss_recovers_without_extending(database):
    assert reconcile(database,"sk_test_fixture",now=1000000,api=api())["recovered"]==1
    assert row(database)==(910000+PERIOD,0,0)
    assert reconcile(database,"sk_test_fixture",now=1000001,api=api())["recovered"]==0
    assert row(database)==(910000+PERIOD,0,0)


@pytest.mark.parametrize("changes",[{"amount_total":1},{"currency":"usd"},{"client_reference_id":"other"},{"livemode":True},{"id":"other"}])
def test_bad_checkout_never_grants(database,changes):
    assert reconcile(database,"sk_test_fixture",now=1000000,api=api(session=remote(**changes)))["failed"]==1
    assert row(database)[0] is None


@pytest.mark.parametrize("changes",[{"amount_received":1},{"livemode":True},{"status":"processing"},{"currency":"usd"},{"latest_charge":None}])
def test_bad_payment_never_grants(database,changes):
    assert reconcile(database,"sk_test_fixture",now=1000000,api=api(paid=payment(**changes)))["failed"]==1
    assert row(database)[0] is None


@pytest.mark.parametrize("change",[{"amount_refunded":1},{"disputed":True}])
def test_refund_or_dispute_holds(database,change):
    paid=payment(); paid["latest_charge"].update(change)
    assert reconcile(database,"sk_test_fixture",now=1000000,api=api(paid=paid))["held"]==1
    assert row(database)[1]==1


def test_expired_unpaid_closes_only_pending(database):
    result=reconcile(database,"sk_test_fixture",now=1000000,api=api(session=remote(status="expired",payment_status="unpaid")))
    assert result["closed"]==1 and row(database)==(None,0,1)


def test_unknown_unpaid_stays_pending(database):
    result=reconcile(database,"sk_test_fixture",now=1000000,api=api(session=remote(status="open",payment_status="unpaid")))
    assert result["pending"]==1 and row(database)==(None,0,0)


def test_refund_race_wins_over_reconciliation(database):
    def retrieve(*args,**kwargs):
        with sqlite3.connect(database) as conn:
            conn.execute("INSERT INTO blocked_intents VALUES ('pi_test')")
        return payment()
    provider=api(); provider.PaymentIntent.retrieve=retrieve
    assert reconcile(database,"sk_test_fixture",now=1000000,api=provider)["held"]==1
    assert row(database)[1]==1


def test_network_failure_and_missing_session_fail_closed(database):
    provider=api()
    def fail(*args,**kwargs):
        raise RuntimeError("secret-provider-detail")
    provider.checkout.Session.retrieve=fail
    result=reconcile(database,"sk_test_fixture",now=1000000,api=provider)
    assert result["failed"]==1 and "secret" not in str(result)
    assert row(database)[0] is None
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE orders SET checkout=NULL")
    assert reconcile(database,"sk_test_fixture",now=1000000,api=api())["failed"]==1


def test_live_key_and_missing_database_refused(database,tmp_path):
    with pytest.raises(ValueError):
        reconcile(database,"sk_live_fixture",api=api())
    path=tmp_path/"missing.db"
    with pytest.raises(ValueError):
        reconcile(path,"sk_test_fixture",api=api())
    assert not path.exists()


def test_lost_create_response_can_find_unique_checkout(database):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE orders SET checkout=NULL")
    provider=api()
    provider.checkout.Session.list=lambda *args,**kwargs:dict(data=[remote()],has_more=False)
    assert reconcile(database,"sk_test_fixture",now=1000000,api=provider)["recovered"]==1
    assert row(database)[0]==910000+PERIOD


@pytest.mark.parametrize("listing",[dict(data=[],has_more=False),dict(data=[remote(),remote()],has_more=False),dict(data=[remote()],has_more=True)])
def test_ambiguous_missing_session_never_grants(database,listing):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE orders SET checkout=NULL")
    provider=api(); provider.checkout.Session.list=lambda *args,**kwargs:listing
    assert reconcile(database,"sk_test_fixture",now=1000000,api=provider)["failed"]==1
    assert row(database)==(None,0,0)
