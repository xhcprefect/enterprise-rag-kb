"""告警链路测试用的最小 fixture（只服务测试，不接触业务/外网）。

两个端口：
  --webhook-port  冒充 generic webhook 接收器：收到 Alertmanager 的 POST 后把
                  {"seq","status","alerts","groupKey"} 按行追加到 --out；
  --metrics-port  暴露 /metrics 与 /control 控制平面：
                  ragkb_alerting_fixture_probe 的取值由 /control/set?v=0|1
                  切换，供真实 Prometheus 规则点火/熄灭，从而走真实的
                  firing -> resolved（Prometheus 发送 EndsAt）路径。

只用标准库；用 `python -I` 启动，避免当前目录被投放的 json.py 等模块影响。
"""

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4 * 1024 * 1024:
            self.send_response(413)
            self.end_headers()
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        if not isinstance(payload, dict):
            self.send_response(400)
            self.end_headers()
            return
        alerts = payload.get("alerts")
        probe_alerts = [
            {"status": alert.get("status"), "fingerprint": alert.get("fingerprint")}
            for alert in (alerts[:100] if isinstance(alerts, list) else [])
            if isinstance(alert, dict) and isinstance(alert.get("labels"), dict)
            and alert["labels"].get("alertname") == "RagkbAlertingDeliveryProbe"
        ]
        record = {
            "status": payload.get("status"),
            "alerts": len(alerts) if isinstance(alerts, list) else -1,
            "groupKey": payload.get("groupKey"),
            "probe_alerts": probe_alerts,
        }
        server = self.server
        with server.lock:
            server.seq += 1
            record["seq"] = server.seq
            with open(server.out_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):  # 静默：不把 URL/请求内容打到 stdout
        return


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        server = self.server
        if parsed.path == "/ready":
            self.send_response(200)
            self.end_headers()
            return
        if parsed.path == "/control/set":
            raw = parse_qs(parsed.query).get("v", [""])[0]
            if raw not in ("0", "1"):
                self.send_response(400)
                self.end_headers()
                return
            with server.lock:
                server.probe = int(raw)
            self.send_response(200)
            self.end_headers()
            return
        if parsed.path == "/metrics":
            with server.lock:
                value = server.probe
            body = (
                "# HELP ragkb_alerting_fixture_probe Synthetic probe for delivery tests.\n"
                "# TYPE ragkb_alerting_fixture_probe gauge\n"
                f"ragkb_alerting_fixture_probe {value}\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def _serve(handler, port, **attrs):
    server = _Server(("0.0.0.0", port), handler)
    server.lock = threading.Lock()
    server.seq = 0
    server.probe = 1
    for key, value in attrs.items():
        setattr(server, key, value)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--webhook-port", type=int, required=True)
    parser.add_argument("--metrics-port", type=int, required=True)
    args = parser.parse_args()

    webhook = _serve(WebhookHandler, args.webhook_port, out_path=args.out)
    metrics = MetricsHandler
    metrics_server = _serve(metrics, args.metrics_port)

    threading.Thread(target=metrics_server.serve_forever, daemon=True).start()
    print(json.dumps({"ready": True, "webhook_port": args.webhook_port}), flush=True)
    try:
        webhook.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        webhook.server_close()
        metrics_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
