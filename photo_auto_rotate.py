#!/usr/bin/env python3
"""fnOS Photo Orientation 2.1.

The program never rotates or re-encodes image pixels. Scans are read-only.
An approved JPEG is corrected by writing only EXIF Orientation to a staged
copy, verifying that decoded pixels are byte-for-byte identical, and then
atomically replacing the original.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from PIL import Image

try:
    import cv2
except ImportError:  # Unit tests can exercise the write path without OpenCV.
    cv2 = None


JPEG = {".jpg", ".jpeg"}
SKIP_DIR_NAMES = {
    ".stfolder",
    "@eaDir",
    "#recycle",
    ".Trash",
    ".AppleDouble",
    "__MACOSX",
    "fnos-photo-auto-rotate",
}
ANGLE_TO_ORIENTATION = {90: 6, 180: 3, 270: 8}
ORIENTATION_TO_ANGLE = {1: 0, 3: 180, 6: 90, 8: 270}
SCHEMA_VERSION = 2
MODEL_VERSION = "yunet-2023mar"
DEFAULT_MODEL = Path(os.environ.get("YUNET_MODEL", "/app/models/face_detection_yunet_2023mar.onnx"))
_CPU_DETECTOR = None


def yes(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fast_file_fingerprint(path: Path, sample_size: int = 64 * 1024) -> str:
    """Cheaply detect replacement without reading the whole photo."""
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


def decoded_pixel_fingerprint(path: Path) -> tuple[str, tuple[int, int], str]:
    """Hash decoded raster bytes, independent of EXIF metadata."""
    with Image.open(path) as image:
        image.load()
        digest = hashlib.sha256()
        digest.update(image.mode.encode("ascii", errors="replace"))
        digest.update(f"{image.width}x{image.height}".encode("ascii"))
        digest.update(image.tobytes())
        return digest.hexdigest(), image.size, image.mode


def read_image_info(path: Path) -> tuple[int, int, int]:
    with Image.open(path) as image:
        orientation = int(image.getexif().get(274, 1) or 1)
        return image.width, image.height, orientation


def safe_relative_path(root: Path, relative: str) -> Path:
    cleaned = relative.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/"):
        raise ValueError("相对路径无效")
    path = (root / cleaned).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("照片路径超出所选目录") from exc
    return path


def iter_jpegs(root: Path, recursive: bool) -> Iterator[Path]:
    iterator = root.rglob("*") if recursive else root.glob("*")
    for path in iterator:
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIR_NAMES or part.startswith(".") for part in relative_parts[:-1]):
            continue
        if path.is_file() and path.suffix.lower() in JPEG:
            yield path


def rotate_frame(frame, degrees: int):
    if cv2 is None:
        raise RuntimeError("当前环境缺少 OpenCV")
    if degrees == 0:
        return frame
    if degrees == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)


def load_for_detection(path: Path, longest_limit: int = 640):
    if cv2 is None:
        raise RuntimeError("当前环境缺少 OpenCV")
    flags = cv2.IMREAD_COLOR
    if hasattr(cv2, "IMREAD_IGNORE_ORIENTATION"):
        flags |= cv2.IMREAD_IGNORE_ORIENTATION
    frame = cv2.imread(str(path), flags)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    longest = max(height, width)
    if longest > longest_limit:
        scale = float(longest_limit) / longest
        frame = cv2.resize(
            frame,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return frame


def create_yunet_detector(model: Path, target: str):
    if cv2 is None:
        raise RuntimeError("当前环境缺少 OpenCV")
    if not model.is_file():
        raise RuntimeError(f"未找到 YuNet 模型：{model}")
    backend = cv2.dnn.DNN_BACKEND_OPENCV
    target_id = cv2.dnn.DNN_TARGET_CPU
    if target == "opencl":
        target_id = cv2.dnn.DNN_TARGET_OPENCL
    return cv2.FaceDetectorYN.create(
        str(model),
        "",
        (320, 320),
        0.72,
        0.3,
        5000,
        backend,
        target_id,
    )


def face_score(detector, frame) -> tuple[float, int]:
    detector.setInputSize((frame.shape[1], frame.shape[0]))
    _, faces = detector.detect(frame)
    if faces is None or len(faces) == 0:
        return 0.0, 0
    image_area = float(frame.shape[0] * frame.shape[1])
    area_score = sum((float(face[2]) * float(face[3])) / image_area for face in faces)
    confidence_score = sum(float(face[-1]) for face in faces)
    return len(faces) * 2.0 + area_score * 25.0 + confidence_score, len(faces)


@dataclass
class ScanItem:
    id: str
    relative_path: str
    width: int
    height: int
    orientation: int
    status: str
    suggested_angle: int
    confidence: float
    reason: str
    file_sha256: str
    size: int
    mtime_ns: int
    scan_fingerprint: str = ""
    scores: str = ""


def candidate_id(relative: str, fingerprint: str) -> str:
    return hashlib.sha256(f"{relative}\0{fingerprint}".encode("utf-8")).hexdigest()[:24]


def classify_frame(
    path: Path,
    relative: str,
    detector,
    min_confidence: float,
    allow_180: bool,
    frame=None,
) -> ScanItem:
    width, height, orientation = read_image_info(path)
    stat = path.stat()
    fingerprint = fast_file_fingerprint(path)

    if orientation != 1:
        return ScanItem(
            id=candidate_id(relative, fingerprint),
            relative_path=relative,
            width=width,
            height=height,
            orientation=orientation,
            file_sha256="",
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            scan_fingerprint=fingerprint,
            status="exif-managed",
            suggested_angle=ORIENTATION_TO_ANGLE.get(orientation, 0),
            confidence=0.0,
            reason=f"已有 EXIF Orientation={orientation}，2.1 不修改",
        )

    common = {
        "id": candidate_id(relative, fingerprint),
        "relative_path": relative,
        "width": width,
        "height": height,
        "orientation": orientation,
        "file_sha256": "",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "scan_fingerprint": fingerprint,
    }

    if frame is None:
        frame = load_for_detection(path)
    if frame is None:
        return ScanItem(
            **common,
            status="unreadable",
            suggested_angle=0,
            confidence=0.0,
            reason="OpenCV 无法读取照片",
        )

    angles = [0, 90, 270] + ([180] if allow_180 else [])
    scored: list[tuple[int, float, int]] = []
    for angle in angles:
        score, count = face_score(detector, rotate_frame(frame, angle))
        scored.append((angle, score, count))
    scored.sort(key=lambda item: item[1], reverse=True)
    scores_text = ",".join(f"{angle}:{score:.3f}/{count}" for angle, score, count in scored)
    best_angle, best_score, best_count = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    confidence = best_score / max(second_score, 0.01)

    if best_count == 0:
        status, reason = "manual-review", "未识别到可用于判断方向的人脸"
        best_angle = 0
    elif best_angle == 0:
        status, reason = "probably-correct", "人脸检测认为当前方向最可能正确"
    elif confidence < min_confidence:
        status, reason = "manual-review", "各方向检测结果接近，需要人工确认"
    else:
        status, reason = "suggested", "人脸检测建议，仅供人工确认，不会自动执行"

    return ScanItem(
        **common,
        status=status,
        suggested_angle=best_angle,
        confidence=confidence,
        reason=reason,
        scores=scores_text,
    )


def classify(path: Path, relative: str, cascade, min_confidence: float, allow_180: bool) -> ScanItem:
    """Backward-compatible wrapper; `cascade` is now a YuNet detector."""
    return classify_frame(path, relative, cascade, min_confidence, allow_180)


def init_cpu_worker(model_path: str) -> None:
    global _CPU_DETECTOR
    if cv2 is not None:
        cv2.setNumThreads(1)
    _CPU_DETECTOR = create_yunet_detector(Path(model_path), "cpu")


def classify_cpu_worker(args: tuple[str, str, float, bool]) -> dict:
    path_text, relative, min_confidence, allow_180 = args
    if _CPU_DETECTOR is None:
        raise RuntimeError("CPU 检测器未初始化")
    return asdict(
        classify_frame(
            Path(path_text),
            relative,
            _CPU_DETECTOR,
            min_confidence,
            allow_180,
        )
    )


def prepare_gpu_worker(args: tuple[str, str]) -> dict:
    if cv2 is not None:
        cv2.setNumThreads(1)
    path_text, relative = args
    path = Path(path_text)
    width, height, orientation = read_image_info(path)
    stat = path.stat()
    fingerprint = fast_file_fingerprint(path)
    common = {
        "id": candidate_id(relative, fingerprint),
        "relative_path": relative,
        "width": width,
        "height": height,
        "orientation": orientation,
        "file_sha256": "",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "scan_fingerprint": fingerprint,
    }
    if orientation != 1:
        return {
            "item": asdict(
                ScanItem(
                    **common,
                    status="exif-managed",
                    suggested_angle=ORIENTATION_TO_ANGLE.get(orientation, 0),
                    confidence=0.0,
                    reason=f"已有 EXIF Orientation={orientation}，2.1 不修改",
                )
            )
        }
    frame = load_for_detection(path)
    if frame is None:
        return {
            "item": asdict(
                ScanItem(
                    **common,
                    status="unreadable",
                    suggested_angle=0,
                    confidence=0.0,
                    reason="OpenCV 无法读取照片",
                )
            )
        }
    return {
        "common": common,
        "shape": list(frame.shape),
        "frame": frame.tobytes(),
    }


def classify_gpu_prepared(prepared: dict, detector, min_confidence: float, allow_180: bool) -> dict:
    if "item" in prepared:
        return prepared["item"]
    import numpy as np

    frame = np.frombuffer(prepared["frame"], dtype=np.uint8).reshape(prepared["shape"])
    common = prepared["common"]
    angles = [0, 90, 270] + ([180] if allow_180 else [])
    scored = []
    for angle in angles:
        score, count = face_score(detector, rotate_frame(frame, angle))
        scored.append((angle, score, count))
    scored.sort(key=lambda item: item[1], reverse=True)
    scores_text = ",".join(f"{angle}:{score:.3f}/{count}" for angle, score, count in scored)
    best_angle, best_score, best_count = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    confidence = best_score / max(second_score, 0.01)
    if best_count == 0:
        status, reason, best_angle = "manual-review", "未识别到可用于判断方向的人脸", 0
    elif best_angle == 0:
        status, reason = "probably-correct", "人脸检测认为当前方向最可能正确"
    elif confidence < min_confidence:
        status, reason = "manual-review", "各方向检测结果接近，需要人工确认"
    else:
        status, reason = "suggested", "YuNet 方向建议，仅供人工确认，不会自动执行"
    return asdict(
        ScanItem(
            **common,
            status=status,
            suggested_angle=best_angle,
            confidence=confidence,
            reason=reason,
            scores=scores_text,
        )
    )


def benchmark_acceleration(model: Path, requested: str, cpu_workers: int = 2) -> tuple[str, dict]:
    if cv2 is None:
        raise RuntimeError("当前环境缺少 OpenCV")
    details = {
        "requested": requested,
        "cpu_workers": cpu_workers,
        "opencl_available": bool(cv2.ocl.haveOpenCL()),
        "opencl_enabled": False,
    }
    if requested == "cpu":
        return "cpu", details
    if not details["opencl_available"] or not Path("/dev/dri").exists():
        details["fallback"] = "未检测到 /dev/dri 或 OpenCL"
        return "cpu", details
    try:
        cv2.setNumThreads(1)
        cv2.ocl.setUseOpenCL(True)
        cpu_detector = create_yunet_detector(model, "cpu")
        gpu_detector = create_yunet_detector(model, "opencl")
        import numpy as np

        sample = np.random.default_rng(42).integers(0, 256, (320, 320, 3), dtype=np.uint8)
        cpu_detector.setInputSize((320, 320))
        gpu_detector.setInputSize((320, 320))
        cpu_detector.detect(sample)
        gpu_detector.detect(sample)
        started = time.perf_counter()
        for _ in range(8):
            cpu_detector.detect(sample)
        details["cpu_seconds_8"] = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(8):
            gpu_detector.detect(sample)
        details["opencl_seconds_8"] = time.perf_counter() - started
        details["opencl_enabled"] = bool(cv2.ocl.useOpenCL())
        details["estimated_cpu_pool_seconds_8"] = details["cpu_seconds_8"] / max(1, cpu_workers)
        details["opencl_faster"] = details["opencl_seconds_8"] < (
            details["estimated_cpu_pool_seconds_8"] * 0.95
        )
        if details["opencl_enabled"] and (requested == "gpu" or details["opencl_faster"]):
            return "opencl", details
        if not details["opencl_enabled"]:
            details["fallback"] = "OpenCL 运行时未真正启用"
        else:
            details["fallback"] = "核显实测没有比 CPU 更快"
    except Exception as exc:
        details["fallback"] = f"GPU 自检失败：{exc}"
    return "cpu", details


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
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def scan_identity(source: Path, settings: dict) -> str:
    payload = json.dumps(
        {"source": str(source), "settings": settings, "model": MODEL_VERSION},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def read_journal(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    items = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("relative_path"):
                items.append(item)
    return items


def append_journal(handle, item: dict, durable: bool) -> None:
    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    handle.flush()
    if durable:
        os.fsync(handle.fileno())


def rewrite_journal_atomic(path: Path, items: list[dict]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def bounded_process_map(executor, function, arguments, max_pending: int):
    iterator = iter(arguments)
    futures = {}
    for _ in range(max_pending):
        try:
            argument = next(iterator)
        except StopIteration:
            break
        futures[executor.submit(function, argument)] = argument
    while futures:
        done, _ = concurrent.futures.wait(
            futures,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        for future in done:
            argument = futures.pop(future)
            yield future, argument
            try:
                next_argument = next(iterator)
            except StopIteration:
                continue
            futures[executor.submit(function, next_argument)] = next_argument


def scan(
    source: Path,
    work: Path,
    recursive: bool,
    min_confidence: float,
    allow_180: bool,
    min_age_minutes: int,
    cpu_workers: int,
    acceleration: str,
    checkpoint_every: int,
    model: Path,
) -> int:
    settings = {
        "recursive": recursive,
        "min_confidence": min_confidence,
        "allow_180": allow_180,
        "min_age_minutes": min_age_minutes,
        "cpu_workers": cpu_workers,
        "acceleration": acceleration,
    }
    scan_id = scan_identity(source, settings)
    scan_root = work / "scans"
    progress_root = scan_root / "in-progress"
    progress_root.mkdir(parents=True, exist_ok=True)
    journal_path = progress_root / f"{scan_id}.jsonl"
    state_path = progress_root / f"{scan_id}.json"
    existing_items = read_journal(journal_path)
    original_existing_count = len(existing_items)

    started = utc_now()
    if state_path.is_file():
        try:
            started = json.loads(state_path.read_text(encoding="utf-8")).get("started_at", started)
        except (OSError, json.JSONDecodeError):
            pass

    backend, acceleration_details = benchmark_acceleration(model, acceleration, cpu_workers)
    all_paths = [
        path
        for path in iter_jpegs(source, recursive)
        if path.stat().st_mtime <= time.time() - max(0, min_age_minutes) * 60
    ]
    paths_by_relative = {path.relative_to(source).as_posix(): path for path in all_paths}
    valid_existing: list[dict] = []
    seen_existing: set[str] = set()
    for item in reversed(existing_items):
        relative = str(item.get("relative_path", ""))
        path = paths_by_relative.get(relative)
        expected = str(item.get("scan_fingerprint", ""))
        if not path or not expected or relative in seen_existing:
            continue
        try:
            unchanged = fast_file_fingerprint(path) == expected
        except OSError:
            unchanged = False
        if unchanged:
            valid_existing.append(item)
            seen_existing.add(relative)
    existing_items = list(reversed(valid_existing))
    if len(existing_items) != original_existing_count:
        rewrite_journal_atomic(journal_path, existing_items)
    processed = {item["relative_path"] for item in existing_items}
    counts: dict[str, int] = {}
    for item in existing_items:
        status = item.get("status", "error")
        counts[status] = counts.get(status, 0) + 1
    pending = [path for path in all_paths if path.relative_to(source).as_posix() not in processed]
    total = len(all_paths)
    completed = len(existing_items)
    errors = counts.get("error", 0)

    print(
        f"加速模式：{'UHD/OpenCL + CPU 预处理' if backend == 'opencl' else f'{cpu_workers} 个 CPU 进程'}",
        flush=True,
    )
    if acceleration_details.get("fallback"):
        print(f"GPU 回退原因：{acceleration_details['fallback']}", flush=True)
    if completed:
        print(f"发现断点：已完成 {completed}/{total} 张，继续扫描。", flush=True)

    state = {
        "schema": SCHEMA_VERSION,
        "kind": "photo-orientation-scan-progress",
        "scan_id": scan_id,
        "source": str(source),
        "started_at": started,
        "updated_at": utc_now(),
        "settings": settings,
        "model": MODEL_VERSION,
        "backend": backend,
        "acceleration": acceleration_details,
        "completed": completed,
        "total": total,
        "counts": counts,
        "journal": str(journal_path),
    }
    write_json_atomic(state_path, state)

    def record(item: dict, journal_handle) -> None:
        nonlocal completed, errors
        completed += 1
        status = item.get("status", "error")
        counts[status] = counts.get(status, 0) + 1
        if status == "error":
            errors += 1
        append_journal(journal_handle, item, completed % checkpoint_every == 0)
        if completed % checkpoint_every == 0 or completed == total:
            state.update(
                {
                    "updated_at": utc_now(),
                    "completed": completed,
                    "total": total,
                    "counts": counts,
                }
            )
            write_json_atomic(state_path, state)
            print(f"[checkpoint       ] {completed}/{total}", flush=True)
        print(
            f"[{status:16}] {int(item.get('suggested_angle', 0)):3}° {item['relative_path']}",
            flush=True,
        )

    with journal_path.open("a", encoding="utf-8", newline="\n") as journal:
        if backend == "opencl":
            detector = create_yunet_detector(model, "opencl")
            args = [(str(path), path.relative_to(source).as_posix()) for path in pending]
            with concurrent.futures.ProcessPoolExecutor(max_workers=cpu_workers) as executor:
                for future, argument in bounded_process_map(
                    executor,
                    prepare_gpu_worker,
                    args,
                    max_pending=max(2, cpu_workers * 2),
                ):
                    try:
                        prepared = future.result()
                        item = classify_gpu_prepared(prepared, detector, min_confidence, allow_180)
                    except Exception as exc:
                        relative = argument[1]
                        item = {
                            "id": candidate_id(relative, "error"),
                            "relative_path": relative,
                            "width": 0,
                            "height": 0,
                            "orientation": 1,
                            "status": "error",
                            "suggested_angle": 0,
                            "confidence": 0.0,
                            "reason": str(exc),
                            "file_sha256": "",
                            "size": 0,
                            "mtime_ns": 0,
                            "scan_fingerprint": "",
                            "scores": "",
                        }
                    record(item, journal)
        else:
            args = [
                (str(path), path.relative_to(source).as_posix(), min_confidence, allow_180)
                for path in pending
            ]
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=cpu_workers,
                initializer=init_cpu_worker,
                initargs=(str(model),),
            ) as executor:
                for future, argument in bounded_process_map(
                    executor,
                    classify_cpu_worker,
                    args,
                    max_pending=max(2, cpu_workers * 3),
                ):
                    relative = argument[1]
                    try:
                        item = future.result()
                    except Exception as exc:
                        item = {
                            "id": candidate_id(relative, "error"),
                            "relative_path": relative,
                            "width": 0,
                            "height": 0,
                            "orientation": 1,
                            "status": "error",
                            "suggested_angle": 0,
                            "confidence": 0.0,
                            "reason": str(exc),
                            "file_sha256": "",
                            "size": 0,
                            "mtime_ns": 0,
                            "scan_fingerprint": "",
                            "scores": "",
                        }
                    record(item, journal)
        journal.flush()
        os.fsync(journal.fileno())

    items = read_journal(journal_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    scan_path = scan_root / f"photo-orientation-scan-{stamp}.json"
    payload = {
        "schema": SCHEMA_VERSION,
        "kind": "photo-orientation-scan",
        "created_at": started,
        "finished_at": utc_now(),
        "source": str(source),
        "settings": settings,
        "model": MODEL_VERSION,
        "backend": backend,
        "acceleration": acceleration_details,
        "counts": {**counts, "errors": errors, "total": len(items)},
        "items": items,
    }
    write_json_atomic(scan_path, payload)
    journal_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    print("")
    print(f"扫描完成：{len(items)} 张 JPEG；错误：{errors} 张")
    print(f"建议人工确认：{counts.get('suggested', 0)} 张")
    print(f"SCAN_FILE={scan_path}")
    return 0 if errors == 0 else 1


def run_exiftool_set_orientation(path: Path, orientation: int) -> None:
    exiftool = os.environ.get("EXIFTOOL_BIN", "exiftool")
    result = subprocess.run(
        [
            exiftool,
            "-overwrite_original",
            "-n",
            f"-EXIF:Orientation#={orientation}",
            str(path),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ExifTool 写入失败：{result.stderr.strip() or result.stdout.strip()}")


def fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_restore(backup: Path, target: Path) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".orientation-restore-", suffix=target.suffix, dir=str(target.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(backup, temp)
        fsync_file(temp)
        os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        temp.unlink(missing_ok=True)


def apply_metadata_orientation(
    source: Path,
    relative: str,
    angle: int,
    expected_file_sha256: str,
    task_dir: Path,
) -> dict:
    if angle not in ANGLE_TO_ORIENTATION:
        raise ValueError("只允许 90、180 或 270 度")
    target = safe_relative_path(source, relative)
    if target.suffix.lower() not in JPEG or not target.is_file():
        raise ValueError("目标必须是现有 JPEG 文件")

    current_hash = sha256_file(target)
    if current_hash != expected_file_sha256:
        raise RuntimeError("照片在扫描后发生变化，拒绝处理")
    _, _, current_orientation = read_image_info(target)
    if current_orientation != 1:
        raise RuntimeError(f"当前 Orientation={current_orientation}，不再是待处理状态")

    before_pixel_hash, before_size, before_mode = decoded_pixel_fingerprint(target)
    backup = safe_relative_path(task_dir / "backups", relative)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise RuntimeError("本任务备份路径已存在，拒绝覆盖")
    shutil.copy2(target, backup)
    fsync_file(backup)
    if sha256_file(backup) != current_hash:
        backup.unlink(missing_ok=True)
        raise RuntimeError("原图备份校验失败")

    fd, temp_name = tempfile.mkstemp(prefix=".orientation-stage-", suffix=target.suffix, dir=str(target.parent))
    os.close(fd)
    staged = Path(temp_name)
    replaced = False
    try:
        shutil.copy2(target, staged)
        orientation = ANGLE_TO_ORIENTATION[angle]
        run_exiftool_set_orientation(staged, orientation)

        staged_pixel_hash, staged_size, staged_mode = decoded_pixel_fingerprint(staged)
        if (staged_pixel_hash, staged_size, staged_mode) != (before_pixel_hash, before_size, before_mode):
            raise RuntimeError("安全校验失败：写入后像素或尺寸发生变化")
        _, _, staged_orientation = read_image_info(staged)
        if staged_orientation != orientation:
            raise RuntimeError("安全校验失败：EXIF Orientation 写入结果不正确")

        fsync_file(staged)
        os.replace(staged, target)
        fsync_directory(target.parent)
        replaced = True

        after_pixel_hash, after_size, after_mode = decoded_pixel_fingerprint(target)
        _, _, after_orientation = read_image_info(target)
        if (
            (after_pixel_hash, after_size, after_mode) != (before_pixel_hash, before_size, before_mode)
            or after_orientation != orientation
        ):
            atomic_restore(backup, target)
            raise RuntimeError("替换后复核失败，已自动恢复原图")
        os.utime(target, None)

        return {
            "relative_path": relative,
            "angle": angle,
            "orientation": orientation,
            "before_sha256": current_hash,
            "after_sha256": sha256_file(target),
            "pixel_sha256": before_pixel_hash,
            "backup": str(backup),
            "status": "applied",
            "applied_at": utc_now(),
        }
    except Exception:
        if replaced and target.exists() and sha256_file(target) != current_hash:
            atomic_restore(backup, target)
        raise
    finally:
        staged.unlink(missing_ok=True)


def apply_manifest(source: Path, work: Path, manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA_VERSION or manifest.get("kind") != "photo-orientation-approval":
        raise ValueError("审批清单格式无效")
    if Path(manifest.get("source", "")).resolve() != source:
        raise ValueError("审批清单与当前照片目录不一致")

    task_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    task_dir = work / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=False)
    task_manifest_path = task_dir / "manifest.json"
    results: list[dict] = []
    failed = 0

    def save_task() -> dict:
        task_manifest = {
            "schema": SCHEMA_VERSION,
            "kind": "photo-orientation-task",
            "task_id": task_id,
            "source": str(source),
            "created_at": utc_now(),
            "approval_manifest": str(manifest_path),
            "results": results,
            "summary": {
                "requested": len(manifest.get("items", [])),
                "applied": sum(item.get("status") == "applied" for item in results),
                "failed": sum(item.get("status") == "error" for item in results),
            },
        }
        write_json_atomic(task_manifest_path, task_manifest)
        return task_manifest

    save_task()

    for item in manifest.get("items", []):
        relative = str(item.get("relative_path", ""))
        pending = {
            "relative_path": relative,
            "angle": int(item.get("angle", 0)),
            "orientation": ANGLE_TO_ORIENTATION.get(int(item.get("angle", 0)), 0),
            "before_sha256": str(item.get("file_sha256", "")),
            "backup": str(safe_relative_path(task_dir / "backups", relative)),
            "status": "pending",
        }
        results.append(pending)
        save_task()
        try:
            result = apply_metadata_orientation(
                source=source,
                relative=relative,
                angle=int(item.get("angle", 0)),
                expected_file_sha256=str(item.get("file_sha256", "")),
                task_dir=task_dir,
            )
            results[-1] = result
            print(f"[metadata-applied] {result['angle']:3}° {relative}", flush=True)
        except Exception as exc:
            failed += 1
            results[-1] = {**pending, "status": "error", "error": str(exc)}
            print(f"[apply-refused   ] {relative}: {exc}", file=sys.stderr, flush=True)
        save_task()

    task_manifest = save_task()
    print("")
    print(f"任务完成：成功 {task_manifest['summary']['applied']} 张；拒绝/失败 {failed} 张")
    print(f"TASK_FILE={task_manifest_path}")
    return 0 if failed == 0 else 1


def rollback_task(source: Path, task_manifest_path: Path) -> int:
    task = json.loads(task_manifest_path.read_text(encoding="utf-8"))
    if task.get("schema") != SCHEMA_VERSION or task.get("kind") != "photo-orientation-task":
        raise ValueError("任务清单格式无效")
    if Path(task.get("source", "")).resolve() != source:
        raise ValueError("任务清单与当前照片目录不一致")

    restored = 0
    refused = 0
    for item in task.get("results", []):
        if item.get("status") not in {"applied", "pending"}:
            continue
        target = safe_relative_path(source, item["relative_path"])
        backup = Path(item["backup"]).resolve()
        try:
            backup.relative_to(task_manifest_path.parent.resolve() / "backups")
        except ValueError:
            refused += 1
            print(f"[rollback-refused] 备份路径超出本任务目录：{item['relative_path']}")
            continue
        if not backup.is_file() or not target.is_file():
            refused += 1
            print(f"[rollback-refused] 文件或备份不存在：{item['relative_path']}")
            continue
        if sha256_file(backup) != item["before_sha256"]:
            refused += 1
            print(f"[rollback-refused] 备份校验失败：{item['relative_path']}")
            continue
        current_hash = sha256_file(target)
        if current_hash == item["before_sha256"]:
            print(f"[rollback-original] 已经是原图：{item['relative_path']}")
            item["status"] = "rolled-back"
            item["rolled_back_at"] = utc_now()
            write_json_atomic(task_manifest_path, task)
            continue
        if item.get("status") == "applied":
            if current_hash != item["after_sha256"]:
                refused += 1
                print(f"[rollback-refused] 照片在任务后又被修改：{item['relative_path']}")
                continue
        else:
            backup_pixels = decoded_pixel_fingerprint(backup)
            current_pixels = decoded_pixel_fingerprint(target)
            _, _, current_orientation = read_image_info(target)
            if current_pixels != backup_pixels or current_orientation != item.get("orientation"):
                refused += 1
                print(f"[rollback-refused] 中断项无法证明是本任务改动：{item['relative_path']}")
                continue
        atomic_restore(backup, target)
        if sha256_file(target) != item["before_sha256"]:
            refused += 1
            print(f"[rollback-error  ] 恢复后校验失败：{item['relative_path']}")
            continue
        os.utime(target, None)
        restored += 1
        item["status"] = "rolled-back"
        item["rolled_back_at"] = utc_now()
        write_json_atomic(task_manifest_path, task)
        print(f"[rolled-back     ] {item['relative_path']}")

    task["rollback_summary"] = {"restored": restored, "refused": refused, "finished_at": utc_now()}
    write_json_atomic(task_manifest_path, task)
    print(f"回滚完成：恢复 {restored} 张；拒绝 {refused} 张")
    return 0 if refused == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="fnOS 照片方向安全修正 2.1")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--mode", choices=["scan", "apply-manifest", "rollback-task"], default="scan")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--recursive", default="yes")
    parser.add_argument("--min-confidence", type=float, default=1.35)
    parser.add_argument("--allow-180", default="no")
    parser.add_argument("--min-age-minutes", type=int, default=10)
    parser.add_argument("--cpu-workers", type=int, default=2)
    parser.add_argument("--acceleration", choices=["auto", "gpu", "cpu"], default="auto")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args()

    source = args.source.resolve()
    work = args.work.resolve()
    if not source.is_dir():
        print("照片目录不存在", file=sys.stderr)
        return 2
    work.mkdir(parents=True, exist_ok=True)

    try:
        if args.mode == "scan":
            return scan(
                source,
                work,
                yes(args.recursive),
                args.min_confidence,
                yes(args.allow_180),
                args.min_age_minutes,
                max(1, min(4, args.cpu_workers)),
                args.acceleration,
                max(1, args.checkpoint_every),
                args.model.resolve(),
            )
        if args.manifest is None:
            raise ValueError("当前模式需要 --manifest")
        if args.mode == "apply-manifest":
            return apply_manifest(source, work, args.manifest.resolve())
        return rollback_task(source, args.manifest.resolve())
    except Exception as exc:
        print(f"任务失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
