function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function reviewStatusBadge(status) {
    const cls = {
        'passed': 'bg-green-100 text-green-800',
        'escalated': 'bg-yellow-100 text-yellow-800',
        'rejected': 'bg-red-100 text-red-800',
        'revised': 'bg-blue-100 text-blue-800',
        'flagged': 'bg-orange-100 text-orange-800',
    }[status] || 'bg-gray-100 text-gray-500';
    const label = {
        'passed': '✓ 已审查',
        'escalated': '⚡ 已仲裁',
        'rejected': '✗ 未通过',
        'revised': '↻ 已修正',
        'flagged': '! 已标记',
    }[status] || '⋅ 待审查';
    return `<span class="px-2 py-0.5 rounded text-xs font-medium ${cls}">${label}</span>`;
}

function auditFlag(metadata) {
    if (!metadata || !metadata.audit_verdict) return '';
    const cfg = {
        'SUPPORTED': { icon: '✓', cls: 'text-green-600', label: '证据充分' },
        'STRETCHED': { icon: '⚠', cls: 'text-yellow-600', label: '过度延伸' },
        'UNSUPPORTED': { icon: '✗', cls: 'text-red-600', label: '缺乏证据' },
    }[metadata.audit_verdict] || { icon: '?', cls: 'text-gray-400', label: metadata.audit_verdict };
    return `<span class="text-xs ${cfg.cls} font-medium ml-1" title="${cfg.label}">${cfg.icon}</span>`;
}

async function loadSparks() {
    const type = document.getElementById('filter-type').value;
    const reviewStatus = document.getElementById('filter-review-status').value;
    const params = new URLSearchParams();
    if (type) params.set('source_type', type);
    if (reviewStatus) params.set('status', reviewStatus);

    const resp = await fetch(`/ideator/api/sparks?${params}`);
    const sparks = await resp.json();

    const container = document.getElementById('spark-list');
    if (!sparks.length) {
        container.innerHTML = '<div class="text-center text-gray-400 py-12">暂无火花，点击「全量挖掘」开始发现</div>';
        return;
    }

    container.innerHTML = sparks.map(s => `
        <div class="spark-card bg-white rounded-lg p-4 mb-3 border ${s.metadata && s.metadata.maybe_duplicate ? 'maybe-duplicate' : ''}" id="spark-${s.id}">
            <div class="flex items-start justify-between">
                <div class="flex-1">
                    <div class="flex items-center gap-2 mb-2 flex-wrap">
                        <span class="text-xs bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded">${escapeHtml(s.source_type)}</span>
                        ${reviewStatusBadge(s.review_status)}
                        ${auditFlag(s.metadata)}
                        <span class="text-xs text-gray-400">置信度 ${(s.quality_score * 100).toFixed(0)}%</span>
                        ${s.metadata && s.metadata.filter_score ? `<span class="text-xs text-gray-400">筛选 ${(s.metadata.filter_score * 100).toFixed(0)}%</span>` : ''}
                        ${s.final_score ? `<span class="text-xs text-gray-400">最终 ${(s.final_score * 100).toFixed(0)}%</span>` : ''}
                    </div>
                    <p class="text-gray-800">${escapeHtml(s.content)}</p>
                    ${s.source_titles && s.source_titles.length > 0 ? `
                    <div class="mt-2 flex flex-wrap gap-1">
                        <span class="text-xs text-gray-400">来源:</span>
                        ${s.source_titles.map(t => `<span class="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">${escapeHtml(t)}</span>`).join('')}
                    </div>` : ''}
                    ${s.depth_content ? `<div class="mt-3 p-3 bg-gray-50 rounded text-sm border-l-2 border-indigo-300 depth-content">${escapeHtml(s.depth_content)}</div>` : ''}
                </div>
            </div>
            <div class="flex gap-2 mt-3 pt-3 border-t border-gray-100 flex-wrap">
                <button onclick="toggleSparkDetail(${s.id})" class="text-xs text-indigo-600 hover:text-indigo-800 font-medium">展开详情</button>
                <button onclick="viewReviews(${s.id})" class="text-xs text-purple-600 hover:text-purple-800">审查记录</button>
                <button onclick="deepenSpark(${s.id})" class="text-xs text-indigo-600 hover:text-indigo-800">深入展开</button>
                <button onclick="startRoundtable(${s.id})" class="px-3 py-1 bg-purple-600 text-white text-xs rounded hover:bg-purple-700">发起圆桌</button>
                <button onclick="feedback(${s.id}, 'useful')" class="text-xs text-green-600 hover:text-green-800">有用</button>
                <button onclick="feedback(${s.id}, 'duplicate')" class="text-xs text-gray-400 hover:text-gray-600">重复</button>
                <button onclick="feedback(${s.id}, 'noise')" class="text-xs text-gray-400 hover:text-gray-600">无用</button>
            </div>
            <div id="spark-detail-${s.id}" class="hidden mt-3 border-t border-gray-200 pt-3"></div>
        </div>
    `).join('');
}

async function deepenSpark(id) {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = '分析中...';
    try {
        const resp = await fetch(`/ideator/api/sparks/${id}/deepen`, { method: 'POST' });
        const data = await resp.json();
        if (data.depth_content) {
            loadSparks();
        } else {
            alert('深化失败，请稍后重试');
        }
    } catch (e) {
        alert('网络错误，请稍后重试');
    } finally {
        btn.disabled = false;
        btn.textContent = '深入展开';
    }
}

async function feedback(id, value) {
    const fd = new FormData();
    fd.append('feedback', value);
    await fetch(`/ideator/api/sparks/${id}/feedback`, { method: 'POST', body: fd });
    loadSparks();
}

async function viewReviews(sparkId) {
    try {
        const resp = await fetch(`/ideator/api/sparks/${sparkId}/reviews`);
        const reviews = await resp.json();
        if (reviews.length === 0) {
            alert('暂无审查记录');
            return;
        }
        const lines = reviews.map(r => {
            const scores = JSON.parse(r.scores || '{}');
            return `${r.reviewer_role} (${r.reviewer_model}): ` +
                   `N=${scores.novelty?.toFixed(1)||'?'} ` +
                   `E=${scores.evidence?.toFixed(1)||'?'} ` +
                   `F=${scores.feasibility?.toFixed(1)||'?'} ` +
                   `→ ${r.verdict}\n${r.reasoning || ''}`;
        });
        alert(`火花 #${sparkId} 审查记录:\n\n${lines.join('\n\n')}`);
    } catch (e) {
        console.error('加载审查记录失败', e);
    }
}

async function viewRuns() {
    try {
        const resp = await fetch('/ideator/api/runs?limit=10');
        const runs = await resp.json();
        if (runs.length === 0) {
            alert('暂无运行记录');
            return;
        }
        const lines = runs.map(r => {
            const stats = JSON.parse(r.stats || '{}');
            return `#[${r.run_id}] ${r.trigger} ${r.started_at} — ` +
                   `状态: ${r.error ? '失败' : '完成'} ` +
                   `Token: ${r.total_tokens}`;
        });
        alert(`最近运行记录:\n\n${lines.join('\n\n')}`);
    } catch (e) {
        console.error('加载运行记录失败', e);
    }
}

async function viewWeights() {
    try {
        const resp = await fetch('/ideator/api/weights');
        const weights = await resp.json();
        if (weights.length === 0) {
            alert('暂无权重数据');
            return;
        }
        const lines = weights.map(w =>
            `${w.source_type}: weight=${w.weight.toFixed(2)} ` +
            `(有用:${w.useful_count} 噪音:${w.noise_count})`
        );
        alert(`召回路径权重:\n\n${lines.join('\n')}`);
    } catch (e) {
        console.error('加载权重失败', e);
    }
}

async function toggleSparkDetail(sparkId) {
    const panel = document.getElementById('spark-detail-' + sparkId);
    if (!panel) return;

    if (panel.classList.contains('hidden')) {
        panel.classList.remove('hidden');
        panel.innerHTML = '<div class="text-center text-gray-400 py-4 text-sm">加载中...</div>';

        try {
            const resp = await fetch(`/ideator/api/sparks/${sparkId}/detail`);
            if (!resp.ok) throw new Error('not found');
            const spark = await resp.json();
            if (spark.error) throw new Error(spark.error);

            let html = '';

            // S3 briefing
            if (spark.s3_briefing) {
                const b = spark.s3_briefing;
                html += `<div class="briefing-card bg-yellow-50 border-l-2 border-yellow-400 rounded p-3 mb-3">
                    <h4 class="text-sm font-semibold text-yellow-700 mb-2">S3 书记员简报</h4>
                    <div class="text-xs space-y-1 text-gray-700">
                        <p><strong>背景：</strong>${escapeHtml(b.background || '')}</p>
                        <p><strong>突破口：</strong>${escapeHtml(b.breakthrough || '')}</p>
                        <p><strong>创新点：</strong>${escapeHtml(b.innovation || '')}</p>
                        ${b.implementation && b.implementation.length > 0 ? `<p><strong>实施方案：</strong>${b.implementation.map(s => escapeHtml(s)).join('；')}</p>` : ''}
                        ${b.open_issues && b.open_issues.length > 0 ? `<p><strong>遗留争议：</strong>${b.open_issues.map(s => escapeHtml(s)).join('；')}</p>` : ''}
                    </div>
                </div>`;
            }

            // Debate summary
            if (spark.debate_summary) {
                html += `<div class="mb-3 p-3 bg-blue-50 rounded border-l-2 border-blue-400 text-xs">
                    <h4 class="font-semibold text-blue-700 mb-1">辩论摘要</h4>
                    <p class="text-gray-700">${escapeHtml(spark.debate_summary)}</p>
                </div>`;
            }

            // Draft
            if (spark.depth_content) {
                html += `<div class="mb-3 p-3 bg-gray-50 rounded border text-sm">
                    <h4 class="font-semibold text-gray-700 mb-1">研究草稿</h4>
                    <div class="draft-content text-gray-800">${escapeHtml(spark.depth_content)}</div>
                </div>`;
            }

            // Debate rounds (detailed Q&A)
            if (spark.debate_rounds && spark.debate_rounds.length > 0) {
                html += `<div class="mb-3">
                    <h4 class="text-xs font-semibold text-gray-500 mb-2">辩论回合 (${spark.debate_rounds.length}轮)</h4>`;
                for (const rnd of spark.debate_rounds) {
                    html += `<div class="mb-2 p-2 bg-white rounded border text-xs">
                        <div class="font-semibold text-indigo-600 mb-1">第 ${rnd.round} 轮</div>`;
                    for (const q of (rnd.questions || [])) {
                        html += `<div class="ml-2 mb-1">
                            <span class="text-red-500 font-medium">[${escapeHtml(q.reviewer || '?')}]</span>
                            <span class="text-gray-700">${escapeHtml(q.content || '')}</span>
                        </div>`;
                    }
                    for (const resp of (rnd.gen_responses || [])) {
                        html += `<div class="ml-2 mb-1">
                            <span class="text-green-600 font-medium">[gen → ${escapeHtml(resp.reviewer || '?')}]</span>
                            <span class="text-gray-700">${escapeHtml(resp.response || '')}</span>
                            ${resp.draft_change ? `<div class="text-gray-400 italic ml-4">修改: ${escapeHtml(resp.draft_change)}</div>` : ''}
                        </div>`;
                    }
                    html += '</div>';
                }
                html += '</div>';
            }

            // Review records
            if (spark.review_records && spark.review_records.length > 0) {
                html += `<div class="mb-3">
                    <h4 class="text-xs font-semibold text-gray-500 mb-2">审查记录 (${spark.review_records.length})</h4>`;
                for (const r of spark.review_records) {
                    const scores = typeof r.scores === 'string' ? JSON.parse(r.scores) : (r.scores || {});
                    html += `<div class="text-xs text-gray-600 mb-1 pl-2 border-l-2 border-gray-200">
                        <span class="font-medium">${escapeHtml(r.reviewer_role || '?')}</span>
                        N=${scores.novelty?.toFixed(1)||'?'} E=${scores.evidence?.toFixed(1)||'?'} F=${scores.feasibility?.toFixed(1)||'?'}
                        → ${escapeHtml(r.verdict || '?')}
                    </div>`;
                }
                html += '</div>';
            }

            // Roundtable messages
            if (spark.roundtable_messages && spark.roundtable_messages.length > 0) {
                html += `<div class="mb-3">
                    <h4 class="text-xs font-semibold text-gray-500 mb-2">圆桌讨论 (${spark.roundtable_messages.length}条消息)</h4>`;
                for (const m of spark.roundtable_messages.slice(-30)) {
                    const roleLabel = m.sender_role ? `[${escapeHtml(m.sender_role)}]` : '';
                    const bg = m.sender_type === 'user' ? 'bg-indigo-50' : m.sender_type === 'system' ? 'bg-gray-50' : 'bg-white';
                    html += `<div class="text-xs text-gray-600 mb-1 pl-2 border-l-2 border-gray-200 ${bg}">
                        <span class="font-medium">${escapeHtml(m.sender_name || '?')}${roleLabel}</span>
                        <span class="text-gray-400 ml-1">${escapeHtml((m.content || '').substring(0, 120))}</span>
                    </div>`;
                }
                html += '</div>';
            }

            if (!html) {
                html = '<div class="text-center text-gray-400 py-4 text-sm">暂无可显示内容</div>';
            }
            panel.innerHTML = html;
        } catch (e) {
            panel.innerHTML = '<div class="text-center text-red-400 py-4 text-sm">加载失败，请稍后重试</div>';
        }
    } else {
        panel.classList.add('hidden');
        panel.innerHTML = '';
    }
}

// ── 圆桌讨论 (Agent Team) ──────────────────────────────────

async function startRoundtable(sparkId) {
    try {
        const resp = await fetch(`/ideator/api/sparks/${sparkId}/roundtable/start`, { method: 'POST' });
        const data = await resp.json();
        if (data.roundtable_id) {
            window.location.href = `/ideator/roundtable/${data.roundtable_id}`;
        } else if (data.error) {
            alert(data.error);
        }
    } catch (e) {
        console.error('Failed to start roundtable', e);
        alert('启动圆桌讨论失败，请稍后重试');
    }
}

function openRoundtableModal(sparkId, rtId) {
    if (window._rtPollInterval) {
        clearInterval(window._rtPollInterval);
        window._rtPollInterval = null;
    }
    const existing = document.getElementById('roundtable-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'roundtable-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50';
    modal.innerHTML = `
        <div class="rt-container bg-white rounded-lg shadow-xl w-11/12 max-w-5xl h-[92vh] flex flex-col">
            <div class="rt-header p-3 border-b flex items-center justify-between bg-gray-50 shrink-0">
                <div class="flex items-center gap-4">
                    <h2 class="text-lg font-semibold text-indigo-700">${sparkId === 0 ? '直接圆桌' : '#' + sparkId + ' 圆桌讨论'}</h2>
                    <span id="rt-round-num" class="text-xs bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded">轮次 0</span>
                </div>
                <div class="flex-1 mx-6">
                    <div id="rt-watermark" class="rt-watermark-bar h-2 bg-gray-200 rounded-full overflow-hidden flex">
                        <div id="rt-hot-bar" class="h-full bg-red-400 transition-all duration-500" style="width:0%"></div>
                        <div id="rt-warm-bar" class="h-full bg-yellow-400 transition-all duration-500" style="width:0%"></div>
                    </div>
                    <div class="flex justify-between text-xs text-gray-400 mt-0.5">
                        <span id="rt-hot-label">hot 0%</span>
                        <span id="rt-warm-label">warm 0%</span>
                    </div>
                </div>
                <div class="flex gap-2">
                    <button onclick="toggleSidePanel()" class="text-xs border px-2 py-1 rounded hover:bg-gray-100">记忆</button>
                    <button onclick="manualGraduate(${rtId})" class="text-xs bg-yellow-100 text-yellow-700 px-2 py-1 rounded hover:bg-yellow-200">毕业</button>
                    <button onclick="closeRoundtable(${rtId})" class="text-xs bg-red-100 text-red-600 px-2 py-1 rounded hover:bg-red-200">结束</button>
                    <button onclick="closeRoundtableModal()" class="text-gray-400 hover:text-gray-600 text-xl">&times;</button>
                </div>
            </div>

            <div class="flex flex-1 overflow-hidden">
                <div id="rt-seats-panel" class="w-44 border-r bg-gray-50 p-2 overflow-y-auto flex flex-col gap-1 shrink-0"></div>
                <div id="rt-messages" class="flex-1 overflow-y-auto p-4 space-y-2">
                    <div class="text-center text-gray-400 py-8">圆桌已启动，输入问题开始讨论</div>
                </div>
                <div id="rt-side-panel" class="w-52 border-l bg-gray-50 p-2 overflow-y-auto text-xs hidden shrink-0">
                    <div class="font-semibold mb-2">团队记忆</div>
                    <div id="rt-memory-content" class="text-gray-500">加载中...</div>
                </div>
            </div>

            <div class="rt-footer p-3 border-t flex flex-col gap-2 shrink-0 bg-white">
                <div class="flex gap-2 flex-wrap text-xs" id="rt-mention-checkboxes">
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="all" class="rt-mention-cb" checked> 全部</label>
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="gen" class="rt-mention-cb"> 生成者</label>
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="rev1" class="rt-mention-cb"> 审查α</label>
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="rev2" class="rt-mention-cb"> 审查β</label>
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="rev3" class="rt-mention-cb"> 审查γ</label>
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="arb1" class="rt-mention-cb"> 仲裁α</label>
                    <label class="flex items-center gap-1 cursor-pointer"><input type="checkbox" value="arb2" class="rt-mention-cb"> 仲裁β</label>
                </div>
                <div class="flex gap-2">
                <input id="rt-question-input" type="text" placeholder="输入你的问题..." class="flex-1 border rounded px-3 py-2 text-sm" onkeydown="if(event.key==='Enter')askRoundtable(${rtId})">
                <button id="rt-send-btn" onclick="askRoundtable(${rtId})" class="bg-indigo-600 text-white px-4 py-2 rounded text-sm hover:bg-indigo-700">提问</button>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    window._rtId = rtId;
    window._rtSparkId = sparkId;
    window._rtLastMsgCount = 0;
    window._rtPollInterval = setInterval(() => pollRoundtableState(rtId), 5000);
    pollRoundtableState(rtId);
}

function closeRoundtableModal() {
    if (window._rtPollInterval) { clearInterval(window._rtPollInterval); window._rtPollInterval = null; }
    const modal = document.getElementById('roundtable-modal');
    if (modal) modal.remove();
    window._rtId = null;
    window._rtSparkId = null;
    window._rtLastMsgCount = 0;
}

const SEAT_COLORS = {gen:'#10b981',rev1:'#3b82f6',rev2:'#f59e0b',rev3:'#8b5cf6',arb1:'#ef4444',arb2:'#ec4899'};
const SEAT_NAMES = {gen:'生成者',rev1:'审查α',rev2:'审查β',rev3:'审查γ',arb1:'仲裁α',arb2:'仲裁β'};

function renderSeatPanel(seats) {
    const panel = document.getElementById('rt-seats-panel');
    if (!panel) return;
    panel.innerHTML = seats.map(s => {
        const color = SEAT_COLORS[s.seat_id] || '#6b7280';
        const name = SEAT_NAMES[s.seat_id] || s.seat_id;
        const pct = s.quota > 0 ? Math.max(0, Math.round(s.remaining / s.quota * 100)) : 0;
        const barColor = pct > 50 ? '#10b981' : pct > 20 ? '#f59e0b' : '#ef4444';
        const stateIcon = s.state === 'exited' ? '✕' : s.state === 'online' ? '●' : '○';
        return `<div class="rt-seat-card p-2 rounded border text-xs bg-white" style="border-left:3px solid ${color}" id="rt-seat-${s.seat_id}">
            <div class="flex justify-between items-center">
                <span class="font-semibold" style="color:${color}">${name}</span>
                <span class="${s.state === 'exited' ? 'text-red-400' : 'text-green-400'} text-xs">${stateIcon}</span>
            </div>
            <div class="text-gray-400">${escapeHtml(s.seat_id)}</div>
            <div class="rt-quota-bar h-1 bg-gray-200 rounded mt-1"><div class="h-full rounded transition-all duration-300" style="width:${pct}%;background:${barColor}"></div></div>
            <div class="text-gray-400 mt-0.5">配额 ${pct}%</div>
        </div>`;
    }).join('');
}

function renderRoundMessages(messages) {
    const msgArea = document.getElementById('rt-messages');
    if (!msgArea) return;
    const opt = document.getElementById('rt-optimistic-msg');
    if (opt) opt.remove();

    const NAMES = SEAT_NAMES;
    const COLS = SEAT_COLORS;

    msgArea.innerHTML = messages.map((m, i) => {
        const content = escapeHtml(m.content || '');
        const sender = m.sender_name || '?';
        const name = NAMES[sender] || sender;
        const color = COLS[sender] || '#6b7280';

        // 分歧报告
        if (m.message_type === 'divergence_report') {
            if (!m.content || m.content === '分歧分析：\n\n\n\n主要分歧点：') return '';
            return `<div style="text-align:center;margin:8px 0"><div style="display:inline-block;background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:8px 16px;font-size:12px;color:#92400e;max-width:500px">${content}</div></div>`;
        }
        // 插话
        if (m.message_type === 'interjection') {
            return `<div style="text-align:center;margin:4px 0"><span style="color:#9ca3af;font-size:12px;font-style:italic">&#x1F4AC; ${escapeHtml(name)}：${content}</span></div>`;
        }
        // 系统消息
        if (m.sender_type === 'system') {
            return `<div style="text-align:center;color:#9ca3af;font-size:12px;padding:8px 0">${content}</div>`;
        }
        // 用户消息
        if (m.sender_type === 'user' || sender === 'user') {
            return `<div style="display:flex;justify-content:flex-end;margin-bottom:12px">
                <div style="max-width:85%;background:#6366f1;color:#fff;border-radius:16px 16px 4px 16px;padding:10px 16px;font-size:14px;line-height:1.6;box-shadow:0 1px 3px rgba(0,0,0,0.1)">${content}</div>
            </div>`;
        }
        // 模型消息：头像 + 气泡
        const prev = i > 0 ? messages[i-1] : null;
        const isConsecutive = prev && prev.sender_name === sender
            && prev.message_type !== 'interjection'
            && prev.message_type !== 'divergence_report'
            && prev.sender_type !== 'system';
        if (isConsecutive) {
            return `<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:12px">
                <div style="width:32px;flex-shrink:0"></div>
                <div style="min-width:0;max-width:85%">
                    <div style="background:#fff;border-radius:4px 16px 16px 16px;padding:10px 16px;font-size:14px;line-height:1.6;box-shadow:0 1px 3px rgba(0,0,0,0.06);border:1px solid #f3f4f6;word-break:break-word">${content}</div>
                </div>
            </div>`;
        }
        return `<div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:12px">
            <div style="width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:700;background:${color}">${escapeHtml(name[0])}</div>
            <div style="min-width:0;max-width:85%">
                <div style="font-size:12px;font-weight:600;margin-bottom:2px;color:${color}">${escapeHtml(name)}</div>
                <div style="background:#fff;border-radius:4px 16px 16px 16px;padding:10px 16px;font-size:14px;line-height:1.6;box-shadow:0 1px 3px rgba(0,0,0,0.06);border:1px solid #f3f4f6;word-break:break-word">${content}</div>
            </div>
        </div>`;
    }).join('');
    msgArea.scrollTop = msgArea.scrollHeight;
}

async function pollRoundtableState(rtId) {
    try {
        const resp = await fetch(`/ideator/api/roundtables/${rtId}`);
        if (!resp.ok) {
            if (resp.status === 404) {
                clearInterval(window._rtPollInterval);
                const msgArea = document.getElementById('rt-messages');
                if (msgArea) msgArea.innerHTML = '<div style="text-align:center;color:#9ca3af;padding:32px 0;font-size:14px">圆桌已结束</div>';
            }
            return;
        }
        const data = await resp.json();
        if (data.error) return;

        const msgCount = data.messages ? data.messages.length : 0;
        const lastCount = window._rtLastMsgCount || 0;

        document.getElementById('rt-round-num').textContent = `轮次 ${data.round_number || 0}`;

        if (data.watermark) {
            const hp = data.watermark.hot_pct || 0;
            const wp = data.watermark.warm_pct || 0;
            document.getElementById('rt-hot-bar').style.width = Math.min(hp, 100) + '%';
            document.getElementById('rt-warm-bar').style.width = Math.min(wp, 100) + '%';
            document.getElementById('rt-hot-label').textContent = `hot ${Number(hp).toFixed(0)}%`;
            document.getElementById('rt-warm-label').textContent = `warm ${Number(wp).toFixed(0)}%`;
        }

        if (data.seats) renderSeatPanel(data.seats);
        if (data.messages && msgCount > lastCount) {
            renderRoundMessages(data.messages);
            window._rtLastMsgCount = msgCount;
        }
    } catch (e) {
        // silent poll failure
    }
}

async function askRoundtable(rtId) {
    const input = document.getElementById('rt-question-input');
    const sendBtn = document.getElementById('rt-send-btn');
    const question = input.value.trim();
    if (!question) return;

    input.disabled = true;
    sendBtn.disabled = true;
    sendBtn.textContent = '等待中...';

    const msgArea = document.getElementById('rt-messages');
    // 乐观显示用户消息，等轮询拿到服务端确认后完整渲染替换
    const optDiv = document.createElement('div');
    optDiv.style.cssText = 'display:flex;justify-content:flex-end';
    optDiv.id = 'rt-optimistic-msg';
    optDiv.innerHTML = `<div style="max-width:85%;background:#6366f1;color:#fff;border-radius:16px 16px 4px 16px;padding:10px 16px;font-size:14px;line-height:1.6;box-shadow:0 1px 3px rgba(0,0,0,0.1);margin-left:auto">${escapeHtml(question)}</div>`;
    msgArea.appendChild(optDiv);
    msgArea.scrollTop = msgArea.scrollHeight;

    const cbs = document.querySelectorAll('.rt-mention-cb:checked');
    const mentioned = Array.from(cbs).map(cb => cb.value);

    try {
        const resp = await fetch(`/ideator/api/roundtables/${rtId}/ask`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question, mentioned}),
        });
        const data = await resp.json();
        document.getElementById('rt-round-num').textContent = `轮次 ${data.round_number || '?'}`;
    } catch (e) {
        msgArea.innerHTML += '<div style="text-align:center;color:#ef4444;padding:8px 0;font-size:14px">请求失败，请重试</div>';
    } finally {
        input.disabled = false;
        input.value = '';
        sendBtn.disabled = false;
        sendBtn.textContent = '提问';
        input.focus();
        // 强制下轮轮询重新渲染全部消息
        window._rtLastMsgCount = 0;
        pollRoundtableState(rtId);
    }
}

async function closeRoundtable(rtId) {
    try {
        await fetch(`/ideator/api/roundtables/${rtId}/close`, {method: 'POST'});
    } catch (e) {}
    closeRoundtableModal();
}

async function manualGraduate(rtId) {
    try {
        const resp = await fetch(`/ideator/api/roundtables/${rtId}/graduate`, {method: 'POST'});
        const data = await resp.json();
        if (data.verdict) {
            const msgArea = document.getElementById('rt-messages');
            msgArea.innerHTML += `<div style="text-align:center;color:#d97706;font-size:12px;padding:4px 0">毕业决策: ${escapeHtml(data.verdict)} — ${escapeHtml(data.warm_summary || '')}</div>`;
            msgArea.scrollTop = msgArea.scrollHeight;
        }
    } catch (e) {}
}

async function supplementContext(rtId) {
    const content = prompt('输入补充资料内容：');
    if (!content) return;
    try {
        await fetch(`/ideator/api/roundtables/${rtId}/supplement`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({seat_id: 'system', content}),
        });
    } catch (e) {}
}

async function toggleSidePanel() {
    const panel = document.getElementById('rt-side-panel');
    if (!panel) return;
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden') && window._rtSparkId) {
        try {
            const resp = await fetch(`/ideator/api/roundtables/${window._rtId}/memory`);
            const memories = await resp.json();
            const content = document.getElementById('rt-memory-content');
            if (Array.isArray(memories) && memories.length > 0) {
                content.innerHTML = memories.map(m =>
                    `<div class="mb-2 p-2 bg-white rounded border text-xs">
                        <span class="font-semibold text-indigo-600">${escapeHtml(m.memory_type || '?')}</span>
                        <div class="mt-1">${escapeHtml((m.content || '').substring(0, 200))}</div>
                    </div>`
                ).join('');
            } else {
                content.textContent = '暂无团队记忆';
            }
        } catch (e) {
            document.getElementById('rt-memory-content').textContent = '加载失败';
        }
    }
}

function openDirectRoundtable() {
    const existing = document.getElementById('direct-rt-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'direct-rt-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50';
    modal.innerHTML = `
        <div class="bg-white rounded-lg shadow-xl w-full max-w-lg mx-4 p-6">
            <h2 class="text-lg font-semibold text-purple-700 mb-4">直接发起圆桌</h2>
            <p class="text-sm text-gray-500 mb-3">输入你的研究内容，6 个模型坐席将围绕它展开讨论。你会以普通参与者身份加入对话。</p>
            <textarea id="direct-rt-content" class="w-full border rounded p-3 text-sm h-40 resize-y" placeholder="在此粘贴你的研究想法、假说或实验设计..."></textarea>
            <div class="flex justify-end gap-2 mt-4">
                <button onclick="document.getElementById('direct-rt-modal').remove()" class="text-sm border px-4 py-1.5 rounded hover:bg-gray-100">取消</button>
                <button id="direct-rt-submit" onclick="startDirectRoundtable()" class="text-sm bg-purple-600 text-white px-4 py-1.5 rounded hover:bg-purple-700">发起</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function startDirectRoundtable() {
    const content = document.getElementById('direct-rt-content').value.trim();
    if (!content) { alert('请输入研究内容'); return; }

    const btn = document.getElementById('direct-rt-submit');
    btn.disabled = true;
    btn.textContent = '创建中...';

    try {
        const resp = await fetch('/ideator/api/roundtable/direct', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content}),
        });
        const data = await resp.json();
        if (data.roundtable_id) {
            document.getElementById('direct-rt-modal').remove();
            window.location.href = `/ideator/roundtable/${data.roundtable_id}`;
        } else {
            alert('创建失败：' + (data.error || '未知错误'));
        }
    } catch (e) {
        console.error('Direct roundtable failed', e);
        alert('网络错误，请稍后重试');
    } finally {
        btn.disabled = false;
        btn.textContent = '发起';
    }
}

async function triggerMine() {
    const btn = event.target;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '挖掘中...';
    try {
        const fd = new FormData();
        fd.append('scope', 'all');
        const resp = await fetch('/ideator/api/mine', { method: 'POST', body: fd });
        const data = await resp.json();
        if (data.spark_ids && data.spark_ids.length > 0) {
            alert(`挖掘完成：生成 ${data.count} 个火花`);
        } else if (data.diag) {
            const d = data.diag;
            let msg = '本轮未产生新火花\n\n诊断信息：\n';
            if (d.pre_check) {
                msg += `预检: insights=${d.pre_check.core_insights}, notes=${d.pre_check.legacy_notes}, papers=${d.pre_check.papers_with_notes}, embedding=${d.pre_check.embedding_ok ? '正常' : '不可用'}\n`;
            }
            msg += `Effort: ${d.effort || '?'}\n`;
            if (d.stages) {
                for (const [stage, info] of Object.entries(d.stages)) {
                    msg += `${stage}: ${JSON.stringify(info)}\n`;
                }
            }
            if (d.error) msg += `错误: ${d.error}\n`;
            alert(msg);
        } else if (data.error) {
            alert('挖掘失败：' + data.error);
        }
        loadSparks();
    } catch (e) {
        console.error('Mine failed', e);
        alert('挖掘请求失败，请检查后端服务是否正常运行');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

if (document.getElementById('spark-list')) loadSparks();
