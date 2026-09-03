# 全能下载器 · 大模型思考构建指南

> **读者**：未来接手本项目的大模型（或工程师）。
> **目标**：不复述所有代码，而是告诉你**为什么是现在这个样子**、**哪些坑已经被踩过并修好**、
> **动手前必须检查什么**。改代码前请先通读第 4 章（踩坑清单），里面每一条都对应一次真实的返工。

---

## 1. 项目一句话

把上游命令行视频下载引擎 `vd`（69 平台解析器；36 个通用解析器已全部移除以保护用户隐私，见 `engine/vd/`）
封装成 Windows 桌面应用（pywebview + Edge WebView2），并在四层做了深度修复：
**引擎调用层、浏览器自动化层、打包层、前端交互层**。

技术栈：Python 3.11 + pywebview(edgechromium) + DrissionPage + requests/curl_cffi + rich + PyInstaller(onedir)。

---

## 2. 目录结构（重组后的最终形态）

```
VideoDownLoad/
├── app/                        # 桌面应用壳
│   ├── app.py                  # 入口。main() 分三路：--selftest / --child(UI) / 默认=监控进程
│   ├── backend/
│   │   ├── core.py             # VideoDlService：配置、任务队列、引擎懒加载、下载编排
│   │   ├── api.py              # JsApi：window.pywebview.api.* 的全部桥接方法
│   │   ├── progress.py         # rich Progress 劫持 → 进度总线（下载进度进 UI）
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
├── bin/                        # N_m3u8DL-RE / aria2c / ffmpeg / ffprobe（打进 _internal/bin）
├── build.spec                  # PyInstaller 配置（在项目根目录运行）
├── requirements.txt            # 引擎 + 桌面壳合并依赖
├── docs/                       # 本指南 + 历史文档（BUILD_zh / DESIGN_zh 路径已过时）
└── release_v10n_19/VideoDLDesktop/   # 打包产物（onedir），名字里的 19 是版本序号
```

`__pycache__` 已清理。

---

## 3. 构建 / 运行 / 验证 命令

```powershell
# 构建（必须在项目根目录；产物在 dist\VideoDLDesktop\）
cd /d D:\CodeBuddy\VideoDownLoad
python -m PyInstaller build.spec --noconfirm

# 覆盖发布目录（必须先关掉正在运行的 exe，否则 xcopy 报 Sharing violation）
taskkill /f /im VideoDLDesktop.exe
xcopy /E /I /Y dist\VideoDLDesktop release_v10n_19\VideoDLDesktop

# 源码运行
$env:PYTHONIOENCODING = "utf-8"
python app\app.py

# 无头自检（解析→下载→校验，报告写用户目录）
release_v10n_19\VideoDLDesktop\VideoDLDesktop.exe --selftest
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

### 4.4 平台解析器层

| # | 平台 | 坑与修复 |
|---|---|---|
| 1 | **B站** | ① 高画质 dash 抓取走 DrissionPage 浏览器方案（`_fetch_playurl_via_browser`）：登录 Cookie 注入 + 主站前端自己完成 wbi 签名/bili_ticket。② **主路径是从浏览器 performance 时间线拿到播放器自己的 playurl 签名 URL 再在页面内 XHR 回放**——不要依赖 CDP 网络监听（异步生效、会被媒体分片淹没、风控时序随机）。③ **不要在等待中 reload 页面**——B 站风控引导（gaia 指纹）会把 playurl 拖到 ~30s，reload 等于重来。④ `__playinfo__` 已被 B 站下线（undefined），别指望它。⑤ 监听兜底匹配 `api.bilibili.com` 而不是旧的 `/x/player/playurl` 字面量（现在实际是 `/x/player/wbi/playurl`）。 |
| 2 | **B站** | **dash 条目设置 `audio_download_url` 时必须同时设置 `audio_save_path`/`audio_ext`/`default_audio_download_headers/cookies`**。漏掉 `audio_save_path` 会让合并下载器 `touchdir(dirname(''))` → `WinError 3 系统找不到指定的路径。: ''`。两处已修：质量枚举条目 + `_build_bili_720p` 兜底条目。 |
| 3 | **抖音** | **解析地址必须 `ratio=default`，不能用 `ratio=1080p`**。实测同视频：1080p 档 301kbps（低码率转码，画质很差），default 档 718kbps（播放器实际使用的主码率流）。SSR 数据里没有 bit_rate 档位表，不要去找。 |
| 4 | **B站 4K 前置条件** | 浏览器 fetch 时要注入用户登录 Cookie（config `per_source_cookies.BilibiliVideoClient`，含 SESSDATA），否则匿名只拿 720P(durl)。Cookie 过期表现为解析正常但 dash 缺失/报错，引导用户重新登录。 |

### 4.5 前端层（app/web）

| # | 坑 | 原因与解法 |
|---|---|---|
| 1 | **平台白名单勾选状态有初始化竞态** | 设置面板由两个独立异步请求驱动：`sources`（平台列表，快）和 `bootstrap`（配置，慢）。勾选状态**只能在两者都就绪后初始化一次**（`initCheckedSources` + `state.pendingCheckedInit` + `checkedInitDone`）。旧代码在 sources 回调里读还没到的 config → 落进"空=全选"分支 → 用户一保存就全量写回。**不要把"空 allowed_sources = 全部"的语义加回来**：后端 `Config.load()` 已把空列表映射为默认 2 平台（抖音+B站），保存时也如实存勾选列表。 |
| 2 | **默认画质选择** | `pickDefaultQualitySelection`：指定画质→每组资源选 1 项，没有该画质**向下兼容**（选低一档），只有全高才取高一档；`best`→每组最高；`auto`→才全选。改这段时保持"**不全选**"约束。 |
| 3 | **任务卡片状态** | 条目状态（.st）始终显示且统一样式（药丸）；任务级徽标在 `done/downloading/queued` 时隐藏（条目已表达），只留 `cancelling/cancelled/error`。 |
| 4 | **主题** | 3 套配色 = CSS 变量组（`:root` / `html[data-theme='emerald']` / `html[data-theme='sunset']`），JS 端 `applyTheme` + localStorage 持久化。新增主题：加一组变量 + 在 `THEMES` 数组加一项。**页面里禁止再硬编码主题色 rgba**，一律 `rgba(var(--accent-rgb), .x)`。 |
| 5 | **引擎计数** | 顶栏显示"已启用 N/M 解析器"（N=白名单数），不要只显示总数。 |

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
  "num_threadings": 5,
  "proxy": "",                      // 空=直连；写 host:port 自动补 http://
  "cookies": "",                    // 全局 Cookie（所有源的兜底）
  "per_source_cookies": {           // 平台登录态（登录弹窗自动写入，键=解析器类名）
    "DouyinVideoClient": "sessionid=...; ...",
    "BilibiliVideoClient": "SESSDATA=...; bili_jct=...; ..."
  },
  "default_quality": "best",        // best|4k|1080p|720p|480p|360p|auto
  "apply_common_clients_only": false, // [已废弃] 通用解析器已全部移除，此配置项无实际作用
  "allowed_sources": ["DouyinVideoClient", "BilibiliVideoClient"],  // 空=回退默认2平台；启动时自动过滤已删除的解析器类名
  "last_url": "..."
}
```

---

## 6. 常见任务手册

| 任务 | 步骤与注意 |
|---|---|
| 加一个新的平台解析器 | 在 `engine/vd/modules/sources/` 加 .py（类继承 BaseVideoClient，AutoRegisterMeta 自动注册）→ 无需改 spec（datas walk 自动带上）→ 前端白名单/登录网格自动出现（数据来自 `sources` API）。 |
| 改 UI 样式 | 只动 `app/web/`。主题色必须用 CSS 变量。改完需重新打包（web 是 datas，热改 bundle 里的文件也能临时验证）。 |
| 发新版本 | 1) 改 `app/app.py APP_VERSION`；2) `python -m PyInstaller build.spec --noconfirm`；3) 建议复制到新目录 `release_v<序号>/`，旧的删掉（用户要求过只留一份）；4) 核对 startup.log 出现 `window shown after successful load`。 |
| 排查启动卡死 | `startup.log`：找 `supervisor: launching UI child (attempt N)`。若 attempt 反复失败→WebView2 环境问题（重启机器 / 修复 WebView2 Runtime / 加 Defender 白名单）。若 `UI loaded OK`→看后续业务日志。 |
| 登录态失效 | 平台重新弹窗登录即可；B 站 Cookie 失效的典型症状：解析到 720P(durl) 而不是 dash 4K。 |
| 验证一次改动 | ① 杀掉运行中的 exe；② 打包；③ 启动后 `startup.log` 必须出现 `webview page loaded` → `window shown after successful load` → `engine ready`；④ 用 B 站链接实测解析（`[DIAG bilibili] browser fetch DONE ddata=dash`）。 |

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

- WebView2 初始化死锁的**根因**未完全定位（与系统环境相关），当前靠隐藏启动+快速自愈把影响降到不可见。若要根治，方向是：给用户机器加 Defender 白名单、或改用 CEF/固定版本 WebView2 Runtime。
- 抖音 1080p 档天生低码率（CDN 行为），已取 `default` 主码率流，理论上限受抖音服务端控制。
- 顶栏工具 chips（FFmpeg 等）已按需求移除；工具缺失信息在设置弹窗底部。
- `docs/BUILD_zh.md`、`docs/DESIGN_zh.md` 是重组前的历史文档，**目录结构和部分结论已过时**，以本指南为准。

---

## 9. 隐私清理变更记录（相对上游 videodl 的删减）

本项目在 fork 上游 `videodl` 引擎后，主动移除了以下存在隐私风险或已废弃的模块。**升级上游时不要重新引入**。

### 9.1 删除的解析器（39 个）

| 类型 | 数量 | 文件 | 原因 |
|------|------|------|------|
| 通用代解析器 | 36 | `modules/common/*.py`（anyfetcher/bugpk/gv/kedou/snapwc 等） | 这些解析器会把用户粘贴的视频 URL 转发给第三方服务器（如 anyfetcher.com、apicx.asia 等），收集用户浏览记录 |
| 平台 DRM 解析器 | 2 | `modules/sources/playerpl.py`、`modules/sources/wittytv.py` | 依赖 Widevine DRM 设备凭证（.wvd 文件），属于外部 DRM 体系 |
| APICX 解析器 | 1 | `modules/common/apicx.py` | 代解析服务，硬编码上游作者混淆前缀 `charlespikachu` |

### 9.2 删除的资源文件

| 文件/目录 | 说明 |
|-----------|------|
| `modules/cdm/*.wvd`（3 个） | Widevine DRM 设备凭证文件（charlespikachu_*.wvd） |
| `modules/utils/cdm.py` | DRM 解密工具（initcdm/closecdm/SearchPsshValueUtils），零调用方 |
| `modules/js/xmflv/` | xmflv 通用解析器的 JS/WASM 资源，解析器删除后变死资源 |

### 9.3 清理的代码字符串

| 字符串 | 原位置 | 处理 |
|--------|--------|------|
| `charlespikachu` | `base.py` decrypt_func 混淆前缀 | 删除整个 decrypt_func（零调用） |
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
