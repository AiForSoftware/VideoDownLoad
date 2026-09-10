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
    loginErrors: {},
    loginSupported: [],
    logsOpen: false,
    logsUnread: 0,
    logFilter: 'all',
    parsing: false,
    tools: {},
    history: [],
    // The parser list is only fetched on demand. Loading it pulls in the whole vd
    // engine (every parser module), which costs a lot of RAM — doing that at
    // startup made a freshly opened, completely idle app hold the entire engine in
    // memory. It is now requested lazily: when the user opens Settings / 登录态
    // (both render the parser list) or when the first parse needs it.
    wantSources: false,
};

// Proxy, not a plain object: the strings must follow the CURRENT language —
// a literal object would freeze the values captured at load time.
const STATUS_TEXT = new Proxy({}, {
    get: (_target, key) => (typeof key === 'string' ? I18N.t('status_' + key) : undefined),
});

const ICONS = {
    play: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M8 5v14l11-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg>',
    cancel: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    audio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/></svg>',
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
            if (n > 40) { clearInterval(t); reject(new Error(I18N.t('ready'))); }
        }, 75);
    });
}

function toast(message, kind, duration) {
    const el = $('toast');
    el.textContent = message;
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.hidden = false;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => { el.hidden = true; }, duration || 2600);
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
    { id: 'space', get label() { return I18N.t('theme_space'); } },
    { id: 'emerald', get label() { return I18N.t('theme_emerald'); } },
    { id: 'sunset', get label() { return I18N.t('theme_sunset'); } },
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
    toast(I18N.t('theme_switched', { name: next.label }), 'ok');
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
        box.innerHTML = `<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="14" rx="2"/><path d="m9 9 6 4-6 4z"/></svg><p>${I18N.t('no_results')}</p></div>`;
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
            item.has_audio ? `<span class="tag good">${I18N.t('tag_has_audio')}</span>` : '',
            item.valid ? '' : `<span class="tag bad">${I18N.t('tag_no_valid')}</span>`,
        ].join('');
        const tooltip = [item.err_msg, item.save_path || item.download_url].filter(Boolean).join('\n');
        return `<div class="result-item${on}" data-key="${esc(item.key)}" title="${esc(tooltip)}">
            ${thumb}
            <div class="result-main">
                <div class="result-title">${esc(item.title)}</div>
                <div class="result-meta">${tags}</div>
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

// 把条目下的进度任务按"流"拆开：视频 / 音频 / 字幕 / 合并封装。
// 之前是把所有流混成一条百分比，音频有没有在跑、有没有下完完全看不出来。
const STREAM_GROUPS = [
    { get label() { return I18N.t('group_video'); }, kinds: ['download', 'm3u8download'] },
    { get label() { return I18N.t('group_audio'); }, kinds: ['audio'] },
    { get label() { return I18N.t('group_subtitle'); }, kinds: ['subtitle'] },
    { get label() { return I18N.t('group_merge'); }, kinds: ['packaging'] },
];

// 一条流算"完成"的条件是跑到了 100%——引擎有时会把已完成任务移除（finished=true），
// 有时只是停在 100% 不移除，两种都要识别成完成，否则音频下完了却显示不出来。
function isStreamDone(p) {
    const t = Number(p.total) || 0;
    return t > 0 && (Number(p.completed) || 0) >= t;
}

function groupStreams(tasks) {
    // 暂停/取消会把未跑完的任务标成 finished，这些残留任务不能再计入，
    // 否则新一轮下载的字节数会和旧的一起叠加。
    const usable = tasks.filter((p) => isStreamDone(p) || !p.finished);
    const out = [];
    for (const g of STREAM_GROUPS) {
        const hit = usable.filter((p) => g.kinds.indexOf(p.kind) >= 0);
        if (!hit.length) continue;
        let totalBytes = 0, doneBytes = 0, speedSum = 0, hasTotal = false;
        for (const p of hit) {
            const t = Number(p.total) || 0, c = Number(p.completed) || 0;
            if (t > 0) { totalBytes += t; hasTotal = true; }
            doneBytes += c;
            if (!isStreamDone(p)) speedSum += Number(p.speed) || 0;
        }
        out.push({
            label: g.label, totalBytes, doneBytes, speedSum, hasTotal,
            finished: hit.every(isStreamDone),
            percent: hasTotal && totalBytes > 0 ? Math.min(100, doneBytes / totalBytes * 100) : null,
        });
    }
    return out;
}

const fmteta = (s) => {
    if (s == null) return '';
    if (s < 1) return I18N.t('eta_soon');
    if (s < 60) return I18N.t('eta_sec', { s: s.toFixed(0) });
    if (s < 3600) return I18N.t('eta_min', { m: Math.floor(s / 60), s: Math.floor(s % 60) });
    return I18N.t('eta_hour', { h: Math.floor(s / 3600), m: Math.floor((s % 3600) / 60) });
};

function renderItemProgress(job, item) {
    // 每个流单独一行：已下载/总体积 · 百分比 · 速度 · 剩余时间，完成后打勾
    const tasks = state.progress.filter((p) => p.job_id === job.id && p.item_key === item.key);
    if (!tasks.length) return '';
    const groups = groupStreams(tasks);
    if (!groups.length) return '';
    let rowsHtml = '', percentSum = 0, percentCount = 0;
    for (const g of groups) {
        const eta = (g.speedSum > 0 && g.hasTotal && g.totalBytes > g.doneBytes)
            ? (g.totalBytes - g.doneBytes) / g.speedSum : null;
        let detail;
        if (g.finished) {
            detail = I18N.t('done_with_size', { size: fmtbytes(g.doneBytes) });
        } else if (!g.hasTotal) {
            // 封装阶段没有总量（ffmpeg 不回报进度），显示"处理中…"而不是 0 B
            detail = I18N.t('processing');
        } else {
            detail = `${fmtbytes(g.doneBytes)} / ${fmtbytes(g.totalBytes)} · ${g.percent.toFixed(1)}%`;
            // Always surface the per-second speed, even when it is 0 (e.g. right
            // after start, while paused, or when YouTube throttles the stream to
            // a crawl) — an empty gap there read as "speed missing" to users.
            detail += ` · ${g.speedSum ? fmtspeed(g.speedSum) : '0 B/s'}`;
            if (eta) detail += ` · ${fmteta(eta)}`;
        }
        g.detail = detail;
        const fill = g.finished
            ? '<div class="progress-fill" data-fill style="width:100%"></div>'
            : (g.percent == null ? '<div class="progress-fill unknown" data-fill></div>'
                : `<div class="progress-fill" data-fill style="width:${g.percent.toFixed(2)}%"></div>`);
        rowsHtml += `<div class="stream-row${g.finished ? ' done' : ''}">
            <div class="stream-top"><span class="stream-name">${g.label}</span><span class="stream-detail" data-detail>${esc(detail)}</span></div>
            <div class="progress-track">${fill}</div>
        </div>`;
        if (g.percent != null) { percentSum += g.percent; percentCount += 1; }
    }
    // 分组构成变化（例如音频流刚创建、或某条流刚完成）时才重建 DOM，
    // 否则只更新数字和宽度，避免每帧重排闪烁。
    const sig = groups.map((g) => `${g.label}:${g.finished ? 1 : 0}:${g.hasTotal ? 1 : 0}`).join('|');
    return {
        percent: percentCount ? percentSum / percentCount : null,
        groups, sig,
        bar: `<div class="item-progress-wrap" data-sig="${esc(sig)}">${rowsHtml}</div>`,
    };
}

function jobSnapshot(jobs) {
    // 仅对影响结构/按钮的状态做快照；进度变化不会触发完整重绘
    return jobs.map((j) => [
        j.id, j.status, j.done_count, j.total_count,
        j.items.map((i) => [i.key, i.status, i.error || '']).join('|'),
    ].join(':')).join(';');
}

function updateJobProgress(jobs) {
    for (const job of jobs) {
        const jobEl = document.querySelector(`.job[data-job-id="${CSS.escape(job.id)}"]`);
        if (!jobEl) continue;
        for (const it of job.items) {
            const itemEl = jobEl.querySelector(`.job-item[data-item-key="${CSS.escape(it.key)}"]`);
            if (!itemEl) continue;
            const shouldShow = it.status === 'downloading' || it.status === 'paused' || it.status === 'pausing';
            let wrapEl = itemEl.querySelector('.item-progress-wrap');
            if (!shouldShow) {
                if (wrapEl) wrapEl.remove();
                continue;
            }
            const prog = renderItemProgress(job, it);
            if (!prog) {
                if (wrapEl) wrapEl.remove();
                continue;
            }
            if (wrapEl && wrapEl.getAttribute('data-sig') === prog.sig) {
                // 流构成没变：原地更新数字与宽度，避免整块重排
                const fills = wrapEl.querySelectorAll('[data-fill]');
                const details = wrapEl.querySelectorAll('[data-detail]');
                const rows = wrapEl.querySelectorAll('.stream-row');
                prog.groups.forEach((g, idx) => {
                    const fill = fills[idx];
                    if (fill && !fill.classList.contains('unknown')) {
                        fill.style.width = (g.percent == null ? 0 : g.percent).toFixed(2) + '%';
                    }
                    if (details[idx]) details[idx].textContent = g.detail || '';
                    if (rows[idx]) rows[idx].classList.toggle('done', !!g.finished);
                });
            } else {
                if (wrapEl) wrapEl.remove();
                itemEl.insertAdjacentHTML('beforeend', prog.bar);
            }
        }
    }
}

function renderJobs() {
    const box = $('jobs');
    if (!state.jobs.length) {
        box.innerHTML = `<div class="empty sm"><p>${I18N.t('no_jobs')}</p></div>`;
        state._jobSnapshot = '';
        return;
    }
    box.innerHTML = state.jobs.map((job) => {
        // Compute the effective status each item will display, taking job-wide
        // transition states into account. We only show per-item badges when the
        // effective statuses differ from the job status (or an item has an error
        // detail). This avoids duplicate status badges in the UI.
        const effectiveStatuses = job.items.map((it) => {
            let stKey = it.status;
            if (job.status === 'pausing' && (stKey === 'downloading' || stKey === 'queued')) stKey = 'pausing';
            if (job.status === 'paused' && (stKey === 'downloading' || stKey === 'queued' || stKey === 'pausing')) stKey = 'paused';
            if (job.status === 'cancelling' && (stKey === 'downloading' || stKey === 'paused' || stKey === 'queued' || stKey === 'pausing')) stKey = 'cancelling';
            return stKey;
        });
        const unique = new Set(effectiveStatuses);
        const allSameAsJob = unique.size <= 1 && effectiveStatuses.every((s) => s === job.status);
        const hasItemError = job.items.some((it) => it.status === 'error') || !!job.error;
        const showItemStatus = !allSameAsJob || hasItemError;

        const items = job.items.map((it, idx) => {
            // 过渡态跟随任务状态显示，避免"暂停中"徽标和"下载中"条目并存
            let stKey = effectiveStatuses[idx];
            const st = STATUS_TEXT[stKey] || stKey;
            // 错误详情不直接展示在列表里，悬停时通过 title 查看
            const errTip = it.error ? ` title="${esc(it.error)}"` : '';
            const statusHtml = showItemStatus
                ? `<span class="st ${esc(stKey)}"${errTip}>${esc(st)}</span>`
                : '';
            const prog = (it.status === 'downloading' || it.status === 'paused' || it.status === 'pausing')
                ? renderItemProgress(job, it)
                : null;
            // 进度条放在标题下方
            return `<div class="job-item" data-item-key="${esc(it.key)}">
                <div class="job-item-main">
                    <span class="name" title="${esc(it.save_path || '')}">${esc(it.title)}</span>
                    ${statusHtml}
                </div>
                ${prog ? prog.bar : ''}
            </div>`;
        }).join('');

        // Icon-only action bar. Folder is always available; pause/resume changes
        // depending on the job state. This stops the folder icon from flashing on/off.
        const folderBtn = `<button class="icon-btn job-action" data-open="${esc(job.id)}" title="${I18N.t('tip_open_file_dir')}">${ICONS.folder}</button>`;
        let stateBtn = '';
        if (job.status === 'downloading' || job.status === 'queued' || job.status === 'pausing') {
            stateBtn = `<button class="icon-btn job-action" data-pause="${esc(job.id)}" title="${I18N.t('tip_pause')}">${ICONS.pause}</button>`;
        } else if (job.status === 'paused' || job.status === 'error') {
            stateBtn = `<button class="icon-btn job-action primary" data-resume="${esc(job.id)}" title="${I18N.t('tip_resume')}">${ICONS.play}</button>`;
        }
        // While resuming the backend is reparsing on a background thread; hide the
        // play button so the user cannot trigger duplicate resume calls.
        const cancelTitle = ['done', 'error', 'cancelled'].includes(job.status) ? I18N.t('tip_remove') : I18N.t('cancel');
        // "补音频" appears for finished (done/error) jobs so a silent-video result
        // can be repaired by re-downloading just the audio + re-merging.
        const retryAudioBtn = (job.status === 'done' || job.status === 'error')
            ? `<button class="icon-btn job-action primary" data-retryaudio="${esc(job.id)}" title="${I18N.t('tip_retry_audio')}">${ICONS.audio}</button>`
            : '';
        const actions = `${stateBtn}${folderBtn}${retryAudioBtn}<button class="icon-btn job-action danger" data-cancel="${esc(job.id)}" title="${esc(cancelTitle)}">${ICONS.cancel}</button>`;

        const time = esc(job.finished_at || job.started_at || job.created_at);
        const remaining = Math.max(0, (job.total_count || 0) - (job.done_count || 0));
        const headerMeta = I18N.t('job_meta', { time: time, done: job.done_count || 0, total: job.total_count || 0, remaining: remaining });
        const badge = (job.status === 'done' || job.status === 'downloading')
            ? ''
            : `<span class="job-status ${esc(job.status)}">${esc(STATUS_TEXT[job.status] || job.status)}</span>`;
        return `<div class="job" data-job-id="${esc(job.id)}">
            <div class="job-head">
                <span class="job-meta">${headerMeta}</span>
                <div class="job-head-right">
                    ${badge}
                    <span class="job-head-actions">${actions}</span>
                </div>
            </div>
            <div class="job-items">${items}</div>
        </div>`;
    }).join('');
    state._jobSnapshot = jobSnapshot(state.jobs);
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
    // 面板收起时累计未读条数，显示在右下角浮标的角标上
    if (!state.logsOpen) {
        state.logsUnread += newLogs.length;
        $('logsFabBadge').textContent = String(state.logsUnread);
    }
}

function setLogsOpen(open) {
    state.logsOpen = open;
    $('logsCard').classList.toggle('open', open);
    $('logsFab').style.display = open ? 'none' : 'inline-flex';
    if (open) {
        state.logsUnread = 0;
        $('logsFabBadge').textContent = '';
    }
}

/* ---------------- language ---------------- */
function openLangModal() {
    // 高亮当前已选语言，让用户一眼看出正在用哪个（而不是固定高亮中文）
    $('langZhBtn').className = 'btn ' + (I18N.current === 'zh-CN' ? 'primary' : 'ghost');
    $('langEnBtn').className = 'btn ' + (I18N.current === 'en-US' ? 'primary' : 'ghost');
    $('langModal').hidden = false;
}
function closeLangModal() { $('langModal').hidden = true; }
function chooseLang(lang) {
    I18N.set(lang);
    I18N.apply();
    closeLangModal();
    state.config = Object.assign({}, state.config, { language: lang });
    api('setconfig', { language: lang }).then((r) => {
        if (r && r.config) state.config = r.config;
    }).catch(() => {});
}

function historySnapshot(history) {
    return history.map((h) => `${h.url}|${h.source || ''}|${h.last_used_at || h.parsed_at || ''}`).join(';');
}

function renderHistory() {
    const box = $('historyList');
    if (!state.history.length) {
        box.innerHTML = `<div class="empty sm"><p>${I18N.t('no_history')}</p></div>`;
        $('historyCount').textContent = '0';
        state._historySnapshot = '';
        return;
    }
    $('historyCount').textContent = String(state.history.length);
    box.innerHTML = state.history.map((h) => {
        let host = '';
        try { host = new URL(h.url).hostname.replace(/^www\./, ''); } catch (e) {}
        const src = shortname(h.source || '');
        const when = (h.last_used_at || h.parsed_at || '').slice(11, 19) || '-';
        const tag = h.source ? `<span class="tag source">${esc(src)}</span>` : `<span class="tag">${I18N.t('generic')}</span>`;
        return `<div class="history-item" data-url="${esc(h.url)}">
            <div class="history-main">
                <div class="history-url" title="${esc(h.url)}">${esc(h.url)}</div>
                <div class="history-meta">${tag} <span class="host">${esc(host)}</span> · <span class="when">${esc(when)}</span></div>
            </div>
            <div class="history-actions">
                <button class="btn ghost sm" data-fill="${esc(h.url)}" title="${I18N.t('tip_fill')}">${I18N.t('fill')}</button>
                <button class="btn ghost sm primary-mini" data-use="${esc(h.url)}" title="${I18N.t('tip_fill_parse')}">${I18N.t('parse')}</button>
                <button class="btn ghost sm danger" data-del="${esc(h.url)}" title="${I18N.t('tip_del_history')}">×</button>
            </div>
        </div>`;
    }).join('');
    state._historySnapshot = historySnapshot(state.history);
}

function renderEngineChip() {
    const chip = $('engineChip');
    const st = state.engineState || 'unloaded';
    if (st === 'error' || state.engineError) {
        chip.className = 'chip err';
        chip.innerHTML = `<span class="dot"></span><span>${I18N.t('engine_failed')}</span>`;
        return;
    }
    if (st === 'ready' && state.engineReady) {
        const total = state.platforms.length + state.generic.length;
        if (!total) {
            // 解析器列表还在异步加载中，sources API 几毫秒内就会回来；
            // 这里显示 "加载中" 避免出现误导性的 "0 解析器"
            chip.className = 'chip warn';
            chip.innerHTML = `<span class="dot"></span><span>${I18N.t('engine_ready_loading_list')}</span>`;
            return;
        }
        // show the ENABLED (whitelisted) count first — the user opted into 2
        // platforms, so "107 解析器" was misleading; keep the total as context
        const enabled = state.checkedSources.size || 0;
        chip.className = 'chip ok';
        chip.innerHTML = `<span class="dot"></span><span>${I18N.t('engine_ready_enabled', { enabled, total })}</span>`;
        return;
    }
    if (st === 'loading') {
        chip.className = 'chip warn';
        chip.innerHTML = `<span class="dot"></span><span>${I18N.t('engine_loading_ellipsis')}</span>`;
        return;
    }
    chip.className = 'chip';
    chip.innerHTML = `<span class="dot"></span><span>${I18N.t('engine_not_loaded')}</span>`;
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
    if (data.history) {
        const newSnap = historySnapshot(data.history);
        if (newSnap !== state._historySnapshot) {
            state.history = data.history;
            renderHistory();
        } else {
            state.history = data.history;
        }
    }
    if (data.logs && data.logs.length) {
        state.logSeq = data.log_seq;
        renderLogs(data.logs);
    }
    const snap = jobSnapshot(state.jobs);
    if (snap !== state._jobSnapshot) renderJobs();
    else updateJobProgress(state.jobs);
    renderEngineChip();
    // Force a refresh of the parser list whenever the engine transitions to
    // ready, so the settings whitelist shows every available parser instead of
    // only the lazy-loaded subset. Also retry while the list is still empty —
    // but ONLY once the user actually asked for it (Settings / 登录态): an
    // unconditional retry here fired `sources()` on the very first poll, which
    // booted the whole vd engine at startup for an app that was doing nothing.
    if (!prevEngineReady && state.engineReady && state.wantSources) loadSources();
    if (state.wantSources && !(state.platforms.length + state.generic.length)) loadSources();
    const busy = state.jobs.some((j) => j.status === 'downloading' || j.status === 'queued');
    if (prevBusy && !busy && hadJobs) toast(I18N.t('all_done'), 'ok');
    schedulePoll(busy);
}

/* Adaptive polling: an idle app does not need a 700ms tick. Every poll builds a
 * fresh JSON payload on the Python side and a fresh object graph here, so a fast
 * idle tick means constant allocation (and GC) for a screen that never changes.
 * Idle -> 1500ms, while anything is downloading/queued -> 700ms (snappy progress). */
let pollTimer = null;
let pollInterval = 1500;
function schedulePoll(busy) {
    if (busy === undefined) busy = state.jobs.some((j) => j.status === 'downloading' || j.status === 'queued');
    const want = busy ? 700 : 1500;
    if (pollTimer && want === pollInterval) return;
    pollInterval = want;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(poll, want);
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
    // Mark the list as requested (this is what allows the polling loop to keep it
    // fresh) and return the promise so callers can chain on the fresh list.
    state.wantSources = true;
    return api('sources').then((res) => {
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
    if (!all.length) { grid.innerHTML = `<div class="empty sm"><p>${I18N.t('engine_loading_hint')}</p></div>`; return; }
    grid.innerHTML = all.map((name) => {
        const checked = state.checkedSources.has(name) ? ' checked' : '';
        return `<label class="source-item" title="${esc(name)}"><input type="checkbox" value="${esc(name)}"${checked} /><span>${esc(shortname(name))}</span></label>`;
    }).join('');
}

function renderSourceCookies() {
    const box = $('sourceCookies');
    const all = state.platforms.concat(state.generic);
    if (!all.length) { box.innerHTML = `<div class="empty sm"><p>${I18N.t('engine_loading_hint')}</p></div>`; return; }
    const cookies = (state.config && state.config.per_source_cookies) || {};
    box.innerHTML = all.map((name) => {
        const val = esc(cookies[name] || '');
        return `<label class="source-cookie" title="${esc(name)}">
            <span>${esc(shortname(name))}</span>
            <textarea data-source="${esc(name)}" rows="2" spellcheck="false" placeholder="${I18N.t('cookie_ph')}">${val}</textarea>
        </label>`;
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
        $('parseHint').textContent = I18N.t('parse_failed', { err: res.error || I18N.t('unknown_error') });
        toast(I18N.t('parse_failed', { err: res.error || I18N.t('unknown_error') }), 'err');
    } else if (!state.items.length) {
        $('parseHint').className = 'hint err';
        $('parseHint').textContent = I18N.t('no_downloadable');
    } else if (state.items.every((i) => !i.valid)) {
        // Every parsed item has no real download URL (anti-bot / 412 / no cookie).
        // Surface the underlying reason and explicitly suggest a cookie so the
        // user knows what to do instead of staring at a cryptic tag.
        const firstErr = (state.items.find((i) => i.err_msg) || {}).err_msg || I18N.t('all_no_valid_url');
        const isAntiBot = /412|403|Precondition|FORBIDDEN|access.denied|Forbidden/i.test(firstErr);
        const isYouTube = /YouTube|youtube/i.test(firstErr);
        // 抖音未登录被反爬时引擎抛的就是这句（douyin.py 三个候选全失败时），
        // 此时对用户只说"请先登录"，原始长串收进 title 悬停可看。
        const isDouyinNoLogin = firstErr.includes('未能从抖音页面提取');
        const hasAnyLogin = !!(state.config && state.config.per_source_cookies && Object.keys(state.config.per_source_cookies).length);
        $('parseHint').className = 'hint err';
        // 任何平台只要一条有效链接都没解析到，用户最可能缺的就是登录态 ——
        // 统一提示"请先登录后重试"（5 秒），具体原因收进 title 悬停查看。
        let _toastMsg, _toastMs = 5000;
        if (isDouyinNoLogin) {
            $('parseHint').textContent = I18N.t('douyin_login_required');
            _toastMsg = I18N.t('douyin_login_required');
        } else if (isYouTube) {
            $('parseHint').textContent = I18N.t('parse_failed_youtube') + I18N.t('login_retry_suffix');
            _toastMsg = I18N.t('login_required_generic');
        } else if (isAntiBot && !hasAnyLogin) {
            $('parseHint').textContent = I18N.t('parse_failed_403_login');
            _toastMsg = I18N.t('login_required_generic');
        } else if (isAntiBot) {
            $('parseHint').textContent = I18N.t('parse_failed_403_relogin');
            _toastMsg = I18N.t('login_required_generic');
        } else {
            $('parseHint').textContent = I18N.t('login_required_generic');
            _toastMsg = I18N.t('login_required_generic');
        }
        $('parseHint').title = firstErr;
        toast(_toastMsg, 'err', _toastMs);
    } else {
        const cnt = state.items.length;
        $('parseHint').className = 'hint ok';
        const _prefLabel = { best: I18N.t('quality_best'), auto: I18N.t('quality_all') }[(state.config || {}).default_quality] || String((state.config || {}).default_quality || 'best').toUpperCase();
        if (res.batch) {
            const ok = (res.url_count || 1) - (res.errors ? res.errors.length : 0);
            const extra = (res.errors && res.errors.length) ? I18N.t('n_links_failed', { n: res.errors.length }) : '';
            $('parseHint').textContent = I18N.t('batch_parse_done', { ok: ok, total: res.url_count, cnt: cnt, q: _prefLabel, n: state.selected.size, extra: extra });
        } else {
            $('parseHint').textContent = I18N.t('parse_ok', { cnt: cnt, q: _prefLabel, n: state.selected.size });
        }
        $('urlInput').value = '';
    }
}

function parseUrl() {
    const raw = $('urlInput').value.trim();
    if (!raw) { toast(I18N.t('enter_url'), 'err'); return; }
    // Split into one-or-many urls: one per line, or separated by spaces / commas
    // (full-width commas too). Enables batch parsing — a single url behaves
    // exactly as before.
    const urls = raw.split(/[\s,，;；]+/).map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { toast(I18N.t('enter_url'), 'err'); return; }
    if (state.parsing) return;
    state.parsing = true;
    $('parseBtn').disabled = true;
    $('parseHint').className = 'hint';
    $('parseHint').textContent = ((state.engineState || 'unloaded') !== 'ready')
        ? I18N.t('engine_first_load')
        : (urls.length > 1 ? I18N.t('parsing_batch', { n: urls.length }) : I18N.t('parsing'));
    // Single url still goes through the per-url `parse` API; multiple urls use
    // `parsebatch`. Both push the merged result back via window.applyParseResult
    // (see JsApi.parse / parsebatch in api.py). The api() call only kicks off the
    // background job and resolves immediately, so the UI stays responsive.
    const call = urls.length === 1 ? api('parse', urls[0]) : api('parsebatch', urls);
    call.catch((err) => {
        state.parsing = false;
        $('parseBtn').disabled = false;
        $('parseHint').className = 'hint err';
        $('parseHint').textContent = I18N.t('parse_request_failed', { err: err });
    });
}

function downloadSelected() {
    const keys = Array.from(state.selected);
    if (!keys.length) return;
    api('download', keys, state.config ? state.config.work_dir : null).then((res) => {
        if (!res.ok) toast(res.error || I18N.t('task_failed_plain'), 'err');
        else if (res.count && res.count > 1) toast(I18N.t('task_created_n', { n: res.count }));
        else toast(I18N.t('task_created', { id: res.job_id }));
    }).catch((err) => toast(I18N.t('task_failed', { err: err }), 'err'));
}

/* ---------------- settings ---------------- */
function openSettings() {
    const cfg = state.config || {};
    $('cfgWorkDir').value = cfg.work_dir || '';
    $('cfgThreads').value = cfg.num_threadings || 5;
    $('cfgConcurrent').value = cfg.concurrent_downloads || 2;
    $('cfgProxy').value = cfg.proxy || '';
    $('cfgQuality').value = (cfg.default_quality || 'best');
    $('cfgSubtitles').checked = !!(cfg.download_subtitles);
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
    // The per-source Cookie editor now lives here (not in Settings), so we
    // make sure the platform list is loaded and (re)render its textareas.
    if (state.platforms && state.platforms.length) renderSourceCookies();
    else loadSources().then(() => renderSourceCookies());
}

function saveCookies() {
    // MERGE with the LIVE stored map (fetched fresh from the backend — a login
    // that finished seconds ago may have updated it server-side). Empty
    // textareas must never wipe previously saved cookies: auto-login capture
    // writes here, and users clicking 保存 with untouched textareas were
    // unknowingly clearing other platforms' logins. Clearing a platform's
    // login is done via its 退出 button, not here.
    api('login_status').then((st) => {
        const map = Object.assign({}, (st && st.per_source_cookies) || {});
        document.querySelectorAll('#sourceCookies textarea[data-source]').forEach((el) => {
            const v = (el.value || '').trim();
            if (v) map[el.getAttribute('data-source')] = v;
        });
        return api('setconfig', { per_source_cookies: map }).then((res) => {
            if (!(res && res.ok)) { toast(I18N.t('save_failed', { err: (res && res.error) || I18N.t('unknown_error') }), 'err'); return; }
            const cfg = state.config || (state.config = {});
            cfg.per_source_cookies = res.config.per_source_cookies;
            // reflect the stored values back so the dialog shows what is really saved
            document.querySelectorAll('#sourceCookies textarea[data-source]').forEach((el) => {
                const src = el.getAttribute('data-source');
                el.value = (cfg.per_source_cookies || {})[src] || '';
            });
            toast(I18N.t('cookie_saved'), 'ok');
            // countdown reminder, then auto-close the login dialog
            let left = 5;
            toast(I18N.t('auto_close_in', { n: 5 }), 'info');
            const timer = setInterval(() => {
                left -= 1;
                if (left <= 0) { clearInterval(timer); closeLoginModal(); return; }
                toast(I18N.t('auto_close_in', { n: left }), 'info');
            }, 1000);
        });
    }).catch((err) => toast(I18N.t('save_failed', { err: err }), 'err'));
}

function closeLoginModal() { $('loginModal').hidden = true; }

/* ---------------- 更新与反馈（顶栏按钮） ---------------- */
function openFeedbackModal() { $('feedbackModal').hidden = false; }
function closeFeedbackModal() { $('feedbackModal').hidden = true; }

function saveSettings() {
    const checked = Array.from(document.querySelectorAll('#sourceGrid input:checked')).map((el) => el.value);
    const payload = {
        work_dir: $('cfgWorkDir').value.trim(),
        num_threadings: parseInt($('cfgThreads').value, 10) || 5,
        concurrent_downloads: parseInt($('cfgConcurrent').value, 10) || 2,
        proxy: $('cfgProxy').value.trim(),
        default_quality: $('cfgQuality').value,
        download_subtitles: $('cfgSubtitles').checked,
        // persist exactly what the user checked. The old "(all checked) -> []"
        // convention silently reset a full whitelist to the 2-platform default
        // on the next launch, and is the reason the whitelist kept "forgetting".
        allowed_sources: checked,
    };
    api('setconfig', payload).then((res) => {
        if (res.ok) {
            state.config = res.config;
            $('workDirLabel').textContent = res.config.work_dir;
            toast(I18N.t('settings_saved'), 'ok');
            closeSettings();
        } else {
            toast(I18N.t('save_failed', { err: res.error || '' }), 'err');
        }
    }).catch((err) => toast(I18N.t('save_failed', { err: err }), 'err'));
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
    $('langBtn').addEventListener('click', openLangModal);
    $('langZhBtn').addEventListener('click', () => chooseLang('zh-CN'));
    $('langEnBtn').addEventListener('click', () => chooseLang('en-US'));
    $('settingsBtn').addEventListener('click', openSettings);
    $('loginBtn').addEventListener('click', openLoginModal);
    $('feedbackBtn').addEventListener('click', openFeedbackModal);
    $('closeLoginBtn').addEventListener('click', closeLoginModal);
    $('closeFeedbackBtn').addEventListener('click', closeFeedbackModal);
    $('closeFeedbackBtn2').addEventListener('click', closeFeedbackModal);
    $('feedbackModal').addEventListener('click', (e) => { if (e.target === $('feedbackModal')) closeFeedbackModal(); });
    $('saveCookiesBtn').addEventListener('click', saveCookies);
    $('closeSettingsBtn').addEventListener('click', closeSettings);
    $('cancelSettingsBtn').addEventListener('click', closeSettings);
    $('saveSettingsBtn').addEventListener('click', saveSettings);
    $('pickDirBtn').addEventListener('click', () => {
        api('pickfolder').then((res) => { if (res.ok && res.path) $('cfgWorkDir').value = res.path; });
    });
    $('openConfigDirBtn').addEventListener('click', () => api('openconfigdir').then((r) => { if (!r.ok) toast(r.error || I18N.t('open_config_dir_failed'), 'err'); }));
    $('logsFab').addEventListener('click', () => setLogsOpen(true));
    $('toggleLogsBtn').addEventListener('click', () => setLogsOpen(false));
    $('clearLogsBtn').addEventListener('click', () => { $('logs').innerHTML = ''; });
    $('logFilterBtn').addEventListener('click', () => {
        state.logFilter = state.logFilter === 'all' ? 'warn' : 'all';
        $('logs').classList.toggle('hide-info', state.logFilter === 'warn');
        $('logFilterBtn').textContent = state.logFilter === 'all' ? I18N.t('logs_concise') : I18N.t('logs_all');
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
        const btn = e.target.closest('button[data-login], button[data-finish], button[data-logout], button[data-cookie]');
        if (!btn) return;
        if (btn.hasAttribute('data-login')) startLogin(btn.getAttribute('data-login'));
        else if (btn.hasAttribute('data-finish')) finishLogin(btn.getAttribute('data-finish'));
        else if (btn.hasAttribute('data-logout')) logoutSource(btn.getAttribute('data-logout'));
        else if (btn.hasAttribute('data-cookie')) openCookieSettings(btn.getAttribute('data-cookie'));
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
    // 任务卡片操作按钮使用事件委托，避免轮询重建 DOM 导致按钮闪烁/点击失效
    $('jobs').addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-pause], button[data-resume], button[data-cancel], button[data-open], button[data-retryaudio]');
        if (!btn) return;
        const jobId = btn.getAttribute('data-pause') || btn.getAttribute('data-resume') || btn.getAttribute('data-cancel') || btn.getAttribute('data-open') || btn.getAttribute('data-retryaudio');
        const job = state.jobs.find((j) => j.id === jobId);
        if (!job) return;
        if (btn.hasAttribute('data-pause')) {
            job.status = 'pausing';
            renderJobs();
            api('pause', jobId);
        } else if (btn.hasAttribute('data-resume')) {
            job.status = job.status === 'error' ? 'downloading' : 'resuming';
            renderJobs();
            api('resume', jobId);
        } else if (btn.hasAttribute('data-retryaudio')) {
            job.status = 'downloading';
            const it = (job.items || []).find((x) => x.status === 'done' || x.status === 'error');
            if (it) it.status = 'downloading';
            renderJobs();
            api('retryaudio', jobId).then((r) => {
                if (!r || !r.ok) toast(r && r.error ? r.error : I18N.t('retry_audio_failed'), 'err');
                else if (r.already) toast(I18N.t('already_has_audio'), 'ok');
            });
        } else if (btn.hasAttribute('data-cancel')) {
            job.status = 'cancelling';
            renderJobs();
            api('cancel', jobId);
        } else if (btn.hasAttribute('data-open')) {
            const saved = (job.items || []).find((it) => it.save_path);
            const fallback = (r) => api('openpath', job.work_dir).then((r2) => { if (!r2.ok) toast(I18N.t('open_dir_failed', { err: r2.error || r.error || '' }), 'err'); });
            if (saved && saved.save_path) api('revealpath', saved.save_path).then((r) => { if (!r.ok) fallback(r); });
            else api('openpath', job.work_dir).then((r) => { if (!r.ok) toast(I18N.t('open_dir_failed', { err: r.error || '' }), 'err'); });
        }
    });
}

function init() {
    felog('ui init started (pywebview bridge ready)', 'info', 'ui-boot');
    bindEvents();
    felog('event handlers bound', 'info', 'ui-boot');
    api('bootstrap').then((res) => {
        state.config = res.config;
        window.__vdConfig = res.config;
        // 后端没存过语言 = 首次打开 → 弹一次语言选择；老用户直接套用已存语言。
        if (res.config.language && res.config.language !== I18N.current) { I18N.set(res.config.language); I18N.apply(); }
        else if (!res.config.language) openLangModal();
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
        felog(`tools detected: ${Object.entries(state.tools).map(([k, v]) => `${k}=${v ? 'y' : 'n'}`).join(' ')}`, 'info', 'ui-boot');
    }).catch((err) => felog(`bootstrap/checktools failed: ${err}`, 'error', 'ui-boot'));
    const hash = decodeURIComponent((location.hash || '').replace(/^#/, ''));
    if (hash) $('urlInput').value = hash;
    poll();
    schedulePoll();
    // NOTE: no `loadSources()` here on purpose. `sources()` boots the vd engine,
    // and doing it 800ms after launch meant every idle start paid the engine's
    // full memory cost. The list is loaded when Settings / 登录态 is opened, or
    // when the first url is parsed — whichever comes first.
    felog('polling started (idle 1500ms / busy 700ms); parser list deferred', 'info', 'ui-boot');
}

window.__prefillUrl = function (url) { $('urlInput').value = url; };

/* ---------------- default quality selection ---------------- */
function qualityRank(label) {
    const map = { '8k': 5, '4k': 4, '1080p+': 3.5, '1080p60': 3.2, '1080p': 3, '720p60': 2.2, '720p': 2, '540p': 1.5, '480p': 1, '360p': 0 };
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
        const base = (it.title || '').replace(/[_-]?(4K|1080P\+?|1080P60|1080P|720P60|720P|540P|480P|360P|8K)\s*$/i, '').trim() || it.title;
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
    if (!all.length) { grid.innerHTML = `<div class="empty sm"><p>${I18N.t('engine_loading_hint')}</p></div>`; return; }
    grid.innerHTML = all.map((name) => {
        const st = (state.logins && state.logins[name]) || 'absent';
        const logged = !!((state.config && state.config.per_source_cookies && state.config.per_source_cookies[name]));
        const err = (state.loginErrors && state.loginErrors[name]) || '';
        const supported = state.loginSupported == null ? true : state.loginSupported.includes(name);
        let action = '';
        if (st === 'waiting') action = `<button class="btn ghost sm primary-mini" data-finish="${esc(name)}">${I18N.t('login_finish_btn')}</button>`;
        else if (st === 'opening') action = `<span class="tag warn">${I18N.t('login_opening')}</span>`;
        else if (st === 'extracting') action = `<span class="tag warn">${I18N.t('login_extracting')}</span>`;
        else if (st === 'incomplete') action = `<span class="tag bad">${I18N.t('login_incomplete')}</span><button class="btn ghost sm" data-login="${esc(name)}">${I18N.t('retry')}</button>`;
        else if (st === 'error') action = `<span class="tag bad">${I18N.t('login_failed')}</span><button class="btn ghost sm" data-login="${esc(name)}">${I18N.t('retry')}</button>`;
        else if (logged) action = `<button class="btn ghost sm danger" data-logout="${esc(name)}">${I18N.t('logout_btn')}</button>`;
        else if (supported) action = `<button class="btn ghost sm" data-login="${esc(name)}">${I18N.t('login_btn')}</button>`;
        else action = `<button class="btn ghost sm" data-cookie="${esc(name)}">${I18N.t('fill_cookie')}</button>`;
        const badge = logged ? `<span class="tag good">${I18N.t('logged_in')}</span>` : (st === 'waiting' ? `<span class="tag warn">${I18N.t('please_login')}</span>` : (st === 'incomplete' ? `<span class="tag bad">${I18N.t('login_error')}</span>` : ''));
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
        state.loginSupported = (r && r.supported) || [];
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
            state.loginSupported = (r && r.supported) || [];
            if (r && r.per_source_cookies) state.config.per_source_cookies = r.per_source_cookies;
            renderLoginGrid();
            const active = (r && r.logins || []).some((l) => ['opening', 'waiting', 'extracting'].includes(l.state));
            if (!active) { clearInterval(loginPollTimer); loginPollTimer = null; }
        }).catch(() => {});
    }, 1500);
}
function openCookieSettings(source) {
    closeLoginModal();
    openSettings();
    // 等设置面板渲染出 Cookie 输入框后再聚焦
    setTimeout(() => {
        const el = document.querySelector(`#sourceCookies textarea[data-source="${CSS.escape(source)}"]`);
        if (el) { el.scrollIntoView({ block: 'center', behavior: 'smooth' }); el.focus(); }
    }, 50);
}

function startLogin(source) {
    api('login', source).then((res) => {
        if (!res || !res.ok) {
            // 不支持自动登录的平台改为引导到设置手动填写，不再显示红色错误提示
            if (res && res.error && res.error.includes('暂不支持平台')) {
                openCookieSettings(source);
                return;
            }
            toast((res && res.error) || I18N.t('login_start_failed'), 'err');
            return;
        }
        toast(I18N.t('login_in_browser'));
        if (res.hint) toast(res.hint, 'warn');
        loadLogins();
    }).catch((err) => toast(I18N.t('login_start_failed_with', { err: err }), 'err'));
}
function finishLogin(source) {
    api('login_finish', source).then((res) => {
        if (!res || !res.ok) { toast((res && res.error) || I18N.t('operation_failed'), 'err'); return; }
        toast(I18N.t('login_saved'));
        loadLogins();
    }).catch((err) => toast(I18N.t('operation_failed_with', { err: err }), 'err'));
}
function logoutSource(source) {
    api('logout', source).then(() => { toast(I18N.t('logged_out')); loadLogins(); })
        .catch((err) => toast(I18N.t('operation_failed_with', { err: err }), 'err'));
}

function waitReady() {
    if (window.pywebview && window.pywebview.api) { init(); return; }
    if (!waitReady._n) waitReady._n = 0;
    waitReady._n += 1;
    if (waitReady._n === 50) felog('pywebview bridge still not ready after ~3s', 'warning', 'ui-boot');
    setTimeout(waitReady, 60);
}
waitReady();
