# 照片方向安全修正 2.1 · fnOS 插件

用于飞牛 fnOS 的 JPEG 照片方向检查与安全修正工具。2.1 针对 Intel J4125 等低功耗 NAS 加入断点续扫、双 CPU 进程和 Intel 核显自动加速。

## 2.1 新功能

- 默认使用 2 个 CPU 工作进程并行解码和检测，适合 4 核 J4125。
- x86 安装包将 `/dev/dri` 传入容器，并安装 Intel OpenCL 驱动。
- 使用 OpenCV 官方 YuNet 轻量人脸模型。
- 自动模式会分别测速 CPU 与 OpenCL；核显没有更快时自动回退 CPU。
- GPU 模式仍由 CPU 并行读取和缩图，GPU 负责方向推理。
- 每 25 张照片保存一次扫描状态，停止、重启或升级后可继续。
- 人工选择保存到 `/data/selections`，刷新网页或升级后不会清空。
- 扫描时不再读取整张原图计算 SHA-256；仅在正式执行前核对时间与大小并计算完整哈希。
- 使用 headless OpenCV 多阶段镜像，减少不需要的图形界面依赖和安装下载量。

## 不变的安全原则

- 扫描过程完全只读。
- 方向识别只提供建议，绝不自动修改照片。
- EXIF Orientation 已经不是 1 的照片保持不动。
- 必须在网页缩略图中人工选择方向并勾选。
- 正式执行只修改 JPEG 的 EXIF Orientation。
- 原始压缩图像不旋转、不裁剪、不重新压缩。

正式执行前会再次检查照片大小和纳秒修改时间；若照片自扫描后发生变化，会要求重新扫描。随后计算完整 SHA-256、建立本次任务的独立备份、在临时文件中写入 EXIF，并逐像素比较写入前后结果。只有像素、尺寸和颜色模式完全一致时才原子替换原文件。

## 加速模式

### 自动（推荐）

启动扫描时对 CPU 与 Intel OpenCL 做小型基准测试。只有核显实际更快才使用 GPU，否则使用多进程 CPU。

### 优先核显

只要 `/dev/dri` 和 OpenCL 可用就使用核显；初始化失败仍会回退 CPU。

### 只用 CPU

禁用 OpenCL，使用设定数量的 CPU 工作进程。J4125 推荐 2，设置为 4 可能影响飞牛其他服务。

ARM 安装包不会挂载 `/dev/dri`，默认使用 CPU。

## 断点与升级

数据目录仍为：

```text
/vol1/docker/fnos-photo-auto-rotate -> /data
```

其中：

- `/data/scans/in-progress`：正在扫描的断点；
- `/data/scans`：已完成扫描；
- `/data/selections`：人工勾选；
- `/data/approvals`：正式执行审批清单；
- `/data/tasks`：每次写入任务和独立备份；
- `/data/backups`：保留的 0.1 旧版恢复文件。

直接升级插件不会清理这些目录。2.1 扫描被安装、重启或手动停止后，再次使用相同目录和相同设置扫描即可继续。

注意：2.0 本身没有中途断点功能，因此从 2.0 升级时只能保留已经完成的扫描，无法恢复尚未完成的 2.0 扫描进度。

## 支持范围

- 正式写入：JPG、JPEG；
- PNG、WEBP、HEIC、视频等不会修改；
- Orientation 2–8 的 JPEG 不处理；
- 支持人工选择顺时针 90°、180°、270°。

## 安装

1. 从 GitHub Releases 下载对应安装包：
   - Intel/AMD：`PhotoAutoRotate_x86.fpk`
   - ARM：`PhotoAutoRotate_arm.fpk`
2. 飞牛「应用中心」→「本地安装」。
3. 上传 `.fpk` 并安装或升级。
4. 从桌面打开「照片方向安全修正」。
5. 建议先选择小目录验证。

默认挂载：

```text
/vol1 -> /storage/vol1
/vol2 -> /storage/vol2
```

网页可直接填写 `/vol2/1000/photos/Moments`。

## 构建与测试

```sh
python -m unittest discover -s tests -v

python build_fpk.py \
  --image ghcr.io/你的用户名/fnos-photo-auto-rotate:2.1.0 \
  --platform x86

python verify_fpk.py dist/PhotoAutoRotate_x86.fpk
```

CI 会运行安全回归测试、真实 YuNet 模型测试，并构建 amd64/arm64 两种容器镜像。

## 第三方组件

- [ExifTool](https://exiftool.org/)：只修改 EXIF Orientation；
- [Pillow](https://python-pillow.org/)：像素校验和缩略图；
- [OpenCV](https://opencv.org/)：YuNet 推理和 OpenCL 后端；
- [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)：MIT 许可证，许可证副本位于 `third_party/yunet-LICENSE`。

本项目为社区第三方应用，并非飞牛官方产品。

## 许可证

MIT
