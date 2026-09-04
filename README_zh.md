# 全能下载器（VideoDLDesktop）

一个基于 `vd` 引擎的桌面视频下载工具，支持 90+ 平台解析与通用解析器兜底。

## 主要功能

- **多平台解析**：抖音、Bilibili、YouTube 等主流平台，按需懒加载解析器。
- **批量下载**：支持一次选择多个媒体项加入下载队列。
- **多路同时下载**：设置中可配置「同时下载数」，默认 2 路并行。
- **下载控制**：每个任务支持开始 / 暂停 / 取消，统一使用图标按钮。
- **断点续传**：原生下载器使用 `.part` 临时文件 + `Range` 请求，暂停/恢复后继续下载。
- **任务进度**：每个正在下载的条目下方显示独立进度条（速度、百分比、剩余时间）。

## 构建

```bash
cd /mnt/d/CodeBuddy/VideoDownLoad
rm -rf dist/ build/
python -m PyInstaller build.spec --noconfirm
```

产物目录：`dist/VideoDLDesktop/`。

## 目录说明

- `app/backend/`：桌面后端（配置、任务调度、引擎适配、进度总线）。
- `app/web/`：前端页面（HTML / CSS / JS）。
- `engine/vd/`：上游视频下载引擎（已裁剪为按需懒加载）。
