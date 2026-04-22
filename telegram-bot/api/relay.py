import json
import os
import requests
from http.server import BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
RELAY_SECRET = os.environ.get("RELAY_SECRET")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        try:
            data = json.loads(body)
        except Exception:
            self.wfile.write(b'{"ok":false,"error":"invalid json"}')
            return

        # ตรวจ secret
        if data.get("secret") != RELAY_SECRET:
            self.wfile.write(b'{"ok":false,"error":"unauthorized"}')
            return

        text = data.get("text", "")
        if not text:
            self.wfile.write(b'{"ok":false,"error":"no text"}')
            return

        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        )
        result = r.json()
        self.wfile.write(json.dumps({"ok": result.get("ok", False)}).encode())

    def log_message(self, format, *args):
        pass
