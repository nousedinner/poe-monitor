"""Synthetic Tencent-shaped HTTP fixture. Never used by production containers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlparse

phase = 1


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global phase
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/fixture/phase":
            phase = int(body)
            self.send({"phase": phase})
            return
        self.send(
            {
                "result": [] if phase == 3 else ["fixture-A", "fixture-B"],
                "id": "fixture-query",
                "total": 0 if phase == 3 else 2,
            }
        )

    def do_GET(self):
        if self.path == "/healthz":
            self.send({"ok": True})
            return
        ids = urlparse(self.path).path.rsplit("/", 1)[-1].split(",")
        rows = []
        for key in ids:
            rows.append(
                {
                    "id": key,
                    "item": {
                        "id": "physical-" + key,
                        "name": "相同名称",
                        "typeLine": "测试基底",
                        "frameType": 2,
                        "explicitMods": ["+10 力量"],
                    },
                    "listing": {
                        "price": {
                            "amount": 12.125 if key == "fixture-A" and phase == 2 else 10,
                            "currency": "divine",
                        },
                        "account": {"name": "fixture-account"},
                    },
                }
            )
        self.send({"result": rows})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18000), Handler).serve_forever()
