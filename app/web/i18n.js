/* ---------------- i18n (zh-CN / en-US) ----------------
   Design notes (ponytail):
   - One file, no dependencies, no build step. `applyI18n` rewrites the DOM via
     `data-i18n` / `data-i18n-title` / `data-i18n-ph` / `data-i18n-alt`.
   - `t()` falls back to the zh-CN string and then to the raw key, so any text
     not yet translated simply stays Chinese instead of rendering "undefined".
   - Language lives in `localStorage['vd_lang']` (same pattern as `vd_theme`)
     and is mirrored to the backend config (`language`); empty = never chosen,
     which is how the first-run language prompt is triggered.
-------------------------------------------------------- */
const I18N = (function () {
    const LANGS = {
        'zh-CN': {
            title: '全能下载器', brand_1: '全能', brand_2: '下载器',
            engine_loading: '引擎加载中', theme_toggle: '切换配色（深空 / 翡翠 / 落日）',
            login_title: '平台登录态（扫码/账号登录，自动保存 Cookie）', settings_title: '设置',
            feedback: '更新与反馈', lang_switch: '切换语言',
            url_ph: '粘贴视频链接，一行一个，可批量解析；例如 https://www.bilibili.com/video/BV...',
            parse: '解析',
            parse_hint: '输入链接后回车即可解析；支持 90+ 平台解析器与通用解析器兜底。',
            results: '解析结果', select_all: '全选', select_none: '取消',
            no_results: '还没有解析结果', save_to: '保存到：', download_selected: '下载选中',
            history: '历史下载', clear: '清空',
            no_history: '暂无历史记录，解析过的链接会自动保存到这里',
            jobs: '下载队列', clear_done: '清空已完成', open_dir: '打开目录',
            no_jobs: '暂无下载任务',
            logs: '运行日志', logs_fab: '日志', logs_concise: '精简', logs_all: '全部',
            logs_concise_tip: '过滤掉 info/debug，只看警告和错误',
            logs_collapse: '收起', logs_collapse_tip: '收起到右下角', clear_logs: '清空',
            settings: '设置', work_dir: '保存目录', browse: '浏览',
            threads: '单任务线程数', concurrent: '同时下载数',
            proxy: '代理（可选，例如 127.0.0.1:7890）', proxy_ph: '留空则不使用代理',
            quality: '默认画质（解析后默认选中的清晰度）', quality_best: '最高画质',
            quality_all: '全部画质', quality_auto: '由解析结果决定（全选）',
            subtitles: '同时下载字幕并合并到视频',
            subtitles_tip: '同时把字幕轨压制进视频文件；支持 HLS 自动提取字幕，个别平台需要对应解析器支持，不支持时自动跳过',
            whitelist: '平台白名单（勾选即启用）', engine_loading_hint: '引擎加载后可用',
            open_config_dir: '打开配置目录', cancel: '取消', save: '保存',
            login_modal: '平台登录态',
            login_hint: '点击「登录」后会弹出浏览器窗口，扫码/账号登录即可自动保存 Cookie；已登录的平台在解析/下载时会自动带上登录态。',
            platform_cookie: '平台 Cookie（不支持自动登录的平台可在此手动填写完整 Cookie 字符串）',
            cookie_ph: '该平台的完整 Cookie 字符串',
            save_cookie: '保存 Cookie',
            feedback_title: '更新与反馈', qr_alt: '公众号二维码',
            qr_tip: '请用微信扫描上方二维码关注公众号，获取最新软件与版本更新；如有使用建议或问题，欢迎在公众号留言反馈。',
            close: '关闭',
            status_queued: '排队中', status_downloading: '下载中', status_done: '已完成',
            status_error: '失败', status_cancelled: '已取消', status_cancelling: '取消中',
            status_paused: '已暂停', status_pausing: '暂停中', status_resuming: '恢复中',
            theme_space: '深空', theme_emerald: '翡翠', theme_sunset: '落日',
            group_video: '视频', group_audio: '音频', group_subtitle: '字幕', group_merge: '合并/封装',
            tag_has_audio: '含音频流', tag_no_valid: '无有效地址', generic: '通用',
            eta_soon: '即将完成', eta_sec: '剩余 {s} 秒', eta_min: '剩余 {m} 分 {s} 秒', eta_hour: '剩余 {h} 时 {m} 分',
            done_with_size: '✓ 完成 · {size}', processing: '处理中…',
            job_meta: '⏱ {time} · 📦 {done}/{total} 剩 {remaining}',
            tip_open_file_dir: '打开文件所在目录', tip_pause: '暂停', tip_resume: '开始/继续',
            tip_remove: '移除', tip_retry_audio: '补音频并重新合并',
            fill: '填入', tip_fill: '仅填入地址栏', tip_fill_parse: '填入并立即解析', tip_del_history: '从历史中删除',
            engine_failed: '引擎加载失败', engine_ready_loading_list: '引擎就绪 · 解析器列表加载中…',
            engine_ready_enabled: '引擎就绪 · 已启用 {enabled}/{total} 解析器',
            engine_loading_ellipsis: '引擎加载中…', engine_not_loaded: '引擎未加载（首次解析时自动加载）',
            all_done: '下载任务已完成', unknown_error: '未知错误',
            parse_failed: '解析失败：{err}',
            douyin_login_required: '解析失败：请先登录抖音（点顶栏「登录态」登录后重试）',
            login_required_generic: '解析失败：未能解析到有效链接，请先登录该平台（点顶栏「登录态」）后重试',
            login_retry_suffix: '请先登录该平台后重试。',
            no_downloadable: '未找到可下载的视频，可尝试点击顶栏「登录态」按钮登录该平台后重试',
            all_no_valid_url: '所有资源均无有效地址',
            parse_failed_youtube: '解析失败：YouTube 反爬拦截（IP 被标记）。',
            parse_failed_403_login: '解析失败：网站返回 412/403（反爬限制），所有资源均无有效地址。请点击顶栏「登录态」按钮登录该平台（抖音等）后重试。',
            parse_failed_403_relogin: '解析失败：网站返回 412/403（反爬），所有资源均无有效地址。当前登录态可能已失效，请点击顶栏「登录态」按钮重新登录后重试。',
            n_links_failed: '（{n} 个链接失败）',
            batch_parse_done: '批量解析完成：{ok}/{total} 个链接成功，共 {cnt} 个资源，已按「{q}」选中 {n} 项{extra}',
            parse_ok: '解析成功，共 {cnt} 个资源，已按「{q}」选中 {n} 项',
            enter_url: '请输入视频链接',
            engine_first_load: '首次使用需加载解析引擎，请稍候…（约 3~8 秒）',
            parsing: '正在解析，请稍候…', parsing_batch: '正在批量解析 {n} 个链接…',
            parse_request_failed: '解析请求失败：{err}',
            task_created: '已创建下载任务 #{id}', task_created_n: '已创建 {n} 个下载任务',
            task_failed: '创建任务失败：{err}', task_failed_plain: '创建任务失败',
            save_failed: '保存失败：{err}', settings_saved: '设置已保存',
            theme_switched: '配色已切换：{name}', cookie_saved: '平台 Cookie 已保存',
            auto_close_in: '{n} 秒后自动关闭登录窗口',
            open_dir_failed: '打开目录失败：{err}', open_config_dir_failed: '打开配置目录失败',
            retry_audio_failed: '补音频启动失败', already_has_audio: '该视频已包含音频，无需补录',
            login_finish_btn: '完成提取', login_opening: '打开中…', login_extracting: '提取中…',
            login_incomplete: '未完成', login_failed: '失败', retry: '重试',
            logout_btn: '退出', login_btn: '登录', fill_cookie: '填写 Cookie',
            logged_in: '已登录', please_login: '请登录', login_error: '登录异常',
            login_start_failed: '登录启动失败',
            login_in_browser: '请在弹出的浏览器窗口中登录，完成后点击「完成提取」',
            login_start_failed_with: '登录启动失败：{err}',
            operation_failed: '操作失败', operation_failed_with: '操作失败：{err}',
            login_saved: '已保存登录态', logged_out: '已退出登录',
            ready: 'pywebview 尚未就绪',
            lang_title: '选择语言', lang_desc: '请选择界面语言，之后可在顶栏随时切换。',
        },
        'en-US': {
            title: 'All-in-One Downloader', brand_1: 'All-in-One', brand_2: 'Downloader',
            engine_loading: 'Loading engine', theme_toggle: 'Switch theme (Space / Emerald / Sunset)',
            login_title: 'Platform login (QR / account; cookies saved automatically)', settings_title: 'Settings',
            feedback: 'Updates & Feedback', lang_switch: 'Switch language',
            url_ph: 'Paste video links, one per line (batch supported); e.g. https://www.bilibili.com/video/BV...',
            parse: 'Parse',
            parse_hint: 'Press Enter after pasting a link; 90+ platform parsers plus a generic fallback.',
            results: 'Results', select_all: 'Select all', select_none: 'Clear',
            no_results: 'No results yet', save_to: 'Save to: ', download_selected: 'Download selected',
            history: 'History', clear: 'Clear',
            no_history: 'No history yet — parsed links are saved here automatically',
            jobs: 'Download queue', clear_done: 'Clear finished', open_dir: 'Open folder',
            no_jobs: 'No download tasks',
            logs: 'Logs', logs_fab: 'Logs', logs_concise: 'Concise', logs_all: 'All',
            logs_concise_tip: 'Hide info/debug, show warnings and errors only',
            logs_collapse: 'Collapse', logs_collapse_tip: 'Collapse to the bottom-right corner',
            clear_logs: 'Clear',
            settings: 'Settings', work_dir: 'Save folder', browse: 'Browse',
            threads: 'Threads per task', concurrent: 'Concurrent downloads',
            proxy: 'Proxy (optional, e.g. 127.0.0.1:7890)', proxy_ph: 'Leave empty to connect directly',
            quality: 'Default quality (pre-selected after parsing)', quality_best: 'Best',
            quality_all: 'All qualities', quality_auto: 'Decided by parse result (select all)',
            subtitles: 'Download subtitles and mux into the video',
            subtitles_tip: 'Muxes subtitle tracks into the file; HLS subtitles are extracted automatically. Skipped when a platform does not support it.',
            whitelist: 'Platform whitelist (checked = enabled)', engine_loading_hint: 'Available once the engine is loaded',
            open_config_dir: 'Open config folder', cancel: 'Cancel', save: 'Save',
            login_modal: 'Platform login',
            login_hint: 'Clicking Login opens a browser window; sign in by QR code or account and the cookie is saved automatically. Logged-in platforms are used for parsing and downloading.',
            platform_cookie: 'Platform cookie (paste a full cookie string for platforms without auto-login)',
            cookie_ph: 'Full cookie string for this platform',
            save_cookie: 'Save cookie',
            feedback_title: 'Updates & Feedback', qr_alt: 'WeChat QR code',
            qr_tip: 'Scan the QR code above with WeChat to follow us for the latest releases; suggestions and bug reports are welcome.',
            close: 'Close',
            status_queued: 'Queued', status_downloading: 'Downloading', status_done: 'Done',
            status_error: 'Failed', status_cancelled: 'Cancelled', status_cancelling: 'Cancelling',
            status_paused: 'Paused', status_pausing: 'Pausing', status_resuming: 'Resuming',
            theme_space: 'Space', theme_emerald: 'Emerald', theme_sunset: 'Sunset',
            group_video: 'Video', group_audio: 'Audio', group_subtitle: 'Subtitle', group_merge: 'Muxing',
            tag_has_audio: 'with audio', tag_no_valid: 'no valid URL', generic: 'Generic',
            eta_soon: 'almost done', eta_sec: '{s}s left', eta_min: '{m}m {s}s left', eta_hour: '{h}h {m}m left',
            done_with_size: '✓ done · {size}', processing: 'processing…',
            job_meta: '⏱ {time} · 📦 {done}/{total}, {remaining} left',
            tip_open_file_dir: 'Open containing folder', tip_pause: 'Pause', tip_resume: 'Start / Resume',
            tip_remove: 'Remove', tip_retry_audio: 'Re-download audio and merge',
            fill: 'Fill', tip_fill: 'Fill the address box only', tip_fill_parse: 'Fill and parse now',
            tip_del_history: 'Remove from history',
            engine_failed: 'Engine failed to load', engine_ready_loading_list: 'Engine ready · loading parser list…',
            engine_ready_enabled: 'Engine ready · {enabled}/{total} parsers enabled',
            engine_loading_ellipsis: 'Loading engine…', engine_not_loaded: 'Engine not loaded (loaded on first parse)',
            all_done: 'All downloads finished', unknown_error: 'unknown error',
            parse_failed: 'Parse failed: {err}',
            douyin_login_required: 'Parse failed: please log in to Douyin first (login button in the top bar), then retry',
            login_required_generic: 'Parse failed: no valid link found — please log in to this platform (login button in the top bar) and retry',
            login_retry_suffix: 'Please log in to this platform and retry.',
            no_downloadable: 'No downloadable video found — try logging in from the top bar and parsing again',
            all_no_valid_url: 'No resource has a valid URL',
            parse_failed_youtube: 'Parse failed: YouTube anti-bot block (IP flagged). ',
            parse_failed_403_login: 'Parse failed: the site returned 412/403 (anti-bot) and no resource has a valid URL. Log in from the top bar (e.g. Douyin) and retry.',
            parse_failed_403_relogin: 'Parse failed: the site returned 412/403 (anti-bot) and no resource has a valid URL. The current login may have expired — log in again from the top bar.',
            n_links_failed: ' ({n} failed)',
            batch_parse_done: 'Batch parse done: {ok}/{total} links OK, {cnt} resources, {n} selected for "{q}"{extra}',
            parse_ok: 'Parsed: {cnt} resources, {n} selected for "{q}"',
            enter_url: 'Please enter a video link',
            engine_first_load: 'Loading the parsing engine on first use, please wait… (~3–8s)',
            parsing: 'Parsing, please wait…', parsing_batch: 'Parsing {n} links…',
            parse_request_failed: 'Parse request failed: {err}',
            task_created: 'Download task #{id} created', task_created_n: '{n} download tasks created',
            task_failed: 'Failed to create task: {err}', task_failed_plain: 'Failed to create task',
            save_failed: 'Save failed: {err}', settings_saved: 'Settings saved',
            theme_switched: 'Theme switched: {name}', cookie_saved: 'Platform cookie saved',
            auto_close_in: 'Closing login dialog in {n}s',
            open_dir_failed: 'Failed to open folder: {err}', open_config_dir_failed: 'Failed to open config folder',
            retry_audio_failed: 'Failed to start audio re-download', already_has_audio: 'This video already has audio',
            login_finish_btn: 'Finish', login_opening: 'Opening…', login_extracting: 'Extracting…',
            login_incomplete: 'Incomplete', login_failed: 'Failed', retry: 'Retry',
            logout_btn: 'Log out', login_btn: 'Log in', fill_cookie: 'Fill cookie',
            logged_in: 'Logged in', please_login: 'Please log in', login_error: 'Login error',
            login_start_failed: 'Failed to start login',
            login_in_browser: 'Log in in the browser window that opened, then click Finish',
            login_start_failed_with: 'Failed to start login: {err}',
            operation_failed: 'Operation failed', operation_failed_with: 'Operation failed: {err}',
            login_saved: 'Login saved', logged_out: 'Logged out',
            ready: 'pywebview is not ready yet',
            lang_title: 'Choose language', lang_desc: 'Pick your interface language; you can switch anytime from the top bar.',
        },
    };
    const DEFAULT_LANG = 'zh-CN';
    let current = DEFAULT_LANG;

    function t(key, vars) {
        let s = (LANGS[current] && LANGS[current][key]) || (LANGS[DEFAULT_LANG] && LANGS[DEFAULT_LANG][key]) || key;
        if (vars) Object.keys(vars).forEach((k) => { s = s.split('{' + k + '}').join(vars[k]); });
        return s;
    }
    function set(lang) {
        if (!LANGS[lang]) lang = DEFAULT_LANG;
        current = lang;
        try { localStorage.setItem('vd_lang', lang); } catch (e) { /* private mode */ }
        document.documentElement.setAttribute('lang', lang === 'en-US' ? 'en' : 'zh-CN');
    }
    function detect() {
        try {
            const saved = localStorage.getItem('vd_lang');
            if (saved && LANGS[saved]) return saved;
        } catch (e) { /* ignore */ }
        // backend config wins for a fresh install (empty = user has not chosen)
        const cfgLang = (window.__vdConfig && window.__vdConfig.language) || '';
        return LANGS[cfgLang] ? cfgLang : DEFAULT_LANG;
    }
    function apply() {
        document.querySelectorAll('[data-i18n]').forEach((el) => { el.textContent = t(el.getAttribute('data-i18n')); });
        document.querySelectorAll('[data-i18n-title]').forEach((el) => { el.setAttribute('title', t(el.getAttribute('data-i18n-title'))); });
        document.querySelectorAll('[data-i18n-ph]').forEach((el) => { el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph'))); });
        document.querySelectorAll('[data-i18n-alt]').forEach((el) => { el.setAttribute('alt', t(el.getAttribute('data-i18n-alt'))); });
    }

    set(detect());
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply);
    else apply();

    return { t: t, apply: apply, set: set, langs: Object.keys(LANGS), DEFAULT_LANG: DEFAULT_LANG, get current() { return current; } };
})();
