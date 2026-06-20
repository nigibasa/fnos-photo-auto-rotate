# 照片方向安全修正 2.0 · fnOS 插件

用于飞牛 fnOS 的 JPEG 照片方向检查与安全修正工具。安装 `.fpk` 后从飞牛桌面打开，不需要在 PC 上运行命令。

## 2.0 为什么重写

0.1 版本会使用 `jpegtran` 旋转照片像素。部分尺寸不是 JPEG 块边界整数倍的旧照片可能出现边缘拼接、绿色区域或缩略图异常。2.0 已彻底删除像素旋转、裁剪和重新编码路径。

2.0 的原则：

- 扫描过程完全只读。
- 人脸检测只提供方向建议，绝不自动修改照片。
- EXIF Orientation 已经不是 1 的照片保持不动。
- 必须在网页缩略图中人工选择方向并勾选照片。
- 正式执行只修改 JPEG 的 EXIF Orientation 元数据。
- 原始压缩图像不旋转、不裁剪、不重新压缩。

## 安全写入流程

每一张经过确认的照片都会依次执行：

1. 对比扫描时记录的完整文件 SHA-256；照片有变化就拒绝处理。
2. 建立本次任务专用的不可覆盖原图备份。
3. 将照片复制到原目录中的临时文件。
4. 使用 ExifTool 只写入 EXIF Orientation。
5. 完整解码写入前后的照片，比较尺寸、颜色模式及全部像素 SHA-256。
6. 只有像素完全一致、Orientation 正确时才原子替换原文件。
7. 替换后再次解码校验；失败会立即自动恢复备份。
8. 每处理一张就更新任务清单；任务中断后仍可恢复。

最近一次 2.0 任务可以一键回滚。若照片在任务后又被其他程序修改，回滚会拒绝覆盖它。

## 支持范围

- 正式写入：JPG、JPEG。
- PNG、WEBP、HEIC、视频等格式不会修改。
- Orientation 2–8 的 JPEG 视为已由 EXIF 管理，2.0 不处理。
- 只支持人工选择的顺时针 90°、180°、270°。

## 安装

1. 从 GitHub Releases 下载设备对应的安装包：
   - Intel/AMD：`PhotoAutoRotate_x86.fpk`
   - ARM：`PhotoAutoRotate_arm.fpk`
2. 打开飞牛「应用中心」→「本地安装」。
3. 上传 `.fpk` 并安装。
4. 从飞牛桌面打开「照片方向安全修正」。
5. 先选择一个很小的测试目录运行只读扫描。
6. 在网页查看旋转预览，选择角度并勾选确认。
7. 输入 `APPLY METADATA` 后执行。

默认挂载：

```text
/vol1                                -> /storage/vol1
/vol2                                -> /storage/vol2
/vol1/docker/fnos-photo-auto-rotate -> /data
```

网页可直接填写 `/vol2/1000/photos/Moments`，程序会转换为 `/storage/vol2/1000/photos/Moments`。

## 从 0.1.6 升级

- 可以直接在应用中心升级安装 2.0.0。
- 旧版 `/data/backups` 不会被删除或覆盖。
- 2.0 的扫描保存在 `/data/scans`。
- 2.0 的审批清单保存在 `/data/approvals`。
- 2.0 的每次任务及独立备份保存在 `/data/tasks/<任务编号>`。
- 0.1 版本的 CSV 不可导入 2.0，避免把旧版错误判断重新执行。

## 从源码验证

容器镜像包含 `python3-pil`、`python3-opencv` 和 `libimage-exiftool-perl`。不再安装或调用 `jpegtran`。

```sh
python -m unittest discover -s tests -v

python build_fpk.py \
  --image ghcr.io/你的用户名/fnos-photo-auto-rotate:2.0.0 \
  --platform x86

python verify_fpk.py dist/PhotoAutoRotate_x86.fpk
```

测试覆盖：

- 101×77 非 JPEG 块整数倍尺寸照片；
- 真实 ExifTool 元数据写入；
- 写入前后全部解码像素哈希一致；
- 扫描后照片被修改时拒绝执行；
- 写入工具改变像素时拒绝替换；
- 正常任务回滚；
- 任务中断后的待处理项目回滚；
- HTTP 候选预览与审批清单验证；
- 构建并检查 fnOS FPK。

## 第三方组件

- [ExifTool](https://exiftool.org/)：只修改 EXIF Orientation。
- [Pillow](https://python-pillow.org/)：解码校验和缩略图。
- [OpenCV](https://opencv.org/)：只生成非强制的人脸方向建议。

本项目为社区第三方应用，并非飞牛官方产品。

## 许可证

MIT
