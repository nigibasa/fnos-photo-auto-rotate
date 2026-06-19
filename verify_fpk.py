#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import re
import tarfile
from pathlib import Path


def normalized(names: list[str]) -> set[str]:
    return {name.removeprefix("./").rstrip("/") for name in names}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    args = parser.parse_args()

    with tarfile.open(args.package, "r:gz") as package:
        names = normalized(package.getnames())
        required = {
            "manifest",
            "app.tgz",
            "ICON.PNG",
            "ICON_256.PNG",
            "PhotoAutoRotate.sc",
            "cmd/main",
            "config/resource",
            "config/privilege",
            "ui/config",
        }
        missing = sorted(required - names)
        if missing:
            raise SystemExit("FPK 缺少：" + ", ".join(missing))
        if any(name.startswith(("fpk/", "docker/")) for name in names):
            raise SystemExit("FPK 根目录结构无效")

        manifest = package.extractfile("manifest").read().decode("utf-8")  # type: ignore[union-attr]
        checksum_match = re.search(r"^checksum\s*=\s*([0-9a-f]{32})$", manifest, re.MULTILINE)
        if not checksum_match:
            raise SystemExit("manifest checksum 无效")
        app_data = package.extractfile("app.tgz").read()  # type: ignore[union-attr]
        if hashlib.md5(app_data).hexdigest() != checksum_match.group(1):
            raise SystemExit("app.tgz checksum 不匹配")

        with tarfile.open(fileobj=io.BytesIO(app_data), mode="r:gz") as app:
            app_names = normalized(app.getnames())
            if "docker/docker-compose.yaml" not in app_names:
                raise SystemExit("app.tgz 缺少 docker-compose.yaml")
            compose = app.extractfile("docker/docker-compose.yaml").read().decode("utf-8")  # type: ignore[union-attr]
            if "__IMAGE__" in compose or "build:" in compose:
                raise SystemExit("compose 尚未替换为预构建镜像")
            if (
                "/vol1:/storage/vol1" not in compose
                or "/vol2:/storage/vol2" not in compose
                or "8321:8321" not in compose
            ):
                raise SystemExit("compose 挂载或端口无效")

    print(f"验证通过：{args.package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
