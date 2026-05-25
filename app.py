import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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

CATEGORY_PHOTO_POINTS = {
    "服": ["正面・背面の全体", "首元・袖口・裾", "タグ（サイズ/素材）", "シミや毛玉のアップ", "実物に近い色味"],
    "靴": ["左右の全体", "つま先とかかとの減り", "靴底の状態", "中敷きやロゴ", "箱や付属品"],
    "家電": ["正面の全体", "型番ラベル", "電源オン状態", "傷がある箇所", "付属ケーブルや説明書"],
    "本": ["表紙と裏表紙", "背表紙の状態", "ページの焼けや書き込み", "付録の有無", "ISBNバーコード"],
    "ゲーム": ["パッケージ表裏", "ディスク/カセット本体", "動作確認画面", "説明書や特典", "傷のアップ"],
    "美容品": ["外観の全体", "残量がわかる写真", "成分表示や期限", "キャップやポンプ部分", "外箱や付属品"],
    "バッグ": ["正面・背面・底面", "持ち手や角のスレ", "内側ポケット", "ファスナーや金具", "ブランドタグ"],
    "その他": ["全体がわかる1枚目", "傷や汚れのアップ", "サイズ感が伝わる写真", "付属品の並び", "明るい背景で撮影"],
}


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


def normalize_users_data(raw):
    users = []
    for u in raw.get("users", []):
        if isinstance(u, str):
            users.append(
                {
                    "id": str(uuid.uuid4()),
                    "username": u,
                    "password_hash": "",
                    "display_name": u,
                    "expires_at": (datetime.utcnow() + timedelta(days=3650)).isoformat(),
                    "is_active": True,
                    "created_at": datetime.utcnow().isoformat(),
                    "memo": "migrated legacy user",
                }
            )
        elif isinstance(u, dict):
            users.append(
                {
                    "id": u.get("id") or str(uuid.uuid4()),
                    "username": (u.get("username") or "").strip(),
                    "password_hash": u.get("password_hash", ""),
                    "display_name": u.get("display_name") or u.get("username") or "",
                    "expires_at": u.get("expires_at") or (datetime.utcnow() + timedelta(days=3650)).isoformat(),
                    "is_active": bool(u.get("is_active", True)),
                    "created_at": u.get("created_at") or datetime.utcnow().isoformat(),
                    "memo": u.get("memo", ""),
                }
            )
    return {"users": [u for u in users if u.get("username")]}


def load_users():
    data = normalize_users_data(load_json(USERS_FILE, {"users": []}))
    save_json(USERS_FILE, data)
    return data


def save_history(record):
    data = load_json(HISTORY_FILE, {"history": []})
    data.setdefault("history", []).append(record)
    save_json(HISTORY_FILE, data)


def current_user():
    return session.get("username")


def is_login_valid():
    username = session.get("username")
    if not username:
        return False
    user = find_user_by_username(username)
    if not user:
        return False
    try:
        return user.get("is_active", False) and datetime.utcnow() < datetime.fromisoformat(user.get("expires_at", ""))
    except ValueError:
        return False


def is_admin_valid():
    return bool(session.get("is_admin"))


def find_user_by_username(username):
    users = load_users()["users"]
    return next((u for u in users if u.get("username") == username), None)


def condition_tag(condition):
    c = condition.lower()
    if "新品" in c or "未使用" in c:
        return "新品・未使用"
    if "美品" in c:
        return "目立った傷や汚れなし"
    if "使用感" in c or "キズ" in c or "汚れ" in c:
        return "使用感あり"
    return condition

# (snip unchanged helper builders)
def build_titles(item_name, condition, category):
    clean_condition = condition_tag(condition)
    return [
        f"{item_name} {category} {clean_condition} 状態わかりやすくご案内",
        f"{item_name} {clean_condition} 丁寧梱包で安心してお取引",
        f"{item_name} {category} {clean_condition} 早め発送 すぐ使える",
    ]

def build_descriptions(item_name, condition, category, notes):
    notes_text = notes if notes else "自宅保管のため、細かな点は写真でご確認ください。"
    shared = (
        f"【商品名】{item_name}\n"
        f"【カテゴリ】{category}\n"
        f"【状態】{condition}\n"
        f"【使用感】{notes_text}\n"
        f"【傷・汚れ】目立つ点がある場合は写真と説明に記載します。\n"
        f"【付属品】写真に写っているものがすべてです。\n"
        f"【発送】匿名配送を予定、1〜2日で発送します。\n"
        "【ひとこと】気持ちのよいお取引を心がけています。\n"
        "※中古品のため、完璧を求める方はご購入前にご確認ください。"
    )
    short = f"{item_name}の出品です。状態は{condition}です。\n{notes_text}\n写真で状態をご確認のうえ、ご検討ください。\n匿名配送で1〜2日以内に発送予定です。"
    safe = f"ご覧いただきありがとうございます。{item_name}（{category}）です。\n状態は{condition}で、{notes_text}\n気になる箇所はできるだけ写真でわかるようにしています。\n中古品にご理解のある方のみお願いいたします。ご不明点はお気軽にコメントください。"
    return [shared, short, safe]

def analyze_result(item_name, condition, category, notes, expected_price, shipping_cost, purchase_price, fee, profit):
    easy = "出品しやすい" if category in ["服", "本", "美容品", "バッグ"] else "やや準備が必要"
    beginner = "★★★★★" if easy == "出品しやすい" else "★★★☆☆"
    return {"listing_ease": easy, "beginner_score": beginner, "one_line": "初心者でも進めやすい条件です。このまま出品準備を進められます。" if profit >= 500 else "出品は可能ですが利益が薄めです。価格か送料を少し調整しましょう。", "price_thinking": "同じ商品の最近の売却価格を3件ほど確認し、手数料10%と送料を引いて利益が残る価格を基準にします。", "quick_sell_price": max(expected_price - 500, 300), "high_trial_price": expected_price + 700, "discount_caution": max(expected_price - 1200, 300), "shipping_note": "送料が高めなので、配送方法の見直しで利益改善が期待できます。" if shipping_cost > 800 else "送料は許容範囲です。梱包サイズを抑えるとさらに安心です。", "photo_must": CATEGORY_PHOTO_POINTS.get(category, CATEGORY_PHOTO_POINTS["その他"]), "improve_if_slow": ["タイトル冒頭に商品名と型番を入れる", "1枚目を明るい全体写真に差し替える", "説明文に傷の場所を具体的に追記する"], "next_actions": ["タイトル案から1つ選んでコピー", "説明文案に実物情報を追記", "価格を最終調整して出品"]}

def build_next_recommendations(category):
    mapping = {
        "服": ["服", "バッグ", "美容品"],
        "靴": ["靴", "バッグ", "服"],
        "家電": ["本", "ゲーム", "家電"],
        "本": ["本", "美容品", "服"],
        "ゲーム": ["ゲーム", "本", "美容品"],
        "美容品": ["美容品", "服", "バッグ"],
        "バッグ": ["バッグ", "服", "美容品"],
    }
    order = mapping.get(category, ["本", "服", "美容品"])
    rows = []
    for idx, g in enumerate(order, start=1):
        rows.append(
            {
                "rank": idx,
                "genre": g,
                "beginner_fit": "高" if idx == 1 else "中",
                "shipping_risk": "低" if g in ["本", "美容品", "服"] else "中",
                "photo_ease": "撮りやすい" if g in ["本", "美容品"] else "普通",
                "reason": f"{g}は状態説明の型が作りやすく、初心者でも出品手順を覚えやすいです。",
                "caution": "サイズ・残量・傷の3点を必ず記載しましょう。",
            }
        )
    return rows

@app.context_processor
def inject_global():
    login_ok = is_login_valid()
    admin_ok = is_admin_valid()
    endpoint = request.endpoint or ""
    hide_user_menu = endpoint in {"login", "admin_login"}
    hide_admin_menu = endpoint == "login"
    return {
        "disclaimer": DISCLAIMER,
        "current_user": current_user(),
        "login_valid": login_ok,
        "admin_valid": admin_ok,
        "show_user_menu": login_ok and not hide_user_menu,
        "show_admin_menu": admin_ok and not hide_admin_menu,
    }

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
        password = request.form.get("password", "")
        user = find_user_by_username(username)
        if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
            flash("ユーザー名またはパスワードが違います")
            return render_template("login.html")
        if not user.get("is_active", False):
            flash("このアカウントは停止中です")
            return render_template("login.html")
        if datetime.utcnow() >= datetime.fromisoformat(user["expires_at"]):
            flash("利用期限が切れています")
            return render_template("login.html")
        session["username"] = user["username"]
        flash("ログインしました")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    admin_username = os.getenv("ADMIN_USERNAME")
    admin_password = os.getenv("ADMIN_PASSWORD")
    missing = not admin_username or not admin_password
    if request.method == "POST" and not missing:
        if request.form.get("username") == admin_username and request.form.get("password") == admin_password:
            session["is_admin"] = True
            return redirect(url_for("admin_top"))
        flash("管理者ユーザー名またはパスワードが違います")
    return render_template("admin_login.html", admin_missing=missing)

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("管理者ログアウトしました")
    return redirect(url_for("admin_login"))

@app.route("/admin")
def admin_top():
    if not is_admin_valid():
        return redirect(url_for("admin_login"))
    users = load_users()["users"]
    now = datetime.utcnow()
    active_count = sum(1 for u in users if u.get("is_active"))
    stopped_count = sum(1 for u in users if not u.get("is_active"))
    expired_count = sum(1 for u in users if u.get("expires_at") and datetime.fromisoformat(u["expires_at"]) <= now)
    total_history = len(load_json(HISTORY_FILE, {"history": []}).get("history", []))
    return render_template("admin_dashboard.html", users=users, total_users=len(users), active_count=active_count, stopped_count=stopped_count, expired_count=expired_count, total_history=total_history, now=now)

@app.route('/admin/users/new', methods=['GET','POST'])
def admin_user_new():
    if not is_admin_valid():
        return redirect(url_for('admin_login'))
    if request.method == 'POST':
        data = load_users()
        username = request.form.get('username','').strip()
        if not username or not request.form.get('password'):
            flash('ユーザー名とパスワードは必須です')
            return render_template('admin_user_form.html', mode='new')
        if any(u['username']==username for u in data['users']):
            flash('同じユーザー名は使えません')
            return render_template('admin_user_form.html', mode='new')
        expires_at = request.form.get('expires_at') or (datetime.utcnow()+timedelta(days=30)).isoformat(timespec='minutes')
        data['users'].append({'id':str(uuid.uuid4()),'username':username,'password_hash':generate_password_hash(request.form.get('password')),'display_name':request.form.get('display_name','').strip() or username,'expires_at':expires_at,'is_active':True,'created_at':datetime.utcnow().isoformat(),'memo':request.form.get('memo','').strip()})
        save_json(USERS_FILE, data)
        return redirect(url_for('admin_top'))
    return render_template('admin_user_form.html', mode='new')

@app.route('/admin/users/<user_id>/edit', methods=['GET','POST'])
def admin_user_edit(user_id):
    if not is_admin_valid(): return redirect(url_for('admin_login'))
    data = load_users(); user = next((u for u in data['users'] if u['id']==user_id), None)
    if not user: flash('ユーザーが見つかりません'); return redirect(url_for('admin_top'))
    if request.method=='POST':
        user['display_name']=request.form.get('display_name','').strip() or user['display_name']
        if request.form.get('password','').strip(): user['password_hash']=generate_password_hash(request.form.get('password').strip())
        user['expires_at']=request.form.get('expires_at') or user['expires_at']
        user['is_active']=request.form.get('is_active')=='true'
        user['memo']=request.form.get('memo','').strip()
        save_json(USERS_FILE, data)
        return redirect(url_for('admin_top'))
    return render_template('admin_user_form.html', mode='edit', user=user)

@app.route('/admin/users/<user_id>/delete', methods=['POST'])
def admin_user_delete(user_id):
    if not is_admin_valid(): return redirect(url_for('admin_login'))
    data = load_users(); data['users'] = [u for u in data['users'] if u['id']!=user_id]; save_json(USERS_FILE, data)
    flash('ユーザーを削除しました')
    return redirect(url_for('admin_top'))

@app.route('/logout')
def logout():
    session.pop('username', None)
    flash('ログアウトしました。')
    return redirect(url_for('login'))

@app.route('/diagnose', methods=['GET', 'POST'])
def diagnose():
    if not is_login_valid(): return redirect(url_for('login'))
    if request.method == 'POST':
        item_name=request.form.get('item_name','').strip(); condition=request.form.get('condition','').strip(); category=request.form.get('category','').strip() or 'その他'; notes=request.form.get('notes','').strip(); purchase_price=int(request.form.get('purchase_price') or 0); expected_price=int(request.form.get('expected_price') or 0); shipping_cost=int(request.form.get('shipping_cost') or 0); fee=int(expected_price*0.1); profit=expected_price-purchase_price-shipping_cost-fee
        result={"id":str(uuid.uuid4()),"user":current_user(),"created_at":datetime.utcnow().isoformat(),"item_name":item_name,"condition":condition,"category":category,"notes":notes,"titles":build_titles(item_name, condition, category),"descriptions":build_descriptions(item_name, condition, category, notes),"purchase_price":purchase_price,"expected_price":expected_price,"shipping_cost":shipping_cost,"fee":fee,"profit":profit,"analysis":analyze_result(item_name, condition, category, notes, expected_price, shipping_cost, purchase_price, fee, profit),"next_recommendations":build_next_recommendations(category)}
        save_history(result); session['latest_result']=result; return redirect(url_for('result'))
    return render_template('diagnose.html')

@app.route('/result')
def result():
    if not is_login_valid(): return redirect(url_for('login'))
    data = session.get('latest_result')
    if not data: flash('先に商品診断を実行してください。'); return redirect(url_for('diagnose'))
    return render_template('result.html', result=data)

@app.route('/history')
def history():
    if not is_login_valid(): return redirect(url_for('login'))
    data = load_json(HISTORY_FILE, {'history':[]}); user_data=[h for h in reversed(data['history']) if h.get('user')==current_user()]
    return render_template('history.html', records=user_data)

@app.route('/history/<record_id>')
def history_detail(record_id):
    if not is_login_valid(): return redirect(url_for('login'))
    data = load_json(HISTORY_FILE, {'history':[]}); record=next((h for h in data['history'] if h.get('id')==record_id and h.get('user')==current_user()), None)
    if not record: flash('対象の履歴が見つからないか、閲覧権限がありません。'); return redirect(url_for('history'))
    return render_template('history_detail.html', result=record)

@app.route('/history/<record_id>/delete', methods=['POST'])
def delete_history(record_id):
    if not is_login_valid(): return redirect(url_for('login'))
    data=load_json(HISTORY_FILE,{'history':[]}); before=len(data.get('history',[])); data['history']=[h for h in data.get('history',[]) if not (h.get('id')==record_id and h.get('user')==current_user())]
    if len(data['history'])<before: save_json(HISTORY_FILE, data); flash('履歴を削除しました')
    else: flash('削除対象の履歴が見つからないか、削除権限がありません。')
    return redirect(url_for('history'))

@app.route('/api/photo_checklist')
def photo_checklist():
    return jsonify(CATEGORY_PHOTO_POINTS.get(request.args.get('category', 'その他'), CATEGORY_PHOTO_POINTS['その他']))

if __name__ == '__main__':
    ensure_data_files()
    app.run(debug=True)
