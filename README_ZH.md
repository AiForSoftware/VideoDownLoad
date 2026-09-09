# 全能下载器 · VideoDownLoad

Windows 桌面视频下载器：把命令行视频下载引擎 `vd` 封装成带 GUI 的桌面应用
（pywebview + Edge WebView2），支持**抖音 / B站 / YouTube** 的解析、选档、并发下载、
任务级暂停恢复，以及**字幕自动下载并封装进视频**。

---

## 🤖 AI 生成声明

> **本项目由 AI 生成。**
>
> 代码、文档与构建脚本的主体由 AI（CodeBuddy）在人类指导下自动生成与迭代，
> 人类负责提出需求、验证效果与验收。
>
> 因此：代码中可能存在非典型写法或冗余的防御性逻辑，多为针对特定历史事故的处理，里面每一条都对应一次真实返工。

---

## ✨ 主要特性

| 特性 | 说明 |
|---|---|
| 多平台解析 | 抖音、B站、YouTube 三个经过完整验证的解析器；另有 `WebMediaGrabber` 网页媒体抓取兜底 |
| 画质选档 | 4K / 1080P+ / 1080P / 720P / 480P … 按档位枚举成独立条目，前端分组可选 |
| 下载编排 | 多条目并发下载（可设"同时下载数"），任务级**暂停 / 恢复 / 取消** |
| 字幕封装 | 解析到字幕轨即自动下载并内封进视频：B站专有 JSON→VTT 转换、YouTube captionTracks、HLS（m3u8）自动提取 |
| 登录态 | 顶栏「登录态」按钮弹窗登录（DrissionPage），Cookie 按平台存入 `per_source_cookies` |
| 自愈启动 | 监控进程 + UI 子进程双进程模型，WebView2 初始化挂死自动重试（最多 5 次），卡死的尝试用户不可见 |
| 单实例 | 重复打开会聚焦已有窗口并提醒，绝不杀掉正在启动的实例 |
| 工具捆绑 | `N_m3u8DL-RE`（HLS/m3u8）、`aria2c`（多线程）随包分发；ffmpeg/ffprobe 依赖系统 PATH |
| 版本管理 | 每次打包自动递增版本号（`version.txt` 为唯一真源），并用于安装/激活数据上报 |

---

## 🏗 项目架构

### 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│  前端 UI       app/web/  index.html + app.js + styles.css     │  Edge WebView2 渲染
│                （3 套主题 = CSS 变量组，700ms/1.5s 自适应轮询） │
├──────────────────────────────────────────────────────────────┤
│  桥接层        app/backend/api.py  JsApi                      │  window.pywebview.api.*
│                ★ 所有属性必须 _ 前缀（否则 pywebview 递归遍历   │
│                  对象图，冷启动 rss 会涨到数 GB）               │
├──────────────────────────────────────────────────────────────┤
│  服务层        app/backend/core.py  VideoDlService            │  配置 / 任务队列 /
│                                                               │  引擎懒加载 / 下载编排
├──────────────────────────────────────────────────────────────┤
│  进度总线      app/backend/progress.py  ProgressBus           │  rich Progress 劫持 →
│                                                               │  暂停/取消中断点（按 job 隔离）
├──────────────────────────────────────────────────────────────┤
│  引擎层        engine/vd/                                     │  解析 / 下载 / 字幕 / 合并
│                ├─ vd.py            VideoClient 总编排          │
│                ├─ modules/sources/ 平台解析器（懒加载）         │
│                ├─ modules/grabber.py  WebMediaGrabber 兜底     │
│                ├─ modules/utils/   chromium / youtubeutils …   │
│                └─ modules/js/      非 py 解密资源（datas 打包）  │
├──────────────────────────────────────────────────────────────┤
│  外部工具      ffmpeg / ffprobe（系统 PATH）、N_m3u8DL-RE、     │
│                aria2c（随包 bin/）、node（可选，系统 PATH）      │
└──────────────────────────────────────────────────────────────┘
```

### 进程模型

```
VideoDLDesktop.exe                     ← supervisor 监控进程（持有单实例锁）
   │  环境变量传递加载标记文件路径
   └── VideoDLDesktop.exe --child      ← UI 子进程
         隐藏启动 → 页面 loaded 写入标记文件 → 原生 ShowWindow 显示
         supervisor 超时未见到标记 → psutil 杀整棵子树 → 重试（最多 5 次）
```

- 前 2 次尝试给 60s（冷启动 + Defender 扫描），后续 30s；
- 存活探针需**连续两次**判定无忙碌 WebView2 进程才重试，避免误杀冷启动；
- 关窗口时 `_hard_exit()` 递归杀掉整棵进程树，保证"关窗口 = 进程消失"。

### 数据流（一次下载）

```
粘贴 URL
  → JsApi.parse() → VideoDlService.parse()
      → 引擎懒加载：按 hostname 匹配解析器模块（模块名必须是 hostname 的子串）
      → 解析器返回 VideoInfo 列表（每个画质档位一条，含 audio 五件套）
  → 前端选档 → enqueue()：每个条目 submit 进线程池（max_workers = 同时下载数）
  → 引擎下载（requests / curl_cffi；HLS 走 N_m3u8DL-RE；m3u8/mpd 走 ffmpeg）
      → ProgressBus 按 (job_id, item_key) 上报进度到对应条目
      → 暂停/取消通过 ProgressBus 中断点抛 DownloadPaused / DownloadCancelled
  → 视频 + 音频分别下载 → ffmpeg 合并（copy / transcode 两次尝试）
  → 字幕：下载 → 必要时转换（B站 JSON→VTT）→ ffmpeg 内封
  → 成品落 work_dir，任务状态 done；未完成任务持久化到 jobs.json，下次启动可"继续"
```

### 目录结构

```
VideoDownLoad/
├── app/                        # 桌面应用壳
│   ├── app.py                  # 入口：--selftest / --child(UI) / 默认(supervisor)
│   ├── backend/
│   │   ├── core.py             # VideoDlService：配置、任务队列、引擎懒加载、下载编排
│   │   ├── api.py              # JsApi：window.pywebview.api.* 全部桥接方法
│   │   ├── progress.py         # ProgressBus：进度总线 + 暂停/取消中断点
│   │   ├── login.py            # 平台登录态：DrissionPage 弹窗登录 + Cookie 提取
│   │   ├── diag.py             # startup.log 诊断日志（每行带耗时/内存/线程）
│   │   └── tracker.py          # SoftwareTracker 安装/激活上报客户端
│   ├── web/                    # index.html + app.js + styles.css
│   ├── tools/                  # 冒烟测试、内存测量等脚本
│   ├── pyinstaller_hooks/      # runtime_setup.py（node PATH、stdio 兜底）
│   └── assets/                 # 图标、二维码
├── engine/vd/                  # ★ 视频下载引擎，sys.path 指向 engine/
│   ├── vd.py                   # VideoClient：parsefromurl / download 总编排
│   └── modules/
│       ├── sources/            # 平台解析器：douyin / bilibili / youtube
│       ├── grabber.py          # WebMediaGrabber 网页媒体抓取兜底
│       ├── utils/              # chromium(DrissionPage 封装) / youtubeutils …
│       └── js/                 # 非 py 解密资源（必须作为 datas 打包）
├── bin/                        # 捆绑工具：N_m3u8DL-RE.exe、aria2c.exe
├── build.spec                  # PyInstaller 配置
├── build_now.ps1               # ★ 唯一打包入口（先自动递增版本号，再构建）
├── bump_version.py             # 版本号递增工具
├── version.txt                 # ★ 版本号唯一真源（每次打包 +1 patch）
├── requirements.txt            # 引擎 + 桌面壳合并依赖
└── dist/VideoDLDesktop/        # 打包产物（onedir，唯一发布位置）
```

---

## 🚀 快速开始

### 环境要求

- Windows 10 / 11（x64）
- Python 3.11
- Microsoft Edge WebView2 Runtime
- **ffmpeg / ffprobe**（合并音视频必需，需在系统 PATH）
- N_m3u8DL-RE、aria2c 已随包捆绑，无需单独安装

### 从源码运行

```powershell
cd D:\CodeBuddy\VideoDownLoad
pip install -r requirements.txt      # ★ 务必装上 yt-dlp，缺失会让音频合并静默失效
$env:PYTHONIOENCODING = "utf-8"
python app\app.py
```

### 打包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File build_now.ps1
```

流程：① 自动递增版本号 → ② 关运行实例 → ③ 预删 `dist`/`build` → ④ PyInstaller 构建
→ ⑤ 打印日志关键行 → ⑥ 产物检查。产物落在 `dist/VideoDLDesktop/`。

> **不要绕过脚本直接 `python -m PyInstaller build.spec`**——那样版本号不递增，
> 激活上报会把新包当成同一版本。

### 无头自检

```powershell
dist\VideoDLDesktop\VideoDLDesktop.exe --selftest
```

---

## ⚙️ 配置

配置文件：`C:\Users\<你>\AppData\Local\vd\vd-desktop\config.json`

| 字段 | 说明 |
|---|---|
| `work_dir` | 下载输出目录 |
| `concurrent_downloads` | 同时下载数（线程池上限） |
| `default_quality` | `best` / `4k` / `1080p` / `720p` / `480p` / `360p` / `auto` |
| `download_subtitles` | 是否下载字幕并封装进视频 |
| `allowed_sources` | 启用的解析器类名列表 |
| `per_source_cookies` | 平台登录态（登录弹窗自动写入，键 = 解析器类名） |
| `proxy` | 代理（`host:port`，空 = 直连） |

同目录下还有诊断日志，排查启动/卡死问题第一现场：
- `Logs\startup.log`（进程级事件，每行带耗时/内存/线程）
- `Logs\desktop.log`（引擎内部日志）

---

## ⚠️ 已知边界

- **HTTP 断点续传未实现**：暂停后恢复是把该条目重新提交，引擎从头下载（非 Range 续传）。
- ffmpeg / ffprobe / node 依赖系统 PATH，缺失会导致合并或解密类解析器不可用。
- YouTube 存在**出口 IP 临时降权**（小时~天级自动恢复），期间所有工具（含 yt-dlp、
  YoutubeDownloader）会同时失效，属网络时变状态，不是代码缺陷。
- 引擎解析内部异常只能引导用户重新解析（根因在引擎对源站数据结构的假设）。

---

## 📄 开源协议

本项目采用 **MIT License**，详见 [`LICENSE`](./LICENSE)。

```
MIT License

Copyright (c) 2026 VideoDownLoad contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- 使用的第三方依赖（pywebview、DrissionPage、yt-dlp、requests、curl_cffi、rich、
  PyInstaller 等）各自遵循其原始协议。

---

## 🙏 致谢与免责

- 本项目仅供**学习与技术研究**使用。请遵守各视频平台的服务条款与所在地区的法律法规，
  尊重内容创作者的版权，勿将下载内容用于商业或侵权用途。
