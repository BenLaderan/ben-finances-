import json
import os
import base64
import re
import requests
from datetime import datetime
from http.server import BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ALLOWED_CHAT_ID = os.environ.get("CHAT_ID")

HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

ACCOUNT_MAP = {
    "บัญชีเงินเย็น": "บัญชีเงินเย็น",
    "บัญชีใช้งาน": "บัญชีใช้งาน",
    "Prepaid Card": "Prepaid Card",
    "prepaid card": "Prepaid Card",
    "prepaid": "Prepaid Card",
    "เงินสด": "เงินสด",
    "เงินในพอร์ตลงทุน": "เงินในพอร์ตลงทุน",
    "พอร์ต": "เงินในพอร์ตลงทุน",
}
DEFAULT_ACCOUNT = "บัญชีใช้งาน"


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


def get_table(content, start, stops):
    rows = []
    active = False
    for line in content.splitlines():
        if start in line:
            active = True
            continue
        if active and any(s in line for s in stops) and line.strip():
            break
        if active and line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if any(cells):
                rows.append(cells)
    return rows[1:] if len(rows) > 1 else []


def update_assets_balance(account_name, delta):
    content, sha = gh_read("finances/assets.md")
    if content is None:
        return None

    lines = content.split("\n")
    new_balance = None
    updated = []

    for line in lines:
        if line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if cells and cells[0] == account_name and len(cells) >= 2:
                try:
                    current = float(cells[1].replace(",", ""))
                    new_balance = current + delta
                    parts = line.split("|")
                    parts[2] = f" {new_balance:,.2f} "
                    line = "|".join(parts)
                except ValueError:
                    pass
        updated.append(line)

    if new_balance is None:
        return None

    # Recalculate cash total
    total = 0.0
    in_cash = False
    for line in updated:
        if "## 🏦" in line:
            in_cash = True
        elif in_cash and line.startswith("## "):
            in_cash = False
        elif in_cash and line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if cells and cells[0] and not cells[0].startswith("รวม") and cells[0] != "รายการ" and len(cells) >= 2:
                try:
                    total += float(cells[1].replace(",", ""))
                except ValueError:
                    pass

    final = []
    for line in updated:
        if line.startswith("|") and "รวมเงินสด/บัญชี" in line:
            parts = line.split("|")
            parts[2] = f" **{total:,.2f}** "
            line = "|".join(parts)
        final.append(line)

    gh_write("finances/assets.md", "\n".join(final), sha, f"assets: {account_name} {delta:+.2f}")
    return new_balance


def handle_ledger(text):
    m = re.match(r"^([+-])(\d+(?:\.\d+)?)\s+(.+?)(?:\s+\[(.+?)\])?\s*$", text.strip())
    if not m:
        return False
    sign, amount, note, acct_raw = m.groups()

    account = DEFAULT_ACCOUNT
    if acct_raw:
        account = ACCOUNT_MAP.get(acct_raw.strip(), acct_raw.strip())

    entry_type = "income" if sign == "+" else "expense"
    today = datetime.now().strftime("%Y-%m-%d")
    new_row = f"{today},{entry_type},general,{amount},{note}\n"

    content, sha = gh_read("finances/ledger.csv")
    if content is None:
        send("❌ ไม่พบไฟล์ ledger.csv")
        return True
    ok = gh_write("finances/ledger.csv", content + new_row, sha, f"ledger: {entry_type} {amount} {note}")
    if not ok:
        send("❌ บันทึก ledger ไม่สำเร็จ")
        return True

    delta = float(amount) if sign == "+" else -float(amount)
    new_balance = update_assets_balance(account, delta)

    emoji = "💰" if sign == "+" else "💸"
    type_th = "รายรับ" if sign == "+" else "รายจ่าย"

    if new_balance is not None:
        bal_line = f"บัญชี: {account} → {new_balance:,.2f} ฿"
    else:
        bal_line = f"บัญชี: {account} (อัพเดท assets ไม่สำเร็จ)"

    send(
        f"{emoji} <b>บันทึกแล้ว</b>\n"
        f"ประเภท: {type_th}\n"
        f"จำนวน: {amount} บาท\n"
        f"หมายเหตุ: {note}\n"
        f"{bal_line}\n"
        f"วันที่: {today}"
    )
    return True


def handle_summary():
    content, _ = gh_read("finances/ledger.csv")
    if not content:
        send("❌ ไม่พบข้อมูล")
        return
    lines = content.strip().split("\n")[1:]
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
    send(f"📊 <b>สรุปเดือนนี้ ({this_month})</b>\n💰 รายรับ: {income:,.2f} บาท\n💸 รายจ่าย: {expense:,.2f} บาท\n📈 คงเหลือ: {sign}{net:,.2f} บาท")


def handle_assets():
    content, _ = gh_read("finances/assets.md")
    if not content:
        send("❌ ไม่พบไฟล์ assets.md")
        return

    out = ["💼 <b>สินทรัพย์ของเบน</b>", ""]

    cash = get_table(content, "เงินในบัญชี", ["## 📈", "## 🪙", "## 💸"])
    if cash:
        out.append("🏦 <b>เงินสด / บัญชี</b>")
        for r in cash:
            if len(r) >= 2:
                if r[0].startswith("รวม"):
                    out.append("━━━━━━━━━━━━")
                    out.append(f"💵 รวม: <b>{r[1]} ฿</b>")
                else:
                    out.append(f"  • {r[0]}: {r[1]} ฿")
        out.append("")

    set_rows = get_table(content, "### SET", ["### NYSE", "## 🪙", "## 💸"])
    if set_rows:
        out.append("📈 <b>หุ้น SET</b>")
        for r in set_rows:
            if len(r) >= 3:
                out.append(f"  • {r[0]}: {r[1]} หุ้น @ ฿{r[2]}")
        out.append("")

    us_rows = get_table(content, "NYSE / NASDAQ", ["## 🪙", "## 💸"])
    if us_rows:
        out.append("📈 <b>หุ้น US</b>")
        for r in us_rows:
            if len(r) >= 3:
                out.append(f"  • {r[0]}: {r[1]} หุ้น @ ${r[2]}")
        out.append("")

    fund_rows = get_table(content, "กองทุน (Funds)", ["## 💸"])
    if fund_rows:
        out.append("🪙 <b>กองทุน</b>")
        for r in fund_rows:
            if len(r) >= 4:
                out.append(f"  • {r[0]}: DCA ฿{r[3]} {r[2]}")
        out.append("")

    debt_rows = get_table(content, "หนี้สิน (Liabilities)", ["## "])
    if debt_rows:
        out.append("💸 <b>หนี้สิน</b>")
        for r in debt_rows:
            if len(r) >= 2 and not r[0].startswith("รวม"):
                note = f" ({r[3]})" if len(r) > 3 and r[3].strip() else ""
                out.append(f"  • {r[0]}: {r[1]} ฿{note}")
        out.append("")

    send("\n".join(out))


HELP_TEXT = (
    "📋 <b>คำสั่งทั้งหมด</b>\n\n"
    "<b>บัญชี:</b>\n"
    "+500 เงินเดือน — บันทึกรายรับ (ตัด บัญชีใช้งาน)\n"
    "-200 ค่าอาหาร — บันทึกรายจ่าย (ตัด บัญชีใช้งาน)\n"
    "-200 ค่าอาหาร [เงินสด] — ระบุบัญชี\n"
    "บัญชีที่รองรับ: บัญชีเงินเย็น, บัญชีใช้งาน, Prepaid Card, เงินสด, เงินในพอร์ตลงทุน\n\n"
    "/summary — สรุปรายรับจ่ายเดือนนี้\n\n"
    "<b>พอร์ต:</b>\n"
    "/assets — ดูสรุปสินทรัพย์\n\n"
    "/help — แสดงเมนูนี้"
)


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
