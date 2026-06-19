#!/usr/bin/env python3
"""在 NAS 中批量识别并纠正照片方向。

优先依据 EXIF Orientation 做确定性修正；没有有效方向标记时，
比较人脸在 0/90/180/270 度下的检测分数，只处理高置信度结果。
"""

from __future__ import annotations

import argparse
import csv
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
            action="rotate",
            transform=EXIF_TRANSFORMS[orientation],
            reason=f"EXIF Orientation={orientation}",
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
        action="rotate",
        transform=f"rotate-{best_angle}",
        reason="旋转后的人脸识别结果明显更好",
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
    args = parser.parse_args()

    source = args.source.resolve()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = work / "logs" / f"photo-rotate-{args.mode}-{timestamp}.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    backup_root = work / "backups"

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

    counts = {"total": 0, "rotate": 0, "ok": 0, "review": 0, "skip": 0, "error": 0}
    cutoff = time.time() - max(0, args.min_age_minutes) * 60

    with log_path.open("w", newline="", encoding="utf-8-sig") as log_file:
        writer = csv.writer(log_file)
        writer.writerow(
            ["状态", "操作", "可信度", "原因", "相对路径", "各方向分数", "备份路径"]
        )

        for path in iter_images(source, yes(args.recursive)):
            counts["total"] += 1
            relative = str(path.relative_to(source))
            if path.stat().st_mtime > cutoff:
                counts["skip"] += 1
                writer.writerow(["skip", "", "", "文件修改时间过近", relative, "", ""])
                continue

            try:
                decision = decide(path, cascade, args.min_confidence, yes(args.allow_180))
                backup_path = ""
                status = decision.action

                if decision.action == "rotate":
                    counts["rotate"] += 1
                    if args.mode == "apply":
                        if yes(args.backup):
                            backup_path = str(backup_file(source, path, backup_root))
                        apply_transform(path, decision.transform)
                        status = "rotated"
                    else:
                        status = "would-rotate"
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
    print(f"判断需旋转：{counts['rotate']} 张")
    print(f"方向正常：{counts['ok']} 张")
    print(f"需人工复核：{counts['review']} 张")
    print(f"跳过：{counts['skip']} 张；错误：{counts['error']} 张")
    print(f"详细清单：{log_path}")
    if args.mode == "scan":
        print("当前仅演练，没有修改任何照片。确认清单后，把 MODE 改成 apply 再运行。")
    elif yes(args.backup):
        print(f"原图备份：{backup_root}")
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
