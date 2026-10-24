# ai_helper.py
# ส่วนเชื่อม AI (Google Gemini - ใช้ free tier ได้): เตรียมข้อมูล -> เรียก AI -> ตรวจรหัสเมนูกับฐานข้อมูล
# AI ทำหน้าที่ "เสนอเมนู + เหตุผล" เท่านั้น ตัวเลขทั้งหมดโปรแกรมคำนวณใหม่เอง
# API key: ใช้คีย์ของกลุ่มใน default_key.py (ติดไปกับ .exe, ไม่ขึ้น GitHub) หรือตั้ง GEMINI_API_KEY

import json
import os

from db import get_conn, today_str
from logic import AppError, best_offer, fmt_qty

# ใช้รุ่น lite เพราะตอบเร็ว (ทดสอบ ~2 วินาที) รุ่น flash ธรรมดาคิดนานจนเกิน 60 วินาที
MODEL = "gemini-flash-lite-latest"

# รูปแบบคำตอบที่บังคับให้ AI ตอบ (JSON)
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "menu_code": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["menu_code", "reason"],
            },
        }
    },
    "required": ["recommendations"],
}


# ---------------- เก็บ API key ----------------

def load_api_key():
    """ลำดับ: GEMINI_API_KEY ในเครื่อง -> คีย์ของกลุ่มที่ติดมากับโปรแกรม (default_key.py)"""
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    try:
        from default_key import DEFAULT_KEY      # ไม่มีไฟล์นี้ (เช่นโหลดจาก GitHub) ก็ไม่เป็นไร
        return DEFAULT_KEY
    except ImportError:
        return ""


# ---------------- เตรียมข้อมูล ----------------

def build_prompt_data(budget, dishes, excluded_codes):
    """รวบรวมเฉพาะข้อมูลที่จำเป็นส่งให้ AI"""
    conn = get_conn()
    menus = []
    for m in conn.execute("SELECT * FROM menus ORDER BY code"):
        if m["code"] in excluded_codes:
            continue
        items = conn.execute("""SELECT i.name, i.unit, r.qty_per_dish FROM recipe_items r
                                JOIN ingredients i ON i.id=r.ingredient_id WHERE r.menu_id=?""",
                             (m["id"],)).fetchall()
        menus.append({"code": m["code"], "name": m["name"],
                      "per_dish": [f"{r['name']} {r['qty_per_dish']:g} {r['unit']}" for r in items]})

    stock = []
    for lot in conn.execute("""SELECT i.name, i.unit, SUM(l.qty_left) AS qty, MIN(l.use_by) AS use_by
                               FROM lots l JOIN ingredients i ON i.id=l.ingredient_id
                               WHERE l.qty_left>0 AND l.use_by>=? GROUP BY i.id""", (today_str(),)):
        stock.append(f"{lot['name']}: {fmt_qty(lot['qty'], lot['unit'])} (ล็อตแรกควรใช้ก่อน {lot['use_by']})")

    produce = []
    for ing in conn.execute("SELECT DISTINCT i.id, i.name, i.unit FROM produce p "
                            "JOIN ingredients i ON i.id=p.ingredient_id"):
        offer = best_offer(conn, ing["id"])
        if offer:
            produce.append(f"{ing['name']}: ราคา {offer['price_std']:g} บาท/{ing['unit']} "
                           f"มี {fmt_qty(offer['qty_std'], ing['unit'])}")
    conn.close()
    return {"menus": menus, "stock": stock, "community_produce": produce,
            "extra_budget_baht": budget, "dishes_per_menu": dishes}


# ---------------- เรียก AI ----------------

def ask_ai(budget, dishes, excluded_codes):
    """คืนค่า [(menu_row, reason)] ที่ผ่านการตรวจแล้ว
    ถ้าไม่มีอินเทอร์เน็ต/ไม่มีคีย์/AI ใช้ไม่ได้ -> AppError ให้ผู้ใช้เลือกเมนูเอง"""
    key = load_api_key()
    if not key:
        raise AppError("ไม่พบ API key ของ Gemini (ไฟล์ default_key.py)\nเลือกเมนูเองได้ตามปกติ")
    try:
        from google import genai
        from google.genai import types, errors
    except ImportError:
        raise AppError("ยังไม่ได้ติดตั้งไลบรารี google-genai (pip install google-genai)\nเลือกเมนูเองได้ตามปกติ")

    data = build_prompt_data(budget, dishes, excluded_codes)
    prompt = (
        "คุณช่วยร้านอาหารเลือกเมนูประจำวัน ให้เสนอ 3 เมนูจากรายการ menus เท่านั้น "
        "โดยเน้นใช้วัตถุดิบในคลังที่ใกล้วันควรใช้ก่อน และผลผลิตชุมชนที่พร้อมขาย "
        "ให้ยอดซื้อเพิ่มไม่เกินงบ ตอบ menu_code ตามที่ให้ไว้ และเหตุผลสั้น ๆ ภาษาไทย 1-2 ประโยค "
        "ไม่ต้องคำนวณตัวเลขปริมาณหรือราคา ไม่ต้องประเมินความปลอดภัยของอาหาร\n\n"
        + json.dumps(data, ensure_ascii=False, indent=1)
    )

    try:
        # timeout 60 วินาที (หน่วยเป็นมิลลิวินาที) กันโปรแกรมรอนานเกินไป
        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60_000))
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=ANSWER_SCHEMA,
            ),
        )
    except errors.ClientError as e:
        if e.code in (400, 401, 403):
            raise AppError("API key ไม่ถูกต้องหรือถูกยกเลิกแล้ว\nเลือกเมนูเองได้ตามปกติ")
        if e.code == 429:
            raise AppError("ใช้ AI ครบโควตาฟรีแล้ว รอสักครู่แล้วลองใหม่\nหรือเลือกเมนูเองได้ตามปกติ")
        raise AppError(f"AI ตอบกลับผิดพลาด ({e.code})\nเลือกเมนูเองได้ตามปกติ")
    except errors.ServerError:
        raise AppError("เซิร์ฟเวอร์ AI ไม่ว่าง ลองใหม่ภายหลัง\nเลือกเมนูเองได้ตามปกติ")
    except Exception:
        raise AppError("เชื่อมต่ออินเทอร์เน็ต/AI ไม่ได้\nเลือกเมนูเองได้ตามปกติ")

    return check_answer(response.text or "")


def check_answer(text):
    """ตรวจรูปแบบคำตอบ และตรวจว่ารหัสเมนูมีจริงในฐานข้อมูล"""
    try:
        answer = json.loads(text)
        recs = answer["recommendations"]
        if not isinstance(recs, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise AppError("รูปแบบคำตอบของ AI ไม่ถูกต้อง กรุณาเลือกเมนูเอง")

    conn = get_conn()
    result = []
    seen = set()
    for r in recs:
        if not isinstance(r, dict):
            continue          # รายการที่ไม่ใช่รูปแบบที่กำหนด ข้ามไป
        code = str(r.get("menu_code", "")).strip()
        menu = conn.execute("SELECT * FROM menus WHERE code=?", (code,)).fetchone()
        if menu is None or code in seen:
            continue          # ตัดเมนูที่ไม่มีในระบบ / ซ้ำ ทิ้ง
        seen.add(code)
        result.append((menu, r.get("reason", "")))
    conn.close()
    if not result:
        raise AppError("AI ไม่ได้เสนอเมนูที่มีในระบบ กรุณาเลือกเมนูเอง")
    return result[:3]
