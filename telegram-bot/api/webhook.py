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

DEBT_KEYWORDS = {
    "ค่างวดรถ": "ค่างวดรถ",
    "spaylater": "หนี้ Spaylater",
}


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


def _parse_num(s):
    clean = s.strip().strip("*").strip("+").replace(",", "")
    try:
        return float(clean)
    except ValueError:
        return None


def _apply_row_delta(lines, row_name, delta):
    """Find row by name, apply delta to column-2. Returns (new_lines, old_val, new_val)."""
    old_val = new_val = None
    result = []
    for line in lines:
        if line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if cells and cells[0] == row_name and len(cells) >= 2:
                v = _parse_num(cells[1])
                if v is not None:
                    old_val = v
                    new_val = v + delta
                    parts = line.split("|")
                    parts[2] = f" {new_val:,.2f} "
                    line = "|".join(parts)
        result.append(line)
    return result, old_val, new_val


def _recalc_total(lines, section_marker, total_marker):
    """Sum all data rows in section, write back to total row. Appends '+' if any row has '-'."""
    total = 0.0
    has_unknown = False
    in_section = False
    SKIP = {"รายการ", "กองทุน", "Ticker", ""}

    for line in lines:
        if section_marker in line:
            in_section = True
        elif in_section and line.startswith("## "):
            in_section = False
        elif in_section and line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if not cells or cells[0] in SKIP or cells[0].startswith("รวม"):
                continue
            if len(cells) >= 2:
                v = _parse_num(cells[1])
                if v is not None:
                    total += v
                elif cells[1].strip() not in ("", "-"):
                    pass
                else:
                    has_unknown = True

    total_str = f"{total:,.2f}+" if has_unknown else f"{total:,.2f}"
    final = []
    for line in lines:
        if line.startswith("|") and total_marker in line:
            parts = line.split("|")
            parts[2] = f" **{total_str}** "
            line = "|".join(parts)
        final.append(line)
    return final, total


def update_cash_account(content, sha, account_name, delta):
    """Update cash account balance + recalculate รวมเงินสด/บัญชี. Returns (new_content, new_sha, old, new)."""
    lines, old_val, new_val = _apply_row_delta(content.split("\n"), account_name, delta)
    if new_val is None:
        return content, sha, None, None
    lines, _ = _recalc_total(lines, "## 🏦", "รวมเงินสด/บัญชี")
    new_content = "\n".join(lines)
    new_sha = sha
    if gh_write("finances/assets.md", new_content, sha, f"assets: {account_name} {delta:+.2f}"):
        _, new_sha = gh_read("finances/assets.md")
        new_sha = new_sha or sha
    return new_content, new_sha, old_val, new_val


def update_debt(content, sha, debt_name, delta):
    """Reduce debt balance + recalculate รวมหนี้สิน. Returns (new_content, new_sha, old, new)."""
    lines, old_val, new_val = _apply_row_delta(content.split("\n"), debt_name, delta)
    if new_val is None:
        return content, sha, None, None
    lines, _ = _recalc_total(lines, "หนี้สิน (Liabilities)", "รวมหนี้สิน")
    new_content = "\n".join(lines)
    new_sha = sha
    if gh_write("finances/assets.md", new_content, sha, f"assets: debt {debt_name} {delta:+.2f}"):
        _, new_sha = gh_read("finances/assets.md")
        new_sha = new_sha or sha
    return new_content, new_sha, old_val, new_val


def detect_debt(note):
    note_lower = note.lower()
    for keyword, debt_name in DEBT_KEYWORDS.items():
        if keyword in note_lower:
            return debt_name
    return None


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

    ledger_content, ledger_sha = gh_read("finances/ledger.csv")
    if ledger_content is None:
        send("❌ ไม่พบไฟล์ ledger.csv")
        return True
    if not gh_write("finances/ledger.csv", ledger_content + new_row, ledger_sha, f"ledger: {entry_type} {amount} {note}"):
        send("❌ บันทึก ledger ไม่สำเร็จ")
        return True

    assets_content, assets_sha = gh_read("finances/assets.md")
    if assets_content is None:
        send("❌ ไม่พบไฟล์ assets.md")
        return True

    delta = float(amount) if sign == "+" else -float(amount)
    emoji = "💰" if sign == "+" else "💸"
    type_th = "รายรับ" if sign == "+" else "รายจ่าย"
    debt_name = detect_debt(note) if sign == "-" else None

    if debt_name:
        # Step 1: deduct from cash account
        assets_content, assets_sha, acct_old, acct_new = update_cash_account(
            assets_content, assets_sha, account, delta
        )
        # Step 2: reduce debt balance (re-read to get fresh SHA after step 1 write)
        fresh_content, fresh_sha = gh_read("finances/assets.md")
        if fresh_content:
            assets_content, assets_sha = fresh_content, fresh_sha
        _, _, debt_old, debt_new = update_debt(assets_content, assets_sha, debt_name, delta)

        acct_line = (
            f"{account}: {acct_old:,.2f} → {acct_new:,.2f} ฿ (ลดลง {float(amount):,.0f})"
            if acct_old is not None else f"{account}: อัพเดทไม่สำเร็จ"
        )
        debt_line = (
            f"{debt_name}: {debt_old:,.2f} → {debt_new:,.2f} ฿ (ลดลง {float(amount):,.0f})"
            if debt_old is not None else f"{debt_name}: อัพเดทไม่สำเร็จ"
        )
        send(
            f"{emoji} <b>บันทึกแล้ว</b>\n"
            f"ประเภท: {type_th} (จ่ายหนี้)\n"
            f"จำนวน: {float(amount):,.0f} บาท\n"
            f"หมายเหตุ: {note}\n"
            f"{acct_line}\n"
            f"{debt_line}\n"
            f"วันที่: {today}"
        )
    else:
        _, _, acct_old, acct_new = update_cash_account(assets_content, assets_sha, account, delta)
        bal_line = (
            f"บัญชี: {account} → {acct_new:,.2f} ฿"
            if acct_new is not None else f"บัญชี: {account} (อัพเดท assets ไม่สำเร็จ)"
        )
        send(
            f"{emoji} <b>บันทึกแล้ว</b>\n"
            f"ประเภท: {type_th}\n"
            f"จำนวน: {float(amount):,.2f} บาท\n"
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
    "-8218 ค่างวดรถ — จ่ายหนี้ (อัพเดทยอดหนี้อัตโนมัติ)\n"
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
