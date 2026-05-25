import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
HISTORY_FILE = DATA_DIR / "history.json"

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev_secret_change_me")

DISCLAIMER = (
    "本アプリは出品準備をサポートする参考ツールです。"
    "価格・売上・販売結果を保証するものではありません。"
    "メルカリへの自動ログイン・自動出品・自動データ収集は行いません。"
)


def ensure_data_files():
    DATA_DIR.mkdir(exist_ok=True)
    if not USERS_FILE.exists():
        USERS_FILE.write_text(json.dumps({"users": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not HISTORY_FILE.exists():
        HISTORY_FILE.write_text(json.dumps({"history": []}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def current_user():
    return session.get("username")


def is_login_valid():
    username = session.get("username")
    expires_at = session.get("expires_at")
    if not username or not expires_at:
        return False
    return datetime.utcnow() < datetime.fromisoformat(expires_at)


def call_gemini(prompt: str):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        res = model.generate_content(prompt)
        return (res.text or "").strip()
    except Exception:
        return None


def fallback_generate(item_name, condition, category, notes):
    titles = [
        f"{condition} {item_name} すぐ使える 人気{category}",
        f"初心者安心 {item_name} {condition} {category}",
        f"美品感あり {item_name} {category} お得価格",
    ]
    descriptions = [
        f"【商品名】{item_name}\n【状態】{condition}\n【カテゴリ】{category}\n【ポイント】{notes or '丁寧に保管しています。'}",
        f"ご覧いただきありがとうございます。{item_name}の出品です。状態は{condition}です。{notes or '即購入OKです。'}",
        f"{item_name}（{category}）です。{condition}のため、写真でご確認ください。{notes or '気になる点はコメントください。'}",
    ]
    return titles, descriptions


def generate_listing_content(item_name, condition, category, notes):
    prompt = f"""
あなたはメルカリ出品サポートアシスタントです。
以下の商品情報をもとに、初心者向けに日本語で回答してください。
- 商品名: {item_name}
- 状態: {condition}
- カテゴリ: {category}
- 補足: {notes}

出力形式:
タイトル案:
1. ...
2. ...
3. ...

説明文案:
1. ...
2. ...
3. ...

価格の考え方:
- ...

次に売る物おすすめ:
- ...
- ...
- ...
"""
    text = call_gemini(prompt)
    if not text:
        titles, descriptions = fallback_generate(item_name, condition, category, notes)
        return {
            "titles": titles,
            "descriptions": descriptions,
            "pricing_advice": "相場検索で同一商品・同状態の価格を確認し、送料と手数料(10%)を差し引いて利益が出る価格に設定しましょう。",
            "next_items": ["未使用コスメ", "ブランド小物", "季節家電"],
        }

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    titles, descriptions, next_items = [], [], []
    pricing = []
    section = None
    for line in lines:
        if "タイトル案" in line:
            section = "titles"
            continue
        if "説明文案" in line:
            section = "descriptions"
            continue
        if "価格の考え方" in line:
            section = "pricing"
            continue
        if "次に売る物おすすめ" in line:
            section = "next"
            continue

        content = line.split(".", 1)[-1].lstrip("-・ ")
        if section == "titles" and len(titles) < 3:
            titles.append(content)
        elif section == "descriptions" and len(descriptions) < 3:
            descriptions.append(content)
        elif section == "pricing":
            pricing.append(content)
        elif section == "next" and len(next_items) < 3:
            next_items.append(content)

    if len(titles) < 3 or len(descriptions) < 3:
        ft, fd = fallback_generate(item_name, condition, category, notes)
        titles = (titles + ft)[:3]
        descriptions = (descriptions + fd)[:3]

    return {
        "titles": titles[:3],
        "descriptions": descriptions[:3],
        "pricing_advice": "\n".join(pricing) if pricing else "相場と送料・手数料を確認して価格を決めましょう。",
        "next_items": next_items[:3] if next_items else ["未使用コスメ", "ブランド小物", "季節家電"],
    }


def save_history(entry):
    history = load_json(HISTORY_FILE, {"history": []})
    history["history"].append(entry)
    save_json(HISTORY_FILE, history)


@app.context_processor
def inject_global():
    return {"disclaimer": DISCLAIMER, "current_user": current_user(), "login_valid": is_login_valid()}


@app.route("/")
def index():
    if not is_login_valid():
        return redirect(url_for("login"))
    return render_template("top.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    ensure_data_files()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            flash("ユーザー名を入力してください。")
            return render_template("login.html")

        users = load_json(USERS_FILE, {"users": []})
        if username not in users["users"]:
            users["users"].append(username)
            save_json(USERS_FILE, users)

        expires = datetime.utcnow() + timedelta(hours=24)
        session["username"] = username
        session["expires_at"] = expires.isoformat()
        flash("ログインしました（利用期限: 24時間）")
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("ログアウトしました。")
    return redirect(url_for("login"))


@app.route("/diagnose", methods=["GET", "POST"])
def diagnose():
    if not is_login_valid():
        return redirect(url_for("login"))
    if request.method == "POST":
        item_name = request.form.get("item_name", "").strip()
        condition = request.form.get("condition", "").strip()
        category = request.form.get("category", "").strip()
        notes = request.form.get("notes", "").strip()
        purchase_price = int(request.form.get("purchase_price") or 0)
        expected_price = int(request.form.get("expected_price") or 0)
        shipping_cost = int(request.form.get("shipping_cost") or 0)

        generated = generate_listing_content(item_name, condition, category, notes)
        fee = int(expected_price * 0.1)
        profit = expected_price - purchase_price - shipping_cost - fee

        result = {
            "id": str(uuid.uuid4()),
            "user": current_user(),
            "created_at": datetime.utcnow().isoformat(),
            "item_name": item_name,
            "condition": condition,
            "category": category,
            "notes": notes,
            "titles": generated["titles"],
            "descriptions": generated["descriptions"],
            "pricing_advice": generated["pricing_advice"],
            "next_items": generated["next_items"],
            "purchase_price": purchase_price,
            "expected_price": expected_price,
            "shipping_cost": shipping_cost,
            "fee": fee,
            "profit": profit,
        }
        save_history(result)
        session["latest_result"] = result
        return redirect(url_for("result"))
    return render_template("diagnose.html")


@app.route("/result")
def result():
    if not is_login_valid():
        return redirect(url_for("login"))
    data = session.get("latest_result")
    if not data:
        flash("先に商品診断を実行してください。")
        return redirect(url_for("diagnose"))
    return render_template("result.html", result=data)


@app.route("/history")
def history():
    if not is_login_valid():
        return redirect(url_for("login"))
    data = load_json(HISTORY_FILE, {"history": []})
    user_data = [h for h in reversed(data["history"]) if h.get("user") == current_user()]
    return render_template("history.html", records=user_data)


@app.route("/api/photo_checklist")
def photo_checklist():
    checklist = [
        "1枚目は全体がわかる明るい写真",
        "キズ・汚れはアップで撮影",
        "付属品を並べて撮影",
        "型番・ブランドタグを撮影",
        "背景をシンプルに整える",
    ]
    return jsonify(checklist)


if __name__ == "__main__":
    ensure_data_files()
    app.run(debug=True)
