#!/usr/bin/env python3
from __future__ import annotations

import io
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image


APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
WEB_ROOT = APP_DIR / "web"
ROTATOR_SCRIPT = APP_DIR / "photo_auto_rotate.py"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
APPROVAL_DIR = DATA_DIR / "approvals"
SELECTION_DIR = DATA_DIR / "selections"
ALLOWED_ROOT = Path(os.environ.get("ALLOWED_ROOT", "/storage")).resolve()
PORT = int(os.environ.get("WEB_PORT", "8321"))
MAX_SELECTION = 5000

DEFAULT_CONFIG = {
    "source": str(ALLOWED_ROOT / "vol1"),
    "recursive": True,
    "min_confidence": 1.35,
    "allow_180": False,
    "min_age_minutes": 10,
    "cpu_workers": 2,
    "acceleration": "auto",
}


def safe_source(value: str) -> Path:
    normalized = value.strip()
    if normalized in {"/vol1", "/vol2"} or normalized.startswith(("/vol1/", "/vol2/")):
        normalized = str(ALLOWED_ROOT / normalized.lstrip("/"))
    path = Path(normalized).resolve()
    try:
        path.relative_to(ALLOWED_ROOT)
    except ValueError as exc:
        raise ValueError("照片目录必须位于 /storage 下") from exc
    if not path.is_dir():
        raise ValueError("照片目录不存在，或当前容器无法访问")
    return path


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_CONFIG.copy()
    return {**DEFAULT_CONFIG, **data}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fast_file_fingerprint(path: Path, sample_size: int = 64 * 1024) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"v1:{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    offsets = {0}
    if stat.st_size > sample_size:
        offsets.add(max(0, (stat.st_size - sample_size) // 2))
        offsets.add(max(0, stat.st_size - sample_size))
    with path.open("rb") as handle:
        for offset in sorted(offsets):
            handle.seek(offset)
            digest.update(offset.to_bytes(8, "big"))
            digest.update(handle.read(sample_size))
    return digest.hexdigest()


def save_config(data: dict) -> dict:
    acceleration = str(data.get("acceleration", "auto"))
    if acceleration not in {"auto", "gpu", "cpu"}:
        acceleration = "auto"
    config = {
        "source": str(safe_source(str(data.get("source", DEFAULT_CONFIG["source"])))),
        "recursive": bool(data.get("recursive", True)),
        "min_confidence": max(1.01, min(10.0, float(data.get("min_confidence", 1.35)))),
        "allow_180": bool(data.get("allow_180", False)),
        "min_age_minutes": max(0, min(10080, int(data.get("min_age_minutes", 10)))),
        "cpu_workers": max(1, min(4, int(data.get("cpu_workers", 2)))),
        "acceleration": acceleration,
    }
    write_json_atomic(CONFIG_FILE, config)
    return config


def newest_json(directory: Path, kind: str) -> tuple[Path | None, dict | None]:
    if not directory.exists():
        return None, None
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") == kind and data.get("schema") == 2:
            return path, data
    return None, None


def latest_scan() -> tuple[Path | None, dict | None]:
    return newest_json(DATA_DIR / "scans", "photo-orientation-scan")


def latest_scan_progress() -> dict | None:
    progress_dir = DATA_DIR / "scans" / "in-progress"
    if not progress_dir.exists():
        return None
    for path in sorted(progress_dir.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") == "photo-orientation-scan-progress":
            return {
                key: data.get(key)
                for key in (
                    "scan_id",
                    "source",
                    "started_at",
                    "updated_at",
                    "backend",
                    "completed",
                    "total",
                    "counts",
                )
            }
    return None


def latest_task() -> tuple[Path | None, dict | None]:
    task_root = DATA_DIR / "tasks"
    if not task_root.exists():
        return None, None
    manifests = list(task_root.glob("*/manifest.json"))
    for path in sorted(manifests, key=lambda item: item.stat().st_mtime_ns, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") == "photo-orientation-task" and data.get("schema") == 2:
            return path, data
    return None, None


def public_scan_summary(path: Path | None, scan: dict | None) -> dict | None:
    if path is None or scan is None:
        return None
    return {
        "name": path.name,
        "created_at": scan.get("created_at"),
        "finished_at": scan.get("finished_at"),
        "source": scan.get("source"),
        "counts": scan.get("counts", {}),
    }


def ensure_scan_backend_safe(scan: dict) -> None:
    if scan.get("backend") == "opencl":
        raise ValueError("该扫描由已禁用的 OpenCL 核显后端生成，请升级后使用 CPU 重新扫描")
    if scan.get("model") != "yunet-2023mar-evidence-v2":
        raise ValueError("该扫描使用旧版方向判断规则生成，请使用 2.1.2 重新扫描")


def public_task_summary(path: Path | None, task: dict | None) -> dict | None:
    if path is None or task is None:
        return None
    return {
        "task_id": task.get("task_id"),
        "created_at": task.get("created_at"),
        "source": task.get("source"),
        "summary": task.get("summary", {}),
        "rollback_available": any(item.get("status") in {"applied", "pending"} for item in task.get("results", [])),
    }


class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.mode: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.exit_code: int | None = None
        self.lines: deque[str] = deque(maxlen=500)

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

    def start(self, mode: str, config: dict, manifest: Path | None = None) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("已有任务正在运行")
            command = [
                "python3",
                str(ROTATOR_SCRIPT),
                "--source",
                config["source"],
                "--work",
                str(DATA_DIR),
                "--mode",
                mode,
            ]
            if mode == "scan":
                command.extend(
                    [
                        "--recursive",
                        "yes" if config["recursive"] else "no",
                        "--min-confidence",
                        str(config["min_confidence"]),
                        "--allow-180",
                        "yes" if config["allow_180"] else "no",
                        "--min-age-minutes",
                        str(config["min_age_minutes"]),
                        "--cpu-workers",
                        str(config["cpu_workers"]),
                        "--acceleration",
                        config["acceleration"],
                        "--checkpoint-every",
                        "25",
                    ]
                )
            else:
                if manifest is None:
                    raise RuntimeError("缺少任务清单")
                command.extend(["--manifest", str(manifest)])

            self.lines.clear()
            labels = {
                "scan": "只读扫描",
                "apply-manifest": "EXIF 元数据安全写入",
                "rollback-task": "任务回滚",
            }
            self.lines.append(f"启动{labels[mode]}：{config['source']}")
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
                start_new_session=(os.name != "nt"),
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
            if os.name == "nt":
                self.process.terminate()
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
            self.lines.append("已请求停止任务；已完成的照片仍可按任务清单回滚。")


JOB = Job()


class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoOrientation/2.1.2"

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
        if length > 5 * 1024 * 1024:
            raise ValueError("请求过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def serve_thumbnail(self, query: dict[str, list[str]]) -> None:
        scan_name = Path(query.get("scan", [""])[0]).name
        item_id = query.get("id", [""])[0]
        scan_path = DATA_DIR / "scans" / scan_name
        if not scan_name or not item_id or not scan_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        item = next((entry for entry in scan.get("items", []) if entry.get("id") == item_id), None)
        if item is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        source = safe_source(scan["source"])
        photo = (source / item["relative_path"]).resolve()
        try:
            photo.relative_to(source)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not photo.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        with Image.open(photo) as image:
            image.thumbnail((720, 720))
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, "JPEG", quality=82, optimize=True)
        body = output.getvalue()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/status":
            scan_path, scan = latest_scan()
            task_path, task = latest_task()
            self.json_response(
                {
                    **JOB.status(),
                    "scan": public_scan_summary(scan_path, scan),
                    "scan_progress": latest_scan_progress(),
                    "task": public_task_summary(task_path, task),
                }
            )
            return
        if parsed.path == "/api/config":
            self.json_response(load_config())
            return
        if parsed.path == "/api/selections":
            scan_name = Path(query.get("scan", [""])[0]).name
            selection_path = SELECTION_DIR / f"{scan_name}.json"
            if not scan_name or not selection_path.is_file():
                self.json_response({"scan": scan_name, "items": []})
                return
            self.json_response(json.loads(selection_path.read_text(encoding="utf-8")))
            return
        if parsed.path == "/api/candidates":
            scan_name = Path(query.get("scan", [""])[0]).name
            scan_path = DATA_DIR / "scans" / scan_name
            if not scan_name or not scan_path.is_file():
                self.json_response({"error": "扫描结果不存在"}, HTTPStatus.NOT_FOUND)
                return
            scan = json.loads(scan_path.read_text(encoding="utf-8"))
            status_filter = query.get("status", ["suggested"])[0]
            allowed_filters = {"suggested", "manual-review", "probably-correct", "error", "all"}
            if status_filter not in allowed_filters:
                status_filter = "suggested"
            items = scan.get("items", [])
            if status_filter != "all":
                items = [item for item in items if item.get("status") == status_filter]
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = max(1, min(100, int(query.get("limit", ["40"])[0])))
            public_items = [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "relative_path",
                        "width",
                        "height",
                        "orientation",
                        "status",
                        "suggested_angle",
                        "confidence",
                        "reason",
                    )
                }
                for item in items[offset : offset + limit]
            ]
            self.json_response(
                {
                    "scan": scan_name,
                    "source": scan.get("source"),
                    "status": status_filter,
                    "total": len(items),
                    "offset": offset,
                    "items": public_items,
                }
            )
            return
        if parsed.path == "/api/thumbnail":
            try:
                self.serve_thumbnail(query)
            except (OSError, ValueError, json.JSONDecodeError):
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/browse":
            try:
                requested = query.get("path", ["/storage"])[0]
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
        if parsed.path == "/api/export-scan":
            name = Path(query.get("name", [""])[0]).name
            path = DATA_DIR / "scans" / name
            if not name or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{quote(name)}"')
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
            if self.path == "/api/selections":
                scan_name = Path(str(payload.get("scan", ""))).name
                items = payload.get("items", [])
                if not scan_name or not isinstance(items, list) or len(items) > MAX_SELECTION:
                    raise ValueError("人工选择保存请求无效")
                clean_items = []
                for item in items:
                    angle = int(item.get("angle", 0))
                    if angle not in {90, 180, 270}:
                        raise ValueError("人工选择角度无效")
                    clean_items.append({"id": str(item.get("id", "")), "angle": angle})
                selection_path = SELECTION_DIR / f"{scan_name}.json"
                write_json_atomic(
                    selection_path,
                    {
                        "schema": 2,
                        "kind": "photo-orientation-selections",
                        "scan": scan_name,
                        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "items": clean_items,
                    },
                )
                self.json_response({"saved": len(clean_items)})
                return
            if self.path == "/api/scan":
                config = save_config(payload.get("config", load_config()))
                JOB.start("scan", config)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/apply":
                if payload.get("confirm") != "APPLY METADATA":
                    raise ValueError("确认文字不正确")
                selections = payload.get("items")
                if not isinstance(selections, list) or not selections:
                    raise ValueError("请至少选择一张照片")
                if len(selections) > MAX_SELECTION:
                    raise ValueError(f"单次最多处理 {MAX_SELECTION} 张照片")
                scan_name = Path(str(payload.get("scan", ""))).name
                scan_path = DATA_DIR / "scans" / scan_name
                if not scan_name or not scan_path.is_file():
                    raise ValueError("扫描结果不存在")
                scan = json.loads(scan_path.read_text(encoding="utf-8"))
                ensure_scan_backend_safe(scan)
                config = save_config(payload.get("config", load_config()))
                if Path(scan["source"]).resolve() != Path(config["source"]).resolve():
                    raise ValueError("扫描目录与当前目录不一致")
                by_id = {item["id"]: item for item in scan.get("items", [])}
                approved = []
                seen: set[str] = set()
                for selection in selections:
                    item_id = str(selection.get("id", ""))
                    if item_id in seen:
                        raise ValueError("审批清单包含重复照片")
                    seen.add(item_id)
                    item = by_id.get(item_id)
                    if item is None or item.get("orientation") != 1:
                        raise ValueError("审批项目无效或照片已有 EXIF 方向")
                    angle = int(selection.get("angle", 0))
                    if angle not in {90, 180, 270}:
                        raise ValueError("只允许选择 90、180 或 270 度")
                    target = (Path(scan["source"]) / item["relative_path"]).resolve()
                    try:
                        target.relative_to(Path(scan["source"]).resolve())
                    except ValueError as exc:
                        raise ValueError("照片路径超出扫描目录") from exc
                    if not target.is_file():
                        raise ValueError("待处理照片不存在")
                    stat = target.stat()
                    if stat.st_size != int(item.get("size", -1)) or stat.st_mtime_ns != int(item.get("mtime_ns", -1)):
                        raise ValueError(f"照片在扫描后发生变化，请重新扫描：{item['relative_path']}")
                    expected_fingerprint = str(item.get("scan_fingerprint", ""))
                    if not expected_fingerprint:
                        raise ValueError(
                            f"Legacy scan lacks safety fingerprint; rescan required: {item['relative_path']}"
                        )
                    if fast_file_fingerprint(target) != expected_fingerprint:
                        raise ValueError(
                            f"Photo content changed after scan; rescan required: {item['relative_path']}"
                        )
                    approved.append(
                        {
                            "id": item_id,
                            "relative_path": item["relative_path"],
                            "file_sha256": sha256_file(target),
                            "angle": angle,
                        }
                    )
                approval = {
                    "schema": 2,
                    "kind": "photo-orientation-approval",
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source": scan["source"],
                    "scan": str(scan_path),
                    "items": approved,
                }
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                approval_path = APPROVAL_DIR / f"approval-{stamp}.json"
                write_json_atomic(approval_path, approval)
                JOB.start("apply-manifest", config, approval_path)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/rollback":
                if payload.get("confirm") != "ROLLBACK":
                    raise ValueError("确认文字不正确")
                task_path, task = latest_task()
                if task_path is None or task is None:
                    raise ValueError("没有可回滚的 2.x 任务")
                config = save_config(payload.get("config", load_config()))
                if Path(task["source"]).resolve() != Path(config["source"]).resolve():
                    raise ValueError("任务目录与当前目录不一致")
                JOB.start("rollback-task", config, task_path)
                self.json_response(JOB.status(), HTTPStatus.ACCEPTED)
                return
            if self.path == "/api/stop":
                JOB.stop()
                self.json_response(JOB.status())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        try:
            save_config(DEFAULT_CONFIG)
        except ValueError:
            write_json_atomic(CONFIG_FILE, DEFAULT_CONFIG)
    print(f"照片方向安全修正 2.1.2 Web UI: 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
