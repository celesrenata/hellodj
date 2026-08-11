# Live Review Findings — HelloDJ Web UI

**Site**: `https://hellodj.celestium.life`
**Date**: 2026-08-11 | **Session**: 19:18–21:36Z
**Method**: Fresh live investigation via Playwright (visual-debug) + screenshot interpretation by visual-auditor. Programmatic evidence (network/console/computed-style/contrast) cross-referenced with visual inspection.

## Evidence paths
- Screenshots: `evidence/live-review/00-baseline.png` … `11-dashboard-desktop.png` (12 files)
- Raw network evidence: `evidence/live-review/raw-evidence-network.json`
- Raw contrast/computed-style evidence: `evidence/live-review/raw-evidence-contrast.json`
- Raw console evidence: `evidence/live-review/raw-evidence-console.json`
- Step-by-step log: `evidence/live-review/evidence.md`
- Host-mount copies for the auditor: `/home/celes/.local/share/playwright-mcp/00-baseline.png` … `11-dashboard-desktop.png`

---

## 1. Functional findings (programmatic)

**All API calls returned HTTP 200** — no 4xx/5xx, no failed requests, no console errors/warnings on any page.

| Endpoint | Method | Status | Result |
|----------|--------|--------|--------|
| `/api/status` | GET | 200 | ok |
| `/api/nfs-status` | GET | 200 | ok |
| `/api/config` | GET | 200 | ok |
| `/api/config` | POST (save) | 200 | "Configuration saved" |
| `/api/backups` | GET | 200 | ok |
| `/api/backups` | POST (create) | 200 | backup created |
| `/api/guilds` | GET | 200 | `{"guilds":[]}` |
| `/api/playlists` | GET | 200 | `{"playlists":[]}` |
| `/api/blacklist` | GET | 200 | `{"blacklist":[]}` |

**Working features (verified)**
- Create Backup (POST 200 → new row appears)
- Save Configuration (POST 200 → persisted; changed default_source to SoundCloud and it saved)
- Restore modal opens correctly with Cancel/Restore; Cancel closes cleanly

**No functional breakage found** — the app's core flows work. Issues are visual/accessibility, not functional.

---

## 2. Critical visual/accessibility issue — dark theme + light-theme text (programmatic + confirmed visually)

The site uses a **dark theme** (card bg `rgba(30,30,45,0.6)`, card headers `rgb(37,37,55)`, page text `#e0e0e0`) but **Bootstrap's default light-theme text color `#212529` (near-black) remains on many elements**. Result: **near-black text on near-black background = unreadable**.

### Measured contrast ratios (all FAIL vs 4.5:1 WCAG AA)
| Location | Text | fg | bg | ratio |
|----------|------|----|----|-------|
| Dashboard NFS card | Config Directory: / Writable: / Data Directory: / Config Contents: / hellodj-config.json | `#212529` | `rgba(30,30,45,0.6)` | **1.06:1** |
| Config page | **Every form label** (Bot Token, Application ID, Public Key, Host, Port, Password, Client ID, Client Secret, API Key, wake-word checkbox, model path, STT, TTS, LLM keys, source/autoplay/repeat) | `#212529` | `rgba(30,30,45,0.6)` | **1.06:1** |
| Dashboard NFS card | code `/app/config`, `/app/data` | `#d63384` | `rgba(30,30,45,0.6)` | **3.65:1** |
| Backups | Restore button text | `#bb86fc` | white | **2.65:1** |
| Restore modal | "This cannot be undone!" | `#dc3545` | `rgb(45,45,68)` | **2.95:1** |

**Visual-auditor confirmed**: form labels and NFS-card labels are effectively unreadable; stat-card numbers lack contrast; delete buttons thin/low-contrast.

---

## 3. Accessibility findings (programmatic + visual)

### Unnamed/icon-only buttons with NO accessible name (no text, no aria-label)
- `.navbar-toggler` — mobile hamburger menu toggle (screen-reader users get no label)
- **8× `.btn.btn-sm.btn-outline-danger`** — delete buttons on backups rows (no label, no aria-label, no title)
- `.btn-close` — restore modal close button

### Other accessibility issues
- No hover-state styling confirmed on interactive elements (buttons/links)
- Warning text fails 4.5:1 contrast
- Restore buttons fail 4.5:1 contrast (2.65:1)

---

## 4. Visual issues (visual-auditor)
- **Dashboard** (00, 11): NFS Storage Info labels near-unreadable dark-on-dark; stat-card numbers lack sufficient contrast.
- **Config** (01, 10): all form labels unreadable; input values visible but labels invisible.
- **Backups** (04, 05): delete buttons render as thin red outlines with poor contrast on dark background — hard to identify/click.
- **Restore modal** (06): warning text red-on-dark fails contrast; buttons differentiated but hover states unverified.
- **Mobile** (08, 09, 10): stat cards stack correctly; menu items visible/readable (PASS); config form layout intact though label spacing tight. Mobile layout itself is OK — the theme/contrast problem dominates.

---

## 5. Root cause (from evidence, not speculation)
The theme override in the app sets dark backgrounds but does **not** override Bootstrap's default `--bs-body-color` / `text-body` / `form-label` colors, so elements relying on Bootstrap's light-theme defaults keep `#212529`. This is the single dominant defect affecting the dashboard NFS card, the entire config form, and several buttons. Fix: define a proper dark-theme palette (light text `#e0e0e0`-family) for `body`, `.form-label`, `.card`, `.btn`, `.modal`, and add accessible names to the icon buttons.

---

## 6. Issue summary (priority order)
1. **P0 — Unreadable dark-on-dark text** across config form labels and dashboard NFS card (1.06:1). Theme color mismatch.
2. **P1 — WCAG contrast failures** on Restore buttons (2.65:1), code text (3.65:1), modal warning (2.95:1), stat-card numbers.
3. **P1 — Unnamed buttons** (hamburger, 8 delete buttons, modal close) inaccessible to screen readers.
4. **P2 — Delete-button visibility** (thin outline-danger on dark bg).
5. **P2 — Missing hover states** on interactive elements.
6. **P3 — Mobile label spacing** on config form (minor).

**No functional/console/network errors found** — the site's backend and UI flows operate correctly; the defects are theme-contrast and accessibility related.
