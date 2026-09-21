"""以现有镜像验证 Alertmanager 原生 webhook 的触发/恢复，仅连接回环接收端。"""

import argparse
import json
import socket
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def exercise(config: Path, output: Path, image: str, rules: Path | None = None) -> None:
    if output.exists():
        raise ValueError("不覆盖已有告警证据")
    received: list[dict[str, object]] = []
    signal = threading.Condition()
    worker_fresh = threading.Event()
    worker_fresh.set()

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            body=f"# TYPE openreviewer_workers_fresh gauge\nopenreviewer_workers_fresh {int(worker_fresh.is_set())}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type","text/plain; version=0.0.4")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 128 * 1024:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(size))
            with signal:
                received.append({"received_at": datetime.now(UTC).isoformat(), "payload": payload})
                signal.notify_all()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as port_reservation:
        port_reservation.bind(("127.0.0.1", 0))
        port = port_reservation.getsockname()[1]
    name = "openreviewer-alert-test-" + uuid4().hex[:12]
    started = False
    prometheus_started = False
    prometheus_port = None
    prometheus_name = name + "-prometheus"
    receiver_port = receiver.server_port
    produced: dict[str, str] = {}

    def request(path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(f"http://127.0.0.1:{port}" + path, data=body,
            headers={"Content-Type":"application/json"}), timeout=3) as response:
            return response.read()

    def wait_for(status):
        with signal:
            if not signal.wait_for(lambda: any(item["payload"]["status"] == status for item in received), timeout=45):
                raise AssertionError(f"未收到 {status} 告警")

    try:
        with tempfile.TemporaryDirectory(prefix="alert-smoke-", dir=output.parent) as directory:
            root = Path(directory)
            configured = root / "alertmanager.yml"
            configured.write_text(config.read_text().replace("group_wait: 30s", "group_wait: 1s")
                .replace("group_interval: 5m", "group_interval: 1s"), encoding="utf-8")
            destination = root / "destination"
            destination.write_text(f"http://127.0.0.1:{receiver_port}/alerts", encoding="utf-8")
            configured.chmod(0o644)
            destination.chmod(0o644)
            subprocess.run(["docker", "run", "-d", "--pull", "never", "--name", name,
                "--network", "host", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", "/alertmanager:uid=65534,gid=65534", "-v", f"{configured}:/etc/alertmanager/alertmanager.yml:ro",
                "-v", f"{destination}:/run/secrets/alert-webhook-url:ro", image,
                "--config.file=/etc/alertmanager/alertmanager.yml", "--storage.path=/alertmanager",
                f"--web.listen-address=127.0.0.1:{port}", "--cluster.listen-address="], check=True, capture_output=True)
            started = True
            for _ in range(40):
                try:
                    request("/-/ready")
                    break
                except URLError:
                    time.sleep(0.25)
            else:
                raise AssertionError("隔离 Alertmanager 未就绪")
            if rules is None:
                now = datetime.now(UTC)
                alert = {"labels":{"alertname":"OpenReviewerIsolationProbe", "severity":"warning"},
                    "annotations":{"summary":"隔离链路测试"}, "startsAt":now.isoformat(),
                    "endsAt":(now + timedelta(minutes=2)).isoformat()}
                produced["firing"] = datetime.now(UTC).isoformat()
                request("/api/v2/alerts", [alert])
                wait_for("firing")
                alert["endsAt"] = datetime.now(UTC).isoformat()
                produced["resolved"] = datetime.now(UTC).isoformat()
                request("/api/v2/alerts", [alert])
                wait_for("resolved")
            else:
                # 直接使用现有 WorkerMissing 规则表达式，只缩短 for 和评估间隔。
                source=rules.read_text(encoding="utf-8")
                rule="      - alert: OpenReviewerWorkerMissing"+source.split("      - alert: OpenReviewerWorkerMissing",1)[1].split("      - alert:",1)[0]
                test_rules=root/"rules.yml"
                test_rules.write_text("groups:\n  - name: isolated\n    rules:\n"+rule.replace("for: 2m","for: 1s"),encoding="utf-8")
                test_rules.chmod(0o644)
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1",0))
                    prometheus_port=reservation.getsockname()[1]
                prometheus_config=root/"prometheus.yml"
                prometheus_config.write_text(f"""global:
  scrape_interval: 1s
  evaluation_interval: 1s
rule_files: [/etc/prometheus/rules.yml]
alerting:
  alertmanagers:
    - static_configs:
        - targets: ['127.0.0.1:{port}']
scrape_configs:
  - job_name: isolated-worker
    static_configs:
      - targets: ['127.0.0.1:{receiver_port}']
""",encoding="utf-8")
                prometheus_config.chmod(0o644)
                subprocess.run(["docker","run","-d","--pull","never","--name",prometheus_name,
                    "--network","host","--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
                    "--tmpfs","/prometheus:uid=65534,gid=65534","-v",f"{prometheus_config}:/etc/prometheus/prometheus.yml:ro",
                    "-v",f"{test_rules}:/etc/prometheus/rules.yml:ro","prom/prometheus:v3.5.0",
                    "--config.file=/etc/prometheus/prometheus.yml",f"--web.listen-address=127.0.0.1:{prometheus_port}",
                    "--storage.tsdb.path=/prometheus"],check=True,capture_output=True)
                prometheus_started=True
                produced["firing"]=datetime.now(UTC).isoformat()
                worker_fresh.clear()
                wait_for("firing")
                produced["resolved"]=datetime.now(UTC).isoformat()
                worker_fresh.set()
                wait_for("resolved")
            for item in received:
                payload = item["payload"]
                assert payload["version"] == "4" and payload["receiver"] == "openreviewer-operations"
                assert payload["alerts"][0]["labels"]["alertname"] == ("OpenReviewerWorkerMissing" if rules else "OpenReviewerIsolationProbe")
    finally:
        try:
            if prometheus_started:
                subprocess.run(["docker","rm","-f",prometheus_name],check=True,capture_output=True)
        finally:
            try:
                if started:
                    subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True)
            finally:
                receiver.shutdown()
                receiver.server_close()
                thread.join(timeout=5)
    assert not thread.is_alive()
    ports=[port,receiver_port]+([prometheus_port] if prometheus_port else [])
    for checked_port in ports:
        with socket.socket() as check:
            assert check.connect_ex(("127.0.0.1", checked_port)) != 0
    output.write_text(json.dumps({"image":image, "scope":"isolated_loopback_only",
        "produced_at":produced, "received":received, "container_removed":True,
        "closed_ports":ports,"rule_evaluated":"OpenReviewerWorkerMissing" if rules else None,
        "external_channel_verified":False}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="prom/alertmanager:v0.28.1")
    parser.add_argument("--rules",type=Path,help="验证真实 WorkerMissing 规则到触发/恢复接收的链路")
    args = parser.parse_args()
    exercise(args.config, args.output, args.image,args.rules)
