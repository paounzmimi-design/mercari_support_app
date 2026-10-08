"""Optional listing copy assistance. Outputs require human fact checking."""
import json
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class DraftError(Exception):
    pass


def format_description(description, item):
    """Add paragraph breaks without changing supplied names or conditions."""
    protected = {}
    for key in ("name", "condition"):
        value = item[key]
        if value and value not in protected.values():
            marker = f"\x00{len(protected)}\x00"
            protected[marker] = value
            description = description.replace(value, marker)
    # Give product facts their own paragraphs, even if the model concatenated
    # them. Keep punctuation inside the original facts intact.
    for marker in protected:
        description = description.replace(marker, "\n\n" + marker + "\n\n")
    description = re.sub(r"。[^\S\n]*(?=[^\n])", "。\n", description)
    description = re.sub(r"\n[ \t]+", "\n", description)
    description = re.sub(r"\n{3,}", "\n\n", description)
    for marker, value in protected.items():
        description = description.replace(marker, value)
    return description.strip()


def generate_listing(item, api_key, model, opener=urlopen):
    if not api_key or not re.fullmatch(r"[A-Za-z0-9._-]{3,100}", model):
        raise DraftError("AIの設定を確認してください")
    facts = {key: item[key] for key in ("name", "condition", "notes")}
    prompt = (
        "日本語の商品出品文を作る。次のJSONに明記された事実だけを短く整える。"
        "ブランド・動作・付属品・発送・保証・真贋・購入時期など未入力の事実を補わない。"
        "勧誘、断定的な品質評価、ハッシュタグ、URLを含めない。"
        "descriptionにはnameとconditionの文字列を一字も変えずに必ず含める。"
        "商品名、状態、補足を別の段落にして、各文の後で改行する。"
        "補足の傷・汚れ・欠品・付属品の情報を省略しない。"
        "未入力の項目の見出しや定型の挨拶は追加しない。"
        "JSONオブジェクトのtitle（40文字以内）とdescription（700文字以内）だけ返す。\n"
        + json.dumps(facts, ensure_ascii=False)
    )
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json",
                                            "temperature": 0.2, "maxOutputTokens": 800}},
                      ensure_ascii=False).encode()
    req = Request("https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                  data=body, headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with opener(req, timeout=12) as response:
            data = json.load(response)
        candidate = data["candidates"][0]
        if candidate.get("finishReason") != "STOP":
            raise DraftError("AIの出力が途中で終わりました。別の情報で試してください")
        result = json.loads("".join(part.get("text", "") for part in
            candidate["content"]["parts"] if not part.get("thought")))
        title, description = result["title"], result["description"]
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 40
                or not isinstance(description, str) or not 1 <= len(description.strip()) <= 700
                or "http" in (title + description).lower()):
            raise DraftError("AIの文章形式を確認できませんでした")
        if item["name"] not in description or item["condition"] not in description:
            raise DraftError("商品名・状態が下書きに正確に含まれず、保存しませんでした")
        formatted = format_description(description.strip(), item)
        if len(formatted) > 700:
            raise DraftError("AIの文章形式を確認できませんでした")
        return title.strip(), formatted
    except DraftError:
        raise
    except HTTPError as exc:
        # Only the numeric status is safe to display; never expose response bodies.
        raise DraftError(f"AIサービスに接続できませんでした（HTTP {exc.code}）") from exc
    except Exception as exc:
        # API errors may contain credentials and prompt data. Never show them.
        raise DraftError("AIの文章を作れませんでした。時間を置いて再試行してください") from exc
