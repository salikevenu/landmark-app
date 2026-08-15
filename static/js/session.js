/* Canonical LANDMARK session: HttpOnly JWT cookies + CSRF header.
   Do not store access/refresh tokens in localStorage. */
(function (global) {
  var LOGIN_URL = "/api/auth/public/login";
  var refreshInFlight = null;

  function getCookie(name) {
    var parts = ("; " + document.cookie).split("; " + name + "=");
    if (parts.length < 2) return "";
    return decodeURIComponent(parts.pop().split(";").shift() || "");
  }

  function csrfToken(refresh) {
    return getCookie(refresh ? "csrf_refresh_token" : "csrf_access_token");
  }

  function withCsrf(headers, method, refresh) {
    headers = headers || {};
    var m = String(method || "GET").toUpperCase();
    if (["POST", "PUT", "PATCH", "DELETE"].indexOf(m) === -1) return headers;
    if (headers["X-CSRF-TOKEN"] || headers["X-CSRF-Token"]) return headers;
    var token = csrfToken(!!refresh);
    if (token) headers["X-CSRF-TOKEN"] = token;
    return headers;
  }

  function redirectToLogin() {
    if (window.location.pathname.indexOf("/login") !== -1) return;
    window.location.href = LOGIN_URL;
  }

  function clearLegacyClientTokens() {
    try {
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      localStorage.removeItem("token");
      localStorage.removeItem("impersonated_token");
      localStorage.removeItem("user");
      localStorage.removeItem("role");
    } catch (e) {}
  }

  async function tryRefresh() {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async function () {
      var headers = withCsrf({ "Content-Type": "application/json" }, "POST", true);
      var res = await fetch("/api/refresh", {
        method: "POST",
        credentials: "include",
        headers: headers
      });
      return res.ok;
    })();
    try {
      return await refreshInFlight;
    } finally {
      refreshInFlight = null;
    }
  }

  async function authFetch(url, options) {
    options = options || {};
    var method = options.method || "GET";
    var headers = withCsrf(Object.assign({}, options.headers || {}), method, false);
    var fetchOpts = Object.assign({}, options, {
      headers: headers,
      credentials: "include"
    });
    delete fetchOpts.authRedirect;
    var res = await fetch(url, fetchOpts);
    var isRefreshCall = String(url).indexOf("/api/refresh") !== -1;
    if (res.status === 401 && !isRefreshCall) {
      var refreshed = await tryRefresh();
      if (refreshed) {
        headers = withCsrf(Object.assign({}, options.headers || {}), method, false);
        res = await fetch(url, Object.assign({}, fetchOpts, { headers: headers }));
      }
      if (res.status === 401 && options.authRedirect !== false) {
        clearLegacyClientTokens();
        redirectToLogin();
      }
    }
    return res;
  }

  global.LandmarkSession = {
    getCookie: getCookie,
    csrfToken: csrfToken,
    authFetch: authFetch,
    tryRefresh: tryRefresh,
    redirectToLogin: redirectToLogin,
    clearLegacyClientTokens: clearLegacyClientTokens,
    LOGIN_URL: LOGIN_URL
  };
})(window);
