/**
 * Visual-only polish: view/modal enter animations and loader fade-out.
 * Does not alter ERP data, API calls, or navigation logic.
 */
(function () {
    'use strict';

    var VIEW_IDS = [
        'landingView',
        'clientView',
        'adminView',
        'auditLogView',
        'reconciliationView',
        'reportsView',
        'dispatchView',
        'poDetailsView',
        'logisticsLandingView',
        'logisticsClientDirectoryView',
        'logisticsClientPageView'
    ];

    var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

    function viewVisible(el) {
        if (!el) return false;
        var display = el.style.display;
        if (display === 'none') return false;
        if (display === 'block' || display === 'grid' || display === 'flex') return true;
        return display === '' && el.offsetParent !== null;
    }

    function pulseEnter(el) {
        if (!el || reducedMotion.matches) return;
        el.classList.remove('view-enter');
        void el.offsetWidth;
        el.classList.add('view-enter');
        el.addEventListener(
            'animationend',
            function () {
                el.classList.remove('view-enter');
            },
            { once: true }
        );
    }

    function hookViews() {
        VIEW_IDS.forEach(function (id) {
            var el = document.getElementById(id);
            if (!el) return;
            var wasVisible = viewVisible(el);
            var observer = new MutationObserver(function () {
                var nowVisible = viewVisible(el);
                if (nowVisible && !wasVisible) {
                    pulseEnter(el);
                    if (id === 'landingView' || id === 'clientView') {
                        var scrollArea = el.closest('.scroll-area') || document.querySelector('.scroll-area');
                        if (scrollArea) scrollArea.scrollTo({ top: 0, behavior: reducedMotion.matches ? 'auto' : 'smooth' });
                    }
                }
                wasVisible = nowVisible;
            });
            observer.observe(el, { attributes: true, attributeFilter: ['style'] });
        });
    }

    function overlayOpen(el) {
        if (!el) return false;
        var inline = el.style.display;
        if (inline === 'flex') return true;
        if (inline === 'none') return false;
        try {
            return window.getComputedStyle(el).display === 'flex';
        } catch (e) {
            return false;
        }
    }

    function hookModals() {
        document.querySelectorAll('.modal-overlay').forEach(function (overlay) {
            var open = overlayOpen(overlay);
            overlay.classList.toggle('modal-visible', open);
            var observer = new MutationObserver(function () {
                var nowOpen = overlayOpen(overlay);
                overlay.classList.toggle('modal-visible', nowOpen);
            });
            observer.observe(overlay, { attributes: true, attributeFilter: ['style', 'class'] });
        });
    }

    function init() {
        hookViews();
        hookModals();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
