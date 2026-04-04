// System Debug Dashboard V2 — Readable graph with real data on nodes
// Datadog-style: nodes show key metrics, click opens detail drawer

var cy = null;
var healthData = {};
var topologyData = {};
var summaryData = {};
var selectedNodeId = null;
var viewMode = 'all'; // 'all' or 'errors'

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(path) {
    try {
        var resp = await fetch(path, { credentials: 'include' });
        if (resp.status === 401) { window.location.href = '/static/index.html'; return null; }
        if (!resp.ok) throw new Error('API ' + resp.status);
        return resp.json();
    } catch (e) { console.error('API failed:', path, e); throw e; }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', init);

async function init() {
    try {
        // Fetch all data in parallel
        var results = await Promise.all([
            api('/api/debug/topology'),
            api('/api/debug/health'),
            api('/api/debug/summary'),
        ]);
        topologyData = results[0];
        healthData = results[1];
        summaryData = results[2];

        if (!topologyData || !healthData) return;

        renderSummaryBar();
        renderGraph();
        applyHealthData();
        updateTimestamp();
    } catch (e) {
        document.getElementById('graph-container').innerHTML =
            '<p class="error-message">Failed to load: ' + esc(e.message) + '</p>';
    }
}

// ---------------------------------------------------------------------------
// Summary Bar
// ---------------------------------------------------------------------------

function renderSummaryBar() {
    var bar = document.getElementById('summary-bar');
    if (!healthData || !healthData.components) { bar.innerHTML = '<span class="summary-loading">No health data</span>'; return; }

    var green = 0, yellow = 0, red = 0;
    Object.values(healthData.components).forEach(function (c) {
        if (c.status === 'green') green++;
        else if (c.status === 'yellow') yellow++;
        else red++;
    });

    var html = '';
    if (green > 0) html += '<span class="summary-badge summary-badge-green">' + green + ' Healthy</span>';
    if (yellow > 0) html += '<span class="summary-badge summary-badge-yellow">' + yellow + ' Degraded</span>';
    if (red > 0) html += '<span class="summary-badge summary-badge-red">' + red + ' Down</span>';

    if (summaryData) {
        html += '<span class="summary-sep">|</span>';
        if (summaryData.jobs !== undefined) html += '<span class="summary-stat"><strong>' + summaryData.jobs + '</strong> jobs</span>';
        if (summaryData.jd_analyses !== undefined) html += '<span class="summary-stat"><strong>' + summaryData.jd_analyses + '</strong> analyzed</span>';
        if (summaryData.match_reports !== undefined) html += '<span class="summary-stat"><strong>' + summaryData.match_reports + '</strong> matches</span>';

        html += '<span class="summary-sep">|</span>';
        if (summaryData.last_ingest) html += '<span class="summary-stat">Last ingest: <strong>' + timeAgo(summaryData.last_ingest) + '</strong></span>';
        if (summaryData.last_analysis) html += '<span class="summary-stat">Last analysis: <strong>' + timeAgo(summaryData.last_analysis) + '</strong></span>';
    }

    bar.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Graph
// ---------------------------------------------------------------------------

function renderGraph() {
    cy = cytoscape({
        container: document.getElementById('graph-container'),
        elements: buildElements(),
        style: buildStyles(),
        layout: { name: 'dagre', rankDir: 'LR', nodeSep: 35, rankSep: 55, padding: 25, fit: true, animate: false },
        userZoomingEnabled: true, userPanningEnabled: true,
        boxSelectionEnabled: false, minZoom: 0.2, maxZoom: 3,
    });

    var _lastClickTime = 0;
    cy.on('tap', 'node', function (evt) {
        var node = evt.target;
        if (node.isParent()) return;
        var now = Date.now();
        if (now - _lastClickTime < 500) return; // debounce
        _lastClickTime = now;
        onNodeClick(node.id());
    });

    cy.on('tap', function (evt) {
        if (evt.target === cy) closeDrawer();
    });
}

function buildElements() {
    var els = [];
    var groups = {};

    // Group nodes
    (topologyData.nodes || []).forEach(function (n) {
        var g = n.group || 'other';
        if (!groups[g]) {
            groups[g] = true;
            var label = g === 'local' ? 'LOCAL (Docker Compose) — PII Zone' : g === 'cloud' ? 'CLOUD (AWS) — Sanitized Data' : g;
            els.push({ data: { id: 'group-' + g, label: label, nodeType: 'group' }, classes: 'group-node' });
        }
    });

    // Component nodes — label is just the name, metric goes in data for styling
    (topologyData.nodes || []).forEach(function (n) {
        var g = n.group || 'other';
        var metric = getNodeMetric(n);
        els.push({
            data: {
                id: n.id,
                label: (n.label || n.id) + '\n' + metric,
                parent: 'group-' + g,
                category: n.category || 'infrastructure',
                nodeGroup: g,
                healthCheck: n.health_check || null,
            },
            classes: nodeClasses(n),
        });
    });

    // Edges
    (topologyData.edges || []).forEach(function (e, i) {
        var sg = findNodeGroup(e.source), tg = findNodeGroup(e.target);
        var cross = sg !== tg;
        els.push({
            data: { id: 'edge-' + i, source: e.source, target: e.target, label: e.label || '' },
            classes: cross ? 'cross-boundary' : '',
        });
    });

    return els;
}

function getNodeMetric(n) {
    // Get key_metric from health data if available
    if (!n.health_check || !healthData.components) return n.description || '';
    var comp = healthData.components[n.health_check];
    if (comp && comp.key_metric) return comp.key_metric;
    return n.description || '';
}

function nodeClasses(n) {
    var cls = ['cat-' + (n.category || 'infrastructure').replace(/_/g, '-'), 'grp-' + (n.group || 'other')];
    if (n.health_check) cls.push('has-health');
    return cls.join(' ');
}

function findNodeGroup(id) {
    var nodes = topologyData.nodes || [];
    for (var i = 0; i < nodes.length; i++) { if (nodes[i].id === id) return nodes[i].group || 'other'; }
    return 'other';
}

function findTopoNode(id) {
    var nodes = topologyData.nodes || [];
    for (var i = 0; i < nodes.length; i++) { if (nodes[i].id === id) return nodes[i]; }
    return null;
}

// ---------------------------------------------------------------------------
// Styles — readable nodes with health-tinted backgrounds
// ---------------------------------------------------------------------------

function buildStyles() {
    return [
        // Groups
        { selector: '.group-node', style: {
            'background-color': '#0d1117', 'background-opacity': 0.5,
            'border-width': 1, 'border-color': '#30363d', 'border-style': 'dashed',
            'label': 'data(label)', 'text-valign': 'top', 'text-halign': 'center',
            'font-size': 11, 'color': '#586069', 'text-margin-y': -6,
            'padding': 18, 'shape': 'roundrectangle',
        }},

        // Default nodes — larger, readable
        { selector: 'node[nodeType != "group"]', style: {
            'label': 'data(label)', 'text-wrap': 'wrap', 'text-max-width': 180,
            'text-valign': 'center', 'text-halign': 'center',
            'font-size': 11, 'color': '#e1e4e8',
            'background-color': '#1c2128', 'border-width': 2, 'border-color': '#484f58',
            'width': 200, 'height': 65, 'shape': 'roundrectangle',
            'text-outline-width': 0, 'min-zoomed-font-size': 7,
        }},

        // Category shapes
        { selector: '.cat-local-agent, .cat-cloud-agent', style: { 'background-color': '#1c2333' }},
        { selector: '.cat-infrastructure, .cat-data-store', style: { 'shape': 'roundrectangle' }},
        { selector: '.cat-scheduler', style: { 'shape': 'diamond', 'width': 110, 'height': 60, 'text-max-width': 100, 'font-size': 10 }},
        { selector: '.cat-serverless, .cat-boundary', style: { 'background-color': '#1c2128' }},

        // Local nodes — muted but readable
        { selector: '.grp-local', style: { 'background-color': '#141820', 'border-color': '#30363d', 'color': '#8b949e' }},

        // Health-tinted backgrounds (applied dynamically)
        { selector: '.health-green', style: { 'background-color': '#132d13', 'border-color': '#2ea043', 'border-width': 3 }},
        { selector: '.health-yellow', style: { 'background-color': '#2d2813', 'border-color': '#d29922', 'border-width': 3 }},
        { selector: '.health-red', style: { 'background-color': '#2d1313', 'border-color': '#f85149', 'border-width': 3 }},
        { selector: '.health-gray', style: { 'background-color': '#141820', 'border-color': '#30363d' }},

        // Selected — brighten border to white, keep health bg
        { selector: '.node-selected', style: { 'border-color': '#ffffff', 'border-width': 4, 'z-index': 10 }},

        // Edges
        { selector: 'edge', style: {
            'width': 1.5, 'line-color': '#30363d', 'target-arrow-color': '#30363d',
            'target-arrow-shape': 'triangle', 'arrow-scale': 0.8, 'curve-style': 'bezier',
            'label': 'data(label)', 'font-size': 9, 'color': '#586069',
            'text-rotation': 'autorotate', 'text-background-color': '#0d1117',
            'text-background-opacity': 0.9, 'text-background-padding': 2,
            'min-zoomed-font-size': 8,
        }},

        // Cross-boundary
        { selector: '.cross-boundary', style: {
            'line-style': 'dashed', 'line-dash-pattern': [6, 3],
            'line-color': '#58a6ff', 'target-arrow-color': '#58a6ff',
        }},

        // Highlight connected edges on selection
        { selector: '.edge-highlight', style: { 'width': 2.5, 'line-color': '#58a6ff', 'target-arrow-color': '#58a6ff' }},
    ];
}

// ---------------------------------------------------------------------------
// Health overlay — apply status colors + key_metric to nodes
// ---------------------------------------------------------------------------

function applyHealthData() {
    if (!cy || !topologyData.nodes || !healthData.components) return;

    topologyData.nodes.forEach(function (n) {
        var cyNode = cy.getElementById(n.id);
        if (!cyNode || cyNode.length === 0) return;

        cyNode.removeClass('health-green health-yellow health-red health-gray');

        if (!n.health_check) { cyNode.addClass('health-gray'); return; }

        var comp = healthData.components[n.health_check];
        if (!comp) { cyNode.addClass('health-gray'); return; }

        if (comp.status === 'green') cyNode.addClass('health-green');
        else if (comp.status === 'yellow') cyNode.addClass('health-yellow');
        else if (comp.status === 'red') cyNode.addClass('health-red');
        else cyNode.addClass('health-gray');

        // Update label with key_metric
        if (comp.key_metric) {
            cyNode.data('label', (n.label || n.id) + '\n' + comp.key_metric);
        }
    });
}

// ---------------------------------------------------------------------------
// Node click — open detail drawer
// ---------------------------------------------------------------------------

async function onNodeClick(nodeId) {
    selectedNodeId = nodeId;

    // Highlight
    if (cy) {
        cy.elements().removeClass('node-selected edge-highlight');
        var node = cy.getElementById(nodeId);
        if (node.length > 0) {
            node.addClass('node-selected');
            node.connectedEdges().addClass('edge-highlight');
        }
    }

    // Open drawer
    var drawer = document.getElementById('detail-drawer');
    drawer.classList.remove('hidden');
    // Cytoscape needs to know its container shrunk
    setTimeout(function () { if (cy) cy.resize(); }, 250);
    document.getElementById('drawer-title').textContent = 'Loading...';
    document.getElementById('drawer-content').innerHTML = '<div class="drawer-loading"><span class="loading-spinner"></span> Loading...</div>';

    try {
        var detail = await api('/api/debug/component/' + encodeURIComponent(nodeId));
        if (!detail) return;
        renderDrawer(nodeId, detail);
    } catch (e) {
        document.getElementById('drawer-content').innerHTML = '<p class="error-message">' + esc(e.message) + '</p>';
    }
}

function closeDrawer() {
    selectedNodeId = null;
    document.getElementById('detail-drawer').classList.add('hidden');
    if (cy) {
        cy.elements().removeClass('node-selected edge-highlight');
        setTimeout(function () { cy.resize(); }, 250);
    }
}

// ---------------------------------------------------------------------------
// Drawer rendering
// ---------------------------------------------------------------------------

function renderDrawer(nodeId, detail) {
    var comp = detail.component || {};
    var topoNode = findTopoNode(nodeId);
    var isLocal = topoNode && topoNode.group === 'local';
    var healthCheck = topoNode ? topoNode.health_check : null;
    var compHealth = healthCheck && healthData.components ? healthData.components[healthCheck] : null;

    // Title
    var statusCls = healthClass(compHealth, isLocal);
    document.getElementById('drawer-title').innerHTML =
        '<span class="health-badge health-badge-' + statusCls + '">' +
        '<span class="health-dot health-dot-' + statusCls + '"></span>' +
        healthLabel(compHealth, isLocal) + '</span> ' +
        esc(comp.label || nodeId);

    var html = '';
    var runs = detail.recent_runs || [];

    // Description
    if (comp.description) {
        html += '<p class="drawer-description">' + esc(comp.description) + '</p>';
    }

    // ── Status / Expected vs Actual ──
    html += '<div class="drawer-section">';
    if (compHealth && compHealth.checks && compHealth.checks.length > 0) {
        html += '<div class="drawer-section-title">Expected vs Actual</div>';
        html += '<ul class="check-list">';
        compHealth.checks.forEach(function (c) {
            html += '<li class="check-item">';
            html += '<span class="check-icon ' + (c.passed ? 'check-pass' : 'check-fail') + '">' + (c.passed ? '&#10003;' : '&#10007;') + '</span>';
            html += '<span class="check-text">';
            html += '<span class="check-expected">Expected: ' + esc(c.expected) + '</span>';
            html += '<span class="check-actual' + (c.passed ? '' : ' fail') + '">Actual: ' + esc(c.actual) + '</span>';
            html += '</span></li>';
        });
        html += '</ul>';
    } else if (compHealth && compHealth.message) {
        html += '<div class="drawer-section-title">Status</div>';
        html += '<p>' + esc(compHealth.message) + '</p>';
    } else {
        // No dedicated health check — infer status from orchestration runs
        html += '<div class="drawer-section-title">Status</div>';
        if (runs.length > 0) {
            var lastRun = runs[0];
            var lastStatus = lastRun.status || 'unknown';
            var lastTime = timeAgo(lastRun.started_at);
            var failed = runs.filter(function (r) { return r.status === 'failed'; }).length;
            var completed = runs.filter(function (r) { return r.status === 'completed'; }).length;

            html += '<ul class="check-list">';
            html += '<li class="check-item">';
            html += '<span class="check-icon ' + (lastStatus === 'completed' ? 'check-pass' : lastStatus === 'failed' ? 'check-fail' : 'check-pass') + '">';
            html += (lastStatus === 'failed' ? '&#10007;' : '&#10003;') + '</span>';
            html += '<span class="check-text">';
            html += '<span class="check-expected">Last run: ' + esc(lastTime) + '</span>';
            html += '<span class="check-actual">' + esc(lastStatus) + ' (' + completed + ' completed, ' + failed + ' failed in recent history)</span>';
            html += '</span></li>';

            if (lastRun.error) {
                html += '<li class="check-item">';
                html += '<span class="check-icon check-fail">&#10007;</span>';
                html += '<span class="check-text">';
                html += '<span class="check-expected">Last error</span>';
                html += '<span class="check-actual fail">' + esc(lastRun.error.substring(0, 300)) + '</span>';
                html += '</span></li>';
            }
            html += '</ul>';
        } else if (isLocal) {
            html += '<p class="no-data">Local component — status inferred from cross-boundary data. Run the local Docker stack to activate.</p>';
        } else {
            html += '<p class="no-data">No recent orchestration runs found for this component. It may not have been triggered yet.</p>';
        }
    }
    html += '</div>';

    // ── Why this exists ──
    if (comp.why) {
        html += '<div class="drawer-section">';
        html += '<div class="drawer-section-title">Why This Exists</div>';
        html += '<p class="drawer-why">' + esc(comp.why) + '</p>';
        html += '</div>';
    }

    // ── Connections ──
    html += '<div class="drawer-section">';
    html += '<div class="drawer-section-title">Connections</div>';
    html += renderConnections(nodeId);
    html += '</div>';

    // ── Recent Runs ──
    if (runs.length > 0) {
        html += '<div class="drawer-section">';
        html += '<div class="drawer-section-title">Recent Activity (' + runs.length + ' runs)</div>';
        html += renderRuns(runs);
        html += '</div>';
    }

    // ── Error Logs (filtered CloudWatch logs per component) ──
    var errorLogs = detail.error_logs || [];
    // Also include Lambda health check logs if available
    if (compHealth && compHealth.details && compHealth.details.recent_logs) {
        compHealth.details.recent_logs.forEach(function (log) {
            errorLogs.push(log);
        });
    }
    if (errorLogs.length > 0) {
        html += '<div class="drawer-section">';
        html += '<div class="drawer-section-title">Error Logs (' + errorLogs.length + ')</div>';
        html += '<div class="raw-section"><pre>';
        errorLogs.forEach(function (log) {
            html += esc(log.timestamp + '  ' + log.message) + '\n\n';
        });
        html += '</pre></div>';
        html += '</div>';
    }

    // ── Health Details (collapsed) ──
    if (compHealth && compHealth.details && Object.keys(compHealth.details).length > 0) {
        html += '<div class="drawer-section raw-section">';
        html += '<div class="drawer-section-title">Health Details</div>';
        Object.keys(compHealth.details).forEach(function (key) {
            if (key === 'recent_logs') return; // already rendered above
            var val = compHealth.details[key];
            html += '<details>';
            html += '<summary>' + esc(key) + '</summary>';
            html += '<pre>' + esc(typeof val === 'object' ? JSON.stringify(val, null, 2) : String(val)) + '</pre>';
            html += '</details>';
        });
        html += '</div>';
    }

    // ── Raw Data (collapsed) ──
    if (compHealth && compHealth.raw && Object.keys(compHealth.raw).length > 0) {
        html += '<div class="drawer-section raw-section">';
        html += '<div class="drawer-section-title">Raw AWS Data</div>';
        Object.keys(compHealth.raw).forEach(function (key) {
            html += '<details>';
            html += '<summary>' + esc(key) + '</summary>';
            html += '<pre>' + esc(JSON.stringify(compHealth.raw[key], null, 2)) + '</pre>';
            html += '</details>';
        });
        html += '</div>';
    }

    // ── Runs raw data (for components without health checks) ──
    if (!compHealth && runs.length > 0) {
        html += '<div class="drawer-section raw-section">';
        html += '<div class="drawer-section-title">Raw Run Data</div>';
        html += '<details><summary>Recent orchestration runs (' + runs.length + ')</summary>';
        html += '<pre>' + esc(JSON.stringify(runs, null, 2)) + '</pre>';
        html += '</details>';
        html += '</div>';
    }

    document.getElementById('drawer-content').innerHTML = html;
}

function renderConnections(nodeId) {
    var inputs = [], outputs = [];
    (topologyData.edges || []).forEach(function (e) {
        if (e.target === nodeId) {
            var src = findTopoNode(e.source);
            inputs.push({ name: src ? (src.label || src.id) : e.source, label: e.label || '' });
        }
        if (e.source === nodeId) {
            var tgt = findTopoNode(e.target);
            outputs.push({ name: tgt ? (tgt.label || tgt.id) : e.target, label: e.label || '' });
        }
    });

    if (inputs.length === 0 && outputs.length === 0) return '<p class="no-data">No known connections.</p>';

    var html = '<ul class="conn-list">';
    inputs.forEach(function (c) {
        html += '<li class="conn-item"><span class="conn-arrow">&#8592;</span> from: ' + esc(c.name);
        if (c.label) html += '<span class="conn-label">' + esc(c.label) + '</span>';
        html += '</li>';
    });
    outputs.forEach(function (c) {
        html += '<li class="conn-item"><span class="conn-arrow">&#8594;</span> to: ' + esc(c.name);
        if (c.label) html += '<span class="conn-label">' + esc(c.label) + '</span>';
        html += '</li>';
    });
    html += '</ul>';
    return html;
}

function renderRuns(runs) {
    if (!runs || runs.length === 0) return '<p class="no-data">No recent activity.</p>';

    var html = '<table class="runs-table"><thead><tr><th>Time</th><th>Event</th><th>Status</th><th>Duration</th></tr></thead><tbody>';
    runs.forEach(function (r) {
        var sc = r.status === 'completed' ? 'status-ok' : r.status === 'failed' ? 'status-fail' : 'status-run';
        var dur = '--';
        if (r.started_at && r.completed_at) {
            var d = Math.round((new Date(r.completed_at) - new Date(r.started_at)) / 1000);
            dur = d < 60 ? d + 's' : Math.round(d / 60) + 'm';
        }
        html += '<tr><td>' + timeAgo(r.started_at) + '</td><td>' + esc(r.event_type || '--') + '</td><td class="' + sc + '">' + esc(r.status) + '</td><td>' + dur + '</td></tr>';
        if (r.error) html += '<tr class="error-row"><td colspan="4">' + esc(r.error.substring(0, 300)) + '</td></tr>';
    });
    html += '</tbody></table>';
    return html;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function healthClass(comp, isLocal) {
    if (isLocal) return 'gray';
    if (!comp) return 'gray';
    if (comp.status === 'green') return 'green';
    if (comp.status === 'yellow') return 'yellow';
    if (comp.status === 'red') return 'red';
    return 'gray';
}

function healthLabel(comp, isLocal) {
    if (isLocal) return 'Local';
    if (!comp) return 'Unknown';
    if (comp.status === 'green') return 'Healthy';
    if (comp.status === 'yellow') return 'Degraded';
    if (comp.status === 'red') return 'Down';
    return 'Unknown';
}

function timeAgo(ts) {
    if (!ts) return '--';
    try {
        var d = new Date(ts), now = new Date(), diff = Math.floor((now - d) / 60000);
        if (isNaN(diff)) return ts;
        if (diff < 1) return 'just now';
        if (diff < 60) return diff + 'm ago';
        var h = Math.floor(diff / 60);
        if (h < 24) return h + 'h ago';
        return Math.floor(h / 24) + 'd ago';
    } catch (e) { return ts; }
}

function esc(str) {
    if (str === null || str === undefined) return '';
    var d = document.createElement('div');
    d.appendChild(document.createTextNode(String(str)));
    return d.innerHTML;
}

function updateTimestamp() {
    var el = document.getElementById('last-refresh');
    var now = new Date();
    el.textContent = 'Last: ' + now.toLocaleTimeString();
}

// ---------------------------------------------------------------------------
// Refresh
// ---------------------------------------------------------------------------

async function refresh() {
    var btn = document.getElementById('refresh-btn');
    btn.disabled = true; btn.textContent = 'Refreshing...';
    try {
        var results = await Promise.all([
            api('/api/debug/health'),
            api('/api/debug/summary'),
        ]);
        healthData = results[0];
        summaryData = results[1];
        if (healthData) { renderSummaryBar(); applyHealthData(); updateTimestamp(); }
        if (selectedNodeId) {
            try {
                var detail = await api('/api/debug/component/' + encodeURIComponent(selectedNodeId));
                if (detail) renderDrawer(selectedNodeId, detail);
            } catch (e) { console.error('Refresh detail failed:', e); }
        }
        if (viewMode === 'errors') { await loadErrorsView(); }
    } catch (e) { console.error('Refresh failed:', e); }
    btn.disabled = false; btn.textContent = 'Refresh';
}

// ---------------------------------------------------------------------------
// Errors Only View
// ---------------------------------------------------------------------------

async function toggleView() {
    var btn = document.getElementById('view-toggle');
    var graphEl = document.getElementById('graph-container');
    var errorEl = document.getElementById('error-summary');

    if (viewMode === 'all') {
        viewMode = 'errors';
        btn.textContent = 'All Components';
        btn.classList.add('active');
        graphEl.style.display = 'none';
        errorEl.classList.remove('hidden');
        closeDrawer();
        await loadErrorsView();
    } else {
        viewMode = 'all';
        btn.textContent = 'Errors Only';
        btn.classList.remove('active');
        graphEl.style.display = '';
        errorEl.classList.add('hidden');
        if (cy) cy.resize();
    }
}

async function loadErrorsView() {
    var errorEl = document.getElementById('error-summary');
    errorEl.innerHTML = '<div class="drawer-loading"><span class="loading-spinner"></span> Loading errors...</div>';

    try {
        var data = await api('/api/debug/errors');
        if (!data) return;
        renderErrorSummary(data);
    } catch (e) {
        errorEl.innerHTML = '<p class="error-message">Failed to load errors: ' + esc(e.message) + '</p>';
    }
}

function renderErrorSummary(data) {
    var el = document.getElementById('error-summary');
    var errors = data.errors || [];

    if (errors.length === 0) {
        el.innerHTML = '<p class="error-none">All components healthy. No errors to show.</p>';
        return;
    }

    var html = '<div class="error-summary-header">' + errors.length + ' component(s) with issues</div>';

    errors.forEach(function (err) {
        var cardClass = err.status === 'red' ? 'error-card' : 'error-card warning';

        html += '<div class="' + cardClass + '">';

        // Header
        html += '<div class="error-card-header">';
        html += '<span class="health-badge health-badge-' + err.status + '">';
        html += '<span class="health-dot health-dot-' + err.status + '"></span>';
        html += (err.status === 'red' ? 'Down' : 'Degraded') + '</span>';
        html += '<span class="error-card-name">' + esc(err.label) + '</span>';
        html += '<span class="error-card-msg">' + esc(err.message) + '</span>';
        html += '</div>';

        // Failed checks
        if (err.failed_checks && err.failed_checks.length > 0) {
            html += '<ul class="error-card-checks">';
            err.failed_checks.forEach(function (c) {
                html += '<li><span class="icon">&#10007;</span> ';
                html += 'Expected: ' + esc(c.expected) + ' — Actual: ' + esc(c.actual);
                html += '</li>';
            });
            html += '</ul>';
        }

        // Error logs
        if (err.error_logs && err.error_logs.length > 0) {
            html += '<div class="error-card-logs">';
            err.error_logs.forEach(function (log) {
                html += esc(log.timestamp + '  ' + log.message) + '\n';
            });
            html += '</div>';
        }

        // Connected components
        if (err.connected && err.connected.length > 0) {
            html += '<div class="error-card-connections">';
            html += 'Connected: ';
            html += err.connected.map(function (c) {
                return '<span>' + (c.direction === 'from' ? '← ' : '→ ') + esc(c.name) + '</span>';
            }).join(' · ');
            html += '</div>';
        }

        html += '</div>';
    });

    el.innerHTML = html;
}
