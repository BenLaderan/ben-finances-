import json
import os
import base64
import re
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # e.g. benladean/ben-finances
ALLOWED_CHAT_ID = os.environ.get("CHAT_ID")  # 8711576571

HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": ALLOWED_CHAT_ID, "text": text, "parse_mode": "HTML"})


def gh_read(path):
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", headers=HEADERS)
    if r.status_code == 200:
        d = r.json()
        return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]
    return None, None


def gh_write(path, content, sha, msg):
    data = {
        "message": msg,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
    }
    r = requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}", json=data, headers=HEADERS)
    return r.status_code in (200, 201)


def handle_ledger(text):
    # รูปแบบ: +500 ค่ากาแฟ  หรือ  -200 ค่าข้าว
    m = re.match(r"^([+-])(\d+(?:\.\d+)?)\s+(.+)$", text.strip())
    if not m:
        return False
    sign, amount, note = m.groups()
    entry_type = "income" if sign == "+" else "expense"
    today = datetime.now().strftime("%Y-%m-%d")
    new_row = f"{today},{entry_type},general,{amount},{note}\n"

    content, sha = gh_read("finances/ledger.csv")
    if content is None:
        send("❌ ไม่พบไฟล์ ledger.csv ใน GitHub")
        return True

    ok = gh_write("finances/ledger.csv", content + new_row, sha, f"ledger: {entry_type} {amount} {note}")
    if ok:
        emoji = "💰" if sign == "+" else "💸"
        send(f"{emoji} <b>บันทึกแล้ว</b>\nประเภท: {entry_type}\nจำนวน: {amount} บาท\nหมายเหตุ: {note}\nวันที่: {today}")
    else:
        send("❌ บันทึกไม่สำเร็จ ลองใหม่อีกครั้ง")
    return True


def handle_summary():
    content, _ = gh_read("finances/ledger.csv")
    if not content:
        send("❌ ไม่พบข้อมูล")
        return
    lines = content.strip().split("\n")[1:]  # skip header
    this_month = datetime.now().strftime("%Y-%m")
    income = expense = 0.0
    for line in lines:
        parts = line.split(",")
        if len(parts) < 4:
            continue
        date, etype, _, amount = parts[0], parts[1], parts[2], parts[3]
        if not date.startswith(this_month):
            continue
        try:
            val = float(amount)
            if etype == "income":
                income += val
            else:
                expense += val
        except ValueError:
            pass
    net = income - expense
    sign = "+" if net >= 0 else ""
    send(
        f"📊 <b>สรุปเดือนนี้ ({this_month})</b>\n"
        f"💰 รายรับ: {income:,.2f} บาท\n"
        f"💸 รายจ่าย: {expense:,.2f} บาท\n"
        f"📈 คงเหลือ: {sign}{net:,.2f} บาท"
    )


HELP_TEXT = (
    "📋 <b>คำสั่งทั้งหมด</b>\n\n"
    "<b>บัญชี:</b>\n"
    "+500 ค่ากาแฟ — บันทึกรายรับ\n"
    "-200 ค่าอาหาร — บันทึกรายจ่าย\n"
    "/summary — สรุปรายรับจ่ายเดือนนี้\n\n"
    "<b>พอร์ต:</b>\n"
    "/assets — ดูสรุปสินทรัพย์\n\n"
    "/help — แสดงเมนูนี้"
)


def handle_assets():
    content, _ = gh_read("finances/assets.md")
    if not content:
        send("❌ ไม่พบไฟล์ assets.md")
        return
    # ส่งแค่ส่วน Net Worth Summary
    lines = content.split("\n")
    summary_lines = []
    in_summary = False
    for line in lines:
        if "Net Worth Summary" in line:
            in_summary = True
        if in_summary:
            summary_lines.append(line)
    if summary_lines:
        send("📊 <b>Net Worth Summary</b>\n" + "\n".join(summary_lines[2:10]))
    else:
        send(content[:1000])


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

        try:
            update = json.loads(body)
        except Exception:
            return

        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        if not text or chat_id != ALLOWED_CHAT_ID:
            return

        if re.match(r"^[+-]\d", text):
            handle_ledger(text)
        elif text == "/summary":
            handle_summary()
        elif text == "/assets":
            handle_assets()
        elif text in ("/help", "/start"):
            send(HELP_TEXT)
        else:
            send("ไม่เข้าใจคำสั่ง พิม /help ดูเมนูทั้งหมด")

    def log_message(self, format, *args):
        pass
