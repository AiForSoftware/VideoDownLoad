'use strict';

/* ---------------- dom helpers ---------------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const state = {
    items: [],
    selected: new Set(),
    logSeq: 0,
    config: null,
    jobs: [],
    progress: [],
    engineReady: false,
    engineError: '',
    platforms: [],
    generic: [],
    checkedSources: new Set(),
    defaultQuality: 'best',
    logins: {},
    logsCollapsed: false,
    logFilter: 'all',
    parsing: false,
    tools: {},
    history: [],
};

const STATUS_TEXT = {
    queued: '排队中', downloading: '下载中', done: '已完成',
    error: '失败', cancelled: '已取消', cancelling: '取消中',
};

/* ---------------- pywebview bridge ---------------- */
function api(name) {
    const args = Array.prototype.slice.call(arguments, 1);
    function _call() {
        if (!window.pywebview || !window.pywebview.api) return null;
        const fn = window.pywebview.api[name];
        return (typeof fn === 'function') ? fn.apply(null, args) : null;
    }
    const first = _call();
    if (first) return first;
    // pywebview on Windows WebView2 registers the per-method bindings on
    // `window.pywebview.api` asynchronously after the page loads. A click
    // that lands during that window used to surface as "pywebview 尚未就绪"
    // and stick forever. Retry briefly so the call goes through as soon as
    // the bridge finishes exposing the requested method.
    return new Promise(function (resolve, reject) {
        let n = 0;
        const t = setInterval(function () {
            n += 1;
            const r = _call();
            if (r) { clearInterval(t); resolve(r); return; }
            if (n > 40) { clearInterval(t); reject(new Error('pywebview 尚未就绪')); }
        }, 75);
    });
}

function toast(message, kind) {
    const el = $('toast');
    el.textContent = message;
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.hidden = false;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.hidden = true; }, 2600);
}

/* frontend diagnostics: forward anchors into the python-side startup.log */
let uiBootMark = Date.now();
function felog(message, level, scope) {
    try {
        const t = ((Date.now() - uiBootMark) / 1000).toFixed(1);
        const p = api('felog', scope || 'ui', `[ui +${t}s] ${message}`, level || 'info');
        if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch (e) { /* never let diagnostics break the app */ }
}

window.addEventListener('error', (e) => {
    felog(`js error: ${e.message} @ ${e.filename}:${e.lineno}`, 'error', 'ui-crash');
});
window.addEventListener('unhandledrejection', (e) => {
    const reason = (e && e.reason && (e.reason.message || e.reason)) || 'unknown';
    felog(`unhandled promise rejection: ${reason}`, 'error', 'ui-crash');
});

/* ---------------- theme (3 palettes, persisted) ---------------- */
const THEMES = [
    { id: 'space', label: '深空' },
    { id: 'emerald', label: '翡翠' },
    { id: 'sunset', label: '落日' },
];
function currentTheme() {
    const saved = localStorage.getItem('vd_theme');
    return THEMES.some((t) => t.id === saved) ? saved : 'space';
}
function applyTheme(id) {
    if (!THEMES.some((t) => t.id === id)) id = 'space';
    document.documentElement.setAttribute('data-theme', id);
    localStorage.setItem('vd_theme', id);
}
applyTheme(currentTheme());
function cycleTheme() {
    const next = THEMES[(THEMES.findIndex((t) => t.id === currentTheme()) + 1) % THEMES.length];
    applyTheme(next.id);
    toast(`配色已切换：${next.label}`, 'ok');
}

/* ---------------- formatting ---------------- */
function fmtbytes(n) {
    if (n == null || isNaN(n)) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = Number(n), i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return value.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

function fmtspeed(n) {
    if (!n) return '';
    return fmtbytes(n) + '/s';
}

function shortname(source) {
    return String(source || '').replace(/VideoClient$/, '') || 'unknown';
}

/* ---------------- rendering ---------------- */
function renderResults() {
    const box = $('results');
    $('resultCount').textContent = String(state.items.length);
    if (!state.items.length) {
        box.innerHTML = '<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="m9 9 6 4-6 4z"/></svg><p>还没有解析结果</p></div>';
        return;
    }
    box.innerHTML = state.items.map((item) => {
        const on = state.selected.has(item.key) ? ' selected' : '';
        const thumb = item.cover_url
            ? `<div class="result-thumb" style="background-image:url('${esc(item.cover_url)}')"></div>`
            : `<div class="result-thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="m9 9 6 4-6 4z"/></svg></div>`;
        const tags = [
            `<span class="tag source">${esc(shortname(item.source))}</span>`,
            item.quality ? `<span class="tag q">${esc(item.quality)}</span>` : '',
            item.ext && item.valid ? `<span class="tag ext">${esc(item.ext)}</span>` : '',
            item.has_audio ? '<span class="tag good">含音频流</span>' : '',
            item.valid ? '' : '<span class="tag bad">无有效地址</span>',
        ].join('');
        const tooltip = [item.err_msg, item.save_path || item.download_url].filter(Boolean).join('\n');
        const errLine = item.err_msg ? `<div class="result-err" style="margin-top:4px;font-size:11px;color:#f87171;line-height:1.4;word-break:break-all" title="${esc(item.err_msg)}">${esc(item.err_msg.length > 140 ? item.err_msg.slice(0,140) + '\u2026' : item.err_msg)}</div>` : '';
        return `<div class="result-item${on}" data-key="${esc(item.key)}" title="${esc(tooltip)}">
            ${thumb}
            <div class="result-main">
                <div class="result-title">${esc(item.title)}</div>
                <div class="result-meta">${tags}</div>
                ${errLine}
            </div>
            <div class="result-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4 4L19 7"/></svg></div>
        </div>`;
    }).join('');
    box.querySelectorAll('.result-item').forEach((el) => {
        el.addEventListener('click', () => {
            const key = el.getAttribute('data-key');
            if (state.selected.has(key)) {
                state.selected.delete(key);
                el.classList.remove('selected');
            } else {
                state.selected.add(key);
                el.classList.add('selected');
            }
            updateDownloadBtn();
        });
    });
    updateDownloadBtn();
}

function updateDownloadBtn() {
    $('selectedCount').textContent = String(state.selected.size);
    $('downloadBtn').disabled = state.selected.size === 0 || state.parsing;
}

function renderProgress() {
    const box = $('progressList');
    // Unify every download-kind progress entry into ONE real-time bar.
    // The original 3 segments (overall videos count + per-video download +
    // per-ts m3u8 chunks) are folded into a single bar that shows live
    // bytes / percent / speed / ETA while downloads are running. When no
    // download is active (queued / completed), the overall videos-count bar
    // takes over so the area never goes blank — the familiar post-completion
    // "1 / 1 · 100.0%" state is still visible right after the download
    // finishes.
    const downloadTasks = state.progress.filter((p) => (p.kind === 'download' || p.kind === 'm3u8download') && !p.finished);
    const overallTasks = state.progress.filter((p) => p.kind === 'overall');

    let entry = null;
    if (downloadTasks.length) {
        let totalBytes = 0, doneBytes = 0, speedSum = 0;
        for (const p of downloadTasks) {
            const t = Number(p.total) || 0;
            const c = Number(p.completed) || 0;
            if (t > 0) totalBytes += t;
            doneBytes += c;
            speedSum += Number(p.speed) || 0;
        }
        const percent = totalBytes > 0 ? Math.min(100, doneBytes / totalBytes * 100) : null;
        const eta = (speedSum > 0 && totalBytes > doneBytes) ? (totalBytes - doneBytes) / speedSum : null;
        const desc = downloadTasks.length === 1
            ? (downloadTasks[0].description || '下载进度')
            : `下载进度 (${downloadTasks.length} 个并行)`;
        entry = {
            kind: 'download',
            description: desc,
            completed: doneBytes,
            total: totalBytes || null,
            percent: percent,
            speed: speedSum || null,
            eta: eta,
        };
    } else if (overallTasks.length) {
        const p = overallTasks[0];
        entry = {
            kind: 'overall',
            description: p.description || '整体进度',
            completed: p.completed,
            total: p.total,
            percent: p.percent,
            speed: null,
            eta: null,
        };
    }

    if (!entry) { box.innerHTML = ''; return; }

    const fmtsource = (d) => {
        const s = String(d || '').trim();
        if (!s) return '下载进度';
        if (/^overall(\s+videos)?$/i.test(s)) return '整体进度';
        if (/^\[bold cyan\]overall videos$/i.test(s)) return '整体进度';
        return s;
    };
    const fmteta = (s) => {
        if (s == null) return '';
        if (s < 1) return '即将完成';
        if (s < 60) return `剩余 ${s.toFixed(0)} 秒`;
        if (s < 3600) return `剩余 ${Math.floor(s / 60)} 分 ${Math.floor(s % 60)} 秒`;
        return `剩余 ${Math.floor(s / 3600)} 时 ${Math.floor((s % 3600) / 60)} 分`;
    };
    const percent = (entry.percent == null) ? null : entry.percent;
    const total = entry.kind === 'download' ? (entry.total ? fmtbytes(entry.total) : null) : entry.total;
    const done = entry.kind === 'download' ? fmtbytes(entry.completed) : entry.completed;
    const eta = (percent != null && percent < 100 && entry.eta) ? fmteta(entry.eta) : '';
    const speedTxt = entry.speed ? fmtspeed(entry.speed) : '';
    const right = percent == null
        ? (entry.kind === 'download' ? done : `${entry.completed || 0}`)
        : `${done} / ${total} · ${percent.toFixed(1)}%${speedTxt ? ' · ' + speedTxt : ''}${eta ? ' · ' + eta : ''}`;
    const fill = percent == null
        ? '<div class="progress-fill unknown"></div>'
        : `<div class="progress-fill" style="width:${percent.toFixed(2)}%"></div>`;
    const desc = esc(fmtsource(entry.description));
    const kindTag = entry.kind && entry.kind !== 'overall'
        ? `<span class="progress-kind">${esc(entry.kind)}</span>` : '';
    box.innerHTML = `<div class="progress-item" title="${desc}">
        <div class="progress-top"><b>${desc}</b>${kindTag}<span>${esc(right)}</span></div>
        <div class="progress-track">${fill}</div>
    </div>`;
}

function renderJobs() {
    const box = $('jobs');
    if (!state.jobs.length) {
        box.innerHTML = '<div class="empty sm"><p>暂无下载任务</p></div>';
        return;
    }
    box.innerHTML = state.jobs.map((job) => {
        const wholeDone = job.status === 'done';
        // only show per-item status when it differs from the job-wide status
        // or when an item errored (then the message carries useful detail).
        // this prevents "下载中" / "已完成" from appearing twice — once as the
        // job-level badge in the header and once on every item row.
        const itemStatuses = job.items.map((it) => it.status);
        const unique = new Set(itemStatuses);
        const allSameAsJob = unique.size <= 1 && itemStatuses.every((s) => s === job.status);
        const hasItemError = itemStatuses.includes('error') || !!job.error;
        const showItemStatus = !allSameAsJob || hasItemError;

        const items = job.items.map((it) => {
            let statusHtml = '';
            if (showItemStatus) {
                const st = STATUS_TEXT[it.status] || it.status;
                const errSuffix = it.error ? ' · ' + esc(it.error) : '';
                statusHtml = `<span class="st ${esc(it.status)}">${esc(st)}${errSuffix}</span>`;
            }
            return `<div class="job-item">
                <span class="name" title="${esc(it.save_path || '')}">${esc(it.title)}</span>
                ${statusHtml}
            </div>`;
        }).join('');
        const running = job.status === 'downloading' || job.status === 'queued' || job.status === 'cancelling';
        const action = running
            ? `<button class="btn ghost sm" data-cancel="${esc(job.id)}">取消</button>`
            : (job.done_count ? `<button class="btn ghost sm" data-open="${esc(job.id)}">打开目录</button>` : '');
        // friendly header: time + progress; status badge is hidden when the whole
        // job is done (dup of the per-item tags) or downloading (the progress bar
        // already conveys it, and the pill duplicated the per-item "下载中" below)
        const time = esc(job.finished_at || job.started_at || job.created_at);
        const headerMeta = `⏱ ${time} · 📦 ${job.done_count}/${job.total_count}`;
        const badge = (job.status === 'done' || job.status === 'downloading')
            ? ''
            : `<span class="job-status ${esc(job.status)}">${esc(STATUS_TEXT[job.status] || job.status)}</span>`;
        const idTip = `<span class="job-id-tip" title="任务编号 #${esc(job.id)}">#${esc(job.id)}</span>`;
        return `<div class="job">
            <div class="job-head">
                <span class="job-meta">${headerMeta} · ${idTip}</span>
                ${badge}
            </div>
            <div class="job-items">${items}</div>
            ${job.error ? `<div class="job-item"><span class="st error">${esc(job.error)}</span></div>` : ''}
            <div class="job-actions">${action}</div>
        </div>`;
    }).join('');
    box.querySelectorAll('[data-cancel]').forEach((el) => {
        el.addEventListener('click', () => { api('cancel', el.getAttribute('data-cancel')); });
    });
    box.querySelectorAll('[data-open]').forEach((el) => {
        el.addEventListener('click', () => {
            const job = state.jobs.find((j) => j.id === el.getAttribute('data-open'));
            if (job) api('openpath', job.work_dir);
        });
    });
}

function renderLogs(newLogs) {
    const box = $('logs');
    if (!newLogs || !newLogs.length) return;
    const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    // consecutive identical entries are collapsed into one line with a ×N badge
    for (const l of newLogs) {
        const sig = l.level + '|' + l.message;
        const last = box.lastElementChild;
        if (last && last.getAttribute('data-sig') === sig) {
            const count = (parseInt(last.getAttribute('data-count'), 10) || 1) + 1;
            last.setAttribute('data-count', String(count));
            let badge = last.querySelector('.log-count');
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'log-count';
                last.appendChild(badge);
            }
            badge.textContent = '×' + count;
        } else {
            box.insertAdjacentHTML('beforeend',
                `<div class="log-line" data-lv="${esc(l.level)}" data-sig="${esc(sig)}" data-count="1">` +
                `<span class="log-time">${esc(l.time)}</span>` +
                `<span class="log-level ${esc(l.level)}">${esc(l.level)}</span>` +
                `<span class="log-msg">${esc(l.message)}</span></div>`);
        }
    }
    while (box.childElementCount > 800) box.removeChild(box.firstChild);
    if (nearBottom) box.scrollTop = box.scrollHeight;
}

function renderToolChips() {
    const box = $('toolChips');
    if (!box) return; // tool chips were removed from the top bar
    const map = [['ffmpeg', 'FFmpeg'], ['ffprobe', 'FFprobe'], ['node', 'Node'], ['nm3u8dlre', 'N_m3u8DL-RE'], ['aria2c', 'Aria2']];
    box.innerHTML = map.map(([k, label]) => {
        const on = !!state.tools[k];
        return `<span class="tool-chip ${on ? 'on' : 'off'}">${on ? label : label + ' 缺失'}</span>`;
    }).join('');
}

function renderHistory() {
    const box = $('historyList');
    if (!state.history.length) {
        box.innerHTML = '<div class="empty sm"><p>暂无历史记录，解析过的链接会自动保存到这里</p></div>';
        $('historyCount').textContent = '0';
        return;
    }
    $('historyCount').textContent = String(state.history.length);
    box.innerHTML = state.history.map((h) => {
        let host = '';
        try { host = new URL(h.url).hostname.replace(/^www\./, ''); } catch (e) {}
        const src = shortname(h.source || '');
        const when = (h.last_used_at || h.parsed_at || '').slice(11, 19) || '-';
        const tag = h.source ? `<span class="tag source">${esc(src)}</span>` : '<span class="tag">通用</span>';
        return `<div class="history-item" data-url="${esc(h.url)}">
            <div class="history-main">
                <div class="history-url" title="${esc(h.url)}">${esc(h.url)}</div>
                <div class="history-meta">${tag} <span class="host">${esc(host)}</span> · <span class="when">${esc(when)}</span></div>
            </div>
            <div class="history-actions">
                <button class="btn ghost sm" data-fill="${esc(h.url)}" title="仅填入地址栏">填入</button>
                <button class="btn ghost sm primary-mini" data-use="${esc(h.url)}" title="填入并立即解析">解析</button>
                <button class="btn ghost sm danger" data-del="${esc(h.url)}" title="从历史中删除">×</button>
            </div>
        </div>`;
    }).join('');
}

function renderEngineChip() {
    const chip = $('engineChip');
    const st = state.engineState || 'unloaded';
    if (st === 'error' || state.engineError) {
        chip.className = 'chip err';
        chip.innerHTML = '<span class="dot"></span><span>引擎加载失败</span>';
        return;
    }
    if (st === 'ready' && state.engineReady) {
        const total = state.platforms.length + state.generic.length;
        if (!total) {
            // 解析器列表还在异步加载中，sources API 几毫秒内就会回来；
            // 这里显示 "加载中" 避免出现误导性的 "0 解析器"
            chip.className = 'chip warn';
            chip.innerHTML = '<span class="dot"></span><span>引擎就绪 · 解析器列表加载中…</span>';
            return;
        }
        // show the ENABLED (whitelisted) count first — the user opted into 2
        // platforms, so "107 解析器" was misleading; keep the total as context
        const enabled = state.checkedSources.size || 0;
        chip.className = 'chip ok';
        chip.innerHTML = `<span class="dot"></span><span>引擎就绪 · 已启用 ${enabled}/${total} 解析器</span>`;
        return;
    }
    if (st === 'loading') {
        chip.className = 'chip warn';
        chip.innerHTML = '<span class="dot"></span><span>引擎加载中…</span>';
        return;
    }
    chip.className = 'chip';
    chip.innerHTML = '<span class="dot"></span><span>引擎未加载（首次解析时自动加载）</span>';
}

/* ---------------- polling ---------------- */
function applyState(data) {
    const hadJobs = state.jobs.length;
    const prevBusy = state.jobs.some((j) => j.status === 'downloading' || j.status === 'queued');
    // Snapshot prevEngineReady BEFORE we overwrite state.engineReady so we can
    // detect the not-ready → ready transition. Without this, the very first
    // `loadSources()` call (made while the engine was still lazy-loading) would
    // populate `state.platforms` with the 1-2 parsers the user has already
    // imported, and the `!(platforms.length + generic.length)` guard would then
    // skip future reloads — leaving the settings UI stuck showing only those
    // 1-2 platforms and the user with no way to enable the other ~60.
    const prevEngineReady = !!state.engineReady;
    state.jobs = data.jobs || [];
    state.progress = data.progress || [];
    state.engineReady = data.engine_ready;
    state.engineState = data.engine_state || 'unloaded';
    state.engineError = data.engine_error || '';
    if (data.history) { state.history = data.history; renderHistory(); }
    if (data.logs && data.logs.length) {
        state.logSeq = data.log_seq;
        renderLogs(data.logs);
    }
    renderProgress();
    renderJobs();
    renderEngineChip();
    // Force a refresh of the parser list whenever the engine transitions to
    // ready, so the settings whitelist shows every available parser instead of
    // only the lazy-loaded subset. Also retry while the list is still empty
    // (first poll races the import).
    if (!prevEngineReady && state.engineReady) loadSources();
    if (!(state.platforms.length + state.generic.length)) loadSources();
    const busy = state.jobs.some((j) => j.status === 'downloading' || j.status === 'queued');
    if (prevBusy && !busy && hadJobs) toast('下载任务已完成', 'ok');
}

let pollFailCount = 0;
let pollWarmup = 3; // the bridge may not be fully wired during the first moments; stay quiet
function poll() {
    api('state', state.logSeq).then((data) => {
        if (pollFailCount > 0) {
            felog(`state polling recovered after ${pollFailCount} failure(s)`, 'warning', 'ui-poll');
            pollFailCount = 0;
        }
        applyState(data);
    }).catch((err) => {
        if (pollWarmup > 0) { pollWarmup -= 1; return; }
        pollFailCount += 1;
        if (pollFailCount === 1 || pollFailCount % 10 === 0) {
            felog(`state polling failed ${pollFailCount} time(s): ${err}`, 'error', 'ui-poll');
        }
    });
}

function loadSources() {
    api('sources').then((res) => {
        state.platforms = res.platforms || [];
        state.generic = res.generic || [];
        state.engineReady = !!res.engine_ready;
        state.engineState = res.engine_state || 'unloaded';
        state.engineError = res.engine_error || '';
        renderEngineChip();
        renderSourceGrid();
        initCheckedSources();
    }).catch(() => {});
}

/* Initialize the whitelist checkboxes from the server-side `allowed_sources`
 * config. MUST run only when BOTH the source list and the config have arrived:
 * these load over two independent async bridge calls, and `bootstrap` (which
 * carries the config) does heavy engine/tool work and usually finishes AFTER
 * `sources`. The old code read `state.config` speculatively inside the sources
 * handler — if the config hadn't arrived yet it fell into the "empty = all"
 * branch and checked EVERY platform, which the user then persisted by saving.
 * The checked state is initialized exactly once; later source refreshes just
 * re-render the grid from the existing (possibly user-edited) selection. */
function initCheckedSources() {
    const all = state.platforms.concat(state.generic);
    if (!all.length) return;
    const cfgAllowed = (state.config || {}).allowed_sources;
    if (!Array.isArray(cfgAllowed)) { state.pendingCheckedInit = true; return; }
    if (state.checkedInitDone) return;
    state.checkedInitDone = true;
    state.pendingCheckedInit = false;
    // The saved whitelist is the literal set of enabled platforms. An empty
    // list normally never reaches the UI (the backend substitutes its 2-platform
    // default on load), but if it does, fall back to that same default instead
    // of "check everything".
    state.checkedSources = cfgAllowed.length
        ? new Set(cfgAllowed.filter((n) => all.includes(n)))
        : new Set(all.filter((n) => n === 'DouyinVideoClient' || n === 'BilibiliVideoClient'));
    renderSourceGrid();
    renderEngineChip();
}

function renderSourceGrid() {
    const grid = $('sourceGrid');
    const all = state.platforms.concat(state.generic);
    if (!all.length) { grid.innerHTML = '<div class="empty sm"><p>引擎加载后可用</p></div>'; return; }
    grid.innerHTML = all.map((name) => {
        const checked = state.checkedSources.has(name) ? ' checked' : '';
        return `<label class="source-item" title="${esc(name)}"><input type="checkbox" value="${esc(name)}"${checked} /><span>${esc(shortname(name))}</span></label>`;
    }).join('');
}

/* ---------------- actions ---------------- */
function applyParseResult(data) {
    const res = (data && data.result) ? data.result : data;
    window.__lastParse = res;  // diagnostic hook (read by gui_download_check.py)
    state.parsing = false;
    $('parseBtn').disabled = false;
    if (!res) return;
    state.items = res.items || [];
    let _sel = new Set(state.items.filter((i) => i.valid).map((i) => i.key));
    const _pref = (state.config && state.config.default_quality) || 'best';
    const _smart = pickDefaultQualitySelection(state.items, _pref);
    if (_smart) _sel = _smart;
    state.selected = _sel;
    if (res.history) state.history = res.history;
    renderResults();
    renderHistory();
    if (!res.ok) {
        $('parseHint').className = 'hint err';
        $('parseHint').textContent = '解析失败：' + (res.error || '未知错误');
        toast('解析失败：' + (res.error || '未知错误'), 'err');
    } else if (!state.items.length) {
        $('parseHint').className = 'hint err';
        $('parseHint').textContent = '未找到可下载的视频，可尝试在设置中填写 Cookie 后重试';
    } else if (state.items.every((i) => !i.valid)) {
        // Every parsed item has no real download URL (anti-bot / 412 / no cookie).
        // Surface the underlying reason and explicitly suggest a cookie so the
        // user knows what to do instead of staring at a cryptic tag.
        const firstErr = (state.items.find((i) => i.err_msg) || {}).err_msg || '所有资源均无有效地址';
        const isAntiBot = /412|403|Precondition|FORBIDDEN|access.denied|Forbidden/i.test(firstErr);
        const isYouTube = /YouTube|youtube/i.test(firstErr);
        const noCookie = !(state.config && state.config.cookies);
        $('parseHint').className = 'hint err';
        if (isYouTube) {
            $('parseHint').textContent = '解析失败：YouTube 反爬拦截（IP 被标记）。' + firstErr.slice(0, 200);
        } else if (isAntiBot && noCookie) {
            $('parseHint').textContent = '解析失败：网站返回 412/403（反爬限制），所有资源均无有效地址。请打开「设置」粘贴该网站（抖音等）的浏览器 Cookie 后重试。';
        } else if (isAntiBot) {
            $('parseHint').textContent = '解析失败：网站返回 412/403（反爬），所有资源均无有效地址。当前 Cookie 可能已失效，请更新后重试。';
        } else {
            $('parseHint').textContent = '解析失败：所有资源均无有效地址（' + firstErr.slice(0, 200) + '）';
        }
        $('parseHint').title = firstErr;
        toast('解析失败：所有资源均无有效地址', 'err');
    } else {
        const cnt = state.items.length;
        $('parseHint').className = 'hint ok';
        const _prefLabel = { best: '最高画质', auto: '全部画质' }[(state.config || {}).default_quality] || String((state.config || {}).default_quality || 'best').toUpperCase();
        if (res.batch) {
            const ok = (res.url_count || 1) - (res.errors ? res.errors.length : 0);
            const extra = (res.errors && res.errors.length) ? `（${res.errors.length} 个链接失败）` : '';
            $('parseHint').textContent = `批量解析完成：${ok}/${res.url_count} 个链接成功，共 ${cnt} 个资源，已按「${_prefLabel}」选中 ${state.selected.size} 项${extra}`;
        } else {
            $('parseHint').textContent = `解析成功，共 ${cnt} 个资源，已按「${_prefLabel}」选中 ${state.selected.size} 项`;
        }
        $('urlInput').value = '';
    }
}

function parseUrl() {
    const raw = $('urlInput').value.trim();
    if (!raw) { toast('请输入视频链接', 'err'); return; }
    // Split into one-or-many urls: one per line, or separated by spaces / commas
    // (full-width commas too). Enables batch parsing — a single url behaves
    // exactly as before.
    const urls = raw.split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { toast('请输入视频链接', 'err'); return; }
    if (state.parsing) return;
    state.parsing = true;
    $('parseBtn').disabled = true;
    $('parseHint').className = 'hint';
    $('parseHint').textContent = ((state.engineState || 'unloaded') !== 'ready')
        ? '首次使用需加载解析引擎，请稍候…（约 3~8 秒）'
        : (urls.length > 1 ? `正在批量解析 ${urls.length} 个链接…` : '正在解析，请稍候…');
    // Single url still goes through the per-url `parse` API; multiple urls use
    // `parsebatch`. Both push the merged result back via window.applyParseResult
    // (see JsApi.parse / parsebatch in api.py). The api() call only kicks off the
    // background job and resolves immediately, so the UI stays responsive.
    const call = urls.length === 1 ? api('parse', urls[0]) : api('parsebatch', urls);
    call.catch((err) => {
        state.parsing = false;
        $('parseBtn').disabled = false;
        $('parseHint').className = 'hint err';
        $('parseHint').textContent = '解析请求失败：' + err;
    });
}

function downloadSelected() {
    const keys = Array.from(state.selected);
    if (!keys.length) return;
    api('download', keys, state.config ? state.config.work_dir : null).then((res) => {
        if (!res.ok) toast(res.error || '创建任务失败', 'err');
        else toast(`已创建下载任务 #${res.job_id}`);
    }).catch((err) => toast('创建任务失败：' + err, 'err'));
}

/* ---------------- settings ---------------- */
function openSettings() {
    const cfg = state.config || {};
    $('cfgWorkDir').value = cfg.work_dir || '';
    $('cfgThreads').value = cfg.num_threadings || 5;
    $('cfgProxy').value = cfg.proxy || '';
    $('cfgCookies').value = cfg.cookies || '';
    $('cfgQuality').value = (cfg.default_quality || 'best');
    $('cfgCommonOnly').checked = !!cfg.apply_common_clients_only;
    loadSources();
    $('settingsModal').hidden = false;
}

function closeSettings() { $('settingsModal').hidden = true; }

/* Login lives in its own top-bar dialog now (moved out of the settings modal);
 * opening it closes the settings popup so the two never stack. */
function openLoginModal() {
    closeSettings();
    $('loginModal').hidden = false;
    loadLogins();
}

function closeLoginModal() { $('loginModal').hidden = true; }

function saveSettings() {
    const checked = Array.from(document.querySelectorAll('#sourceGrid input:checked')).map((el) => el.value);
    const payload = {
        work_dir: $('cfgWorkDir').value.trim(),
        num_threadings: parseInt($('cfgThreads').value, 10) || 5,
        proxy: $('cfgProxy').value.trim(),
        cookies: $('cfgCookies').value.trim(),
        default_quality: $('cfgQuality').value,
        apply_common_clients_only: $('cfgCommonOnly').checked,
        // persist exactly what the user checked. The old "(all checked) -> []"
        // convention silently reset a full whitelist to the 2-platform default
        // on the next launch, and is the reason the whitelist kept "forgetting".
        allowed_sources: checked,
    };
    api('setconfig', payload).then((res) => {
        if (res.ok) {
            state.config = res.config;
            $('workDirLabel').textContent = res.config.work_dir;
            toast('设置已保存', 'ok');
            closeSettings();
        } else {
            toast('保存失败：' + (res.error || ''), 'err');
        }
    }).catch((err) => toast('保存失败：' + err, 'err'));
}

/* ---------------- bootstrap ---------------- */
function bindEvents() {
    $('parseBtn').addEventListener('click', parseUrl);
    $('urlInput').addEventListener('keydown', (e) => {
        // Enter submits; Shift+Enter inserts a newline (so multi-url paste stays on separate lines).
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); parseUrl(); }
    });
    $('selectAllBtn').addEventListener('click', () => {
        // update in place to avoid resetting the whole list (which used to drop the
        // click handler momentarily and made a single click feel unresponsive)
        state.selected = new Set(state.items.map((i) => i.key));
        $('results').querySelectorAll('.result-item').forEach((el) => el.classList.add('selected'));
        updateDownloadBtn();
    });
    $('selectNoneBtn').addEventListener('click', () => {
        state.selected = new Set();
        $('results').querySelectorAll('.result-item').forEach((el) => el.classList.remove('selected'));
        updateDownloadBtn();
    });
    $('downloadBtn').addEventListener('click', downloadSelected);
    $('clearJobsBtn').addEventListener('click', () => api('clearjobs').then(poll));
    $('openDirBtn').addEventListener('click', () => api('openpath', (state.config || {}).work_dir || ''));
    $('themeBtn').addEventListener('click', cycleTheme);
    $('settingsBtn').addEventListener('click', openSettings);
    $('loginBtn').addEventListener('click', openLoginModal);
    $('closeLoginBtn').addEventListener('click', closeLoginModal);
    $('closeSettingsBtn').addEventListener('click', closeSettings);
    $('cancelSettingsBtn').addEventListener('click', closeSettings);
    $('saveSettingsBtn').addEventListener('click', saveSettings);
    $('pickDirBtn').addEventListener('click', () => {
        api('pickfolder').then((res) => { if (res.ok && res.path) $('cfgWorkDir').value = res.path; });
    });
    $('openConfigDirBtn').addEventListener('click', () => api('openconfigdir').then((r) => { if (!r.ok) toast(r.error || '打开配置目录失败', 'err'); }));
    $('toggleLogsBtn').addEventListener('click', () => {
        state.logsCollapsed = !state.logsCollapsed;
        $('logs').classList.toggle('collapsed', state.logsCollapsed);
        $('toggleLogsBtn').textContent = state.logsCollapsed ? '展开' : '折叠';
    });
    $('clearLogsBtn').addEventListener('click', () => { $('logs').innerHTML = ''; });
    $('logFilterBtn').addEventListener('click', () => {
        state.logFilter = state.logFilter === 'all' ? 'warn' : 'all';
        $('logs').classList.toggle('hide-info', state.logFilter === 'warn');
        $('logFilterBtn').textContent = state.logFilter === 'all' ? '精简' : '全部';
        felog(`log filter switched to ${state.logFilter}`, 'info', 'ui');
    });
    $('sourceGrid').addEventListener('change', (e) => {
        const el = e.target;
        if (el && el.tagName === 'INPUT') {
            if (el.checked) state.checkedSources.add(el.value); else state.checkedSources.delete(el.value);
        }
    });
    $('clearHistoryBtn').addEventListener('click', () => {
        api('clearhistory').then((r) => { state.history = (r && r.history) || []; renderHistory(); });
    });
    $('loginGrid').addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-login], button[data-finish], button[data-logout]');
        if (!btn) return;
        if (btn.hasAttribute('data-login')) startLogin(btn.getAttribute('data-login'));
        else if (btn.hasAttribute('data-finish')) finishLogin(btn.getAttribute('data-finish'));
        else if (btn.hasAttribute('data-logout')) logoutSource(btn.getAttribute('data-logout'));
    });
    $('historyList').addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-del], button[data-use], button[data-fill]');
        if (!btn) return;
        const url = btn.getAttribute('data-del') || btn.getAttribute('data-use') || btn.getAttribute('data-fill');
        if (btn.hasAttribute('data-del')) {
            api('removehistory', url).then((r) => { state.history = (r && r.history) || []; renderHistory(); });
            return;
        }
        $('urlInput').value = url;
        if (btn.hasAttribute('data-use')) parseUrl();
    });
}

function init() {
    felog('ui init started (pywebview bridge ready)', 'info', 'ui-boot');
    bindEvents();
    felog('event handlers bound', 'info', 'ui-boot');
    api('bootstrap').then((res) => {
        state.config = res.config;
        // if the source grid rendered before the config arrived, the checkbox
        // init was deferred — run it now that the whitelist is known
        if (state.pendingCheckedInit) initCheckedSources();
        $('versionLabel').textContent = `All-in-One Downloader v${res.version}`;
        $('workDirLabel').textContent = res.config.work_dir;
        // NOTE: we deliberately do NOT restore the previous link into the input —
        // the most-recent url already lives in the History panel below.
        felog(`bootstrap ok: v${res.version}, work_dir=${res.config.work_dir}`, 'info', 'ui-boot');
        return api('history');
    }).then((h) => {
        state.history = (h && h.history) || [];
        renderHistory();
        return api('checktools');
    }).then((tools) => {
        state.tools = tools || {};
        renderToolChips();
        felog(`tools detected: ${Object.entries(state.tools).map(([k, v]) => `${k}=${v ? 'y' : 'n'}`).join(' ')}`, 'info', 'ui-boot');
    }).catch((err) => felog(`bootstrap/checktools failed: ${err}`, 'error', 'ui-boot'));
    const hash = decodeURIComponent((location.hash || '').replace(/^#/, ''));
    if (hash) $('urlInput').value = hash;
    poll();
    setInterval(poll, 700);
    setTimeout(loadSources, 800);
    felog('polling started (700ms interval)', 'info', 'ui-boot');
}

window.__prefillUrl = function (url) { $('urlInput').value = url; };

/* ---------------- default quality selection ---------------- */
function qualityRank(label) {
    const map = { '8k': 5, '4k': 4, '1080p+': 3.5, '1080p60': 3.2, '1080p': 3, '720p60': 2.2, '720p': 2, '480p': 1, '360p': 0 };
    return map[String(label || '').toLowerCase()];
}
function pickDefaultQualitySelection(items, pref) {
    // pref: 'best' | 'auto' | '4k' | '1080p' | '720p' | '480p' | '360p'
    // Returns a Set of keys. For a concrete quality (and for 'best' = highest
    // available) exactly ONE item per media group is selected — never the whole
    // list. If the preferred quality is absent the next LOWER one wins (向下兼容);
    // only when nothing lower exists does a higher stream get picked.
    // 'auto' keeps the legacy "select everything" behaviour.
    if (!pref || pref === 'auto') return null;
    const want = qualityRank(pref); // undefined for 'best'
    const groups = {};
    for (const it of items) {
        if (!it.valid) continue;
        const base = (it.title || '').replace(/[_-]?(4K|1080P\+?|1080P60|1080P|720P60|720P|480P|360P|8K)\s*$/i, '').trim() || it.title;
        (groups[base] = groups[base] || []).push(it);
    }
    const selected = new Set();
    for (const base in groups) {
        const g = groups[base];
        if (g.length === 1) { selected.add(g[0].key); continue; }
        const ranked = g.map((it) => ({ it, r: qualityRank(it.quality) })).filter((x) => x.r !== undefined);
        if (!ranked.length) { selected.add(g[0].key); continue; }
        let pick;
        if (want === undefined) {
            // 'best': the highest quality in the group
            ranked.sort((a, b) => b.r - a.r);
            pick = ranked[0];
        } else {
            // 向下兼容: the highest stream at or below the wanted quality;
            // fall back to the lowest available if everything is higher
            const atOrBelow = ranked.filter((x) => x.r <= want);
            if (atOrBelow.length) {
                atOrBelow.sort((a, b) => b.r - a.r);
                pick = atOrBelow[0];
            } else {
                ranked.sort((a, b) => a.r - b.r);
                pick = ranked[0];
            }
        }
        if (pick) selected.add(pick.it.key);
    }
    return selected;
}

/* ---------------- login (in-app browser) ---------------- */
let loginPollTimer = null;
function renderLoginGrid() {
    const grid = $('loginGrid');
    if (!grid) return;
    const all = state.platforms.concat(state.generic);
    if (!all.length) { grid.innerHTML = '<div class="empty sm"><p>引擎加载后可用</p></div>'; return; }
    grid.innerHTML = all.map((name) => {
        const st = (state.logins && state.logins[name]) || 'absent';
        const logged = !!((state.config && state.config.per_source_cookies && state.config.per_source_cookies[name]));
        const err = (state.loginErrors && state.loginErrors[name]) || '';
        let action = '';
        if (st === 'waiting') action = `<button class="btn ghost sm primary-mini" data-finish="${esc(name)}">完成提取</button>`;
        else if (st === 'opening') action = '<span class="tag warn">打开中…</span>';
        else if (st === 'extracting') action = '<span class="tag warn">提取中…</span>';
        else if (st === 'incomplete') action = `<span class="tag bad">未完成</span><button class="btn ghost sm" data-login="${esc(name)}">重试</button>`;
        else if (st === 'error') action = `<span class="tag bad">失败</span><button class="btn ghost sm" data-login="${esc(name)}">重试</button>`;
        else if (logged) action = `<button class="btn ghost sm danger" data-logout="${esc(name)}">退出</button>`;
        else action = `<button class="btn ghost sm" data-login="${esc(name)}">登录</button>`;
        const badge = logged ? '<span class="tag good">已登录</span>' : (st === 'waiting' ? '<span class="tag warn">请登录</span>' : (st === 'incomplete' ? '<span class="tag bad">登录异常</span>' : ''));
        const hint = (st === 'incomplete' || st === 'error') && err ? `<div class="login-hint" style="color:#d9534f;font-size:12px;margin-top:4px;white-space:normal;line-height:1.4;">${esc(err)}</div>` : '';
        return `<div class="login-item" data-source="${esc(name)}">
            <span class="login-name" title="${esc(name)}">${esc(shortname(name))}</span>
            ${badge}
            <span class="login-action">${action}</span>
            ${hint}
        </div>`;
    }).join('');
}
function loadLogins() {
    api('login_status').then((r) => {
        const logins = {};
        const errors = {};
        for (const l of (r && r.logins) || []) { logins[l.source] = l.state; if (l.error) errors[l.source] = l.error; }
        state.logins = logins;
        state.loginErrors = errors;
        if (r && r.per_source_cookies) {
            const cfg = state.config || (state.config = {});
            cfg.per_source_cookies = r.per_source_cookies;
        }
        renderLoginGrid();
        const active = (r && r.logins || []).some((l) => ['opening', 'waiting', 'extracting'].includes(l.state));
        if (active) scheduleLoginPoll();
    }).catch(() => {});
}
function scheduleLoginPoll() {
    if (loginPollTimer) return;
    loginPollTimer = setInterval(() => {
        api('login_status').then((r) => {
            const logins = {};
            const errors = {};
            for (const l of (r && r.logins) || []) { logins[l.source] = l.state; if (l.error) errors[l.source] = l.error; }
            state.logins = logins;
            state.loginErrors = errors;
            if (r && r.per_source_cookies) state.config.per_source_cookies = r.per_source_cookies;
            renderLoginGrid();
            const active = (r && r.logins || []).some((l) => ['opening', 'waiting', 'extracting'].includes(l.state));
            if (!active) { clearInterval(loginPollTimer); loginPollTimer = null; }
        }).catch(() => {});
    }, 1500);
}
function startLogin(source) {
    api('login', source).then((res) => {
        if (!res || !res.ok) { toast((res && res.error) || '登录启动失败', 'err'); return; }
        toast('请在弹出的浏览器窗口中登录，完成后点击「完成提取」');
        if (res.hint) toast(res.hint, 'warn');
        loadLogins();
    }).catch((err) => toast('登录启动失败：' + err, 'err'));
}
function finishLogin(source) {
    api('login_finish', source).then((res) => {
        if (!res || !res.ok) { toast((res && res.error) || '操作失败', 'err'); return; }
        toast('已保存登录态');
        loadLogins();
    }).catch((err) => toast('操作失败：' + err, 'err'));
}
function logoutSource(source) {
    api('logout', source).then(() => { toast('已退出登录'); loadLogins(); })
        .catch((err) => toast('操作失败：' + err, 'err'));
}

function waitReady() {
    if (window.pywebview && window.pywebview.api) { init(); return; }
    if (!waitReady._n) waitReady._n = 0;
    waitReady._n += 1;
    if (waitReady._n === 50) felog('pywebview bridge still not ready after ~3s', 'warning', 'ui-boot');
    setTimeout(waitReady, 60);
}
waitReady();
