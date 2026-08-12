# Visual Re-Audit — HelloDJ Web UI (`https://hellodj.celestium.life`)
Session: 2026-08-12T01:36-02:58Z | Mode: visual-debug (gathering) + visual-auditor (interpretation)
Purpose: **VERIFICATION pass** against `evidence/live-review/` baselines after contrast/accessibility fixes deployed (web-ui digest sha256:818b64f2..., pod hellodj-web-ui-8568b7db96-ln8n8).

## Screenshot inventory (host-accessible)
Copied into workspace at `evidence/visual-reaudit/` AND present on host mount `/home/celes/.local/share/playwright-mcp/`.

| # | File | State captured |
|---|------|----------------|
| 00 | `evidence/visual-reaudit/00-baseline.png` | Dashboard (index) full page, desktop 1280x720 |
| 01 | `evidence/visual-reaudit/01-config.png` | Config page, full form, desktop |
| 02 | `evidence/visual-reaudit/02-config-form.png` | Config form filled (provider → SoundCloud) |
| 03 | `evidence/visual-reaudit/03-after-save.png` | Config after Save (POST 200) |
| 04 | `evidence/visual-reaudit/04-backups.png` | Backups list (1 backup) |
| 05 | `evidence/visual-reaudit/05-backups-after-create.png` | Backups after Create Backup (POST 200, now 2) |
| 06 | `evidence/visual-reaudit/06-restore-modal.png` | Restore modal open |
| 07 | `evidence/visual-reaudit/07-blacklist.png` | Blacklist page (empty), desktop |
| 08 | `evidence/visual-reaudit/08-guilds.png` | Guilds page (empty), desktop |
| 09 | `evidence/visual-reaudit/09-playlists.png` | Playlists page (empty), desktop |
| 10 | `evidence/visual-reaudit/10-mobile.png` | Dashboard @ 375x667 (hamburger visible) |
| 11 | `evidence/visual-reaudit/11-mobile-menu.png` | Mobile nav menu expanded |
| 12 | `evidence/visual-reaudit/12-dashboard-final.png` | Dashboard final, desktop 1280x720 |

Host-mount equivalents: `/home/celes/.local/share/playwright-mcp/{file}`.

## Acceptance Gate Verification

### 1. Contrast audit — ALL previously-failing elements now PASS
Raw dumps: `raw-evidence-contrast.json` (dashboard), `raw-evidence-contrast-config.json` (config labels), `raw-evidence-modal.json` (modal).

Body theme: `#e0e0e0` on card bg `rgba(30,30,45,0.6)`.

| Element | Was | Now | Ratio | Verdict |
|---------|-----|-----|-------|---------|
| `.form-label`/`.form-check-label`/`label` (config, all) | 1.06:1 | `#e0e0e0` | **12.44:1** | ✅ PASS |
| `.card` text (config cards) | 1.06:1 | `#e0e0e0` | **12.44:1** | ✅ PASS |
| `#nfs-info strong` labels/values (dashboard) | 1.06:1 | `#e0e0e0` | **12.44:1** | ✅ PASS |
| Stat-card numbers `h2.display-4, #stat-*` | 1.06:1 | `#f5f5ff` | **15.16:1** | ✅ PASS |
| Code text (`code`) | 3.65:1 | `#f48fb1` | **7.36:1** | ✅ PASS |
| "Mounted" green badge (`.text-success`) | 3.62:1 | `#6fcf7e` | **8.53:1** | ✅ PASS |
| Restore/`btn-outline-hellodj` (dashboard & backups) | 2.65:1 | `#e0e0e0` | **12.44:1** | ✅ PASS |
| Restore modal warning "This cannot be undone!" | 2.95:1 | `#ff6b6b` | **4.82:1** | ✅ PASS |
| Delete `.btn-outline-danger` (backups table) | 2.27:1 | text `#ff8a8a` border `#ff6b6b` | **7.24:1** (vs card bg) | ✅ PASS |
| Save/Reload buttons (config) | — | `#e0e0e0` | **15.9:1** | ✅ PASS |

`failures` array in dashboard dump: **empty (0 failures)**. Config `formLabelFails`: **empty**. All acceptance elements pass WCAG AA.

**NOTE / residual finding:** the modal **Cancel button** (`#e0e0e0` on Bootstrap secondary `rgb(108,117,125)`) measures **3.55:1** — below 4.5:1. This was not in the prior failing list, but is a minor residual AA miss in the modal. Delete/restore buttons pass against the dark card bg (the earlier 2.27:1 reading was against a transparent table cell; the real backing is the dark card at 7.24:1).

### 2. Accessible names — ALL present (verified programmatically)
- `.navbar-toggler` → `aria-label="Toggle navigation"` ✅
- Modal `.btn-close` → `aria-label="Close"` ✅
- 8× delete buttons → `aria-label="Delete backup"` + `title="Delete <backup>"` ✅ (snapshot shows accessible name "Delete backup")

### 3. Functional / console / network
- **All API calls return 200** — `GET/POST /api/config`, `GET/POST /api/backups`, `/api/status`, `/api/nfs-status` all 200 (see `raw-evidence-network.json`).
- **Create Backup** works: POST 200, new row appears (1 → 2 backups).
- **Save Configuration** works: POST 200 `{"message":"Configuration saved","status":"ok"}` (default_source → SoundCloud).
- **Restore modal** opens with Cancel/Restore/Close; Close closes cleanly.
- **Console:** 0 errors, 0 warnings on all pages.

### 4. ❌ FAIL — DOM VERBOSE warning still present on /config
The prior `[DOM] Password field is not contained in a form` VERBOSE message is **STILL PRESENT** (7 instances on `/config`). It is logged at VERBOSE/info level, **not** as a console warning or error (0 warnings, 0 errors). The acceptance gate explicitly required this DOM VERBOSE warning to be GONE — it is **NOT**. The wrapping `<form>` fix was not applied, OR the password fields remain outside the form element.

## Steps verified
1. Loaded dashboard (index): stat cards, NFS card, config status, quick actions — all rendered, contrast passes.
2. Config page: all 22+ form labels visible `#e0e0e0` at 12.44:1; filled form + saved (POST 200).
3. Backups: created backup (POST 200), restore modal opened (warning `#ff6b6b` 4.82:1), close worked.
4. Blacklist/Guilds/Playlists pages: rendered, no console/network errors.
5. Mobile (375x667): dashboard renders, hamburger accessible with name, menu opens.
6. Desktop restore: final dashboard.

## Questions for visual-auditor
1. Confirm dark-theme cards render with **readable light text** (labels, stat numbers, code) — no dark-on-dark.
2. Confirm stat cards show correct numbers (Guilds=0, Playlists=0, Backups=2) on dark cards.
3. Confirm backups delete buttons (outline-danger, `#ff8a8a`/`#ff6b6b`) are visible/clickable on dark card.
4. Confirm restore modal (06) warning `#ff6b6b` readable, buttons aligned.
5. Confirm mobile menu toggle (11) renders with menu items visible.
6. **Verify the Cancel button in the restore modal** — programmatic 3.55:1 may render low-contrast against gray.
