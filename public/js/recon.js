/* AchintERP - Reconciliation SPA module (Workstream 2B carve-out)
 *
 * Pulled verbatim out of `public/index.html`. Exposes the same `window.<name>`
 * functions the inline `onclick=` handlers expect so existing markup keeps
 * working untouched.
 *
 * Cross-module deps it relies on (all globals defined elsewhere in index.html):
 *   currentClient, hideAllViews, resetFilters, updateSidebarNav,
 *   escHtml, customApplet, ErpApi.
 */
(function () {
    'use strict';

    var reconState = { lastReport: null };

    function openReconciliationView() {
        window.currentClient = 'RECON';
        window.hideAllViews();
        window.resetFilters(false);
        var actionsEl = document.getElementById('clientActions');
        if (actionsEl) actionsEl.style.display = 'none';
        document.getElementById('reconciliationView').style.display = 'block';
        document.getElementById('topbarTitle').innerText = 'Financial Reconciliation';
        window.updateSidebarNav();
        runReconciliation();
    }

    function reconStatusPillStyle(status) {
        switch ((status || '').toLowerCase()) {
            case 'errors': return 'background:#fee2e2; color:#991b1b;';
            case 'warnings': return 'background:#fef3c7; color:#92400e;';
            case 'ok':
            default: return 'background:#d1fae5; color:#065f46;';
        }
    }

    async function runReconciliation() {
        var banner = document.getElementById('reconOverallBanner');
        var sectionsEl = document.getElementById('reconSections');
        var generatedEl = document.getElementById('reconGeneratedAt');
        sectionsEl.innerHTML = '<div class="card" style="padding: 24px; text-align:center; color:var(--text-muted);">Running reconciliation checks...</div>';
        banner.innerHTML = '';
        try {
            var resp = await window.ErpApi.fetch('/api/reconciliation/summary');
            if (!resp.ok) {
                var err = await resp.json().catch(function () { return {}; });
                sectionsEl.innerHTML = '<div class="card" style="padding: 24px; color: var(--danger);">' + window.escHtml(err.detail || 'Failed to load reconciliation report') + '</div>';
                return;
            }
            var data = await resp.json();
            reconState.lastReport = data;

            generatedEl.innerText = data.generated_at ? 'Generated ' + data.generated_at.replace('T', ' ').slice(0, 19) + ' UTC' : '';

            banner.innerHTML =
                '<div class="card" style="display:flex; gap:18px; align-items:center; padding: 16px 20px;">' +
                    '<span class="status-pill" style="' + reconStatusPillStyle(data.overall_status) + ' font-size:0.78rem; padding: 6px 14px;">' + window.escHtml((data.overall_status || 'ok').toUpperCase()) + '</span>' +
                    '<span style="font-size:0.9rem;"><strong>' + data.errors + '</strong> error(s) &middot; <strong>' + data.warnings + '</strong> warning(s) across ' + data.sections.length + ' check(s)</span>' +
                '</div>';

            if (!data.sections || !data.sections.length) {
                sectionsEl.innerHTML = '<div class="card" style="padding: 24px; color: var(--text-muted);">No checks configured.</div>';
                return;
            }

            sectionsEl.innerHTML = data.sections.map(function (sec) {
                var issuesHtml = (sec.issues && sec.issues.length)
                    ? (
                        '<div class="table-wrapper" style="border:none; box-shadow:none; border-radius:0;">' +
                            '<table style="min-width:100%;">' +
                                '<thead>' +
                                    '<tr>' +
                                        '<th style="text-align:left;">Severity</th>' +
                                        '<th style="text-align:left;">Entity</th>' +
                                        '<th style="text-align:left;">Code</th>' +
                                        '<th style="text-align:left;">Message</th>' +
                                        '<th style="text-align:right;">Expected</th>' +
                                        '<th style="text-align:right;">Actual</th>' +
                                    '</tr>' +
                                '</thead>' +
                                '<tbody>' +
                                    sec.issues.map(function (iss) {
                                        return (
                                            '<tr>' +
                                                '<td><span class="status-pill" style="' + reconStatusPillStyle(iss.severity === 'error' ? 'errors' : 'warnings') + '">' + window.escHtml(iss.severity || '') + '</span></td>' +
                                                '<td>' + window.escHtml(iss.entity_type || '') + '<br><span style="color:var(--text-muted); font-size:0.78rem;">' + window.escHtml(iss.entity_id || '') + '</span></td>' +
                                                '<td><code style="font-size:0.78rem;">' + window.escHtml(iss.code || '') + '</code></td>' +
                                                '<td style="font-size:0.85rem;">' + window.escHtml(iss.message || '') + '</td>' +
                                                '<td style="text-align:right; font-family: var(--font-mono, monospace); font-size:0.78rem;">' + (iss.expected != null ? window.escHtml(typeof iss.expected === 'number' ? iss.expected.toFixed(2) : JSON.stringify(iss.expected)) : '') + '</td>' +
                                                '<td style="text-align:right; font-family: var(--font-mono, monospace); font-size:0.78rem;">' + (iss.actual != null ? window.escHtml(typeof iss.actual === 'number' ? iss.actual.toFixed(2) : JSON.stringify(iss.actual)) : '') + '</td>' +
                                            '</tr>'
                                        );
                                    }).join('') +
                                '</tbody>' +
                            '</table>' +
                        '</div>'
                    )
                    : '<div style="padding: 16px 20px; color: var(--text-muted); font-size:0.85rem;">No issues found.</div>';

                return (
                    '<div class="card" style="margin-bottom: 16px; padding: 0; overflow:hidden;">' +
                        '<div style="display:flex; justify-content:space-between; align-items:center; padding: 16px 20px; border-bottom: 1px solid var(--border);">' +
                            '<div>' +
                                '<h3 class="section-title" style="margin:0; border:none; padding:0;">' + window.escHtml(sec.label) + '</h3>' +
                                '<div style="margin-top:4px; font-size:0.78rem; color:var(--text-muted);"><code>' + window.escHtml(sec.key) + '</code></div>' +
                            '</div>' +
                            '<div style="display:flex; gap:8px; align-items:center;">' +
                                '<span style="font-size:0.78rem; color:var(--text-muted);"><strong>' + sec.errors + '</strong> error(s) &middot; <strong>' + sec.warnings + '</strong> warning(s)</span>' +
                                '<span class="status-pill" style="' + reconStatusPillStyle(sec.status) + '">' + window.escHtml((sec.status || 'ok').toUpperCase()) + '</span>' +
                            '</div>' +
                        '</div>' +
                        issuesHtml +
                    '</div>'
                );
            }).join('');
        } catch (err) {
            sectionsEl.innerHTML = '<div class="card" style="padding: 24px; color: var(--danger);">Reconciliation failed to run.</div>';
        }
    }

    function exportReconciliationCsv() {
        var data = reconState.lastReport;
        if (!data || !Array.isArray(data.sections)) {
            return window.customApplet({ title: 'Nothing to Export', message: 'Run reconciliation first.' });
        }
        var header = ['Section', 'Severity', 'Entity Type', 'Entity ID', 'Code', 'Message', 'Expected', 'Actual', 'Context'];
        var rows = [];
        data.sections.forEach(function (sec) {
            (sec.issues || []).forEach(function (iss) {
                rows.push([
                    sec.label,
                    iss.severity || '',
                    iss.entity_type || '',
                    iss.entity_id || '',
                    iss.code || '',
                    iss.message || '',
                    iss.expected != null ? (typeof iss.expected === 'number' ? iss.expected : JSON.stringify(iss.expected)) : '',
                    iss.actual != null ? (typeof iss.actual === 'number' ? iss.actual : JSON.stringify(iss.actual)) : '',
                    iss.context ? JSON.stringify(iss.context) : ''
                ]);
            });
        });
        if (!rows.length) {
            return window.customApplet({ title: 'Nothing to Export', message: 'Reconciliation found no issues.' });
        }
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
        a.download = 'reconciliation_' + new Date().toISOString().slice(0, 10) + '.csv';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    window.openReconciliationView = openReconciliationView;
    window.runReconciliation = runReconciliation;
    window.exportReconciliationCsv = exportReconciliationCsv;
})();
