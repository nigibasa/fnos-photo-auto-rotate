#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import os
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FPK = ROOT / "fpk"
DIST = ROOT / "dist"
EXECUTABLE_NAMES = {
    "main",
    "install_init",
    "install_callback",
    "uninstall_init",
    "uninstall_callback",
    "upgrade_init",
    "upgrade_callback",
    "config_init",
    "config_callback",
}


def add_path(archive: tarfile.TarFile, path: Path, arcname: str) -> None:
    info = archive.gettarinfo(str(path), arcname)
    if path.is_file():
        info.mode = 0o755 if path.name in EXECUTABLE_NAMES else 0o644
        with path.open("rb") as handle:
            archive.addfile(info, handle)
    else:
        info.mode = 0o755
        archive.addfile(info)
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            add_path(archive, child, f"{arcname}/{child.name}")


def create_app_payload(destination: Path, image: str, platform: str) -> None:
    compose = (FPK / "docker" / "docker-compose.yaml").read_text(encoding="utf-8")
    if "__IMAGE__" not in compose:
        raise SystemExit("fpk/docker/docker-compose.yaml 缺少 __IMAGE__ 占位符")
    compose = compose.replace("__IMAGE__", image)
    if "__GPU_DEVICES__" not in compose:
        raise SystemExit("fpk/docker/docker-compose.yaml 缺少 __GPU_DEVICES__ 占位符")
    gpu_devices = "    devices:\n      - /dev/dri:/dev/dri" if platform == "x86" else ""
    compose = compose.replace("__GPU_DEVICES__", gpu_devices)

    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        data = compose.encode("utf-8")
        info = tarfile.TarInfo("docker/docker-compose.yaml")
        info.size = len(data)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(data))
        add_path(archive, FPK / "ui", "ui")


def patched_manifest(checksum: str, platform: str) -> bytes:
    lines = (FPK / "manifest").read_text(encoding="utf-8").splitlines()
    result = []
    for line in lines:
        if line.startswith("checksum"):
            result.append(f"checksum              = {checksum}")
        elif line.startswith("platform"):
            result.append(f"platform              = {platform}")
        else:
            result.append(line)
    return ("\n".join(result) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 fnOS FPK")
    parser.add_argument("--image", required=True, help="预构建容器镜像，例如 ghcr.io/owner/repo:2.1.0")
    parser.add_argument("--platform", choices=["x86", "arm"], default="x86")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        args.output = DIST / f"PhotoAutoRotate_{args.platform}.fpk"

    if not args.image.startswith(("ghcr.io/", "docker.io/")) or ":" not in args.image:
        raise SystemExit("--image 必须是带版本标签的公开容器镜像")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    required = [
        FPK / "manifest",
        FPK / "PhotoAutoRotate.sc",
        FPK / "ICON.PNG",
        FPK / "ICON_256.PNG",
        FPK / "cmd",
        FPK / "config",
        FPK / "ui",
        FPK / "wizard",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("缺少 FPK 文件：" + ", ".join(missing))

    with tempfile.TemporaryDirectory(prefix="photo-auto-rotate-") as temp_dir:
        app_tgz = Path(temp_dir) / "app.tgz"
        create_app_payload(app_tgz, args.image, args.platform)
        checksum = hashlib.md5(app_tgz.read_bytes()).hexdigest()

        with tarfile.open(args.output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            manifest = patched_manifest(checksum, args.platform)
            info = tarfile.TarInfo("manifest")
            info.size = len(manifest)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(manifest))
            add_path(archive, app_tgz, "app.tgz")
            for name in ["PhotoAutoRotate.sc", "ICON.PNG", "ICON_256.PNG", "cmd", "config", "ui", "wizard"]:
                add_path(archive, FPK / name, name)

    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
