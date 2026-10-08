"""Deterministic calculations: estimates are never presented as market evidence."""
import csv
import io

MAX_PRICE = 9_999_999


def yen(value, label, optional=False):
    text = str(value if value is not None else "").strip()
    if optional and not text:
        return None
    if not text.isascii() or not text.isdigit() or len(text) > 7:
        raise ValueError(f"{label}は0〜9999999の整数で入力してください")
    number = int(text)
    if number > MAX_PRICE:
        raise ValueError(f"{label}が上限を超えています")
    return number


def proceeds(price, shipping, packing, cost=0):
    return price - price // 10 - shipping - packing - cost


def minimum_price(shipping, packing, cost, target):
    """Lowest integer yen price satisfying target with a floor-rounded 10% fee."""
    low, high = 300, MAX_PRICE
    if proceeds(high, shipping, packing, cost) < target:
        return None
    while low < high:
        middle = (low + high) // 2
        if proceeds(middle, shipping, packing, cost) >= target:
            high = middle
        else:
            low = middle + 1
    return low


def prepare(raw):
    name = str(raw.get("商品名", "")).strip()
    condition = str(raw.get("状態", "")).strip()
    notes = str(raw.get("補足", "")).strip()
    if not name or len(name) > 120 or not condition or len(condition) > 120 or len(notes) > 1000:
        raise ValueError("商品名・状態は必須で各120文字まで、補足は1000文字までです")
    price = yen(raw.get("売値"), "売値")
    if price < 300:
        raise ValueError("売値は300円以上で入力してください")
    shipping = yen(raw.get("送料", "0"), "送料")
    packing = yen(raw.get("梱包費", "0"), "梱包費")
    cost = yen(raw.get("仕入れ値", ""), "仕入れ値", optional=True)
    target = yen(raw.get("希望手残り", "0"), "希望手残り")
    floor = minimum_price(shipping, packing, cost or 0, target)
    net = proceeds(price, shipping, packing)
    return dict(name=name, condition=condition, notes=notes, price=price,
                shipping=shipping, packing=packing, cost=cost, target=target,
                fee=price // 10, net=net, profit=None if cost is None else net-cost,
                minimum=floor, discount=None if floor is None else max(price-floor, 0),
                meets_target=floor is not None and price >= floor,
                title=name[:40],
                description=f"【商品名】{name}\n【状態】{condition}\n{notes}".strip())


def parse_batch(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not {"商品名", "状態", "売値", "送料"}.issubset(reader.fieldnames or []):
        raise ValueError("見出しに商品名・状態・売値・送料が必要です")
    rows = []
    for index, raw in enumerate(reader, start=2):
        if len(rows) >= 50:
            raise ValueError("一度に登録できるのは50商品までです")
        try:
            rows.append(prepare(raw))
        except ValueError as exc:
            raise ValueError(f"{index}行目：{exc}") from exc
    if not rows:
        raise ValueError("商品を1件以上入力してください")
    return rows
