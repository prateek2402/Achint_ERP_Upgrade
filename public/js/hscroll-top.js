/**
 * Duplicate horizontal scrollbar above a scroll host (tables, AG Grid).
 * Keeps scrollLeft in sync with the primary (bottom) scroller.
 */
(function (global) {
    'use strict';

    function ensureWrap(parent, wrapClass) {
        if (parent.classList.contains(wrapClass)) return parent;
        parent.classList.add(wrapClass);
        return parent;
    }

    function wireTopBar(top, scrollEl, refresh) {
        const inner = top.querySelector('.hscroll-top-inner');
        if (!inner) return;

        let lock = false;

        const syncFromHost = () => {
            if (lock) return;
            lock = true;
            top.scrollLeft = scrollEl.scrollLeft;
            lock = false;
        };

        const syncFromTop = () => {
            if (lock) return;
            lock = true;
            scrollEl.scrollLeft = top.scrollLeft;
            lock = false;
        };

        if (!top.dataset.hscrollWired) {
            top.dataset.hscrollWired = '1';
            top.addEventListener('scroll', syncFromTop, { passive: true });
            scrollEl.addEventListener('scroll', syncFromHost, { passive: true });
        }

        refresh._syncFromHost = syncFromHost;
        refresh._inner = inner;
        refresh._top = top;
        refresh._scrollEl = scrollEl;
    }

    function makeRefresh(top, scrollEl) {
        const refresh = () => {
            const inner = refresh._inner;
            const bar = refresh._top;
            if (!inner || !bar || !scrollEl) return;
            const sw = scrollEl.scrollWidth;
            const cw = scrollEl.clientWidth;
            inner.style.width = sw + 'px';
            bar.style.display = sw > cw + 1 ? '' : 'none';
            if (bar.scrollLeft !== scrollEl.scrollLeft) {
                bar.scrollLeft = scrollEl.scrollLeft;
            }
        };
        wireTopBar(top, scrollEl, refresh);
        return refresh;
    }

  /** @param {HTMLElement} scrollEl element with overflow-x scroll */
    function attachTopHorizontalScroll(scrollEl) {
        if (!scrollEl || scrollEl.dataset.topHscrollBound === '1') {
            return scrollEl && scrollEl._topHscrollRefresh;
        }
        scrollEl.dataset.topHscrollBound = '1';
        scrollEl.classList.add('hscroll-body');

        const parent = scrollEl.parentElement;
        if (!parent) return null;
        ensureWrap(parent, 'hscroll-sync');

        let top = scrollEl.previousElementSibling;
        if (!top || !top.classList.contains('hscroll-top')) {
            top = document.createElement('div');
            top.className = 'hscroll-top';
            top.setAttribute('aria-hidden', 'true');
            const inner = document.createElement('div');
            inner.className = 'hscroll-top-inner';
            top.appendChild(inner);
            parent.insertBefore(top, scrollEl);
        }

        const refresh = makeRefresh(top, scrollEl);
        const ro = new ResizeObserver(() => refresh());
        ro.observe(scrollEl);
        if (scrollEl.firstElementChild) ro.observe(scrollEl.firstElementChild);

        refresh();
        scrollEl._topHscrollRefresh = refresh;
        return refresh;
    }

    function getAgGridHorizontalViewport(gridEl) {
        if (!gridEl) return null;
        return (
            gridEl.querySelector('.ag-body-horizontal-scroll-viewport') ||
            gridEl.querySelector('.ag-center-cols-viewport')
        );
    }

    function bindAgGridTopHorizontalScroll(gridEl) {
        if (!gridEl) return null;
        const viewport = getAgGridHorizontalViewport(gridEl);
        if (!viewport) return null;

        let wrap = gridEl.parentElement;
        if (!wrap || !wrap.classList.contains('hscroll-sync--grid')) {
            wrap = document.createElement('div');
            wrap.className = 'hscroll-sync hscroll-sync--grid';
            gridEl.parentNode.insertBefore(wrap, gridEl);
            wrap.appendChild(gridEl);
        }

        let top = wrap.querySelector(':scope > .hscroll-top');
        if (!top) {
            top = document.createElement('div');
            top.className = 'hscroll-top';
            top.setAttribute('aria-hidden', 'true');
            const inner = document.createElement('div');
            inner.className = 'hscroll-top-inner';
            top.appendChild(inner);
            wrap.insertBefore(top, gridEl);
        }

        const key = 'ag-' + (gridEl.id || 'grid');
        if (viewport.dataset.topHscrollAgKey !== key) {
            viewport.dataset.topHscrollAgKey = key;
            viewport.dataset.topHscrollBound = '';
        }

        if (viewport.dataset.topHscrollBound !== '1') {
            viewport.dataset.topHscrollBound = '1';
        }

        let refresh = gridEl._topHscrollRefresh;
        if (!refresh || refresh._scrollEl !== viewport) {
            refresh = makeRefresh(top, viewport);
            gridEl._topHscrollRefresh = refresh;
            const ro = new ResizeObserver(() => refresh());
            ro.observe(gridEl);
            ro.observe(viewport);
            const center = gridEl.querySelector('.ag-center-cols-container');
            if (center) ro.observe(center);
        }

        refresh();
        return refresh;
    }

    function enhanceTableWrappers(root) {
        (root || document).querySelectorAll('.table-wrapper').forEach((el) => {
            if (el.closest('#dispatchAgGrid')) return;
            attachTopHorizontalScroll(el);
        });
    }

    function scheduleAgGridTopScroll(gridEl) {
        if (!gridEl) return;
        const run = () => bindAgGridTopHorizontalScroll(gridEl);
        run();
        requestAnimationFrame(run);
        setTimeout(run, 120);
        setTimeout(run, 400);
    }

    global.attachTopHorizontalScroll = attachTopHorizontalScroll;
    global.bindAgGridTopHorizontalScroll = bindAgGridTopHorizontalScroll;
    global.enhanceTableWrappers = enhanceTableWrappers;
    global.scheduleAgGridTopScroll = scheduleAgGridTopScroll;
})(window);
