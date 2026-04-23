import json
import os
import base64
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
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

USDTHB_RATE = 36.5

YF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


# ─── Yahoo Finance ────────────────────────────────────────────────────────────

def get_price(symbol):
    """Fetch current price from Yahoo Finance. Returns float or None."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(url, headers=YF_HEADERS, timeout=5)
        if r.status_code == 200:
            return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        pass
    return None


def get_prices(symbols):
    """Fetch multiple prices in parallel. Returns {symbol: price_or_None}."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(symbols)) as ex:
        futures = {ex.submit(get_price, s): s for s in symbols}
        for f in as_completed(futures, timeout=8):
            s = futures[f]
            try:
                results[s] = f.result()
            except Exception:
                results[s] = None
    return results


# ─── GitHub helpers ───────────────────────────────────────────────────────────

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


# ─── Table parsing ────────────────────────────────────────────────────────────

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


# ─── Low-level asset update helpers ──────────────────────────────────────────

def _parse_num(s):
    try:
        return float(str(s).strip().strip("*").strip("+").replace(",", ""))
    except ValueError:
        return None


def _apply_row_delta(lines, row_name, delta):
    """Find row by name, apply delta to column-2. Returns (lines, old_val, new_val)."""
    old_val = new_val = None
    result = []
    for line in lines:
        if line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if cells and cells[0] == row_name and len(cells) >= 2:
                v = _parse_num(cells[1])
                if v is not None:
                    old_val, new_val = v, v + delta
                    parts = line.split("|")
                    parts[2] = f" {new_val:,.2f} "
                    line = "|".join(parts)
        result.append(line)
    return result, old_val, new_val


def _recalc_total(lines, section_marker, total_marker):
    """Sum data rows in section and write result to total row."""
    SKIP = {"รายการ", "กองทุน", "Ticker", ""}
    total = 0.0
    has_unknown = False
    in_section = False

    for line in lines:
        if section_marker in line:
            in_section = True
        elif in_section and line.startswith("## "):
            in_section = False
        elif in_section and line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            name = cells[0] if cells else ""
            if name in SKIP or name.startswith("รวม") or name.startswith("มูลค่าสุทธิ"):
                continue
            if len(cells) >= 2:
                v = _parse_num(cells[1])
                if v is not None:
                    total += v
                elif cells[1].strip() in ("-", ""):
                    has_unknown = True

    total_str = f"{total:,.2f}+" if has_unknown else f"{total:,.2f}"
    final = []
    for line in lines:
        if line.startswith("|") and total_marker in line:
            parts = line.split("|")
            if len(parts) >= 3:
                parts[2] = f" **{total_str}** "
                line = "|".join(parts)
        final.append(line)
    return final, total


def _update_car_note(lines, direction=-1):
    """Update 'เหลืออีก X งวด'. direction=-1 to decrement (pay), +1 to increment (reverse).
    Returns (lines, old_note, new_note).
    """
    result = []
    old_note = new_note = ""
    for line in lines:
        if line.startswith("|") and "ค่างวดรถ" in line and "---" not in line:
            parts = line.split("|")
            if len(parts) >= 5:
                m = re.search(r"เหลืออีก\s+(\d+)\s+งวด", parts[4])
                if m:
                    current = int(m.group(1))
                    old_note = f"เหลืออีก {current} งวด"
                    new_count = current + direction
                    new_note = "ชำระครบแล้ว" if new_count <= 0 else f"เหลืออีก {new_count} งวด"
                    parts[4] = f" {new_note} "
                    line = "|".join(parts)
                elif "ชำระครบแล้ว" in parts[4] and direction > 0:
                    old_note = "ชำระครบแล้ว"
                    new_note = "เหลืออีก 1 งวด"
                    parts[4] = f" {new_note} "
                    line = "|".join(parts)
        result.append(line)
    return result, old_note, new_note


def _calc_net_worth(lines):
    """Compute net worth from assets.md lines using cost basis for stocks."""
    section = None
    cash_total = set_val = us_val = fund_val = debt_total = 0.0

    for line in lines:
        if "## 🏦" in line:
            section = "cash"
        elif "### SET" in line:
            section = "set"
        elif line.startswith("###") and ("NYSE" in line or "NASDAQ" in line):
            section = "us"
        elif "## 🪙" in line:
            section = "funds"
        elif "## 💸" in line or ("หนี้สิน" in line and line.startswith("##")):
            section = "debt"
        elif line.startswith("## "):
            section = None

        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if not cells or len(cells) < 2:
            continue
        name = cells[0]

        if section == "cash" and "รวมเงินสด/บัญชี" in name:
            v = _parse_num(cells[1])
            if v is not None:
                cash_total = v

        elif section == "set" and name and name not in ("Ticker",) and not name.startswith("รวม") and len(cells) >= 3:
            shares, cost = _parse_num(cells[1]), _parse_num(cells[2])
            if shares is not None and cost is not None:
                set_val += shares * cost

        elif section == "us" and name and name not in ("Ticker",) and not name.startswith("รวม") and len(cells) >= 3:
            shares, cost_usd = _parse_num(cells[1]), _parse_num(cells[2])
            if shares is not None and cost_usd is not None:
                us_val += shares * cost_usd * USDTHB_RATE

        elif section == "funds" and name and name not in ("กองทุน",) and not name.startswith("รวม") and len(cells) >= 2:
            v = _parse_num(cells[1])
            if v is not None:
                fund_val += v

        elif section == "debt" and "รวมหนี้สิน" in name:
            v = _parse_num(cells[1])
            if v is not None:
                debt_total = v

    return (cash_total + set_val + us_val + fund_val) - debt_total


def _recalc_net_worth_row(lines):
    """Update or insert มูลค่าสุทธิ row after รวมหนี้สิน."""
    net_worth = _calc_net_worth(lines)
    net_str = f"{net_worth:,.2f}"

    if any("มูลค่าสุทธิ" in l for l in lines):
        final = []
        for line in lines:
            if line.startswith("|") and "มูลค่าสุทธิ" in line:
                parts = line.split("|")
                if len(parts) >= 3:
                    parts[2] = f" **{net_str}** "
                    line = "|".join(parts)
            final.append(line)
        return final

    final = []
    for line in lines:
        final.append(line)
        if line.startswith("|") and "รวมหนี้สิน" in line:
            final.append(f"| **มูลค่าสุทธิ (สินทรัพย์ - หนี้สิน)** | **{net_str}** | | |")
    return final


# ─── Main assets update (single write) ───────────────────────────────────────

def update_assets_on_ledger(content, sha, account, delta, debt_name=None):
    """Apply all ledger changes to assets.md in one write.
    Returns (acct_old, acct_new, debt_old, debt_new, old_car_note, new_car_note).
    """
    lines = content.split("\n")

    lines, acct_old, acct_new = _apply_row_delta(lines, account, delta)

    debt_old = debt_new = None
    old_car_note = new_car_note = ""
    if debt_name:
        lines, debt_old, debt_new = _apply_row_delta(lines, debt_name, delta)
        if debt_name == "ค่างวดรถ":
            direction = -1 if delta < 0 else +1
            lines, old_car_note, new_car_note = _update_car_note(lines, direction)
        lines, _ = _recalc_total(lines, "หนี้สิน (Liabilities)", "รวมหนี้สิน")

    lines, _ = _recalc_total(lines, "## 🏦", "รวมเงินสด/บัญชี")
    lines = _recalc_net_worth_row(lines)

    commit_msg = (
        f"assets: debt {debt_name} {delta:+.2f}" if debt_name
        else f"assets: {account} {delta:+.2f}"
    )
    gh_write("finances/assets.md", "\n".join(lines), sha, commit_msg)
    return acct_old, acct_new, debt_old, debt_new, old_car_note, new_car_note


# ─── Stock trade helpers ─────────────────────────────────────────────────────

US_SYMBOLS = {
    "DOCN", "VOO", "ASTS", "SOFI", "NVDA", "TSM", "AVGO",
    "MU", "AMD", "CIEN", "PLTR", "SPY", "QQQ", "AAPL", "MSFT",
    "GOOGL", "AMZN", "META", "TSLA", "BRK.B",
}


def _fmt_shares(n):
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:.7g}"


def _trade_update(lines, ticker, trade_qty, price, is_buy, is_us):
    """Buy/sell stock in assets.md lines.
    Returns (new_lines, old_qty, old_cost, new_qty, new_cost, pnl).
    """
    section_start = "### NYSE / NASDAQ" if is_us else "### SET"
    section_stops = ["## 🪙", "## 💸"] + ([] if is_us else ["### NYSE"])

    in_sec = False
    row_idx = -1
    last_data_idx = -1
    old_qty = old_cost = None

    for i, line in enumerate(lines):
        if section_start in line:
            in_sec = True
            continue
        if in_sec and line.strip() and any(s in line for s in section_stops):
            in_sec = False
        if in_sec and line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if not cells or cells[0] in ("Ticker", ""):
                continue
            last_data_idx = i
            if cells[0].upper() == ticker and len(cells) >= 3:
                row_idx = i
                old_qty = _parse_num(cells[1])
                old_cost = _parse_num(cells[2])

    # Compute new values
    pnl = None
    if is_buy:
        if old_qty is not None and old_cost is not None:
            new_qty = old_qty + trade_qty
            new_cost = (old_cost * old_qty + price * trade_qty) / new_qty
        else:
            new_qty, new_cost = trade_qty, price
    else:
        if old_qty is None:
            return lines, None, None, None, None, None
        new_qty = old_qty - trade_qty
        new_cost = old_cost or price
        if old_cost is not None:
            pnl = (price - old_cost) * trade_qty

    result = list(lines)

    if row_idx >= 0:
        if new_qty <= 0:
            del result[row_idx]
        else:
            parts = result[row_idx].split("|")
            parts[2] = f" {_fmt_shares(new_qty)} "
            parts[3] = f" {new_cost:.4f} "
            result[row_idx] = "|".join(parts)
    elif is_buy and new_qty > 0:
        note = "NYSE" if ticker in {"DOCN", "VOO"} else ("NASDAQ" if is_us else "")
        new_row = f"| {ticker} | {_fmt_shares(new_qty)} | {new_cost:.4f} | {note} |"
        insert_at = last_data_idx + 1 if last_data_idx >= 0 else len(result)
        result.insert(insert_at, new_row)

    return result, old_qty, old_cost, new_qty, new_cost, pnl


def handle_trade(text):
    m = re.match(r"^(ซื้อ|ขาย)\s+(\S+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*$", text.strip())
    if not m:
        send("รูปแบบ: ซื้อ/ขาย [SYMBOL] [จำนวน] [ราคา]\nตัวอย่าง: ซื้อ AAV 1000 1.85")
        return

    action, ticker, qty_str, price_str = m.groups()
    ticker = ticker.upper()
    qty = float(qty_str)
    price = float(price_str)
    is_buy = action == "ซื้อ"

    assets_content, assets_sha = gh_read("finances/assets.md")
    if assets_content is None:
        send("❌ ไม่พบไฟล์ assets.md")
        return

    # Detect market — existing table takes priority
    set_rows = get_table(assets_content, "### SET", ["### NYSE", "## 🪙", "## 💸"])
    us_rows = get_table(assets_content, "NYSE / NASDAQ", ["## 🪙", "## 💸"])
    if any(r[0].upper() == ticker for r in set_rows if r):
        is_us = False
    elif any(r[0].upper() == ticker for r in us_rows if r):
        is_us = True
    else:
        is_us = ticker in US_SYMBOLS or "." in ticker

    lines = assets_content.split("\n")
    lines, old_qty, old_cost, new_qty, new_cost, pnl = _trade_update(
        lines, ticker, qty, price, is_buy, is_us
    )

    if old_qty is None and not is_buy:
        send(f"❌ ไม่พบหุ้น {ticker} ในพอร์ต")
        return

    # Adjust เงินในพอร์ตลงทุน
    if is_us:
        usdthb = get_price("USDTHB=X") or USDTHB_RATE
        total_thb = qty * price * usdthb
    else:
        total_thb = qty * price
    portfolio_delta = -total_thb if is_buy else total_thb
    lines, port_old, port_new = _apply_row_delta(lines, "เงินในพอร์ตลงทุน", portfolio_delta)

    # Recalculate totals
    lines, _ = _recalc_total(lines, "## 🏦", "รวมเงินสด/บัญชี")
    lines = _recalc_net_worth_row(lines)
    gh_write("finances/assets.md", "\n".join(lines), assets_sha,
             f"assets: {'buy' if is_buy else 'sell'} {ticker} {qty}@{price}")

    # Write ledger
    today = datetime.now().strftime("%Y-%m-%d")
    ledger_content, ledger_sha = gh_read("finances/ledger.csv")
    if ledger_content:
        entry_type = "expense" if is_buy else "income"
        new_row = f"{today},{entry_type},stock,{qty * price:.2f},{'ซื้อ' if is_buy else 'ขาย'} {ticker}\n"
        gh_write("finances/ledger.csv", ledger_content + new_row, ledger_sha,
                 f"ledger: {'ซื้อ' if is_buy else 'ขาย'} {ticker}")

    # Build response
    cur = "$" if is_us else "฿"
    today_str = today
    if is_buy:
        old_qty_str = _fmt_shares(old_qty) if old_qty else "0"
        old_cost_str = f"{cur}{old_cost:.2f}" if old_cost else "ใหม่"
        parts = [
            "🛒 <b>ซื้อหุ้นแล้ว</b>",
            f"{ticker}: {old_qty_str} → {_fmt_shares(new_qty)} หุ้น",
            f"ต้นทุนเฉลี่ย: {old_cost_str} → {cur}{new_cost:.2f}",
        ]
    else:
        new_qty_str = _fmt_shares(new_qty) if new_qty > 0 else "0 (ขายหมด)"
        parts = [
            "💰 <b>ขายหุ้นแล้ว</b>",
            f"{ticker}: {_fmt_shares(old_qty)} → {new_qty_str} หุ้น",
            f"ราคาขาย: {cur}{price:.2f}" + (f" | ต้นทุน: {cur}{old_cost:.2f}" if old_cost else ""),
        ]
        if pnl is not None:
            pnl_str = f"+{cur}{pnl:.2f}" if pnl >= 0 else f"-{cur}{abs(pnl):.2f}"
            parts.append(f"กำไร: {pnl_str}")

    if port_old is not None and port_new is not None:
        parts.append(f"เงินในพอร์ต: {port_old:,.2f} → {port_new:,.2f} ฿")
    parts.append(f"วันที่: {today_str}")
    send("\n".join(parts))


# ─── Command handlers ─────────────────────────────────────────────────────────

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

    ledger_content, ledger_sha = gh_read("finances/ledger.csv")
    if ledger_content is None:
        send("❌ ไม่พบไฟล์ ledger.csv")
        return True
    new_row = f"{today},{entry_type},general,{amount},{note}\n"
    if not gh_write("finances/ledger.csv", ledger_content + new_row, ledger_sha, f"ledger: {entry_type} {amount} {note}"):
        send("❌ บันทึก ledger ไม่สำเร็จ")
        return True

    assets_content, assets_sha = gh_read("finances/assets.md")
    if assets_content is None:
        send("❌ ไม่พบไฟล์ assets.md")
        return True

    delta = float(amount) if sign == "+" else -float(amount)
    debt_name = detect_debt(note)

    acct_old, acct_new, debt_old, debt_new, old_car_note, new_car_note = update_assets_on_ledger(
        assets_content, assets_sha, account, delta, debt_name
    )

    is_reversal = sign == "+" and debt_name is not None
    emoji = "↩️" if is_reversal else ("💰" if sign == "+" else "💸")
    type_th = "รายรับ" if sign == "+" else "รายจ่าย"

    if debt_name:
        acct_line = (
            f"{account}: {acct_old:,.2f} → {acct_new:,.2f} ฿"
            if acct_old is not None else f"{account}: อัพเดทไม่สำเร็จ"
        )
        debt_line = (
            f"{debt_name}: {debt_old:,.2f} → {debt_new:,.2f} ฿"
            if debt_old is not None else f"{debt_name}: อัพเดทไม่สำเร็จ"
        )
        header = "ยกเลิกรายการ" if is_reversal else f"บันทึกแล้ว</b>\nประเภท: {type_th} (จ่ายหนี้)"
        parts = [
            f"{emoji} <b>{header}",
            f"จำนวน: {float(amount):,.0f} บาท",
            acct_line,
            debt_line,
        ]
        if old_car_note and new_car_note:
            parts.append(f"{old_car_note} → {new_car_note}")
        parts.append(f"วันที่: {today}")
        send("\n".join(parts))
    else:
        bal_line = (
            f"บัญชี: {account} → {acct_new:,.2f} ฿"
            if acct_new is not None else f"บัญชี: {account} (อัพเดทไม่สำเร็จ)"
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

    set_rows = get_table(content, "### SET", ["### NYSE", "## 🪙", "## 💸"])
    us_rows = get_table(content, "NYSE / NASDAQ", ["## 🪙", "## 💸"])

    # Fetch all prices in parallel
    symbols = (
        [f"{r[0]}.BK" for r in set_rows if len(r) >= 3] +
        [r[0] for r in us_rows if len(r) >= 3] +
        ["USDTHB=X"]
    )
    prices = get_prices(symbols) if symbols else {}
    usdthb = prices.get("USDTHB=X") or USDTHB_RATE

    out = ["💼 <b>สินทรัพย์ของเบน</b>", ""]

    # ── เงินสด / บัญชี ──
    cash = get_table(content, "เงินในบัญชี", ["## 📈", "## 🪙", "## 💸"])
    cash_total = 0.0
    if cash:
        out.append("🏦 <b>เงินสด / บัญชี</b>")
        for r in cash:
            if len(r) >= 2:
                if r[0].startswith("รวม"):
                    out.append("━━━━━━━━━━━━")
                    out.append(f"💵 รวม: <b>{r[1]} ฿</b>")
                    v = _parse_num(r[1])
                    if v is not None:
                        cash_total = v
                else:
                    out.append(f"  • {r[0]}: {r[1]} ฿")
        out.append("")

    # ── หุ้น SET ──
    set_market_thb = 0.0
    if set_rows:
        out.append("📈 <b>หุ้น SET</b>")
        set_cost_total = 0.0
        for r in set_rows:
            if len(r) < 3:
                continue
            ticker, shares_str, cost_str = r[0], r[1], r[2]
            shares = _parse_num(shares_str)
            cost = _parse_num(cost_str)
            price = prices.get(f"{ticker}.BK")
            if price is not None and shares is not None and cost is not None:
                market = shares * price
                cost_val = shares * cost
                set_market_thb += market
                set_cost_total += cost_val
                out.append(
                    f"  • {ticker}: {shares_str} หุ้น @ ฿{price:.2f} "
                    f"(ต้นทุน ฿{cost_str}) | มูลค่า ฿{market:,.0f}"
                )
            else:
                if shares is not None and cost is not None:
                    set_cost_total += shares * cost
                out.append(f"  • {ticker}: {shares_str} หุ้น @ ฿{cost_str} (ราคาไม่พร้อมใช้งาน)")
        out.append("━━━━━━━━━━━━")
        pnl = set_market_thb - set_cost_total
        pnl_str = f"+฿{pnl:,.0f}" if pnl >= 0 else f"-฿{abs(pnl):,.0f}"
        out.append(f"📊 รวมหุ้น SET: ฿{set_market_thb:,.0f} | กำไร/ขาดทุน: {pnl_str}")
        out.append("")

    # ── หุ้น US ──
    us_market_usd = 0.0
    if us_rows:
        out.append("📈 <b>หุ้น US</b>")
        us_cost_total_usd = 0.0
        for r in us_rows:
            if len(r) < 3:
                continue
            ticker, shares_str, cost_str = r[0], r[1], r[2]
            shares = _parse_num(shares_str)
            cost_usd = _parse_num(cost_str)
            price = prices.get(ticker)
            if price is not None and shares is not None and cost_usd is not None:
                market_usd = shares * price
                us_market_usd += market_usd
                us_cost_total_usd += shares * cost_usd
                out.append(
                    f"  • {ticker}: {shares_str} หุ้น @ ${price:.2f} "
                    f"(ต้นทุน ${cost_str}) | มูลค่า ${market_usd:.2f}"
                )
            else:
                if shares is not None and cost_usd is not None:
                    us_cost_total_usd += shares * cost_usd
                out.append(f"  • {ticker}: {shares_str} หุ้น @ ${cost_str} (ราคาไม่พร้อมใช้งาน)")
        out.append("━━━━━━━━━━━━")
        us_market_thb = us_market_usd * usdthb
        pnl_usd = us_market_usd - us_cost_total_usd
        pnl_str = f"+${pnl_usd:.2f}" if pnl_usd >= 0 else f"-${abs(pnl_usd):.2f}"
        out.append(
            f"📊 รวมหุ้น US: ${us_market_usd:.2f} | ≈ ฿{us_market_thb:,.0f} "
            f"(อัตรา ฿{usdthb:.2f}/$)"
        )
        out.append("")
    else:
        us_market_thb = 0.0

    # ── กองทุน ──
    fund_total = 0.0
    fund_rows = get_table(content, "กองทุน (Funds)", ["## 💸"])
    if fund_rows:
        out.append("🪙 <b>กองทุน</b>")
        for r in fund_rows:
            if len(r) >= 4:
                v = _parse_num(r[1]) or 0.0
                fund_total += v
                out.append(f"  • {r[0]}: ฿{v:,.2f} | DCA ฿{r[3]} {r[2]}")
        out.append("━━━━━━━━━━━━")
        out.append(f"💰 รวมกองทุน: ฿{fund_total:,.2f}")
        out.append("")

    # ── หนี้สิน ──
    debt_total = 0.0
    debt_rows = get_table(content, "หนี้สิน (Liabilities)", ["## "])
    if debt_rows:
        out.append("💸 <b>หนี้สิน</b>")
        total_str = ""
        for r in debt_rows:
            name = r[0] if r else ""
            if "รวมหนี้สิน" in name and len(r) >= 2:
                total_str = r[1].rstrip("+")
                v = _parse_num(total_str)
                if v is not None:
                    debt_total = v
            elif "มูลค่าสุทธิ" in name:
                pass  # skip stored value; recompute below
            elif not name.startswith("รวม") and len(r) >= 2:
                note = f" ({r[3]})" if len(r) > 3 and r[3].strip() else ""
                out.append(f"  • {r[0]}: {r[1]} ฿{note}")
        if total_str:
            out.append("━━━━━━━━━━━━")
            out.append(f"🔴 รวมหนี้สิน: <b>{total_str} ฿</b>")
        out.append("")

    # ── มูลค่าสุทธิ (live) ──
    total_assets = cash_total + set_market_thb + us_market_thb + fund_total
    net_worth = total_assets - debt_total
    sign = "+" if net_worth >= 0 else ""
    out.append(f"📊 <b>มูลค่าสุทธิ (live): {sign}{net_worth:,.0f} ฿</b>")

    send("\n".join(out))


def handle_fund(text):
    m = re.match(r"^/fund\s+(\S+)\s+(\d+(?:\.\d+)?)\s*$", text.strip())
    if not m:
        send("รูปแบบ: /fund [ชื่อกองทุน] [มูลค่า]\nตัวอย่าง: /fund SCBworld 788.53")
        return
    name_raw, value_str = m.groups()
    value = float(value_str)

    FUND_MAP = {
        "scbworld": "SCBworld(A)",
        "scbs&p500": "SCBS&P500(A)",
        "scbsp500": "SCBS&P500(A)",
        "SCBworld": "SCBworld(A)",
        "SCBS&P500": "SCBS&P500(A)",
    }
    fund_full = FUND_MAP.get(name_raw, FUND_MAP.get(name_raw.lower(), name_raw))

    content, sha = gh_read("finances/assets.md")
    if content is None:
        send("❌ ไม่พบไฟล์ assets.md")
        return

    lines = content.split("\n")
    old_val = new_val = None
    updated = []
    for line in lines:
        if line.startswith("|") and "---" not in line:
            cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
            if cells and cells[0] == fund_full and len(cells) >= 2:
                old_val = _parse_num(cells[1])
                new_val = value
                parts = line.split("|")
                parts[2] = f" {new_val:,.2f} "
                line = "|".join(parts)
        updated.append(line)

    if new_val is None:
        send(f"❌ ไม่พบกองทุน '{fund_full}'\nกองทุนที่รองรับ: SCBworld, SCBS&P500")
        return

    updated = _recalc_net_worth_row(updated)
    if gh_write("finances/assets.md", "\n".join(updated), sha, f"assets: fund {fund_full} = {value}"):
        old_str = f"{old_val:,.2f}" if old_val is not None else "?"
        send(f"🪙 <b>อัพเดทกองทุนแล้ว</b>\n{fund_full}: {old_str} → {new_val:,.2f} ฿")
    else:
        send("❌ อัพเดทไม่สำเร็จ")


HELP_TEXT = (
    "📋 <b>คำสั่งทั้งหมด</b>\n\n"
    "<b>บัญชี:</b>\n"
    "+500 เงินเดือน — บันทึกรายรับ (ตัด บัญชีใช้งาน)\n"
    "-200 ค่าอาหาร — บันทึกรายจ่าย (ตัด บัญชีใช้งาน)\n"
    "-200 ค่าอาหาร [เงินสด] — ระบุบัญชี\n"
    "-8218 ค่างวดรถ — จ่ายหนี้ (อัพเดทยอดหนี้ + งวด)\n"
    "+8218 ค่างวดรถ — ยกเลิก/แก้ไขรายการ\n"
    "บัญชีที่รองรับ: บัญชีเงินเย็น, บัญชีใช้งาน, Prepaid Card, เงินสด, เงินในพอร์ตลงทุน\n\n"
    "/summary — สรุปรายรับจ่ายเดือนนี้\n\n"
    "<b>พอร์ต:</b>\n"
    "/assets — ดูสรุปสินทรัพย์\n"
    "/fund SCBworld 788.53 — อัพเดทมูลค่ากองทุน\n"
    "ซื้อ AAV 1000 1.85 — ซื้อหุ้น SET\n"
    "ซื้อ DOCN 0.5 95.00 — ซื้อหุ้น US\n"
    "ขาย GULF 10 52.00 — ขายหุ้น\n\n"
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
        if re.match(r"^(ซื้อ|ขาย)\s", text):
            handle_trade(text)
        elif re.match(r"^[+-]\d", text):
            handle_ledger(text)
        elif text == "/summary":
            handle_summary()
        elif text == "/assets":
            handle_assets()
        elif text.startswith("/fund"):
            handle_fund(text)
        elif text in ("/help", "/start"):
            send(HELP_TEXT)
        else:
            send("ไม่เข้าใจคำสั่ง พิม /help ดูเมนูทั้งหมด")

    def log_message(self, format, *args):
        pass
