# Ben Telegram Bot

Webhook สำหรับ bot Micky — รับคำสั่งจาก Telegram แล้วอัพเดต finances files ใน GitHub

## คำสั่ง
- `+500 ค่ากาแฟ` — บันทึกรายรับ
- `-200 ค่าอาหาร` — บันทึกรายจ่าย
- `/summary` — สรุปรายรับจ่ายเดือนนี้
- `/assets` — ดูสรุปสินทรัพย์
- `/help` — แสดงเมนูทั้งหมด

## Environment Variables (ตั้งใน Vercel)
- `TELEGRAM_TOKEN` — bot token
- `GITHUB_TOKEN` — GitHub PAT (repo scope)
- `GITHUB_REPO` — เช่น `benladean/ben-finances`
- `CHAT_ID` — Telegram chat ID ของเบน
