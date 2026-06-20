# 照片自动回正 · fnOS 插件

用于飞牛 fnOS 的批量照片方向检查与修正工具。安装 `.fpk` 后可直接从飞牛桌面打开网页控制台，不需要在 PC 上运行命令。

> 这是社区第三方应用，并非飞牛官方产品。正式处理大量照片前，请先对小目录运行安全扫描并保留备份。

> v0.1.5 是紧急恢复版本：因部分 JPEG 处理后出现绿色或损坏，正式处理和 CSV 执行暂时禁用，仅保留安全扫描与从备份恢复功能。

## 功能

- 优先读取 EXIF Orientation，做确定性的方向修正。
- EXIF 不可用时，比较照片在 0°、90°、180°、270°方向的人脸检测结果；只生成建议，绝不自动修改照片。
- 默认只处理高可信度结果；判断不明确的照片进入 CSV 人工复核清单。
- “安全扫描”模式完全不修改照片，并把 EXIF 固化与实验性人脸建议分开标记。
- 正式执行前可按原目录结构备份原图。
- JPEG 使用 `jpegtran` 无损旋转，尽量避免二次压缩。
- 支持 JPG、JPEG、PNG、WEBP。
- 自带目录浏览、实时进度、任务停止和 CSV 下载。
- 可导入已有扫描 CSV 直接执行，无需再次进行完整人脸扫描；执行前会复核当前 EXIF，防止重复旋转。
- 若处理后照片异常，可按最新任务清单从原图备份中一键恢复本次全部改动。

## 安装

1. 从仓库 Releases 下载与你的设备匹配的安装包：
   - Intel/AMD：`PhotoAutoRotate_x86.fpk`
   - ARM：`PhotoAutoRotate_arm.fpk`
2. 打开飞牛「应用中心」并选择本地安装。
3. 上传 `.fpk` 并完成安装。
4. 从飞牛桌面打开「照片自动回正」。
5. 先选择一个小文件夹运行「安全扫描」。
6. 下载 CSV：`would-normalize-exif` 表示固化 EXIF，肉眼方向不变；`face-suggest` 是实验性建议。
7. 确认后点击「备份并正式回正」。默认只执行 EXIF 固化，不执行人脸建议。

默认挂载：

```text
/vol1                                -> /storage/vol1
/vol2                                -> /storage/vol2
/vol1/docker/fnos-photo-auto-rotate -> /data
```

网页中选择 `/vol2/1000/photos/Moments` 时，容器内显示的路径是：

```text
/storage/vol2/1000/photos/Moments
```

## 安全设计

- 网页只能选择 `/storage` 下的目录。
- 后端不接受任意命令，仅调用固定的照片处理程序。
- `apply` 模式要求再次输入 `ROTATE`。
- 默认跳过最近 10 分钟内修改的文件，避免碰到尚未上传完成的照片。
- 卸载插件不会自动删除配置、日志和备份。

## 从源码构建

构建容器：

```sh
docker build -t fnos-photo-auto-rotate:dev .
```

构建 FPK：

```sh
python build_fpk.py \
  --image ghcr.io/你的用户名/fnos-photo-auto-rotate:0.1.5 \
  --platform x86
```

验证：

```sh
python verify_fpk.py dist/PhotoAutoRotate_x86.fpk
python -m unittest discover -s tests -v
```

## GitHub 自动发布

- 推送到 `main`：自动构建 amd64/arm64 GHCR 镜像和 FPK artifact。
- 推送版本标签（例如 `v0.1.5`）：额外创建 GitHub Release，并附带 x86、ARM 两个可安装的 FPK。
- GHCR 镜像需要设为 Public，飞牛才能匿名拉取。

## 处理后飞牛仍显示旧方向

脚本修改的是原照片。如果飞牛相册仍显示旧缩略图，请触发相册重新索引；也可将照片临时移出相册目录，等待索引更新后再移回。

## 许可证

MIT
