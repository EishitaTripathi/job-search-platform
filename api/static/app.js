// Job Search Intelligence Platform — Dashboard JS

const API = '';

async function api(path, options = {}) {
    const resp = await fetch(API + path, {
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
    });
    if (resp.status === 401) {
        document.getElementById('main-view').classList.add('hidden');
        document.getElementById('login-view').classList.remove('hidden');
        throw new Error('Not authenticated');
    }
    if (!resp.ok) throw new Error(await resp.text());
    return resp.json();
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

async function login() {
    const pw = document.getElementById('login-password').value;
    try {
        await api('/login', { method: 'POST', body: JSON.stringify({ password: pw }) });
        document.getElementById('login-view').classList.add('hidden');
        document.getElementById('main-view').classList.remove('hidden');
        loadJobs();
        loadResumes();
    } catch (e) {
        document.getElementById('login-error').textContent = 'Invalid password';
    }
}

document.getElementById('login-password').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') login();
});

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

function showTab(tab) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
    document.getElementById('tab-' + tab).classList.remove('hidden');
    document.querySelectorAll('.nav-links a').forEach(el => {
        el.classList.toggle('active', el.dataset.tab === tab);
    });

    if (tab === 'jobs') loadJobs();
    else if (tab === 'followups') loadFollowups();
    else if (tab === 'deadlines') loadDeadlines();
    else if (tab === 'chat') loadChatJobSelect();
    else if (tab === 'ops') { loadOpsMetrics(); loadRuns(); }
    else if (tab === 'resumes') loadResumes();
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

async function loadJobs() {
    const sort = document.getElementById('job-sort').value;
    const status = document.getElementById('job-status').value;
    const resumeSelect = document.getElementById('job-resume');

    // Show resume picker when sorting by match
    if (sort === 'match') {
        resumeSelect.classList.remove('hidden');
        if (resumeSelect.options.length <= 1) await populateResumeSelect();
    } else {
        resumeSelect.classList.add('hidden');
    }

    const postedAfter = document.getElementById('job-posted-after').value;

    let url = `/api/jobs?sort=${sort}`;
    if (status) url += `&status=${status}`;
    if (postedAfter) url += `&posted_after=${postedAfter}`;
    if (sort === 'match' && resumeSelect.value) url += `&resume_id=${resumeSelect.value}`;

    const jobs = await api(url);
    const list = document.getElementById('jobs-list');

    if (!jobs.length) {
        list.innerHTML = '<p class="card-meta" style="padding:16px">No jobs found.</p>';
        return;
    }

    list.innerHTML = jobs.map(j => `
        <div class="card" onclick="showJobDetail(${j.id})">
            <div class="card-header">
                <span class="card-title">${esc(j.company)} — ${esc(j.role)}</span>
                <div class="badge-group">
                    <span class="badge badge-${j.status}">${formatStatus(j.status)}</span>
                    ${j.referral_status && j.referral_status !== 'none' ? `<span class="badge badge-referral-${j.referral_status}">${esc(j.referral_status)}</span>` : ''}
                </div>
            </div>
            <div class="card-body">
                ${j.location ? `<span class="card-meta">${esc(j.location)}</span>` : ''}
                ${j.overall_fit_score != null ? `<span class="score">${(j.overall_fit_score * 100).toFixed(0)}%</span>` : ''}
                ${j.match_score != null ? `<span class="card-meta">Best match: ${(j.match_score * 100).toFixed(0)}%</span>` : ''}
                <span class="card-meta">${j.date_posted ? new Date(j.date_posted).toLocaleDateString() : ''}</span>
                ${j.ats_url ? `<a href="${esc(j.ats_url)}" target="_blank" class="apply-link" onclick="event.stopPropagation()">Apply</a>` : ''}
            </div>
        </div>
    `).join('');
}

function formatStatus(status) {
    if (!status) return '';
    return status.replace(/_/g, ' ');
}

async function showJobDetail(jobId) {
    const data = await api(`/api/jobs/${jobId}`);
    const j = data.job;
    const a = data.analysis;

    let html = `
        <div class="detail-section">
            <h3>Job</h3>
            <p class="card-title">${esc(j.company)} — ${esc(j.role)}</p>
            <p class="card-meta">${j.location || ''} | ${j.source} | ${formatStatus(j.status)}</p>
            ${j.link ? `<p><a href="${esc(j.link)}" target="_blank" style="color:#58a6ff">View listing</a></p>` : ''}
            ${j.ats_url ? `<p><a href="${esc(j.ats_url)}" target="_blank" style="color:#3fb950">Apply on ATS</a></p>` : ''}
        </div>
    `;

    // Referral status section
    html += `
        <div class="detail-section">
            <h3>Referral Status</h3>
            <div style="display:flex;align-items:center;gap:12px">
                <select id="modal-referral-status" onchange="updateReferralStatus(${j.id}, this.value)">
                    <option value="none" ${j.referral_status === 'none' || !j.referral_status ? 'selected' : ''}>None</option>
                    <option value="seeking" ${j.referral_status === 'seeking' ? 'selected' : ''}>Seeking</option>
                    <option value="submitted" ${j.referral_status === 'submitted' ? 'selected' : ''}>Submitted</option>
                    <option value="confirmed" ${j.referral_status === 'confirmed' ? 'selected' : ''}>Confirmed</option>
                </select>
                ${j.referral_status && j.referral_status !== 'none' ? `<span class="badge badge-referral-${j.referral_status}">${esc(j.referral_status)}</span>` : ''}
            </div>
        </div>
    `;

    if (a) {
        html += `
            <div class="detail-section">
                <h3>JD Analysis</h3>
                <p><strong>Role type:</strong> ${a.role_type || 'N/A'}</p>
                <p><strong>Required:</strong> ${(a.required_skills || []).map(s => `<span class="skill-tag">${esc(s)}</span>`).join(' ')}</p>
                <p><strong>Preferred:</strong> ${(a.preferred_skills || []).map(s => `<span class="skill-tag">${esc(s)}</span>`).join(' ')}</p>
                <p><strong>Stack:</strong> ${(a.tech_stack || []).map(s => `<span class="skill-tag">${esc(s)}</span>`).join(' ')}</p>
                ${(a.deal_breakers || []).length ? `<p><strong>Deal breakers:</strong> ${a.deal_breakers.map(s => `<span class="skill-tag skill-gap">${esc(s)}</span>`).join(' ')}</p>` : ''}
            </div>
        `;
    }

    if (data.match_reports.length) {
        html += `<div class="detail-section"><h3>Match Reports</h3>`;
        for (const m of data.match_reports) {
            html += `
                <div class="card" style="cursor:default">
                    <div class="card-header">
                        <span class="card-title">${esc(m.resume_name)}</span>
                        <span class="badge badge-${m.fit_category}">${m.fit_category}</span>
                    </div>
                    <div class="card-body">
                        <span class="score">${(m.overall_fit_score * 100).toFixed(0)}%</span>
                        <p>${esc(m.reasoning || '')}</p>
                        ${(m.skill_gaps || []).length ? `<p>Gaps: ${m.skill_gaps.map(s => `<span class="skill-tag skill-gap">${esc(s)}</span>`).join(' ')}</p>` : ''}
                    </div>
                </div>
            `;
        }
        html += `</div>`;
    }

    // Deadlines section in modal
    html += `<div class="detail-section" id="modal-deadlines-${j.id}"><h3>Deadlines</h3><p class="card-meta">Loading...</p></div>`;

    if (data.followups.length) {
        html += `<div class="detail-section"><h3>Follow-ups</h3>`;
        for (const f of data.followups) {
            html += `
                <div class="card" style="cursor:default">
                    <div class="card-header">
                        <span class="badge badge-${f.urgency_level}">${f.urgency_level}</span>
                        <span class="card-meta">${new Date(f.created_at).toLocaleDateString()}</span>
                    </div>
                    <div class="card-body">
                        <p>${esc(f.recommended_action)}</p>
                        <p class="card-meta">${esc(f.urgency_reasoning || '')}</p>
                    </div>
                </div>
            `;
        }
        html += `</div>`;
    }

    // Inline chat section in modal
    html += `
        <div class="detail-section">
            <h3>Chat</h3>
            <div class="chat-container" id="modal-chat-messages-${j.id}"></div>
            <div class="chat-input-area">
                <input type="text" class="chat-input" id="modal-chat-input-${j.id}" placeholder="Ask about this job..." onkeydown="if(event.key==='Enter')sendModalChat(${j.id})">
                <button onclick="sendModalChat(${j.id})">Send</button>
            </div>
        </div>
    `;

    document.getElementById('job-detail').innerHTML = html;
    document.getElementById('job-modal').classList.remove('hidden');

    // Load deadlines for this job
    loadModalDeadlines(jobId);
}

async function loadModalDeadlines(jobId) {
    try {
        const deadlines = await api('/api/deadlines');
        const jobDeadlines = deadlines.filter(d => d.job_id === jobId);
        const container = document.getElementById('modal-deadlines-' + jobId);
        if (!container) return;

        if (!jobDeadlines.length) {
            container.innerHTML = '<h3>Deadlines</h3><p class="card-meta">No upcoming deadlines.</p>';
            return;
        }

        container.innerHTML = '<h3>Deadlines</h3>' + jobDeadlines.map(d => {
            const urgencyClass = getDeadlineUrgency(d.deadline_date);
            return `<div class="deadline-card ${urgencyClass}">
                <span class="badge ${urgencyClass}">${urgencyClass === 'deadline-urgent' ? 'Urgent' : urgencyClass === 'deadline-warning' ? 'Soon' : 'Upcoming'}</span>
                <span>${new Date(d.deadline_date).toLocaleDateString()}</span>
                <span class="card-meta">${esc(d.deadline_text || '')}</span>
            </div>`;
        }).join('');
    } catch (e) {
        // Ignore deadline loading errors in modal
    }
}

async function sendModalChat(jobId) {
    const input = document.getElementById('modal-chat-input-' + jobId);
    const messagesEl = document.getElementById('modal-chat-messages-' + jobId);
    const question = input.value.trim();
    if (!question) return;

    // Show user message
    messagesEl.innerHTML += `<div class="chat-bubble-user">${esc(question)}</div>`;
    input.value = '';
    messagesEl.scrollTop = messagesEl.scrollHeight;

    // Show loading
    const loadingId = 'loading-' + Date.now();
    messagesEl.innerHTML += `<div class="chat-bubble-bot" id="${loadingId}">Thinking...</div>`;
    messagesEl.scrollTop = messagesEl.scrollHeight;

    try {
        const result = await api('/api/chat', {
            method: 'POST',
            body: JSON.stringify({ job_id: jobId, question }),
        });
        document.getElementById(loadingId).textContent = result.answer;
    } catch (e) {
        document.getElementById(loadingId).textContent = 'Error: ' + e.message;
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function updateReferralStatus(jobId, status) {
    try {
        await api(`/api/jobs/${jobId}`, {
            method: 'PATCH',
            body: JSON.stringify({ status: undefined, referral_status: status }),
        });
    } catch (e) {
        // Silently fail — the PATCH endpoint may not support referral_status yet
    }
}

function closeModal() {
    document.getElementById('job-modal').classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Follow-ups
// ---------------------------------------------------------------------------

async function loadFollowups() {
    const urgency = document.getElementById('followup-urgency').value;
    let url = '/api/followups';
    if (urgency) url += `?urgency=${urgency}`;

    const items = await api(url);
    const list = document.getElementById('followups-list');

    if (!items.length) {
        list.innerHTML = '<p class="card-meta" style="padding:16px">No pending follow-ups.</p>';
        return;
    }

    list.innerHTML = items.map(f => `
        <div class="card">
            <div class="card-header">
                <span class="card-title">${esc(f.company)} — ${esc(f.role)}</span>
                <span class="badge badge-${f.urgency_level}">${f.urgency_level}</span>
            </div>
            <div class="card-body">
                <p>${esc(f.recommended_action)}</p>
                <p class="card-meta">${esc(f.urgency_reasoning || '')}</p>
                <button onclick="markActed(${f.id})" style="margin-top:8px;padding:4px 12px;background:#238636;color:white;border:none;border-radius:4px;cursor:pointer">Done</button>
            </div>
        </div>
    `).join('');
}

async function markActed(id) {
    await api(`/api/followups/${id}/act`, { method: 'POST' });
    loadFollowups();
}

// ---------------------------------------------------------------------------
// Deadlines
// ---------------------------------------------------------------------------

function getDeadlineUrgency(deadlineDate) {
    const now = new Date();
    const dl = new Date(deadlineDate);
    const hoursUntil = (dl - now) / (1000 * 60 * 60);
    if (hoursUntil < 24) return 'deadline-urgent';
    if (hoursUntil < 72) return 'deadline-warning';
    return 'deadline-ok';
}

async function loadDeadlines() {
    const items = await api('/api/deadlines');
    const list = document.getElementById('deadlines-list');

    if (!items.length) {
        list.innerHTML = '<p class="card-meta" style="padding:16px">No upcoming deadlines.</p>';
        return;
    }

    list.innerHTML = items.map(d => {
        const urgencyClass = getDeadlineUrgency(d.deadline_date);
        const urgencyLabel = urgencyClass === 'deadline-urgent' ? 'Urgent' : urgencyClass === 'deadline-warning' ? 'Soon' : 'Upcoming';
        return `
            <div class="deadline-card ${urgencyClass}">
                <div class="card-header">
                    <span class="card-title">${esc(d.company)} — ${esc(d.role)}</span>
                    <span class="badge ${urgencyClass}">${urgencyLabel}</span>
                </div>
                <div class="card-body">
                    <p><strong>Deadline:</strong> ${new Date(d.deadline_date).toLocaleDateString()}</p>
                    <p class="card-meta">${esc(d.deadline_text || '')}</p>
                    <p class="card-meta">Job status: ${formatStatus(d.job_status)}</p>
                </div>
            </div>
        `;
    }).join('');
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

async function loadChatJobSelect() {
    const jobs = await api('/api/jobs?sort=date');
    const sel = document.getElementById('chat-job-select');
    sel.innerHTML = '<option value="">Select a job...</option>' +
        jobs.map(j => `<option value="${j.id}">${esc(j.company)} — ${esc(j.role)}</option>`).join('');

    sel.onchange = function () {
        const input = document.getElementById('chat-input');
        const btn = document.getElementById('chat-send-btn');
        if (this.value) {
            input.disabled = false;
            btn.disabled = false;
            document.getElementById('chat-messages').innerHTML = '';
        } else {
            input.disabled = true;
            btn.disabled = true;
            document.getElementById('chat-messages').innerHTML = '<p class="card-meta" style="padding:16px">Select a job to start chatting.</p>';
        }
    };
}

function handleChatSend() {
    const jobId = document.getElementById('chat-job-select').value;
    const input = document.getElementById('chat-input');
    const question = input.value.trim();
    if (!jobId || !question) return;
    sendChatMessage(parseInt(jobId), question);
}

async function sendChatMessage(jobId, question) {
    const messagesEl = document.getElementById('chat-messages');
    const input = document.getElementById('chat-input');

    // Show user message
    messagesEl.innerHTML += `<div class="chat-bubble-user">${esc(question)}</div>`;
    input.value = '';
    messagesEl.scrollTop = messagesEl.scrollHeight;

    // Show loading indicator
    const loadingId = 'loading-' + Date.now();
    messagesEl.innerHTML += `<div class="chat-bubble-bot" id="${loadingId}">Thinking...</div>`;
    messagesEl.scrollTop = messagesEl.scrollHeight;

    try {
        const result = await api('/api/chat', {
            method: 'POST',
            body: JSON.stringify({ job_id: jobId, question }),
        });
        document.getElementById(loadingId).textContent = result.answer;
    } catch (e) {
        document.getElementById(loadingId).textContent = 'Error: ' + e.message;
    }
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') handleChatSend();
});

// ---------------------------------------------------------------------------
// Pipeline Ops
// ---------------------------------------------------------------------------

async function loadOpsMetrics() {
    const metrics = await api('/api/ops/metrics');
    const container = document.getElementById('ops-metrics');

    if (!metrics.length) {
        container.innerHTML = '<p class="card-meta" style="padding:16px">No metrics recorded yet.</p>';
        return;
    }

    // Group by source
    const grouped = {};
    for (const m of metrics) {
        if (!grouped[m.source]) grouped[m.source] = [];
        grouped[m.source].push(m);
    }

    let html = '';
    for (const [source, items] of Object.entries(grouped)) {
        html += `<div class="metrics-source"><h4>${esc(source)}</h4><div class="metrics-row">`;
        for (const item of items) {
            html += `
                <div class="metric-card">
                    <span class="metric-count">${item.count}</span>
                    <span class="metric-label">${esc(item.metric_name)}</span>
                    <span class="card-meta">${item.last_recorded ? new Date(item.last_recorded).toLocaleString() : ''}</span>
                </div>
            `;
        }
        html += '</div></div>';
    }

    container.innerHTML = html;
}

async function loadRuns() {
    const runs = await api('/api/runs?limit=20');
    const tbody = document.getElementById('runs-tbody');

    if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="card-meta" style="padding:12px">No runs recorded yet.</td></tr>';
        return;
    }

    tbody.innerHTML = runs.map(r => {
        const duration = r.duration_seconds != null ? `${r.duration_seconds.toFixed(1)}s` : (r.duration || '—');
        const statusClass = r.status === 'completed' ? 'badge-offer' : r.status === 'failed' ? 'badge-rejected' : 'badge-to_apply';
        return `
            <tr>
                <td>${esc(r.event_type || '')}</td>
                <td>${esc(r.agent_chain || '')}</td>
                <td><span class="badge ${statusClass}">${esc(r.status || '')}</span></td>
                <td>${r.started_at ? new Date(r.started_at).toLocaleString() : ''}</td>
                <td>${duration}</td>
            </tr>
        `;
    }).join('');
}

// ---------------------------------------------------------------------------
// Resumes
// ---------------------------------------------------------------------------

async function loadResumes() {
    const resumes = await api('/api/resumes');
    const list = document.getElementById('resumes-list');

    if (!resumes.length) {
        list.innerHTML = '<p class="card-meta" style="padding:16px">No resumes uploaded.</p>';
        return;
    }

    list.innerHTML = resumes.map(r => `
        <div class="card" style="cursor:default">
            <div class="card-header">
                <span class="card-title">${esc(r.name)}</span>
                <span class="card-meta">${new Date(r.uploaded_at).toLocaleDateString()}</span>
            </div>
        </div>
    `).join('');

    await populateResumeSelect();
}

async function populateResumeSelect() {
    const resumes = await api('/api/resumes');
    const sel = document.getElementById('job-resume');
    sel.innerHTML = '<option value="">Select resume</option>' +
        resumes.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
}

async function uploadResume() {
    const isLocal = ['localhost', '127.0.0.1'].includes(location.hostname);
    if (!isLocal) {
        alert('Resume upload is only available locally (PII stays on your machine).');
        return;
    }

    const fileInput = document.getElementById('resume-file');
    const nameInput = document.getElementById('resume-name');

    if (!fileInput.files.length) return;

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);

    const name = nameInput.value || 'Default Resume';
    const resp = await fetch(`http://localhost:8001/upload?name=${encodeURIComponent(name)}`, {
        method: 'POST',
        body: formData,
    });

    if (resp.ok) {
        fileInput.value = '';
        nameInput.value = '';
        loadResumes();
    }
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function esc(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Blocklist
// ---------------------------------------------------------------------------

function toggleBlocklist() {
    document.getElementById('blocklist-panel').classList.toggle('hidden');
}

async function loadBlocklist() {
    try {
        const bl = await api('/api/blocklist');
        document.getElementById('blocklist-companies').value = bl.companies || '';
        document.getElementById('blocklist-titles').value = bl.titles || '';
    } catch (e) { /* ignore if not loaded yet */ }
}

async function saveBlocklist() {
    const companies = document.getElementById('blocklist-companies').value;
    const titles = document.getElementById('blocklist-titles').value;
    await api('/api/blocklist', {
        method: 'POST',
        body: JSON.stringify({ companies, titles }),
    });
    loadJobs();
}

// Auto-login check on load
(async () => {
    try {
        await api('/api/jobs?sort=date');
        document.getElementById('login-view').classList.add('hidden');
        document.getElementById('main-view').classList.remove('hidden');
        loadJobs();
        loadResumes();
        loadBlocklist();
    } catch (e) {
        // Not logged in, show login view
    }
})();
