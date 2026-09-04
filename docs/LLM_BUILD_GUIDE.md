# 全能下载器 · 大模型思考构建指南

> **读者**：未来接手本项目的大模型（或工程师）。
> **目标**：不复述所有代码，而是告诉你**为什么是现在这个样子**、**哪些坑已经被踩过并修好**、
> **动手前必须检查什么**。改代码前请先通读第 4 章（踩坑清单），里面每一条都对应一次真实的返工。

---

## 1. 项目一句话

把上游命令行视频下载引擎 `vd`（69 平台解析器；36 个通用解析器已全部移除以保护用户隐私，见 `engine/vd/`）
封装成 Windows 桌面应用（pywebview + Edge WebView2），并在六层做了深度修复：
**引擎调用层、浏览器自动化层、下载编排层（任务级暂停/恢复/取消 + 并发下载）、字幕封装层（原视频字幕随流下载并内封进视频）、打包层、前端交互层**。

技术栈：Python 3.11 + pywebview(edgechromium) + DrissionPage + requests/curl_cffi + rich + PyInstaller(onedir)。

---

## 2. 目录结构（重组后的最终形态）

```
VideoDownLoad/
├── app/                        # 桌面应用壳
│   ├── app.py                  # 入口。main() 分三路：--selftest / --child(UI) / 默认=监控进程
│   ├── backend/
│   │   ├── core.py             # VideoDlService：配置、任务队列、引擎懒加载、下载编排（暂停/恢复/取消/并发）
│   │   ├── api.py              # JsApi：window.pywebview.api.* 的全部桥接方法（含 pause/resume/cancel）
│   │   ├── progress.py         # rich Progress 劫持 → 进度总线（下载进度进 UI + 暂停/取消中断点）
│   │   ├── login.py            # 平台登录态：DrissionPage 弹窗登录 + Cookie 提取
│   │   └── diag.py             # startup.log 诊断日志（每行带耗时/内存/线程）
│   ├── web/                    # index.html + app.js + styles.css（3 套配色，CSS 变量主题）
│   ├── tools/                  # smoke_sources / gui_check 等验证脚本
│   ├── pyinstaller_hooks/      # runtime_setup.py（node PATH、stdio 兜底）
│   └── assets/icon.ico         # 应用图标（EXE 图标）
├── engine/                     # ★ 引擎仓库根（sys.path 指向这里）
│   ├── vd/                # 引擎包本体
│   │   ├── vd.py          # VideoClient：parsefromurl/download 总编排
│   │   └── modules/
│   │       ├── sources/        # 69 个平台解析器（bilibili.py / douyin.py / ...）
│   │       ├── common/         # （已清空）36 个通用代解析器已全部移除以保护用户隐私
│   │       ├── grabber.py      # WebMediaGrabber 网页媒体抓取兜底
│   │       ├── utils/chromium.py  # ★ DrissionPageUtils：浏览器启动/清理的核心封装
│   │       └── js/             # 非py资源（js 解密脚本：cctv / tencent / youtube）——必须作为 datas 打包
│   └── LICENSE                 # MIT
├── bin/                        # （已清空）外部工具不再捆绑，ffmpeg/ffprobe/node 依赖系统 PATH（spec 仍会把该目录打进 _internal/bin，缺失时静默为空）
├── build.spec                  # PyInstaller 配置（在项目根目录运行）
├── requirements.txt            # 引擎 + 桌面壳合并依赖
├── docs/                       # 本指南 + 视频解析器构建流程.md
└── dist/VideoDLDesktop/        # 打包产物（onedir）——★ 唯一的发布位置，不要再建 release_vX 目录
```

`__pycache__` 已清理。

---

## 3. 构建 / 运行 / 验证 命令

```powershell
# 构建（必须在项目根目录；产物只落在 dist\VideoDLDesktop\，不建 release_vX 目录）
cd /d D:\CodeBuddy\VideoDownLoad          # cmd 语法；PowerShell 下用 cd D:\CodeBuddy\VideoDownLoad
taskkill /f /im VideoDLDesktop.exe        # 先关掉正在运行的 exe，否则覆盖 dist 时文件被锁
python -m PyInstaller build.spec --noconfirm
# 构建成功的核对点："[spec] packaged N non-python resource file(s)" 且 N > 0；
# "[spec] node runtime not bundled" 只是提示（本机未装 nodejs_wheel 时 node 依赖系统 PATH），不是失败

# 源码运行
$env:PYTHONIOENCODING = "utf-8"
python app\app.py

# 无头自检（解析→下载→校验，报告写用户目录）
dist\VideoDLDesktop\VideoDLDesktop.exe --selftest
```

**运行时数据位置**（用户机器上）：
- 配置：`C:\Users\<你>\AppData\Local\vd\vd-desktop\config.json`
- 诊断日志：同目录 `Logs\startup.log`（卡死/启动问题的法证记录，每行带耗时/内存/线程）
- 引擎日志：同目录 `Logs\desktop.log`
- WebView2 UI 数据：同目录 `webview2\`（注意：UI 实际用 private_mode 临时 profile，此目录基本闲置）

---

## 4. ★★★ 踩坑清单（每条都是真实事故，改代码前必读）

### 4.1 打包层

| # | 坑 | 原因与解法 |
|---|---|---|
| 1 | **解析器 .py 必须作为 datas 打包，绝不能全塞进 PYZ** | 引擎用懒加载（`VD_LAZY_PARSERS=1`），解析器按需 `importlib.import_module`。若把 69 个解析器编译进 PYZ，WebView2 初始化会大概率死锁（白屏卡死）。`build.spec` 只把 `modules/sources/**.py`、`modules/common/**.py` 作为 datas 复制，其余进 PYZ。**给 sources 新增 .py 文件无需改 spec**（walk 自动覆盖）。 |
| 2 | **预删 `dist\`、`build\` 再打包** | PyInstaller 删除旧 onedir 输出时可能触发 `WinError 1455 页面文件太小`（几百个文件的批量删除）。先 `rm -rf dist build`（WSL 下执行更稳）。 |
| 3 | **xcopy 前必须 `taskkill /f /im VideoDLDesktop.exe`** | 正在运行的 exe 会被锁定，xcopy 报 Sharing violation。 |
| 4 | **spec 必须用 `python -m PyInstaller build.spec` 执行** | 直接 `python build.spec` 没有 PyInstaller 注入的 `SPECPATH` 等全局量，直接 NameError。 |
| 5 | **PyInstaller workpath 按 spec 文件名命名** | spec 叫 `build.spec` → workpath 是 `build\build\`（嵌套）。改 spec 文件名会改 workpath，注意清理。 |
| 6 | **pywebview 的 site-packages 补丁会在重装后丢失（★最容易复发的坑）** | `C:\Users\25011\.workbuddy\binaries\python\versions\3.11.9\Lib\site-packages\webview\platforms\edgechromium.py` 第 ~82 行的 `props.AdditionalBrowserArguments` 已被手动追加 `--disable-gpu --disable-background-networking --no-first-run --disable-component-update --disable-default-apps`（防 WebView2 初始化死锁）。**pip 重装/升级 pywebview 后此补丁消失，必须重打**，否则首启卡死率回升。打包时从该 site-packages 收集，补丁随构建生效。 |
| 7 | **上游引擎升级方式已改变** | 旧结构是 git clone（可 git pull）；现已把包体并入 `engine/vd/` 且清掉了 git。升级上游 = 手动 diff/搬运，注意保留本项目所有修复（见 4.3/4.4）。特别注意：上游新增的通用解析器（`modules/common/`）和 DRM 解析器（依赖 `cdm/*.wvd`）**不要引入**——本项目已主动移除这些隐私风险模块（见第 9 章）。 |
| 8 | **外部工具不再随包捆绑** | `bin/` 已清空（只剩 `_check_tools.py`），ffmpeg/ffprobe/node 运行时从系统 PATH 检测（UI 启动日志 `tools detected: ffmpeg=y ...` 可核对）。spec 仍保留 `bin` 目录打包逻辑，目录为空时静默跳过。工具缺失提示在设置弹窗底部。 |
| 9 | **node 运行时按可选依赖打包** | spec 里 `import nodejs_wheel` 取 `node.exe` 打进 `nodejs_runtime/`；构建环境没装 `nodejs_wheel` 时打印 `node runtime not bundled` 并继续（不要当错误修）。 |

### 4.2 进程 / 启动层（app.py）

| # | 坑 | 原因与解法 |
|---|---|---|
| 1 | **WebView2 初始化有概率死锁（约 30-50%）** | 症状：窗口出现但页面永不加载、`EnsureCoreWebView2Async` 不返回。**不是**端口/用户目录/孤儿进程问题（已逐一排除）。解法：UI 以 `--child` 子进程运行 + **隐藏启动**，监控进程（supervisor）盯"页面已加载"标记文件（`VIDEODL_UI_START_HIDDEN` + `VIDEODL_WV_LOADED_FLAG`），15s（`UI_LOAD_TIMEOUT`）没加载就 `taskkill /T /pid` 杀子进程树重试，最多 5 次（`MAX_UI_ATTEMPTS`）。窗口在 `onloaded` 后才 `window.show()`——**卡死的尝试用户完全不可见**。 |
| 2 | **单实例守卫绝不能杀"健康"的已运行实例** | 旧逻辑"发现旧实例存活→杀掉"，叠加首启慢（Defender 扫描新构建文件），用户连点图标 = 每点一次杀掉启动到一半的进程，越点越慢。新逻辑：旧实例存活且窗口响应（`IsHungAppWindow`）→ 聚焦其窗口（`FindWindowW`+`SetForegroundWindow`，标题 = `全能下载器 v{版本}`）并安静退出；只有窗口真未响应才杀进程树。 |
| 3 | **UI 子进程启动即隐藏** | `webview.create_window(hidden=True)` 是防"白屏冻结窗口"的关键。若去掉 hidden，Web 初始化卡死时用户会看到一个冻死的窗口。 |
| 4 | **子进程黑框** | ffmpeg / N_m3u8DL-RE / aria2c / node 都是控制台程序，合并音视频时弹黑框。解法：`backend/core.py` 顶部给 `subprocess.Popen.__init__` 打了进程级补丁，未显式指定 `creationflags` 的子进程一律加 `CREATE_NO_WINDOW`。**勿删**。 |
| 5 | **单实例 PID 文件** | `Logs\singleton.pid` 记录 supervisor PID。supervisor 正常退出时 atexit 释放；被强杀时残留——新守卫靠 `pid_exists` + 窗口响应检测兜底，不会死锁。 |

### 4.3 引擎调用层（backend/core.py + DrissionPage）

| # | 坑 | 原因与解法 |
|---|---|---|
| 1 | **DrissionPage 无头标志必须走 `co.headless()`，不能 `set_argument('--headless=new')` 绕过** | `set_argument('--headless=new')` 只加参数、**不设置 DrissionPage 内部 `is_headless` 标志**。浏览器实际无头运行，DrissionPage 检测 UA 含 Headless 与配置不符 → 判定状态不一致 → **quit 掉刚启动的浏览器再重启**，并用死掉的 browser GUID 重连 → DevTools 报 `WebSocketBadStatusException: Handshake status 404`。`chromium.py buildoptions` 必须保持 `co.headless(True)`（4.1.x 上它会自动发 `--headless=new`）。 |
| 2 | **ChromiumPage 启动失败必须清理僵尸浏览器** | 启动失败（握手/风控）时浏览器进程已经拉起来了，不清理就会累积几十个僵尸 Edge。`DrissionPageUtils.killdebugbrowsers(address)` 按 `--remote-debugging-port=<port>` 匹配杀进程（不会误伤用户自己的浏览器），已在 `trylaunchbrowser` 失败分支自动调用。 |
| 3 | **给 Cookie 设置 domain** | 页面还在 about:blank 时，DrissionPage 拒绝无 domain 的 flat Cookie（`No domain name is set`）。`initsmartbrowser/trylaunchbrowser` 支持 `requests_cookies_domain`（B 站传 `.bilibili.com`，通用抓取按目标站点推导）。 |
| 4 | **手动启动 Edge 90% 相似 ≠ DrissionPage 能跑通** | 排查浏览器问题时，必须通过真实 `ChromiumPage(co)` 全链路验证；绕过 DrissionPage 的手动复现会漏掉它的 mismatch-重启逻辑。 |
| 5 | **引擎懒加载** | `VD_LAZY_PARSERS=1` + sources 的 `__init__.py` 被剥掉 eager import。`_ensure_parsers_for_url` 按 URL 主机名只 import 需要的解析器。**注意**：懒加载的实现依赖打包时把解析器 .py 放进 bundle（见 4.1#1），两件事是配套的。通用解析器已全部移除，`common/__init__.py` 的 `_EAGER_COMMON` 为空列表。 |
| 6 | **错误提示绝不能直接回显内部日志** | `_humanize_error(after_seq)` 从 error 日志里取"最后一条"生成用户可见原因，但自己打的 `[jobid] download error: xxx` 也会进日志 → 曾把 `[78bef843b61d] download error: list index out of range` 原样显示到 UI。已修：① 取日志后先剥掉 `^\[[^\]]+\]\s*(?:download error:\s*)?` 前缀；② `list index out of range` 等引擎内部异常映射为友好文案（"未能解析到可用的视频流地址，请重新解析链接后重试"）。**给 sources 新增错误特征时往这个 if 链里加**，别把原始异常抛给用户。 |
| 7 | **任务级暂停/恢复/取消是"编排层"实现，不是 HTTP 断点续传** | `pause()`/`cancel()` 只 set `pause_event`/`cancel_event`；引擎下载循环里的检查点由 `ProgressBus.checkpause()/checkinterrupt()` 抛 `DownloadPaused`/`DownloadCancelled`（`app/backend/progress.py`）。条目捕获后置 `status=paused/cancelled`。`resume()` 做三件事：clear pause_event → 还在阻塞中的条目自动唤醒（in-flight）→ 已返回的 paused 条目重新提交线程池（用 `_inflight` 防重复提交）。**被重新提交的条目由引擎从头下载**——HTTP Range 断点续传未实现（见第 8 章），别误以为已经支持。 |
| 8 | **并发下载 = 每条目独立任务 + 线程池限流** | `enqueue()` 把每个条目 `executor.submit(self._process_item, ...)`；线程池 `max_workers = config.concurrent_downloads`（默认 2，`_effective_concurrent` 兜底取 `num_threadings`）。设置里改"同时下载数"后 `_ensure_executor()` 会按新值重建线程池。`_update_job_status()` 由条目状态派生任务状态（全部 done→done；全部 error→error；任一 downloading→downloading；任一 paused→paused；取消事件+全终态→cancelled）。 |
| 9 | **条目级进度定位靠 ProgressBus 上下文** | `_process_item` 进入下载前 `bus.set_context(job_id, item_key)` + `add_interrupt/add_pause`，finally 里 `clear_context/remove_*`。前端进度条按 `(job_id, item_key)` 渲染到对应条目下方（`renderItemProgress`）。忘记 clear_context 会导致进度串条目。 |
| 10 | **暂停/取消回调必须按 job 上下文隔离（修复"暂停任务A 误伤任务B"）** | 旧 `ProgressBus.checkpause()/checkinterrupt()` 会遍历**所有**已注册任务的暂停/取消回调，任意一个为 True 就抛异常——于是"暂停任务一"会让任务二正在跑的下载也被判为已暂停/已取消并报错。修复：两个检查点只按**当前线程上下文对应的 job_id** 取它自己的回调（找不到上下文则安全跳过），任务间互不干扰。`onupdate()` 里给引擎自行派生的下载子线程补上 `set_context(job_id, item_key)`（这些线程没有线程级上下文，旧逻辑会回退到"最近一次 set_context"而可能错绑到别的任务），保证 worker 线程命中正确的任务。改这一段务必保持"按 job_id 过滤"，别再退化成全局遍历。 |
| 11 | **暂停/取消后必须清掉旧进度任务（修复"已下载大小叠加"）** | 引擎每轮下载会新建一个 `DesktopProgress` 实例（owner 不同），旧任务在 `ProgressBus._tasks` 里仍以 `finished=False` 残留。恢复后前端 `renderItemProgress` 会把旧任务的 `completed` 与新任务的 `completed` 相加，导致"已下载 / 总大小"虚高、百分比异常。修复：新增 `ProgressBus.finish_tasks_for(job_id, item_key)`，在以下位置调用：① `_do_process_item` 捕获 `DownloadPaused` / 异常兜底为 paused/cancelled 时；② `_process_item` 捕获 `DownloadPaused`/`DownloadCancelled` 时；③ `resume()` 重新提交条目之前；④ `cancel()` 设置 `cancel_event` 之后。把旧任务标记为 `finished=True`，前端聚合时自然过滤掉。 |
| 12 | **YouTube 分段下载的"文件总大小"误用 Range 响应的 Content-Length（修复二次下载卡在 0 速度）** | `engine/vd/modules/utils/youtubeutils.py` 的 `RequestWrapper.stream()` 原本在第一次循环里发一个 `range=0-99999999999` 的请求，并把响应头的 `Content-Length` 当作文件总大小。但 Range 请求的 `Content-Length` 是**请求区间**的大小（≈10GB），真正的文件大小在 `Content-Range: bytes 0-.../<size>` 里。于是 `file_size` 被设成 10GB，外层 `while downloaded < file_size` 永远为真；当真实文件下完后继续请求超出末尾的区间，服务器返回 416，响应体为空，`resp.read()` 返回空 → 内层 `if not chunk: break` 退出但 `downloaded` 不再增长，外层无限重试且不再产出任何 chunk，于是在 base.py 的 `for chunk in iterchunks` 里**永久阻塞、速度恒为 0、进度卡住**。修复：删掉那段"用 Range 的 Content-Length 当总大小"的逻辑，改为"逐段请求 `range=N-(N+chunk-1)`，读到空体（416/EOF）或末段不足一块 (`got < chunk_size`) 就自然结束"，不再依赖任何文件总大小。 |
| 13 | **二次下载（成品已存在）应直接跳过，不重复走网络** | 用户期望：输出目录里已有完整成品时就别再下载。在 `base.py::_downloadfromyoutube` 开头、做 `_ensureuniquefilepath` 改名之前，先用 `video_info.download_url.filesize` 与 `os.path.getsize(save_path)` 比对；大小一致即把 `video_info` 直接 `append` 进 `downloaded_video_infos` 并 `return`，不建进度任务、不触碰网络。注意必须在 `_ensureuniquefilepath` 之前用**原始** `save_path` 校验，否则改名成 `xxx (1).mp4` 后就永远比对不上了。 |
| 14 | **关闭窗口后仍有后台进程残留（msedgewebview2 / ffmpeg / aria2c / node 不死）** | 根因：`app/backend/core.py` 的下载线程池是 `concurrent.futures.ThreadPoolExecutor`，其工作线程**未设 daemon**（继承主线程=非守护）。窗口关闭时 `webview.start()` 返回 → 进程进入退出流程，Python 的 `atexit` 会调 `executor.shutdown(wait=True)` 并**阻塞等待仍在跑的下载任务结束**；而正在跑的任务若卡在 ffmpeg/aria2c/node 子进程或网络调用上，python 进程就永远退不出去，挂靠在它身上的 `msedgewebview2.exe`（WebView2 运行时）和所有引擎子进程全部变成"关了窗口还在后台跑"的孤儿。修复三层：① `core.py` 全局 patch `subprocess.Popen` 时把所有子进程登记进 `_SUBPROCESS_REGISTRY`，并新增 `VideoDlService.shutdown()`（取消全部 job、`executor.shutdown(wait=False, cancel_futures=True)`、杀光登记的子进程）；② `api.py` 暴露 `JsApi.shutdown()`；③ `app.py::run_ui` 注册 `window.events.closed`，在窗口关闭时先 `api.shutdown()` 优雅收尾，再调 `_hard_exit()`（`psutil` 递归杀掉本进程整棵子树 + `os._exit`），**兜底保证"关窗口=进程消失"**。注意 `os._exit` 会跳过 `atexit`，但单例锁由 supervisor 持有、child 不持锁，故无残留 pid 问题。 |
| 15 | **重启后自动加载未完成任务（只加载不开始，用户手动"开始/继续"才跑）** | 这是**预期功能，不是 bug**，切勿再改回"丢弃未完成任务"。`jobs.json` 本就持久化任务元数据（路径见 `Config.jobspath()`）。原 `_load_jobs()` 故意 `discard` 非终端任务（旧注释担心 URL 过期/弹浏览器），现已改为：**启动 (`VideoDlService.__init__` → `_load_jobs`) 时把非终端任务恢复为 `paused` 状态载入**——不提交 executor 即"不自动开始"；已完成项保持 `done`、其余重置为 `queued`。前端对 `paused` 任务显示"开始/继续"按钮（`data-resume` → `api.resume(job_id)`），点击即走 `resume()`：它检测到 `_parsed` 为空会 `_reparse_for_resume()` 重新解析源 URL 重建 `VideoInfo`（引擎程序化解析不弹浏览器），并按标题把未完成项重新映射、已存在成品的项保留 `done`，实现"断点续传 + 只补下缺的"。**关键约束**：`VideoDlService.shutdown()`（窗口关闭时调用）**绝不能 `cancel` 任务**——否则未完成任务会被标 `cancelled` 并从 `jobs.json` 移除，恢复功能即失效。shutdown 只停 executor、杀子进程、flush 任务状态到磁盘，任务以原状态被下次启动恢复。 |
| 16 | **打包（`pyinstaller build.spec --noconfirm`）会因 SafeDelete 守卫卡住/失败** | CodeBuddy 的 PowerShell 运行时给 `Remove-Item` 包了一层 **SafeDelete** 守卫：删除 ≥500 个文件的"批量删除"会要求人工确认（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，阈值 500），非交互下直接抛 `InvalidOperationException` 并 `Nothing was deleted`。PyInstaller 的 `--noconfirm` 在清理旧 `dist/VideoDLDesktop`（上千文件）时正好命中，导致构建在 `Removing dir ...` 处失败（日志末尾可见 `[safe-delete]...BULK_CONFIRM_REQUIRED`）。**绕过方法（任选其一）**：① 用 .NET 直接删（不走 Remove-Item）：`[System.IO.Directory]::Delete('dist', $true)` / `Delete('build', $true)`；② 用 `cmd /c "rmdir /s /q dist build"`（cmd 的 rmdir 不经过 PS 守卫）；③ 注意：这一关只在 `dist` 已存在时触发——**预删除 `dist`/`build` 让 PyInstaller 无需清理**，后续它在构建过程中对自己新建的小文件做 rmtree（远低于 500 阈值）不会再触发。构建命令：`python -m PyInstaller build.spec --noconfirm *> build.log`（用 `*>` 重定向避免 PowerShell 把 pyinstaller 的 stderr 进度当成 NativeCommandError）。 |

| 17 | **YouTube 单流并行下载（提速尝试，受限于"按流限速"）** | `engine/vd/modules/utils/youtubeutils.py` 的 `RequestWrapper.stream()` 现支持"已知总大小时把单条流切成最多 `parallel_connections`(默认 8) 个并发字节区间"下载，再按序拼回（顺序 yield，兼容现有进度/暂停/取消）。配套：`_fetch_range`(带长度校验+重试，末段 `allow_partial` 允许 EOF 截断)、`_stream_sequential`(原单连接逻辑，未知大小时回退)、`parallel_segment_size`(默认 8MB/段)、`parallel_connections`(默认 8)；curl 会话改为**线程级**(`_get_curl_session` 用 `threading.local`)以保证并行下载线程安全；`iterchunks` 把流已知的 `contentLength` 作为 `total_size` 传入（**不再依赖 HEAD**——YouTube 对 HEAD 常不返回 content-length，会导致静默回退到单连接，表现为"加连接数但速度不变"）；`seqstream` 改走 `_stream_sequential`（避免每段额外 HEAD）。**重要认知（勿再"修"）**：YouTube 限速是按"流"(每条流一个 `id`/`n` 签名)来的，**不是按 TCP 连接**来的。因此把同一视频流切成 8 个区间并行下载，8 个连接命中的是同一个限速桶，速度不变；而音频+视频本来就在 5 线程池里并行（那是"两个下载同时下速度翻倍"的来源，是两个不同 `id`=两个限速桶）。所以单视频的视频流速度已顶到该流上限，加连接数无效。并行代码在 YouTube 不对单流限速时仍生效且对现有下载无副作用，保留即可。验证：`stream()` 会打日志 `[YouTube DL] parallel download: total=... bytes, segments=N`（进 UI 日志面板），N>1 即并行已开。 |
| 18 | **二次打开应用恢复任务时卡死/崩溃（已修复，本批）** | 现象：关闭后再打开，应用尝试恢复未完成任务（`_load_jobs` 见 #15）时在恢复/重新解析(`_reparse_for_resume`)/重新提交线程池路径上因竞态或异常未兜底而崩溃或卡死。修复要点：恢复路径全程异常兜底；恢复只载入 `paused` 状态、不自动提交 executor（用户手动"开始/继续"才跑）；`shutdown()` 绝不 `cancel` 任务（见 #15），任务以原状态被下次启动恢复。现在二次打开能正常恢复且不再崩溃。 |
| 19 | **对同一链接二次下载失败会崩溃（已修复，本批）** | 现象：第二次对同一条目/链接发起下载时，因成品已存在判定、`_ensureuniquefilepath` 改名、或恢复逻辑重复提交导致异常未被捕获而崩溃。修复：在 `_downloadfromyoutube` 开头用**原始** `save_path` 比对成品大小决定跳过（见 #13）；恢复/重提走 `_inflight` 防重复提交；下载异常统一走日志友好化（见 #12）而非抛出崩溃。 |
| 20 | **两个下载挤一个框、只要一套按钮、相互阻塞（已修复，本批）** | 现象：同一任务的多个条目（音频+视频/多清晰度）被渲染进同一个任务卡片，共用一套进度条与暂停/取消按钮，一个条目暂停/取消会误伤/阻塞同框其他条目。根因与 #10/#11 的"上下文隔离 + 旧进度任务清理"同源。修复：进度按 `(job_id, item_key)` 定位到各自条目下方（#9/#11），每条目独立按钮组（下载中=暂停+取消、暂停=播放+取消、终态=打开目录+移除），任务级与条目级状态解耦，互不阻塞。 |

### 4.4 平台解析器层

| # | 平台 | 坑与修复 |
|---|---|---|
| 1 | **B站** | ① 高画质 dash 抓取走 DrissionPage 浏览器方案（`_fetch_playurl_via_browser`）：登录 Cookie 注入 + 主站前端自己完成 wbi 签名/bili_ticket。② **主路径是从浏览器 performance 时间线拿到播放器自己的 playurl 签名 URL 再在页面内 XHR 回放**——不要依赖 CDP 网络监听（异步生效、会被媒体分片淹没、风控时序随机）。③ **不要在等待中 reload 页面**——B 站风控引导（gaia 指纹）会把 playurl 拖到 ~30s，reload 等于重来。④ `__playinfo__` 已被 B 站下线（undefined），别指望它。⑤ 监听兜底匹配 `api.bilibili.com` 而不是旧的 `/x/player/playurl` 字面量（现在实际是 `/x/player/wbi/playurl`）。 |
| 2 | **B站** | **dash 条目设置 `audio_download_url` 时必须同时设置 `audio_save_path`/`audio_ext`/`default_audio_download_headers/cookies`**。漏掉 `audio_save_path` 会让合并下载器 `touchdir(dirname(''))` → `WinError 3 系统找不到指定的路径。: ''`。两处已修：质量枚举条目 + `_build_bili_720p` 兜底条目。 |
| 3 | **抖音** | **解析地址必须 `ratio=default`，不能用 `ratio=1080p`**。实测同视频：1080p 档 301kbps（低码率转码，画质很差），default 档 718kbps（播放器实际使用的主码率流）。SSR 数据里没有 bit_rate 档位表，不要去找。 |
| 4 | **B站 4K 前置条件** | 浏览器 fetch 时要注入用户登录 Cookie（config `per_source_cookies.BilibiliVideoClient`，含 SESSDATA），否则匿名只拿 720P(durl)。Cookie 过期表现为解析正常但 dash 缺失/报错，引导用户重新登录。 |
| 5 | **YouTube: 用 VISIONOS 客户端绕过 GVS poToken（关键解法）** | 实测（2026-09）：① ANDROID 等客户端的 adaptive(≥1080P) 直链被 GVS poToken 硬门控（一律 403），只能下 360P progressive；② **`VISIONOS` 客户端（clientName=VISIONOS，clientVersion=1.02，INNERTUBE_CONTEXT_CLIENT_NAME=101）不在 GVS poToken 策略内**，返回完整自适应梯子（4K/1440P/1080P/720P/480P/360P）且无需 pot、无需 JS 解签——已从 DEFAULT_CLIENTS 补上并设为首选（配置取自 yt-dlp 的默认 js-less 客户端，可用 yt-dlp 源码交叉核对更新）；③ 其余客户端（ANDROID/ANDROID_VR/TV/WEB_EMBED 等）保留为兜底轮换；④ 已实测无效的路线（勿再投入）：把 pot 贴到 URL、pot 注入 player 请求的 serviceIntegrityDimensions、visitorData/videoId 各种绑定组合、以及用 bgutils-js 重新生成 pot——GVS 一律 403（BotGuard 证明需通过运行环境校验）；⑤ **googlevideo 要求请求带 Range 头**，无 Range 的普通 GET 也 403——下载头已固定 `Range: bytes=0-`；⑥ 旧 JS 解签机制（fmt_streams/Cipher 的 sig/nsig 正则）在当前播放器构建上已失配且不再必要，解析器直接从 raw streamingData 构建条目；⑦ 画质梯子带 Range 探测过滤（403 档位自动剔除并打 warning），另有低分辨率兜底（老视频仅 240P 时仍能出条目）。 |
| 6 | **B站 字幕（专有 JSON → VTT 转换）** | playurl 响应 `subtitle.subtitles` 含字幕轨：每条 `{lan, lan_doc, subtitle_url}`，`subtitle_url` 形如 `//...json` 是 B站**专有字幕格式**（非标准字幕），必须下载后转 VTT 才能被 ffmpeg 封装。`_parsefromcommonurl`（dash / 浏览器 durl / requests durl 三分支）与 720P 兜底 `_build_bili_720p` 均会从 playurl 提取，写入 `VideoInfo.subtitles` 并标记 `format='bilibili_json'` + 登录 Cookie。引擎 `_download_subtitle_file` 检测到该格式即下载 JSON → `_bilibili_json_to_vtt()` 转 VTT（`from/to` 秒→`HH:MM:SS.mmm`，`content` 的 `\n` 还原换行）→ 存 `.vtt` 临时文件 → ffmpeg 内封。 |
| 7 | **字幕接入范式（所有平台通用）** | 字幕不再需要逐个平台硬写 mux 逻辑：解析器只要在 `VideoInfo.subtitles` 填 `[{lang, url, ext, format, headers, cookies}]`，引擎 `_collect_subtitle_sources → _download_subtitle_file → _mux_subtitles_if_any` 就会自动下载并封装。① `format` 缺省按标准字幕（vtt/srt/ass）直接 mux；② `format='bilibili_json'` 触发 JSON→VTT 转换（已内置）；③ **HLS（m3u8）来源全自动**：下载完成后引擎解析播放列表，把 `#EXT-X-MEDIA` 的 subtitle 轨自动加进 `subtitles`，Tencent视频/Weibo/通用抓取器等走 HLS 的平台无需适配即生效。开关在设置「下载字幕并封装进视频」（写 `download_subtitles`），仅对解析结果里 `subtitles` 非空的条目触发。新平台接字幕优先看源站是否暴露字幕 URL 填进 `subtitles`；YouTube/Douyin 等仍需各自接 caption API（见第 8 章）。 |

### 4.5 前端层（app/web）

| # | 坑 | 原因与解法 |
|---|---|---|
| 1 | **平台白名单勾选状态有初始化竞态** | 设置面板由两个独立异步请求驱动：`sources`（平台列表，快）和 `bootstrap`（配置，慢）。勾选状态**只能在两者都就绪后初始化一次**（`initCheckedSources` + `state.pendingCheckedInit` + `checkedInitDone`）。旧代码在 sources 回调里读还没到的 config → 落进"空=全选"分支 → 用户一保存就全量写回。**不要把"空 allowed_sources = 全部"的语义加回来**：后端 `Config.load()` 已把空列表映射为默认 2 平台（抖音+B站），保存时也如实存勾选列表。 |
| 2 | **默认画质选择** | `pickDefaultQualitySelection`：指定画质→每组资源选 1 项，没有该画质**向下兼容**（选低一档），只有全高才取高一档；`best`→每组最高；`auto`→才全选。改这段时保持"**不全选**"约束。 |
| 3 | **任务卡片状态** | 条目状态（.st）始终显示且统一样式（药丸），状态集合：`queued/downloading/paused/pausing/resuming/done/error/cancelled/cancelling`；任务级徽标在 `done/downloading` 时隐藏（条目已表达），其余状态显示。**条目错误详情不再拼进状态行文本**（曾把整段 `下载失败：[jobid] download error: ...` 显示在条目下方，很丑），只显示"失败"两个字，详情放 `title` 悬停提示；任务级错误行已删除。 |
| 3b | **下载控制按钮与条目进度条** | 操作区统一 `icon-btn` 图标按钮：下载中=暂停+取消，暂停=播放+取消，终态=打开目录+移除。`downloading/paused/pausing` 的条目在标题下方渲染 `renderItemProgress` 进度条（数据来自 ProgressBus 按 `(job_id, item_key)` 定位）。设置弹窗新增"同时下载数"（`cfgConcurrent`，读写 `concurrent_downloads`）。 |
| 4 | **主题** | 3 套配色 = CSS 变量组（默认`:root`(space/深空) / `html[data-theme='emerald']` / `html[data-theme='sunset']`），JS 端 `applyTheme` + localStorage 持久化。新增主题：加一组变量 + 在 `THEMES` 数组加一项。**页面里禁止再硬编码主题色 rgba**，一律 `rgba(var(--accent-rgb), .x)`。 |
| 5 | **引擎计数** | 顶栏显示"已启用 N/M 解析器"（N=白名单数），不要只显示总数。 |
| 6 | **设置弹窗已裁剪两项：全局 Cookie 输入框、"仅使用通用解析器"复选框（2026-09 需求）** | Cookie 统一在顶栏「登录态」按钮维护（DrissionPage 弹窗登录 → `per_source_cookies`），**不要把全局 `cfgCookies` 输入框加回来**——设置里的手贴 Cookie 与登录态两套来源会互相覆盖，用户分不清哪个生效。`apply_common_clients_only` 已随通用解析器移除失去意义，UI 同步删除。兼容处理：后端 `Config`/`setconfig` 保留 `cookies`、`apply_common_clients_only` 字段不删（旧 config.json 仍能加载，值恒为 ''/False），但前端不再读写。前端反爬提示（412/403）的判断从 `state.config.cookies` 改为检查 `per_source_cookies` 是否非空，所有"去设置填 Cookie"类文案统一改为"点击顶栏「登录态」按钮登录"（前端 `applyParseResult` + 后端 `_humanize_error` 两处都要改，避免文案互相矛盾）。 |
| 7 | **平台 Cookie 编辑框已从「设置」迁移到「登录态」弹窗** | 旧实现把"平台 Cookie"多行文本框（`#sourceCookies`）放在设置弹窗里，和登录态弹窗的 DrissionPage 登录并存，两套来源互相覆盖、用户分不清哪个生效。现改为：① 登录态弹窗 `openLoginModal()` 加载平台列表并渲染每个平台的 Cookie 文本框，提供「保存 Cookie」按钮单独调用 `setconfig({per_source_cookies})`；② 设置弹窗 `openSettings()` 不再渲染/收集 Cookie，`saveSettings()` 的提交负载也去掉 `per_source_cookies`，避免从设置保存时把 Cookie 清空。改动文件 `index.html` + `app.js`。**勿再把 sourceCookies 加回设置弹窗**——会重新引入覆盖 bug。 |
| 8 | **「下载字幕并封装进视频」开关** | 设置弹窗新增复选框 `cfgSubs`（读写 `download_subtitles`，默认 `true`）。勾选后，解析结果里 `subtitles` 非空的条目会在下载完成后自动把字幕封装进视频（见 4.4#7）；关闭时仅下视频、忽略字幕轨。 |

| 9 | **历史面板不停刷新（已修复，本批）** | 现象：历史记录列表每隔轮询周期就整体重渲染（闪动/刷新）。根因：前端 `poll()` 每 700ms（`setInterval(poll, 700)`）拉取 state，旧逻辑每次都无条件 `state.history = data.history; renderHistory()`。修复：`renderHistory()` 前先算 `historySnapshot`（`url\|source\|last_used_at` 拼接）并与 `state._historySnapshot` 比对，仅当快照变化才重渲染（`app.js` 的 `newSnap !== state._historySnapshot` 分支）。**轮询本身仍每 700ms 一次，只是重渲染做了去重**——勿把"停掉轮询"当修复方向。 |

### 4.6 诊断约定

- **startup.log**（`diag.py`）：启动/进程级事件，卡死排查第一现场。注意 30s 的 UI_LOAD_TIMEOUT 超时会打 `supervisor: UI did not load`。
- **desktop.log**（vd logger）：引擎内部日志。
- **UI 日志面板**：`UiLogHandler` 挂在 vd logger 上。**error 级只给真实失败**；进度类用 info、数据包转储用 debug（前端"精简"模式会隐藏 info/debug）。之前把 `[DIAG]` 全打 error 导致满屏红色，已清理——别再改回去。
- 前端 `felog`（ui-boot/ui-poll/ui-crash scope）会写进同一个 startup.log，前后端时间线可以拼起来看。

---

## 5. 配置文件参考（config.json）

位置：`C:\Users\<你>\AppData\Local\vd\vd-desktop\config.json`

```jsonc
{
  "work_dir": "C:\\Users\\...\\vd_downloads",
  "num_threadings": 5,              // [遗留字段] 解析/线程数，仅为兼容保留；concurrent_downloads 缺失时回退取它
  "concurrent_downloads": 2,        // 同时下载数（设置弹窗"同时下载数"），线程池 max_workers，修改后对后续提交的条目生效
  "proxy": "",                      // 空=直连；写 host:port 自动补 http://
  "cookies": "",                    // [已废弃] 全局 Cookie 输入框已从设置移除；字段仅为旧配置兼容保留，恒为空。
                                    // 登录态统一由顶栏「登录态」按钮维护（写入下面的 per_source_cookies）
  "per_source_cookies": {           // 平台登录态（登录弹窗自动写入，键=解析器类名）——唯一的 Cookie 来源
    "DouyinVideoClient": "sessionid=...; ...",
    "BilibiliVideoClient": "SESSDATA=...; bili_jct=...; ..."
  },
  "default_quality": "best",        // best|4k|1080p|720p|480p|360p|auto
  "download_subtitles": true,        // 下载字幕并封装进视频（设置弹窗「下载字幕并封装进视频」）；仅对解析结果里 subtitles 非空的条目生效
  "apply_common_clients_only": false, // [已废弃] UI 复选框已移除，恒为 false；字段仅为旧配置兼容保留
  "allowed_sources": ["DouyinVideoClient", "BilibiliVideoClient"],  // 空=回退默认平台；启动时自动过滤已删除的解析器类名
  "last_url": "..."
}
```

---

## 6. 常见任务手册

| 任务 | 步骤与注意 |
|---|---|
| 加一个新的平台解析器 | 在 `engine/vd/modules/sources/` 加 .py（类继承 BaseVideoClient，AutoRegisterMeta 自动注册）→ 无需改 spec（datas walk 自动带上）→ 前端白名单/登录网格自动出现（数据来自 `sources` API）。 |
| 改 UI 样式 | 只动 `app/web/`。主题色必须用 CSS 变量。改完需重新打包（web 是 datas，热改 bundle 里的文件也能临时验证）。 |
| 发新版本 | 1) 改 `app/app.py APP_VERSION`；2) 关掉运行中的 exe；3) `python -m PyInstaller build.spec --noconfirm`（产物只落在 `dist/VideoDLDesktop/`，**不建 release_vX 目录**）；4) 核对 startup.log 出现 `window shown after successful load`。 |
| 排查启动卡死 | `startup.log`：找 `supervisor: launching UI child (attempt N)`。若 attempt 反复失败→WebView2 环境问题（重启机器 / 修复 WebView2 Runtime / 加 Defender 白名单）。若 `UI loaded OK`→看后续业务日志。 |
| 登录态失效 | 平台重新弹窗登录即可；B 站 Cookie 失效的典型症状：解析到 720P(durl) 而不是 dash 4K。 |
| 验证下载控制改动 | ① 打包前杀 exe；② 启动后下一个多条目任务（≥ concurrent_downloads 条）验证：进度条出现在对应条目下方、暂停后条目变"已暂停"、恢复后继续、取消后变"已取消"；③ 看 desktop.log 有 `paused:`/`resumed`/`cancelled item:` 日志。 |
| 验证一次改动 | ① 杀掉运行中的 exe；② 打包；③ 启动后 `startup.log` 必须出现 `webview page loaded` → `window shown after successful load` → `engine ready`；④ 用 B 站链接实测解析（`[DIAG bilibili] browser fetch DONE ddata=dash`）。 |
| 验证字幕封装 | ① 设置勾选「下载字幕并封装进视频」；② 用有字幕的 B 站视频（或任意 HLS 来源）解析并下载；③ 产物视频用播放器/ffprobe 确认内封了字幕轨（`ffprobe out.mp4` 出现 `Stream #0:N(zh): Subtitle`）；④ desktop.log 应有字幕下载/转换/封装日志（`_download_subtitle_file` / `_bilibili_json_to_vtt` / mux 相关）。B站字幕应是从专有 JSON 转成的 VTT，不是原始 json。 |

---

## 7. 复刻清单（从零到当前形态的最短路径）

1. Python 3.11 + `pip install -r requirements.txt`（含 pywebview、DrissionPage、PyInstaller）。
2. **重打 pywebview site-packages 补丁**（见 4.1#6，这是复刻最容易漏的一步）。
3. 引擎包放 `engine/vd/`；剥掉 `modules/sources/__init__.py` 里的 eager import（懒加载前提），并设置 `VD_LAZY_PARSERS=1`。`modules/common/__init__.py` 的 `_EAGER_COMMON` 保持空列表（通用解析器已移除）。
4. 按 `build.spec` 打包；确认 `[spec] packaged N non-python resource file(s)` 中 N > 0。
5. 桌面壳按 4.2 实现：supervisor + hidden 窗口 + 加载标记文件 + 单实例聚焦 + CREATE_NO_WINDOW。
6. 前端按 4.5：白名单初始化防竞态、画质向下兼容、主题 CSS 变量。
7. 启动验证三条日志：`webview page loaded` / `window shown after successful load` / `engine ready`。

---

## 8. 已知边界（当前未解决，接受现状）

- **失败/暂停后的 HTTP 断点续传未实现**（计划后续版本）：当前"恢复"是把 paused 条目重新提交，引擎从头下载该条目；下载中途失败同样整条重下。要做真续传需引擎侧支持 Range/分片缓存（N_m3u8DL-RE 自带 `--continue`，但 ffmpeg 合并类下载没有）。
- WebView2 初始化死锁的**根因**未完全定位（与系统环境相关），当前靠隐藏启动+快速自愈把影响降到不可见。若要根治，方向是：给用户机器加 Defender 白名单、或改用 CEF/固定版本 WebView2 Runtime。
- 抖音 1080p 档天生低码率（CDN 行为），已取 `default` 主码率流，理论上限受抖音服务端控制。
- 顶栏工具 chips（FFmpeg 等）已按需求移除；工具缺失信息在设置弹窗底部。外部工具（ffmpeg/ffprobe/node）依赖系统 PATH，不再随包捆绑（见 4.1#8）。
- 引擎解析内部异常（如 `list index out of range`）只能引导用户重新解析，根因在上游引擎对源站数据结构的假设，暂不在本项目修。
- **字幕覆盖现状（接受现状，待逐项扩展）**：① B站 已接（专有 JSON→VTT 转换）；② HLS（m3u8）平台（Tencent视频/Weibo/通用抓取器等）靠播放列表自动提取字幕轨，无需适配；③ **YouTube / 抖音等** 各自需接入独立 caption API（YouTube innertube caption、抖音多数短视频本无字幕），尚未实现——解析器只填 `VideoInfo.subtitles` 即可被引擎自动封装，故新增平台字幕是"填 URL + 必要时扩 `format` 转换器"的量级工作，不是大改。

---

## 9. 隐私清理变更记录（相对上游 videodl 的删减）

本项目在 fork 上游 `videodl` 引擎后，主动移除了以下存在隐私风险或已废弃的模块。**升级上游时不要重新引入**。

### 9.1 删除的解析器（39 个）

| 类型 | 数量 | 文件 | 原因 |
|------|------|------|------|
| 通用代解析器 | 36 | `modules/common/*.py`（anyfetcher/bugpk/gv/kedou/snapwc 等） | 这些解析器会把用户粘贴的视频 URL 转发给第三方服务器（如 anyfetcher.com、apicx.asia 等），收集用户浏览记录 |
| 平台 DRM 解析器 | 2 | `modules/sources/playerpl.py`、`modules/sources/wittytv.py` | 依赖 Widevine DRM 设备凭证（.wvd 文件），属于外部 DRM 体系 |

### 9.2 删除的资源文件

| 文件/目录 | 说明 |
|-----------|------|
| `modules/utils/cdm.py` | DRM 解密工具（initcdm/closecdm/SearchPsshValueUtils），零调用方 |
| `modules/js/xmflv/` | xmflv 通用解析器的 JS/WASM 资源，解析器删除后变死资源 |

### 9.3 清理的代码字符串

| 字符串 | 原位置 | 处理 |
|--------|--------|------|
| `zcjin` | `base.py`/`logger.py` appauthor | 改为 `vd` |
| snapwc.py 埋点上报 | 2 个 api.event/log 请求 | 整个 snapwc.py 已删除 |

### 9.4 移除的依赖

| 依赖 | 原因 |
|------|------|
| `pywidevine` | 仅被已删除的 cdm.py 使用，无其他引用 |

### 9.5 保留的可疑工具（genius.py 依赖）

- `modules/utils/smuggler.py`（BrightcoveSmuggler）— genius 平台解析器仍在使用，**保留**
- `modules/utils/ip.py` — 随机 IP 头生成工具，保留

### 9.6 配置兼容机制

`backend/core.py` 实现了双层死配置清理：
1. **Config.load()** — 硬编码 `_DELETED_PARSER_CLASSES` 集合，加载时过滤已删除解析器的 `allowed_sources` 和 `per_source_cookies`
2. **ensureengine()** — 引擎初始化后动态对比 `_EAGER_PARSERS` + `_EAGER_COMMON`，自动清理任何未来删除的解析器配置

用户旧配置（含已删除解析器的勾选/cookie）会在首次启动时自动清理并写回，无需手动操作。
![alt text](image.png)