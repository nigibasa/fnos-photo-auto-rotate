#!/usr/bin/env python3
"""在 NAS 中批量识别并纠正照片方向。

优先依据 EXIF Orientation 做确定性修正；没有有效方向标记时，
比较人脸在 0/90/180/270 度下的检测分数，只处理高置信度结果。
"""

from __future__ import annotations

import argparse
import csv
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
from PIL import Image, ImageOps


SUPPORTED = {".jpg", ".jpeg", ".png", ".webp"}
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


def yes(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}


@dataclass
class Decision:
    action: str
    transform: str = ""
    reason: str = ""
    confidence: float = 0.0
    scores: str = ""


def read_exif_orientation(path: Path) -> int:
    try:
        with Image.open(path) as image:
            return int(image.getexif().get(274, 1))
    except Exception:
        return 1


EXIF_TRANSFORMS = {
    2: "flip-horizontal",
    3: "rotate-180",
    4: "flip-vertical",
    5: "transpose",
    6: "rotate-90",
    7: "transverse",
    8: "rotate-270",
}


def rotate_frame(frame, degrees: int):
    if degrees == 0:
        return frame
    if degrees == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if degrees == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)


def load_for_detection(path: Path):
    flags = cv2.IMREAD_COLOR
    if hasattr(cv2, "IMREAD_IGNORE_ORIENTATION"):
        flags |= cv2.IMREAD_IGNORE_ORIENTATION
    frame = cv2.imread(str(path), flags)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    longest = max(height, width)
    if longest > 1800:
        scale = 1800.0 / longest
        frame = cv2.resize(
            frame,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return frame


def face_score(cascade, frame) -> tuple[float, int]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    min_side = max(24, int(min(gray.shape[:2]) * 0.035))
    faces = cascade.detectMultiScale(
        gray,
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(min_side, min_side),
    )
    if len(faces) == 0:
        return 0.0, 0
    image_area = float(gray.shape[0] * gray.shape[1])
    area_score = sum((w * h) / image_area for _, _, w, h in faces)
    return len(faces) * 2.0 + area_score * 25.0, len(faces)


def decide(path: Path, cascade, min_confidence: float, allow_180: bool) -> Decision:
    orientation = read_exif_orientation(path)
    if orientation in EXIF_TRANSFORMS:
        return Decision(
            action="normalize-exif",
            transform=EXIF_TRANSFORMS[orientation],
            reason=f"EXIF Orientation={orientation}；固化方向后视觉显示不变",
            confidence=99.0,
        )

    frame = load_for_detection(path)
    if frame is None:
        return Decision(action="skip", reason="无法读取图片")

    angles = [0, 90, 270]
    if allow_180:
        angles.append(180)

    scored = []
    for angle in angles:
        score, count = face_score(cascade, rotate_frame(frame, angle))
        scored.append((angle, score, count))
    scored.sort(key=lambda item: item[1], reverse=True)

    scores_text = ",".join(f"{angle}:{score:.3f}/{count}" for angle, score, count in scored)
    best_angle, best_score, best_count = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0

    if best_score <= 0 or best_count == 0:
        return Decision(action="review", reason="未识别到正面人脸", scores=scores_text)

    confidence = best_score / max(second_score, 0.01)
    if best_angle == 0:
        return Decision(
            action="ok",
            reason="当前方向的人脸识别结果最佳",
            confidence=confidence,
            scores=scores_text,
        )
    if confidence < min_confidence:
        return Decision(
            action="review",
            reason="多个方向结果接近，无法可靠判断",
            confidence=confidence,
            scores=scores_text,
        )
    return Decision(
        action="face-suggest",
        transform=f"rotate-{best_angle}",
        reason="人脸方向建议（实验性，不会默认修改照片）",
        confidence=confidence,
        scores=scores_text,
    )


JPEGTRAN_ARGS = {
    "rotate-90": ["-rotate", "90"],
    "rotate-180": ["-rotate", "180"],
    "rotate-270": ["-rotate", "270"],
    "flip-horizontal": ["-flip", "horizontal"],
    "flip-vertical": ["-flip", "vertical"],
    "transpose": ["-transpose"],
    "transverse": ["-transverse"],
}

PIL_METHODS = {
    "rotate-90": Image.Transpose.ROTATE_270,
    "rotate-180": Image.Transpose.ROTATE_180,
    "rotate-270": Image.Transpose.ROTATE_90,
    "flip-horizontal": Image.Transpose.FLIP_LEFT_RIGHT,
    "flip-vertical": Image.Transpose.FLIP_TOP_BOTTOM,
    "transpose": Image.Transpose.TRANSPOSE,
    "transverse": Image.Transpose.TRANSVERSE,
}


def reset_orientation(path: Path) -> None:
    subprocess.run(
        [
            "exiftool",
            "-overwrite_original_in_place",
            "-n",
            "-Orientation=1",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def apply_transform(path: Path, transform: str) -> None:
    stat = path.stat()
    suffix = path.suffix.lower()
    temp_fd, temp_name = tempfile.mkstemp(prefix=".rotate-", suffix=suffix, dir=str(path.parent))
    os.close(temp_fd)
    temp = Path(temp_name)
    try:
        if suffix in JPEG:
            with temp.open("wb") as output:
                subprocess.run(
                    ["jpegtran", "-copy", "all", *JPEGTRAN_ARGS[transform], str(path)],
                    check=True,
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
            reset_orientation(temp)
        else:
            with Image.open(path) as image:
                metadata = {
                    key: value
                    for key, value in image.info.items()
                    if key in {"icc_profile", "dpi"}
                }
                exif = image.getexif()
                if exif:
                    exif[274] = 1
                    metadata["exif"] = exif.tobytes()
                result = image.transpose(PIL_METHODS[transform])
                result.save(temp, **metadata)
        os.replace(temp, path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    finally:
        temp.unlink(missing_ok=True)


def backup_file(source_root: Path, path: Path, backup_root: Path) -> Path:
    target = backup_root / path.relative_to(source_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(path, target)
    return target


def iter_images(root: Path, recursive: bool):
    iterator = root.rglob("*") if recursive else root.glob("*")
    for path in iterator:
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIR_NAMES or part.startswith(".") for part in relative_parts[:-1]):
            continue
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            yield path


def safe_csv_path(source: Path, relative: str) -> Path:
    cleaned = relative.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/"):
        raise ValueError("CSV 中的相对路径无效")
    path = (source / cleaned).resolve()
    try:
        path.relative_to(source)
    except ValueError as exc:
        raise ValueError("CSV 路径超出照片目录") from exc
    return path


def iter_csv_decisions(csv_path: Path, source: Path):
    with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required = {"状态", "操作", "原因", "相对路径"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV 格式不兼容，缺少状态、操作、原因或相对路径列")
        for row in reader:
            status = (row.get("状态") or "").strip()
            reason = (row.get("原因") or "").strip()
            transform = (row.get("操作") or "").strip()
            relative = (row.get("相对路径") or "").strip()

            # 兼容 v0.1.2 及更早版本的 would-rotate + EXIF Orientation=N。
            is_exif = reason.startswith("EXIF Orientation=")
            is_face = status in {"face-suggest", "would-rotate"} and not is_exif
            if not is_exif and not is_face:
                continue
            yield safe_csv_path(source, relative), relative, transform, reason, is_exif


def restore_task_changes(csv_path: Path, source: Path, work: Path) -> int:
    backup_root = work / "backups"
    restored = 0
    missing = 0
    failed = 0

    with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required = {"状态", "相对路径"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("恢复清单格式不兼容")

        for row in reader:
            if (row.get("状态") or "").strip() not in {"rotated-face", "normalized-exif"}:
                continue
            relative = (row.get("相对路径") or "").strip()
            target = safe_csv_path(source, relative)
            backup = safe_csv_path(backup_root, relative)
            if not backup.is_file():
                missing += 1
                print(f"[restore-missing] {relative}")
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
                restored += 1
                print(f"[restored      ] {relative}")
            except Exception as exc:
                failed += 1
                print(f"[restore-error ] {relative}: {exc}", file=sys.stderr)

    print("")
    print(f"已恢复本次任务改动：{restored} 张")
    print(f"缺少备份：{missing} 张；恢复失败：{failed} 张")
    return 0 if missing == 0 and failed == 0 else 1


def refresh_restored_task(csv_path: Path, source: Path, work: Path) -> int:
    backup_root = work / "backups"
    refreshed = 0
    mismatch = 0
    missing = 0
    seen: set[str] = set()

    with csv_path.open("r", newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        required = {"状态", "相对路径"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("刷新清单格式不兼容")

        for row in reader:
            if (row.get("状态") or "").strip() not in {"rotated-face", "normalized-exif"}:
                continue
            relative = (row.get("相对路径") or "").strip()
            if relative in seen:
                continue
            seen.add(relative)
            target = safe_csv_path(source, relative)
            backup = safe_csv_path(backup_root, relative)
            if not target.is_file() or not backup.is_file():
                missing += 1
                print(f"[refresh-missing] {relative}")
                continue
            if not filecmp.cmp(target, backup, shallow=False):
                mismatch += 1
                print(f"[refresh-refused] 当前文件与原图备份不一致：{relative}")
                continue
            os.utime(target, None)
            refreshed += 1
            print(f"[index-refresh ] {relative}")

    print("")
    print(f"已触发飞牛重新索引：{refreshed} 张")
    print(f"文件与备份不一致，拒绝刷新：{mismatch} 张；缺失：{missing} 张")
    return 0 if mismatch == 0 and missing == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--mode", choices=["scan", "apply"], default="scan")
    parser.add_argument("--recursive", default="yes")
    parser.add_argument("--min-confidence", type=float, default=1.35)
    parser.add_argument("--allow-180", default="no")
    parser.add_argument("--min-age-minutes", type=int, default=10)
    parser.add_argument("--backup", default="yes")
    parser.add_argument("--apply-face-suggestions", default="no")
    parser.add_argument("--input-csv", type=Path)
    parser.add_argument("--restore-task-csv", type=Path)
    parser.add_argument("--refresh-task-csv", type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = work / "logs" / f"photo-rotate-{args.mode}-{timestamp}.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    backup_root = work / "backups"

    if args.restore_task_csv:
        return restore_task_changes(args.restore_task_csv.resolve(), source, work)
    if args.refresh_task_csv:
        return refresh_restored_task(args.refresh_task_csv.resolve(), source, work)

    cascade_candidates = []
    cv2_data = getattr(cv2, "data", None)
    if cv2_data is not None:
        cascade_candidates.append(
            Path(cv2_data.haarcascades) / "haarcascade_frontalface_default.xml"
        )
    cascade_candidates.extend(
        [
            Path("/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"),
            Path("/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml"),
        ]
    )
    cascade_path = next((path for path in cascade_candidates if path.is_file()), None)
    if cascade_path is None:
        print("错误：未找到 OpenCV 人脸检测模型。", file=sys.stderr)
        return 2
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        print("错误：无法加载人脸检测模型。", file=sys.stderr)
        return 2

    counts = {
        "total": 0,
        "normalize-exif": 0,
        "face-suggest": 0,
        "face-rotated": 0,
        "ok": 0,
        "review": 0,
        "skip": 0,
        "error": 0,
    }
    cutoff = time.time() - max(0, args.min_age_minutes) * 60

    with log_path.open("w", newline="", encoding="utf-8-sig") as log_file:
        writer = csv.writer(log_file)
        writer.writerow(
            ["状态", "操作", "可信度", "原因", "相对路径", "各方向分数", "备份路径"]
        )

        if args.input_csv:
            items = iter_csv_decisions(args.input_csv.resolve(), source)
        else:
            items = ((path, str(path.relative_to(source)), "", "", None) for path in iter_images(source, yes(args.recursive)))

        for path, relative, csv_transform, csv_reason, csv_is_exif in items:
            counts["total"] += 1
            if not path.is_file():
                counts["skip"] += 1
                writer.writerow(["skip", "", "", "CSV 中的照片不存在", relative, "", ""])
                continue
            if path.stat().st_mtime > cutoff:
                counts["skip"] += 1
                writer.writerow(["skip", "", "", "文件修改时间过近", relative, "", ""])
                continue

            try:
                if args.input_csv:
                    if csv_is_exif:
                        current_orientation = read_exif_orientation(path)
                        current_transform = EXIF_TRANSFORMS.get(current_orientation)
                        if current_transform is None:
                            decision = Decision(
                                action="skip",
                                reason="当前 EXIF 已是正常方向，跳过以防重复旋转",
                            )
                        elif current_transform != csv_transform:
                            decision = Decision(
                                action="review",
                                reason=f"当前 EXIF 方向与 CSV 不一致：Orientation={current_orientation}",
                            )
                        else:
                            decision = Decision(
                                action="normalize-exif",
                                transform=current_transform,
                                reason=f"从 CSV 导入；EXIF Orientation={current_orientation}；固化后视觉显示不变",
                                confidence=99.0,
                            )
                    else:
                        decision = Decision(
                            action="face-suggest",
                            transform=csv_transform,
                            reason="从旧版 CSV 导入的人脸方向建议（实验性）",
                        )
                else:
                    decision = decide(path, cascade, args.min_confidence, yes(args.allow_180))
                backup_path = ""
                status = decision.action

                if decision.action == "normalize-exif":
                    counts["normalize-exif"] += 1
                    if args.mode == "apply":
                        if yes(args.backup):
                            backup_path = str(backup_file(source, path, backup_root))
                        apply_transform(path, decision.transform)
                        status = "normalized-exif"
                    else:
                        status = "would-normalize-exif"
                elif decision.action == "face-suggest":
                    counts["face-suggest"] += 1
                    # Haar 人脸方向检测误报率较高，只保留建议，永不自动修改照片。
                    status = "face-suggest"
                elif decision.action in counts:
                    counts[decision.action] += 1

                writer.writerow(
                    [
                        status,
                        decision.transform,
                        f"{decision.confidence:.3f}",
                        decision.reason,
                        relative,
                        decision.scores,
                        backup_path,
                    ]
                )
                print(f"[{status:12}] {decision.transform:15} {relative}")
            except Exception as exc:
                counts["error"] += 1
                writer.writerow(["error", "", "", str(exc), relative, "", ""])
                print(f"[error       ] {relative}: {exc}", file=sys.stderr)

    print("")
    print(f"模式：{args.mode}")
    print(f"扫描：{counts['total']} 张")
    print(f"EXIF 方向待固化：{counts['normalize-exif']} 张（视觉方向不变）")
    print(f"人脸方向建议：{counts['face-suggest']} 张（默认不修改）")
    print(f"已按人脸建议旋转：{counts['face-rotated']} 张")
    print(f"方向正常：{counts['ok']} 张")
    print(f"需人工复核：{counts['review']} 张")
    print(f"跳过：{counts['skip']} 张；错误：{counts['error']} 张")
    print(f"详细清单：{log_path}")
    if args.input_csv:
        print(f"导入清单：{args.input_csv}")
    if args.mode == "scan":
        print("当前仅演练，没有修改任何照片。确认清单后，把 MODE 改成 apply 再运行。")
    elif yes(args.backup):
        print(f"原图备份：{backup_root}")
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
