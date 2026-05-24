/* Achint ERP - shared client-side helpers.
 *
 * Loaded BEFORE the main inline <script>. Exposes a small global namespace
 * so existing code can adopt the new helpers incrementally without a big
 * refactor. New code should prefer ErpApi.fetch(...) and ErpAuth.* helpers
 * over poking storage directly.
 */
(function () {
    "use strict";

    var TOKEN_KEY = "erp_token";
    var ROLE_KEY = "erp_role";
    var USER_KEY = "erp_username";
    var EXPIRY_KEY = "erp_token_expires_at";   // ms since epoch

    /* ----------------------------------------------------------------------
     * Storage layer.
     *
     * The canonical store is localStorage so a refresh / browser restart
     * keeps the operator signed in until the JWT actually expires (the
     * backend bakes that expiry into /api/login). For backward compatibility
     * with the ~20 sites that still read sessionStorage.getItem('erp_role')
     * directly inside index.html, we also mirror writes into sessionStorage
     * and read from sessionStorage as a fallback.
     * ---------------------------------------------------------------------- */
    function lsGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
    function lsSet(key, v) { try { localStorage.setItem(key, v); } catch (_) {} }
    function lsRem(key) { try { localStorage.removeItem(key); } catch (_) {} }
    function ssGet(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } }
    function ssSet(key, v) { try { sessionStorage.setItem(key, v); } catch (_) {} }
    function ssRem(key) { try { sessionStorage.removeItem(key); } catch (_) {} }

    function getOne(key) {
        var v = lsGet(key);
        if (v != null) return v;
        return ssGet(key);
    }
    function setOne(key, v) { lsSet(key, v); ssSet(key, v); }
    function remOne(key) { lsRem(key); ssRem(key); }
    function mirrorLocalSession() {
        [TOKEN_KEY, ROLE_KEY, USER_KEY, EXPIRY_KEY].forEach(function (key) {
            var value = lsGet(key);
            if (value != null) ssSet(key, value);
        });
    }

    function isExpired() {
        var raw = lsGet(EXPIRY_KEY) || ssGet(EXPIRY_KEY);
        if (!raw) return false;                  // legacy session without expiry: trust until /api fails
        var ms = parseInt(raw, 10);
        if (isNaN(ms)) return false;
        return Date.now() >= ms;
    }

    var ErpAuth = {
        // Returns raw value (null when absent) so it is a drop-in replacement
        // for sessionStorage.getItem('erp_token') used throughout the app.
        getToken: function () {
            if (isExpired()) { ErpAuth.clearSession(); return null; }
            mirrorLocalSession();
            return getOne(TOKEN_KEY);
        },
        getRole: function () { return getOne(ROLE_KEY); },
        getUsername: function () { return getOne(USER_KEY); },
        getExpiry: function () {
            var raw = lsGet(EXPIRY_KEY) || ssGet(EXPIRY_KEY);
            return raw ? parseInt(raw, 10) : null;
        },
        isAuthenticated: function () { return !!ErpAuth.getToken(); },
        setSession: function (data) {
            if (!data) return;
            if (data.token) setOne(TOKEN_KEY, data.token);
            if (data.role) setOne(ROLE_KEY, data.role);
            if (data.username) setOne(USER_KEY, data.username);
            // Prefer absolute expiry from the backend; fall back to seconds-from-now.
            var expiryMs = null;
            if (data.expires_at) {
                var t = Date.parse(data.expires_at);
                if (!isNaN(t)) expiryMs = t;
            }
            if (expiryMs == null && data.expires_in_seconds) {
                expiryMs = Date.now() + (Number(data.expires_in_seconds) * 1000);
            }
            if (expiryMs != null) setOne(EXPIRY_KEY, String(expiryMs));
        },
        clearSession: function () {
            remOne(TOKEN_KEY); remOne(ROLE_KEY); remOne(USER_KEY); remOne(EXPIRY_KEY);
        },
        authHeader: function () {
            var token = ErpAuth.getToken();
            return token ? { Authorization: "Bearer " + token } : {};
        }
    };

    /* ----------------------------------------------------------------------
     * Global loading overlay.
     *
     * Ref-counted: any number of concurrent .show()/.hide() pairs work
     * without flicker. Auto-shown by ErpApi.fetch when a request takes
     * longer than SLOW_FETCH_MS, unless the caller passes { quiet: true }.
     * Idempotent + tolerant: never throws, never blocks the app.
     * ---------------------------------------------------------------------- */
    var SLOW_FETCH_MS = 400;
    var ErpLoader = (function () {
        var pending = 0;
        var lastMessage = "";

        function el() { return document.getElementById("erpGlobalLoader"); }
        function msgEl() { return document.getElementById("erpGlobalLoaderMsg"); }

        function ensure() {
            if (el()) return el();
            try {
                var overlay = document.createElement("div");
                overlay.id = "erpGlobalLoader";
                overlay.setAttribute("role", "status");
                overlay.setAttribute("aria-live", "polite");
                overlay.style.cssText = [
                    "position:fixed", "inset:0",
                    "display:none",
                    "align-items:center", "justify-content:center",
                    "flex-direction:column", "gap:14px",
                    "background:rgba(15,23,42,0.45)",
                    "backdrop-filter:blur(2px)",
                    "z-index:99999",
                    "color:#fff", "font-family:inherit", "font-size:0.95rem",
                    "transition:opacity 0.15s ease-in-out",
                    "opacity:0"
                ].join(";");
                overlay.innerHTML = ''
                    + '<div class="erp-loader-spinner" aria-hidden="true"></div>'
                    + '<div id="erpGlobalLoaderMsg" style="text-align:center; max-width:80vw; text-shadow:0 1px 2px rgba(0,0,0,0.3); font-weight:600;"></div>';
                document.body.appendChild(overlay);
            } catch (_) { /* DOM not ready; show() will retry on next call */ }
            return el();
        }

        function show(message) {
            pending += 1;
            if (typeof message === "string" && message.length) {
                lastMessage = message;
            }
            var ov = ensure();
            if (!ov) return;
            var m = msgEl();
            if (m) m.textContent = lastMessage || "Loading...";
            ov.style.display = "flex";
            // double rAF so the transition fires after display:flex applies
            requestAnimationFrame(function () {
                requestAnimationFrame(function () { ov.style.opacity = "1"; });
            });
        }

        function hide() {
            pending = Math.max(0, pending - 1);
            if (pending > 0) return;
            lastMessage = "";
            var ov = el();
            if (!ov) return;
            ov.style.opacity = "0";
            setTimeout(function () { if (pending === 0 && ov) ov.style.display = "none"; }, 160);
        }

        function reset() {
            pending = 0;
            lastMessage = "";
            var ov = el();
            if (ov) { ov.style.opacity = "0"; ov.style.display = "none"; }
        }

        function withLoader(promise, message) {
            show(message);
            var p = (promise && typeof promise.then === "function")
                ? promise
                : Promise.resolve(promise);
            return p.then(
                function (v) { hide(); return v; },
                function (e) { hide(); throw e; }
            );
        }

        return { show: show, hide: hide, reset: reset, withLoader: withLoader };
    })();

    /**
     * Thin fetch wrapper that:
     *   - injects Authorization header when a token exists,
     *   - sets JSON Content-Type for body objects,
     *   - calls onUnauthorized when the server returns 401,
     *   - shows the global loader when the call exceeds SLOW_FETCH_MS
     *     (pass { quiet: true } in options to opt out, e.g. silent refresh).
     *
     * Returns the raw Response so callers keep full control over parsing.
     */
    function apiFetch(url, options) {
        options = options || {};
        var quiet = options.quiet === true;
        var loaderMessage = options.loaderMessage;
        // Strip our custom keys before handing off to fetch().
        var pass = {};
        for (var k in options) {
            if (k === "quiet" || k === "loaderMessage") continue;
            pass[k] = options[k];
        }

        var headers = Object.assign({}, pass.headers || {});
        var auth = ErpAuth.authHeader();
        if (auth.Authorization && !headers.Authorization) {
            headers.Authorization = auth.Authorization;
        }
        var body = pass.body;
        if (body && typeof body === "object" && !(body instanceof FormData)) {
            if (!headers["Content-Type"]) headers["Content-Type"] = "application/json";
            body = JSON.stringify(body);
        }
        var finalOpts = Object.assign({}, pass, { headers: headers, body: body });

        var loaderArmed = false;
        var loaderTimer = null;
        if (!quiet) {
            loaderTimer = setTimeout(function () {
                loaderArmed = true;
                ErpLoader.show(loaderMessage || "Loading...");
            }, SLOW_FETCH_MS);
        }

        function settle() {
            if (loaderTimer) { clearTimeout(loaderTimer); loaderTimer = null; }
            if (loaderArmed) { ErpLoader.hide(); loaderArmed = false; }
        }

        return fetch(url, finalOpts).then(function (resp) {
            if (resp.status === 401 && typeof ErpApi.onUnauthorized === "function") {
                try { ErpApi.onUnauthorized(resp); } catch (_) { /* ignore */ }
            }
            settle();
            return resp;
        }, function (err) {
            settle();
            throw err;
        });
    }

    var ErpApi = {
        fetch: apiFetch,
        SLOW_FETCH_MS: SLOW_FETCH_MS,
        // Default 401 handler is a no-op; main app may override to trigger logout/redirect.
        onUnauthorized: null
    };

    window.ErpAuth = ErpAuth;
    window.ErpApi = ErpApi;
    window.ErpLoader = ErpLoader;
})();
