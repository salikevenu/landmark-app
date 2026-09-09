# LANDMARK Critical Contracts

**Purpose:** future feature work (by a human, Claude Code, Cursor, or any
other coding agent) must not *silently* break authentication, the PWA, or
offline behavior. This document is short on purpose — it is a checklist,
not a spec.

> **Future code can change. Critical LANDMARK behavior cannot silently
> disappear.**

This is not a new testing framework. It is a thin layer on top of the
existing pytest suite: a few structural tests that catch a route, a file,
or a piece of logic quietly vanishing, plus pointers to the much larger
existing test files that already prove real behavior.

---

## 1. What must never be broken accidentally

- A user who registers once and signs in once must stay signed in across
  page loads, tab closes, and browser restarts — **without needing a
  fresh OTP** — until they explicitly log out or their token genuinely
  expires.
- A network hiccup must never be treated as a logout.
- The PWA must remain installable, with the correct name and icon, and
  the service worker must keep controlling the whole site (scope `/`),
  not just `/static/`.
- No protected route may lose its auth check "by accident" during a
  refactor.
- No critical file (a layout, `manifest.json`, an icon, `session.js`,
  `sw.js`) may be deleted or renamed without every reference to it being
  updated in the same change.

---

## 2. Authentication contract

**Rule:** REGISTER ONCE → SIGN IN ONCE → REMAIN AUTHENTICATED.

Protected by (already existing, do not duplicate):
- `tests/test_auth_session.py` — cookie flags (`HttpOnly`, `Secure`,
  `SameSite`, explicit `Max-Age`), CSRF header handling, admin/user login
  session-redirect behavior, banned-user rejection.
- `tests/test_silent_refresh.py` — the `/api/refresh/silent` full-page
  flow (expired/missing/invalid access token + valid refresh cookie →
  silent re-auth, not a forced OTP).
- `tests/test_referral_attribution.py` — registration/OTP/referral
  end-to-end flow.
- `tests/test_security_stage*.py` — admin/role/authorization edge cases.

New, additive (`tests/test_critical_contracts.py`):
- Critical auth routes (`/api/auth/send-otp`, `/api/auth/verify-otp`,
  `/api/auth/public/login`, `/api/refresh`, `/api/refresh/silent`,
  `/logout`, `/admin/login`) still exist in the URL map.
- `/admin/dashboard` and `/api/admin/stats` still reject a request with
  no session.
- `static/js/session.js`'s `authFetch` has no `catch` block and its
  login-redirect is reachable only after a real `401` — i.e. a network
  failure cannot become a logout.
- `services/jwt_session.py`'s `lookup_jwt_user` still rejects blocked and
  inactive users.
- A known-good list of authenticated user pages still call
  `LandmarkSession.authFetch` for their `/api/` calls.

**Do not weaken:** cookie `Secure`/`HttpOnly`/`SameSite`, CSRF
double-submit, the live DB re-check inside `admin_required`, or the
banned/inactive exclusion in `lookup_jwt_user`.

---

## 3. PWA contract

Protected by `tests/test_pwa_installability.py` (20 tests, already
passing — **do not duplicate them**):
- `static/manifest.json` exists, is valid, and keeps `start_url: "/"`
  and `scope: "/"`.
- `static/images/icon-192.png`, `icon-512.png`, and their maskable
  counterparts exist, are valid PNGs, and match their declared sizes.
- `/sw.js` is served from the site root (not only `/static/sw.js`), with
  `Service-Worker-Allowed: /`, and both `layout_public.html` and
  `layout_app.html` register it with `{ scope: '/' }`.
- `static/js/pwa-install.js` only ever uses the real
  `beforeinstallprompt` → `event.prompt()` flow — no fake/forced install
  path.

New, additive (`tests/test_critical_contracts.py`):
- None of the above PWA files have been deleted or renamed
  (`CriticalFilesExistTests`).

**Do not weaken:** never go back to registering the service worker from
`/static/sw.js` without a root-scoped alternative, never remove the
`Service-Worker-Allowed` header, never make `pwa-install.js` trigger
anything other than the browser's own `prompt()`.

---

## 4. Offline contract

- Temporary network failure ≠ logout — proved structurally in
  `test_critical_contracts.py` (`NetworkFailureIsNotLogoutTests`) and
  behaviorally in `test_silent_refresh.py`.
- Persistent authentication survives an application restart — proved by
  the explicit `Max-Age` cookie assertions in `test_auth_session.py`
  (`AppJwtConfigTests`).
- The service worker keeps registering on every page load — proved by
  `test_pwa_installability.py`'s `ServiceWorkerScopeTests`.

---

## 5. Required verification before committing

Before merging any change that touches authentication, PWA/manifest,
service worker, offline behavior, or `templates/layouts/*.html`:

```
python -m pytest tests/test_auth_session.py tests/test_silent_refresh.py \
    tests/test_pwa_installability.py tests/test_critical_contracts.py -q
python -m pytest -q          # full suite
git diff --check
```

All of the above must pass. A change that requires editing one of these
tests to make it pass again needs an explicit reason in the commit
message — not a silent edit.

---

## 6. Which tests protect each contract

| Contract | Primary tests |
|---|---|
| Authentication | `test_auth_session.py`, `test_silent_refresh.py`, `test_referral_attribution.py`, `test_security_stage*.py` |
| PWA | `test_pwa_installability.py` |
| Offline / network-failure-≠-logout | `test_critical_contracts.py::NetworkFailureIsNotLogoutTests`, `test_silent_refresh.py` |
| Application structure (routes/files) | `test_critical_contracts.py::CriticalRoutesExistTests`, `::CriticalFilesExistTests` |

---

## 7. How future developers and AI coding agents should work here

**Before modifying authentication, PWA, service worker, manifest,
offline behavior, or shared layouts, inspect the existing implementation
and run the relevant regression tests first.**

**Do not replace an existing working architecture merely because a
simpler implementation is possible.** If a change would touch any file
listed in section 6, run that contract's tests before and after — not
just the file(s) you edited — and read this document first. If a
regression test fails, that is the system telling you something real
broke; fix the regression, don't edit the test to match the new
(broken) behavior unless you can explain in the commit message why the
old behavior was wrong.

When adding a new critical route, template, or static asset that should
never silently disappear, add it to the relevant list in
`tests/test_critical_contracts.py` rather than creating a new test file.
