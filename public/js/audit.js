/* AchintERP - Audit Log SPA module (Workstream 2B carve-out)
 *
 * Pulled verbatim out of `public/index.html`. Exposes the same `window.<name>`
 * functions the inline `onclick=` handlers expect, so this swap is invisible
 * to the rest of the SPA.
 *
 * Cross-module deps it relies on (all globals defined elsewhere in index.html):
 *   currentClient, hideAllViews, resetFilters, updateSidebarNav,
 *   escHtml, customApplet, ErpApi.
 */
(function () {
    'use strict';

    var AUDIT_PAGE_SIZE = 50;
    var auditLogState = { offset: 0, total: 0, lastItems: [] };

    function openAuditLogView() {
        window.currentClient = 'AUDIT';
        window.hideAllViews();
        window.resetFilters(false);
        var actionsEl = document.getElementById('clientActions');
        if (actionsEl) actionsEl.style.display = 'none';
        document.getElementById('auditLogView').style.display = 'block';
        document.getElementById('topbarTitle').innerText = 'Audit Log';
        window.updateSidebarNav();
        renderAuditLog(0);
    }

    function resetAuditLogFilters() {
        ['auditFilterEntityType', 'auditFilterAction', 'auditFilterEntityId', 'auditFilterUsername', 'auditFilterSince', 'auditFilterUntil']
            .forEach(function (id) { var el = document.getElementById(id); if (el) el.value = ''; });
        renderAuditLog(0);
    }

    function buildAuditQueryParams(offset) {
        var params = new URLSearchParams();
        var entityType = document.getElementById('auditFilterEntityType').value;
        var action = document.getElementById('auditFilterAction').value;
        var entityId = document.getElementById('auditFilterEntityId').value.trim();
        var username = document.getElementById('auditFilterUsername').value.trim();
        var since = document.getElementById('auditFilterSince').value;
        var until = document.getElementById('auditFilterUntil').value;
        if (entityType) params.set('entity_type', entityType);
        if (action) params.set('action', action);
        if (entityId) params.set('entity_id', entityId);
        if (username) params.set('username', username);
        if (since) params.set('since', since + 'T00:00:00');
        if (until) {
            var d = new Date(until + 'T00:00:00');
            d.setUTCDate(d.getUTCDate() + 1);
            params.set('until', d.toISOString().slice(0, 19));
        }
        params.set('limit', String(AUDIT_PAGE_SIZE));
        params.set('offset', String(Math.max(0, offset || 0)));
        return params;
    }

    async function renderAuditLog(offset) {
        var body = document.getElementById('auditLogBody');
        var summary = document.getElementById('auditLogSummary');
        var pageLabel = document.getElementById('auditPageLabel');
        if (!body) return;
        body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">Loading audit entries...</td></tr>';
        var params = buildAuditQueryParams(offset);
        try {
            var resp = await window.ErpApi.fetch('/api/audit-log?' + params.toString());
            if (!resp.ok) {
                var err = await resp.json().catch(function () { return {}; });
                body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--danger);">' + window.escHtml(err.detail || 'Failed to load audit log') + '</td></tr>';
                summary.innerText = '';
                return;
            }
            var data = await resp.json();
            auditLogState.offset = data.offset || 0;
            auditLogState.total = data.total || 0;
            auditLogState.lastItems = Array.isArray(data.items) ? data.items : [];

            if (!auditLogState.lastItems.length) {
                body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--text-muted);">No audit entries match the current filters.</td></tr>';
            } else {
                body.innerHTML = auditLogState.lastItems.map(function (entry, idx) {
                    var when = entry.at_utc ? entry.at_utc.replace('T', ' ').slice(0, 19) : '';
                    var entityLabel = window.escHtml(entry.entity_type || '') + (entry.entity_id ? ' <span style="color:var(--text-muted);">(' + window.escHtml(entry.entity_id) + ')</span>' : '');
                    var pillBg = entry.action === 'delete' ? '#fee2e2' : entry.action === 'create' ? '#d1fae5' : '#e0e7ff';
                    var pillFg = entry.action === 'delete' ? '#991b1b' : entry.action === 'create' ? '#065f46' : '#3730a3';
                    var actionPill = '<span class="status-pill" style="background: ' + pillBg + '; color: ' + pillFg + ';">' + window.escHtml(entry.action || '') + '</span>';
                    var summaryText = window.escHtml(entry.summary || '');
                    return (
                        '<tr>' +
                            '<td style="white-space:nowrap; font-family: var(--font-mono, monospace); font-size: 0.78rem;">' + window.escHtml(when) + '</td>' +
                            '<td>' + window.escHtml(entry.username || '') + (entry.role ? ' <span style="color:var(--text-muted); font-size:0.75rem;">(' + window.escHtml(entry.role) + ')</span>' : '') + '</td>' +
                            '<td>' + entityLabel + '</td>' +
                            '<td>' + actionPill + '</td>' +
                            '<td>' + summaryText + '</td>' +
                            '<td style="text-align:center;"><button class="btn-secondary btn-sm" onclick="toggleAuditDetails(' + idx + ')">View</button></td>' +
                        '</tr>' +
                        '<tr id="auditDetails_' + idx + '" style="display:none;"><td colspan="6" style="background: var(--bg-body);"><pre style="margin:0; white-space: pre-wrap; word-break: break-word; font-size: 0.78rem;">' +
                            window.escHtml(JSON.stringify(entry.details || {}, null, 2)) +
                            (entry.ip_address ? '\n\nIP: ' + window.escHtml(entry.ip_address) : '') +
                        '</pre></td></tr>'
                    );
                }).join('');
            }

            var start = auditLogState.total === 0 ? 0 : auditLogState.offset + 1;
            var end = Math.min(auditLogState.total, auditLogState.offset + auditLogState.lastItems.length);
            summary.innerText = auditLogState.total + ' entries';
            pageLabel.innerText = start + '-' + end + ' of ' + auditLogState.total;
            document.getElementById('auditPrevBtn').disabled = auditLogState.offset <= 0;
            document.getElementById('auditNextBtn').disabled = (auditLogState.offset + auditLogState.lastItems.length) >= auditLogState.total;
        } catch (err) {
            body.innerHTML = '<tr><td colspan="6" style="text-align:center; padding: 24px; color: var(--danger);">Failed to load audit log.</td></tr>';
        }
    }

    function toggleAuditDetails(idx) {
        var row = document.getElementById('auditDetails_' + idx);
        if (!row) return;
        row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
    }

    function changeAuditPage(delta) {
        var next = auditLogState.offset + (delta * AUDIT_PAGE_SIZE);
        if (next < 0) return;
        if (next >= auditLogState.total && delta > 0) return;
        renderAuditLog(next);
    }

    async function exportAuditLogCsv() {
        var params = buildAuditQueryParams(0);
        params.set('limit', '1000');
        try {
            var resp = await window.ErpApi.fetch('/api/audit-log?' + params.toString());
            if (!resp.ok) return window.customApplet({ title: 'Export Failed', message: 'Could not fetch audit data.' });
            var data = await resp.json();
            var items = Array.isArray(data.items) ? data.items : [];
            if (!items.length) return window.customApplet({ title: 'Nothing to Export', message: 'Current filters returned no audit entries.' });
            var header = ['When (UTC)', 'User', 'Role', 'IP', 'Entity Type', 'Entity ID', 'Action', 'Summary', 'Details (JSON)'];
            var rows = items.map(function (e) {
                return [
                    e.at_utc || '',
                    e.username || '',
                    e.role || '',
                    e.ip_address || '',
                    e.entity_type || '',
                    e.entity_id || '',
                    e.action || '',
                    e.summary || '',
                    e.details ? JSON.stringify(e.details) : ''
                ];
            });
            var csv = [header].concat(rows).map(function (r) {
                return r.map(function (v) {
                    var s = String(v == null ? '' : v).replace(/"/g, '""');
                    return /[",\n]/.test(s) ? '"' + s + '"' : s;
                }).join(',');
            }).join('\r\n');
            var blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = 'audit_log_' + new Date().toISOString().slice(0, 10) + '.csv';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (err) {
            window.customApplet({ title: 'Export Failed', message: 'Could not export audit data.' });
        }
    }

    window.openAuditLogView = openAuditLogView;
    window.resetAuditLogFilters = resetAuditLogFilters;
    window.buildAuditQueryParams = buildAuditQueryParams;
    window.renderAuditLog = renderAuditLog;
    window.toggleAuditDetails = toggleAuditDetails;
    window.changeAuditPage = changeAuditPage;
    window.exportAuditLogCsv = exportAuditLogCsv;
})();
