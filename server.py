#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import subprocess
import threading
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
WEB_ROOT = APP_DIR / "web"
ROTATOR_SCRIPT = APP_DIR / "photo_auto_rotate.py"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
IMPORT_DIR = DATA_DIR / "imports"
ALLOWED_ROOT = Path(os.environ.get("ALLOWED_ROOT", "/storage")).resolve()
PORT = int(os.environ.get("WEB_PORT", "8321"))

DEFAULT_CONFIG = {
    "source": str(ALLOWED_ROOT / "vol1"),
    "recursive": True,
    "min_confidence": 1.35,
    "allow_180": False,
    "min_age_minutes": 10,
    "backup": True,
}


def safe_source(value: str) -> Path:
    normalized = value.strip()
    # 用户通常知道飞牛宿主机路径（/vol1、/vol2），网页中允许直接粘贴，
    # 后端自动换算为容器内的 /storage/vol1、/storage/vol2。
    if normalized in {"/vol1", "/vol2"} or normalized.startswith(("/vol1/", "/vol2/")):
        normalized = str(ALLOWED_ROOT / normalized.lstrip("/"))
    path = Path(normalized).resolve()
    try:
        path.relative_to(ALLOWED_ROOT)
    except ValueError as exc:
        raise ValueError("照片目录必须位于 /storage 下") from exc
    if not path.is_dir():
        raise ValueError("照片目录不存在或当前容器无法访问")
    return path


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()
    return {**DEFAULT_CONFIG, **data}


def save_config(data: dict) -> dict:
    config = {
        "source": str(safe_source(str(data.get("source", DEFAULT_CONFIG["source"])))),
        "recursive": bool(data.get("recursive", True)),
        "min_confidence": max(1.01, min(10.0, float(data.get("min_confidence", 1.35)))),
        "allow_180": bool(data.get("allow_180", False)),
        "min_age_minutes": max(0, min(10080, int(data.get("min_age_minutes", 10)))),
        "backup": bool(data.get("backup", True)),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(CONFIG_FILE)
    return config


class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.mode: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.exit_code: int | None = None
        self.lines: deque[str] = deque(maxlen=300)

    def status(self) -> dict:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "running": running,
                "mode": self.mode,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "exit_code": self.exit_code,
                "output": list(self.lines),
            }

    def start(self, mode: str, config: dict, input_csv: Path | None = None) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("已有任务正在运行")
            command = ["python3", str(ROTATOR_SCRIPT), "--source", config["source"], "--work", str(DATA_DIR)]
            if mode in {"restore-task", "refresh-task"}:
                if input_csv is None:
                    raise RuntimeError("缺少恢复清单")
                flag = "--restore-task-csv" if mode == "restore-task" else "--refresh-task-csv"
                command.extend([flag, str(input_csv)])
            else:
                command.extend(
                    [
                        "--mode",
                        mode,
                        "--recursive",
                        "yes" if config["recursive"] else "no",
                        "--min-confidence",
                        str(config["min_confidence"]),
                        "--allow-180",
                        "yes" if config["allow_180"] else "no",
                        "--min-age-minutes",
                        str(config["min_age_minutes"]),
                        "--backup",
                        "yes" if config["backup"] else "no",
                    ]
                )
            if input_csv is not None and mode not in {"restore-task", "refresh-task"}:
                command.extend(["--input-csv", str(input_csv)])
            self.lines.clear()
            labels = {"restore-task": "恢复本次任务原图", "refresh-task": "校验原图并刷新飞牛索引"}
            task_label = labels.get(mode, "CSV 导入执行" if input_csv else mode)
            self.lines.append(f"启动 {task_label} 任务：{config['source']}")
            self.mode = mode
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.finished_at = None
            self.exit_code = None
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            threading.Thread(target=self._collect, daemon=True).start()

    def _collect(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            with self.lock:
                self.lines.append(line.rstrip())
        code = process.wait()
        with self.lock:
            self.exit_code = code
            self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.lines.append("任务完成。" if code == 0 else f"任务异常结束，代码 {code}。")

    def stop(self) -> None:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                return
            self.process.terminate()
            self.lines.append("已请求停止任务。")


JOB = Job()


def list_logs() -> list[dict]:
    log_dir = DATA_DIR / "logs"
    if not log_dir.exists():
        return []
    result = []
    for path in sorted(log_dir.glob("*.csv"), key=lambda item: item.stat().st_mtime, reverse=True):
        result.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                "url": f"/api/log?name={quote(path.name)}",
            }
        )
    return result[:30]


def latest_changed_task_log() -> tuple[Path | None, int]:
    log_dir = DATA_DIR / "logs"
    if not log_dir.exists():
        return None, 0
    paths = sorted(log_dir.glob("photo-rotate-apply-*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
                count = sum(
                    1
                    for row in csv.DictReader(csv_file)
                    if (row.get("状态") or "").strip() in {"rotated-face", "normalized-exif"}
                )
            if count:
                return path, count
        except (OSError, UnicodeError):
            continue
    return None, 0


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoAutoRotate/0.1"

    def log_message(self, format: str, *args) -> None:
        return

    def json_response(self, payload, status=HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 25 * 1024 * 1024:
            raise ValueError("请求过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            recovery_log, recovery_count = latest_changed_task_log()
            self.json_response(
                {
                    **JOB.status(),
                    "logs": list_logs(),
                    "task_recovery": {
                        "available": recovery_log is not None,
                        "count": recovery_count,
                        "log": recovery_log.name if recovery_log else None,
                    },
                }
            )
            return
        if parsed.path == "/api/config":
            self.json_response(load_config())
            return
        if parsed.path == "/api/browse":
            try:
                requested = parse_qs(parsed.query).get("path", ["/storage"])[0]
                path = safe_source(requested)
                directories = [
                    {"name": child.name, "path": str(child)}
                    for child in sorted(path.iterdir(), key=lambda item: item.name.lower())
                    if child.is_dir() and not child.name.startswith(("@", ".", "#"))
                ][:500]
                parent = str(path.parent) if path != ALLOWED_ROOT else None
                self.json_response({"path": str(path), "parent": parent, "directories": directories})
            except (OSError, ValueError) as exc:
                self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/log":
            name = Path(parse_qs(parsed.query).get("name", [""])[0]).name
            path = DATA_DIR / "logs" / name
            if not name or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        relative = parsed.path.lstrip("/") or "index.html"
        path = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in path.parents and path != WEB_ROOT:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            path = WEB_ROOT / "index.html"
        body = path.read_bytes()
        mime = "text/html; charset=utf-8"
        if path.suffix == ".svg":
            mime = "image/svg+xml"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            if self.path == "/api/config":
                self.json_response(save_config(payload))
                return
            if self.path == "/api/run":
                mode = payload.get("mode")
                if mode not in {"scan", "apply"}:
                    raise ValueError("模式无效")
                if mode == "apply":
                    raise ValueError("为保护照片，正式处理已在紧急恢复版本中暂停；请先使用恢复按钮")
                if mode == "apply" and payload.get("confirm") != "ROTATE":
                    raise ValueError("正式执行需要输入 ROTATE 确认")
                config = save_config(payload.get("config", load_config()))
                JOB.start(mode, config)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/run-csv":
                raise ValueError("为保护照片，CSV 正式执行已暂停；请先使用恢复按钮")
                if payload.get("confirm") != "ROTATE":
                    raise ValueError("CSV 执行需要输入 ROTATE 确认")
                name = Path(str(payload.get("name", "scan.csv"))).name
                if not name.lower().endswith(".csv"):
                    raise ValueError("请选择 CSV 文件")
                csv_text = payload.get("csv")
                if not isinstance(csv_text, str) or not csv_text.strip():
                    raise ValueError("CSV 内容为空")
                if len(csv_text.encode("utf-8")) > 20 * 1024 * 1024:
                    raise ValueError("CSV 文件超过 20MB")
                config = save_config(payload.get("config", load_config()))
                IMPORT_DIR.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                csv_path = IMPORT_DIR / f"{timestamp}-{name}"
                csv_path.write_text(csv_text, encoding="utf-8")
                JOB.start("apply", config, input_csv=csv_path)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/restore-task":
                if payload.get("confirm") != "RESTORE":
                    raise ValueError("恢复操作需要输入 RESTORE 确认")
                config = save_config(payload.get("config", load_config()))
                recovery_log, recovery_count = latest_changed_task_log()
                if recovery_log is None or recovery_count == 0:
                    raise ValueError("没有找到可恢复的任务记录")
                JOB.start("restore-task", config, input_csv=recovery_log)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/refresh-task":
                if payload.get("confirm") != "REFRESH":
                    raise ValueError("索引刷新需要输入 REFRESH 确认")
                config = save_config(payload.get("config", load_config()))
                recovery_log, recovery_count = latest_changed_task_log()
                if recovery_log is None or recovery_count == 0:
                    raise ValueError("没有找到可刷新的任务记录")
                JOB.start("refresh-task", config, input_csv=recovery_log)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/stop":
                JOB.stop()
                self.json_response(JOB.status())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        try:
            save_config(DEFAULT_CONFIG)
        except ValueError:
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"照片自动回正 Web UI: 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
