# Design Document

## Overview

This feature adds an admin-controlled global **registration mode** (`OPEN` /
`CLOSED`) to the HelloDJ web-ui. The mode governs whether anonymous visitors may
self-register through the first-party Cognito sign-up flow at `/auth/register`
(`auth.register` → `auth_forms.handle_register`). The login page shows the
current mode and conditionally exposes the Register link; administrators view
and change the mode from the admin panel; every change is written to an audit
trail. Single-use invite links (`/invite/<token>`) are unaffected in either
state.

The design mirrors patterns already established in the codebase:

- A **pure, side-effect-free helper** (`registration_mode.py`) that normalizes a
  raw config value to `OPEN`/`CLOSED` with a **secure default of `CLOSED`**
  (unset OR invalid ⇒ `CLOSED`), in the spirit of `entitlements_core.py` /
  `register_policy.py`. No `boto3`, no Flask — trivially unit- and
  property-testable.
- **Storage** via the existing `ConfigStore.get_global()` / `set_global()` over
  `CoreTable` (`PK=CONFIG#GLOBAL`, `SK=CONFIG`). The mode is one field
  (`registration_mode`) in the global config payload — no new table, no schema
  change.
- **Server-side authoritative enforcement** at the `auth.register` route on both
  GET and POST. Hiding the login-page link is advisory only.
- **Admin-only** view + change control on `pages.admin`, gated by the same
  `pages._is_admin()` check and the hardened guard pattern used by
  `entitlement_routes.py`.
- **Audit** of each change, consistent with `ConfigStore`/`CoreTable` usage,
  stored as `CONFIG#GLOBAL` items with a dedicated audit sort key.

All touched Python files stay under the 500-line ceiling. The bulk of the new
logic lands in a small new module (`registration_mode.py`); `auth.py`,
`auth_forms.py`, and `pages.py` receive minimal edits, plus template edits to
`login.html` and `admin.html`.

## Architecture

```
                       ┌──────────────────────────────────────────┐
   anonymous visitor   │  auth.register (GET/POST)                 │
   ───────────────────▶│    reads mode via registration_mode      │
                       │    CLOSED → redirect login + notice       │
                       │    OPEN   → handle_register (unchanged)   │
                       └───────────────┬──────────────────────────┘
                                       │ reads
                                       ▼
   ┌──────────────────────┐   ┌────────────────────────────┐
   │ registration_mode.py │◀──│ ConfigStore.get_global()    │
   │ (pure normalize)     │   │ PK=CONFIG#GLOBAL, SK=CONFIG │
   │  current_mode(cfg)   │   │ data.registration_mode      │
   │  RegistrationMode    │   └────────────────────────────┘
   └──────────▲───────────┘                 ▲
              │ reads mode                   │ set_global / audit write
   ┌──────────┴───────────┐   ┌──────────────┴──────────────────────┐
   │ pages.login          │   │ pages.admin (GET, admin-only)        │
   │  banner text +       │   │ pages.admin_set_registration_mode    │
   │  register link       │   │   (POST, admin-only + hardened guard)│
   └──────────────────────┘   │   → RegistrationModeAudit write      │
                              └──────────────────────────────────────┘

   /invite/<token>  ── invite_public_routes ──▶ existing invite flow (UNTOUCHED)
```

Data lives on the shared `hellodj-core` single table via the injected
`CoreTable`, so the design has no new infrastructure and degrades gracefully in
no-datastore mode (config store is `None`), matching every other web-ui service.

## Components and Interfaces

### 1. `registration_mode.py` — pure mode helper (new module)

Side-effect-free, dependency-free logic that reads a raw config value and
normalizes it. This is the single source of truth both the enforcement route and
the login banner import.

```python
"""Registration-mode normalization for the web-ui (pure, side-effect-free).

Mirrors the entitlements_core / register_policy split: no boto3, no Flask, no
I/O — just the secure-by-default decision that maps a raw stored config value to
the two-valued Registration_Mode. Imported unchanged by the auth enforcement
route and the login banner so display and enforcement never drift.

Secure default (R1): an absent OR invalid stored value resolves to CLOSED, so
the platform stays invite-only unless an admin deliberately opens it.

Requirements: 1.1, 1.2, 1.3
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "OPEN",
    "CLOSED",
    "VALID_MODES",
    "CONFIG_KEY",
    "BANNER_OPEN",
    "BANNER_CLOSED",
    "normalize_mode",
    "current_mode",
    "banner_text",
    "is_open",
]

OPEN: Final = "OPEN"
CLOSED: Final = "CLOSED"
VALID_MODES: Final = (OPEN, CLOSED)

#: Field name for the mode inside the global config payload (ConfigStore).
CONFIG_KEY: Final = "registration_mode"

#: Fixed login-page banner copy (R3.1 / R3.2).
BANNER_OPEN: Final = "Registration is open — create an account"
BANNER_CLOSED: Final = "Registration is currently closed — invite only"


def normalize_mode(raw: Any) -> str:
    """Return CLOSED unless ``raw`` is exactly a valid mode (R1.1, R1.3).

    Secure by default: ``None``, missing, non-string, or any string that is not
    ``OPEN``/``CLOSED`` (after upper-casing a trimmed string) resolves to
    ``CLOSED``. A valid stored value passes through unchanged (R1.2).
    """
    if isinstance(raw, str):
        candidate = raw.strip().upper()
        if candidate in VALID_MODES:
            return candidate
    return CLOSED


def current_mode(config: dict[str, Any] | None) -> str:
    """Return the effective mode from a global-config payload (R1.1–R1.3).

    Reads :data:`CONFIG_KEY` out of ``config`` (an empty/``None`` payload has no
    key) and normalizes it. Any absent or invalid value yields ``CLOSED``.
    """
    value = (config or {}).get(CONFIG_KEY)
    return normalize_mode(value)


def is_open(config: dict[str, Any] | None) -> bool:
    """Return whether self-registration is currently permitted."""
    return current_mode(config) == OPEN


def banner_text(mode: str) -> str:
    """Return the fixed login banner copy for ``mode`` (R3.1, R3.2)."""
    return BANNER_OPEN if mode == OPEN else BANNER_CLOSED
```

**Why a pure function over a dict, not a store object:** the read path already
has `ConfigStore.get_global()`. Wrapping it in a class buys nothing; the pure
`current_mode(cfg)` keeps the "Registration_Mode_Store" contract (report the
current mode) trivially testable and lets both the route and the template share
one normalization rule.

### 2. Enforcement point — `auth.register` (edit `auth.py`)

The existing route resolves the auth provider then calls `handle_register()`.
The mode gate is inserted **before** the handler runs, on both GET and POST, so a
`CLOSED` mode never renders the form (GET) and never reaches Cognito `SignUp`
(POST).

```python
# in auth.py, inside build_auth_blueprint()

@bp.route("/register", methods=["GET", "POST"])
def register():
    """Initial registration via first-party Cognito ``SignUp`` form (R8.3).

    Gated by the global Registration_Mode: when CLOSED, both GET and POST are
    rejected with a redirect to the login page carrying a registration-closed
    notice, before the form is rendered or Cognito SignUp is invoked (R2.1,
    R2.2). When OPEN the existing first-party flow runs unchanged (R2.3, R2.4).
    """
    provider = route_auth(AuthPurpose.INITIAL_REGISTRATION, UserType.ANONYMOUS)
    assert provider is AuthProvider.COGNITO
    if not registration_mode.is_open(_global_config()):
        return redirect(url_for("pages.login", registration="closed"))
    return handle_register()
```

A tiny module-local helper reads the global config off the app extensions,
degrading to an empty payload (⇒ `CLOSED`, secure default) when no store is
configured:

```python
def _global_config() -> dict[str, Any]:
    """Return the global config payload, or {} in no-datastore mode."""
    store = current_app.extensions.get("config_store")
    return store.get_global() if store else {}
```

The invite route lives in `invite_public_routes.py` (`/invite/<token>`) and is
**not** touched — Invite_Registration is independent of the mode (R2.5).

### 3. Login banner + Register link (edit `pages.login` + `login.html`)

`pages.login` passes the current mode and derived flags into the template:

```python
@bp.route("/login")
def login():
    """Public login landing page (Discord / Cognito / register entry)."""
    store = _config_store()
    mode = registration_mode.current_mode(store.get_global() if store else {})
    return render_template(
        "pages/login.html",
        error=request.args.get("error"),
        registration_mode=mode,
        registration_open=(mode == registration_mode.OPEN),
        registration_banner=registration_mode.banner_text(mode),
        registration_closed_notice=(request.args.get("registration") == "closed"),
    )
```

Template edits (dark-glass preserved; reuse the existing `aria-live` region):

```html
<div aria-live="polite">
  {% if error %}
    <p class="notice notice--danger" role="alert">{{ error }}</p>
  {% elif registration_closed_notice %}
    <p class="notice notice--warning" role="status">
      Registration is currently closed. Access is invite only.
    </p>
  {% elif request.args.get('registered') %}
    ...
  {% endif %}
  <p class="notice notice--info registration-banner" role="status">
    {{ registration_banner }}
  </p>
</div>

<div class="login-card__links">
  {% if registration_open %}
    <a href="{{ url_for('auth.register') }}">Register</a>
    <span aria-hidden="true">·</span>
  {% endif %}
  <a href="{{ url_for('auth.recover') }}">Forgot password?</a>
</div>
```

The Register link is omitted when `CLOSED` (R3.4) and shown when `OPEN` (R3.3);
the banner text is fixed per mode (R3.1, R3.2). Hiding the link is advisory —
the route (component 2) is authoritative.

### 4. Admin control + change route (edit `pages.py` + `admin.html`)

**Read (view control):** `pages.admin` (already admin-gated: login-required +
`_is_admin()` redirect) additionally passes the current mode so the panel shows
it and a set control. Because a non-admin is redirected off `/admin` before any
content renders, the control is never emitted to a non-admin (R4.3, R4.4).

```python
# pages.admin(): add to the render_template context
mode = registration_mode.current_mode(store.get_global() if (store := _config_store()) else {})
...
return render_template(
    "pages/admin.html",
    ...,
    registration_mode=mode,
    registration_open=(mode == registration_mode.OPEN),
)
```

**Change (mutation route):** a new admin-only POST route with the same two-layer
guard `entitlement_routes.py` uses (redirect non-admins early, then a hardened
in-body deny fallback). It normalizes the submitted value, writes the audit
record, and persists via `set_global`.

```python
@bp.route("/admin/registration-mode", methods=["POST"])
def admin_set_registration_mode():
    """Set the global Registration_Mode. Admin-only (R4.2, R4.3, R5.1).

    Two-layer guard: a non-admin is redirected to the dashboard before any
    change, and a hardened in-body check denies with 403 + session clear if a
    non-admin somehow reaches the body (defense in depth). The submitted value
    is normalized to OPEN/CLOSED; an actual change is audited (old, new, admin,
    timestamp) then persisted. Submitting the unchanged value is a no-op (R5.2).
    """
    if not _require_login():
        return redirect(url_for("pages.login"))
    if not _is_admin():
        return redirect(url_for("pages.dashboard"))
    if not _is_admin():  # hardened fallback (Property: admin-only)
        session.clear()
        return "Forbidden", 403
    store = _config_store()
    if store is None:
        return redirect(url_for("pages.admin", regmode="unavailable"))
    requested = registration_mode.normalize_mode(request.form.get("mode"))
    _apply_registration_mode(store, requested)
    return redirect(url_for("pages.admin", regmode="saved"))
```

The apply helper is where the audit + persist ordering lives (see
`RegistrationModeStore` write below). Non-admins are denied at both the panel
and the change route and the mode is never mutated (R4.3).

`admin.html` gains a section (glass-panel, matching the existing invite/users
sections) with the current mode and a two-option control that POSTs to the new
route:

```html
<div class="glass-panel panel-section">
  <h2 class="panel-section__title">Self-registration</h2>
  <p class="panel-section__muted">
    Controls whether anonymous visitors can create an account at
    <code>/register</code>. Invite links always work regardless of this setting.
  </p>
  <p class="panel-section__muted" aria-live="polite">
    Current mode: <strong>{{ registration_mode }}</strong>
  </p>
  <form class="field-row" method="post"
        action="{{ url_for('pages.admin_set_registration_mode') }}">
    <button type="submit" name="mode" value="OPEN"
            class="btn {{ 'btn-primary' if not registration_open else 'btn-ghost' }}">
      Open registration
    </button>
    <button type="submit" name="mode" value="CLOSED"
            class="btn {{ 'btn-primary' if registration_open else 'btn-ghost' }}">
      Close registration
    </button>
  </form>
</div>
```

### 5. Audit write — `RegistrationModeStore` semantics (in `registration_mode.py` or a thin `pages.py` helper)

Each mode change writes a **Mode_Change_Audit_Record** with the acting admin
identity, previous value, new value, and timestamp. Consistent with
`ConfigStore`/`CoreTable`, the audit item is a `CONFIG#GLOBAL`-partition item
with a dedicated sort key so it co-locates with the setting it audits and sorts
chronologically:

- **Setting:** `PK=CONFIG#GLOBAL`, `SK=CONFIG`, `data.registration_mode` (existing
  ConfigStore item — reuses `set_global`).
- **Audit:** `PK=CONFIG#GLOBAL`, `SK=REGMODEAUDIT#<iso-ts>#<rand>`,
  `entityType=RegistrationModeAudit`,
  `data={admin_sub, old, new, at}` (written with `CoreTable.put_new`, mirroring
  `EntitlementService._write_audit_entries`).

The apply helper follows the same **write-before-apply** ordering as
`EntitlementService.set_fields`: compute the current mode, and only if the
requested value differs, write the audit entry first (`put_new`), then persist
via `ConfigStore.set_global({registration_mode: new})`. A no-op (requested ==
current) writes nothing and leaves the mode unchanged (R5.2).

```python
def apply_mode_change(
    config_store: ConfigStore,
    core_table: CoreTable,
    *,
    requested: str,
    admin_sub: str,
) -> str:
    """Audit-then-persist a mode change; no-op when unchanged (R5.1, R5.2)."""
    current = current_mode(config_store.get_global())
    new = normalize_mode(requested)
    if new == current:
        return current                       # R5.2 no-op, no audit row
    at = _now_iso()
    core_table.put_new(
        GLOBAL_CONFIG_PK,
        f"REGMODEAUDIT#{at}#{secrets.token_hex(4)}",
        "RegistrationModeAudit",
        {"admin_sub": admin_sub, "old": current, "new": new, "at": at},
    )
    config_store.set_global({CONFIG_KEY: new})   # R4.2 persist
    return new
```

`admin_sub` comes from `session["user"]["sub"]` (the verified Cognito subject),
exactly as `entitlement_routes._admin_sub()` sources it. If the audit `put_new`
fails, `set_global` is never reached — the mode is not changed and no partial
state results. To keep `pages.py` under the line ceiling and reuse the CoreTable
reference, `apply_mode_change` lives in `registration_mode.py` (it takes the
store + table as arguments, staying free of Flask globals); `pages.py`'s
`_apply_registration_mode` is a two-line adapter that pulls `config_store` and
the underlying `core_table` off `current_app.extensions`.

### Wiring

No new `bootstrap.py` service is required: `registration_mode.py` is pure and
imported directly. The change route needs the underlying `CoreTable` for the
audit write; `ConfigStore` already holds it (`self._core`). We expose it via a
narrow read-only accessor on `ConfigStore` (`core_table` property) rather than
reaching into a private attribute, keeping the audit write on the same table the
config uses.

## Data Models

**Global config item (existing, one new field):**

| Attribute | Value |
| --- | --- |
| `PK` | `CONFIG#GLOBAL` |
| `SK` | `CONFIG` |
| `entityType` | `Config` |
| `data.registration_mode` | `"OPEN"` \| `"CLOSED"` (absent ⇒ `CLOSED`) |

**Mode_Change_Audit_Record (new item type):**

| Attribute | Value |
| --- | --- |
| `PK` | `CONFIG#GLOBAL` |
| `SK` | `REGMODEAUDIT#<iso8601-ts>#<rand>` |
| `entityType` | `RegistrationModeAudit` |
| `data.admin_sub` | acting admin's Cognito subject |
| `data.old` | previous mode (`OPEN`/`CLOSED`) |
| `data.new` | new mode (`OPEN`/`CLOSED`) |
| `data.at` | ISO-8601 timestamp |

`RegistrationMode` is the closed set `{"OPEN", "CLOSED"}`; all reads pass through
`normalize_mode`, so no other value can escape the helper.

## Error Handling

- **No ConfigStore (degraded mode):** every read path (`_global_config`,
  `pages.login`, `pages.admin`) treats an absent store as an empty payload, which
  normalizes to `CLOSED` — fail-safe/invite-only. The change route returns to the
  admin panel with a `regmode=unavailable` notice and mutates nothing.
- **Invalid / malicious submitted value:** `normalize_mode` collapses anything
  that is not exactly `OPEN`/`CLOSED` to `CLOSED`, so a tampered form field can
  only ever close registration, never open it to an unintended state.
- **Audit write failure:** write-before-apply ordering means a failed `put_new`
  aborts before `set_global`; the mode is unchanged and no untracked change
  occurs.
- **Non-admin reaching the change route:** redirected before any mutation; the
  hardened in-body fallback returns HTTP 403 and clears the session if the
  redirect guard is somehow bypassed. The mode is never changed by a non-admin.
- **Cognito unconfigured:** unchanged from today — when `OPEN`, `handle_register`
  already renders an "auth unavailable" state (R6.4 of the auth spec); the mode
  gate runs first and is independent of Cognito availability.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — a formal statement about what the system should
do. Properties bridge human-readable specifications and machine-verifiable
guarantees.*

### Property 1: Secure default for absent or invalid values

*For any* raw config value that is not exactly a valid mode string — including a
missing key, `None`, a non-string, or any string that is not `OPEN`/`CLOSED`
after trimming and upper-casing — `current_mode` / `normalize_mode` returns
`CLOSED`.

**Validates: Requirements 1.1, 1.3**

### Property 2: Valid stored value passes through

*For any* stored value that is a valid mode (`OPEN` or `CLOSED`, in any casing or
surrounding whitespace), `current_mode` returns that mode normalized to its
canonical upper-case form.

**Validates: Requirements 1.2**

### Property 3: Login page reflects the current mode

*For any* mode, the rendered login page displays the banner text that matches
that mode and includes the `/register` link if and only if the mode is `OPEN`.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

### Property 4: CLOSED rejects registration on GET and POST

*For any* request method in {GET, POST} to `/auth/register` while the mode is
`CLOSED`, the response is a redirect to the login page carrying a
registration-closed notice, the self-registration form is not rendered, and the
Cognito `SignUp` collaborator is never invoked.

**Validates: Requirements 2.1, 2.2**

### Property 5: Admin mode change round-trips

*For any* target mode in {`OPEN`, `CLOSED`} submitted by an admin, after the
change route runs, `current_mode` (read back from the global config) equals the
submitted target mode.

**Validates: Requirements 2.3, 4.2**

### Property 6: Only admins can change the mode

*For any* non-admin session (anonymous or Discord-authenticated) and *any*
submitted target value, the change route denies the request and the stored
`Registration_Mode` is identical before and after; the admin panel likewise
never renders the mode control to a non-admin.

**Validates: Requirements 4.3, 4.4**

### Property 7: Every actual change is audited

*For any* change of the mode from one valid value to the other by an admin,
exactly one Mode_Change_Audit_Record is written containing the acting admin
identity, the previous value, the new value, and a timestamp.

**Validates: Requirements 5.1**

### Property 8: Unchanged submission is idempotent

*For any* current mode, submitting that same value leaves the stored
`Registration_Mode` at that value and writes no audit record.

**Validates: Requirements 5.2**

### Property 9: Invites are independent of the mode

*For any* mode in {`OPEN`, `CLOSED`}, a request to `/invite/<token>` is processed
by the existing invite flow and is never redirected or blocked by the
registration-mode gate.

**Validates: Requirements 2.5**

## Testing Strategy

**Dual approach:** property-based tests for the universal properties above, and
example/route tests for specific rendered strings, redirects, and the
happy-path Cognito chain.

**Unit / property tests (pure helper — `tests/test_registration_mode.py`, Hypothesis):**

- **Property 1** — generate arbitrary values (missing key, `None`, ints, random
  strings excluding valid modes) → assert `current_mode`/`normalize_mode` returns
  `CLOSED`. (min 100 iterations)
- **Property 2** — generate `OPEN`/`CLOSED` with random casing + surrounding
  whitespace → assert canonical passthrough. (min 100 iterations)
- **Property 8** (helper level) — `apply_mode_change` with `requested == current`
  returns current and writes nothing (fake CoreTable spy).

**Flask test-client route tests (`tests/test_registration_mode_routes.py`):**

- **Property 3** — render `/login` with a fake ConfigStore returning each mode;
  assert exact banner strings (`BANNER_OPEN`/`BANNER_CLOSED`) and Register-link
  presence/absence. (EXAMPLE assertions on 3.1–3.4)
- **Property 4** — GET and POST `/auth/register` with mode `CLOSED` and a spy
  `CognitoAuth`; assert 302 to login with `registration=closed`, form not in
  body, and `sign_up` never called. Property-style over both methods.
- **Property 5** — as an admin session, POST each target mode to
  `/admin/registration-mode`; assert `get_global()` and `current_mode` reflect
  it. (both directions)
- **Property 6** — for anonymous and Discord (non-admin) sessions and each target
  value, POST the change route; assert redirect/403 and that `get_global()` is
  unchanged; GET `/admin` redirects (control never rendered).
- **Property 7** — perform each change direction as admin; assert exactly one
  `REGMODEAUDIT#` item exists on `CONFIG#GLOBAL` with `admin_sub`, correct
  `old`/`new`, and an `at` timestamp.
- **Property 8** — POST the current mode as admin; assert mode unchanged and no
  new audit item.
- **Property 9** — for each mode, GET `/invite/<token>` (fake invite service);
  assert the request reaches invite handling (not the mode redirect).
- **OPEN happy path (R2.3, R2.4, EXAMPLE)** — GET renders the form; POST reaches
  `handle_register` → `sign_up` (existing register tests remain green).

**Property test configuration:** ≥100 iterations per property test; each test is
tagged **Feature: registration-mode-control, Property N: {property text}** and
references its design property. Fakes (in-memory CoreTable / ConfigStore, spy
CognitoAuth, session-injecting test client) keep everything in-process — no AWS,
no Cognito.

**Gate commands (must pass before push):**

```bash
cd platform/components/web-ui && ruff check --target-version py314 .
cd platform/components/web-ui && python3 -m pytest tests/ -q
python3 platform/tools/check_line_count.py platform/components/web-ui   # 500-line ceiling
```
