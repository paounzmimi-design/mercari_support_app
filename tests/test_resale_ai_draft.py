import io
import json

import pytest

from resale.ai_draft import DraftError, generate_listing
from test_resale import CSV, event, order, register, sql, token
from resale.web import create_app


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


def answer(title, description, finish="STOP"):
    return Response(json.dumps({"candidates": [{"finishReason": finish,
        "content": {"parts": [{"text": json.dumps({"title": title, "description": description})}]}}]}).encode())


def test_ai_request_only_selected_facts_and_response_validation():
    item = {"name": "古本", "condition": "角に傷", "notes": "小さな汚れ", "cost": 750}
    def fake(req, timeout):
        assert timeout == 12
        assert req.get_header("X-goog-api-key") == "key-fixture"
        assert "cost" not in req.data.decode()
        return answer("古本", "古本。角に傷。小さな汚れ。")
    assert generate_listing(item, "key-fixture", "gemini-test", fake)[0] == "古本"
    with pytest.raises(DraftError):
        generate_listing(item, "key-fixture", "gemini-test", lambda *_ , **__: answer("古本", "新品同様"))
    with pytest.raises(DraftError):
        generate_listing(item, "key-fixture", "bad/model", fake)


def test_ai_opt_in_limits_and_edit_invalidates(tmp_path, monkeypatch):
    app = create_app({"TESTING": True, "SECRET_KEY": "x"*40,
        "DATABASE": str(tmp_path/"db.sqlite3"), "SESSION_COOKIE_SECURE": False,
        "NOW": lambda: 1_000_000, "WEBHOOK_SECRET": "whsec_fixture",
        "GEMINI_KEY": "test-key", "GEMINI_MODEL": "gemini-test"})
    client = app.test_client(); uid = register(client); order(app, uid); event(client, app)
    client.post("/batch", data={"csrf": token(client), "csv": CSV})
    item = sql(app,"SELECT id FROM items")[0][0]
    calls = []
    def fake(item, key, model):
        calls.append(item["name"])
        return ("本", "本。傷あり。")
    monkeypatch.setattr("resale.web.generate_listing", fake)
    assert client.post(f"/items/{item}/ai-draft").status_code == 400
    assert client.post(f"/items/{item}/ai-draft", data={"csrf": token(client)}).status_code == 302
    assert "AIが整えた下書き" in client.get("/workbench").get_data(as_text=True)
    assert client.post(f"/items/{item}/ai-draft", data={"csrf": token(client)}).status_code == 302
    assert client.post(f"/items/{item}/ai-draft", data={"csrf": token(client)}).status_code == 429
    assert calls == ["本", "本"]
    edit = {"csrf": token(client), "商品名": "変更した本", "状態": "傷あり", "補足": "角に傷",
        "売値": "1200", "送料": "230", "梱包費": "30", "仕入れ値": "", "希望手残り": "500"}
    assert client.post(f"/items/{item}/edit", data=edit).status_code == 302
    assert "AIが整えた下書き" not in client.get("/workbench").get_data(as_text=True)


def test_ai_disabled_and_other_user_cannot_generate(tmp_path, monkeypatch):
    app = create_app({"TESTING": True, "SECRET_KEY": "x"*40,
        "DATABASE": str(tmp_path/"db.sqlite3"), "SESSION_COOKIE_SECURE": False,
        "NOW": lambda: 1_000_000, "WEBHOOK_SECRET": "whsec_fixture"})
    client = app.test_client(); uid = register(client); order(app, uid); event(client, app)
    client.post("/batch", data={"csrf": token(client), "csv": CSV})
    item = sql(app,"SELECT id FROM items")[0][0]
    assert "AIで出品文" not in client.get("/workbench").get_data(as_text=True)
    assert client.post(f"/items/{item}/ai-draft", data={"csrf": token(client)}).status_code == 503
    app.config.update(GEMINI_KEY="test", GEMINI_MODEL="gemini-test")
    second = app.test_client(); other = register(second, "other")
    sql(app, "INSERT INTO orders(id,owner,expires,amount) VALUES ('o2',?,?,980)", (other, 1_000_000+3600))
    assert second.post(f"/items/{item}/ai-draft", data={"csrf": token(second)}).status_code == 404
