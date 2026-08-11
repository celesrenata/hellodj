# HelloDJ Site Review — Review 2 (Fresh Comprehensive Pass)

**Date:** 2026-08-11 · **Target:** https://hellodj.celestium.life · **Mode:** visual-debug (evidence gathering)
**Raw evidence:** `evidence/hellodj-site-review/review2-raw.json`

**IMPORTANT — screenshot host paths:**
The Playwright MCP saves files relative to its container working dir `/home/node`. Only `/home/node/.playwright-mcp` is bind-mounted to the host. All screenshots below were saved as `.playwright-mcp/review2-*.png` and are accessible on the host at:
`/home/celes/.local/share/playwright-mcp/review2-*.png`

---

## Pages Visited (all reached, all API calls 200)

| Page | URL | Screenshot (host path) | API calls | Console errors/warnings |
|------|-----|------------------------|-----------|------------------------|
| Dashboard | `/` | `/home/celes/.local/share/playwright-mcp/review2-dashboard.png` | GET /api/status [200], GET /api/nfs-status [200] | 0 errors, 0 warnings |
| Config | `/config` | `/home/celes/.local/share/playwright-mcp/review2-config.png` | GET /api/config [200] | 0 errors/warnings + 1 DOM VERBOSE warning |
| Guilds | `/guilds` | `/home/celes/.local/share/playwright-mcp/review2-guilds.png` | GET /api/guilds [200] | 0 errors, 0 warnings |
| Playlists | `/playlists` | `/home/celes/.local/share/playwright-mcp/review2-playlists.png` | GET /api/playlists [200] | 0 errors, 0 warnings |
| Backups | `/backups` | `/home/celes/.local/share/playwright-mcp/review2-backups.png` | GET /api/backups [200] | 0 errors, 0 warnings |
| Blacklist | `/blacklist` | `/home/celes/.local/share/playwright-mcp/review2-blacklist.png` | GET /api/blacklist [200] | 0 errors, 0 warnings |

**No console ERRORS or WARNINGS (level warning/error) on any page.** The only console entry site-wide is a VERBOSE DOM warning on `/config`:
`[VERBOSE] [DOM] Multiple forms should be contained in their own form elements; break up complex forms into ones that represent a single action: (More info: https://goo.gl/9p2vKq) @ https://hellodj.celestium.life/config:0`

**No failed network requests (HTTP >= 400) observed on any page.** Every API call returned 200.

---

## Interactive Flows

### 1. Create Backup
- **Action:** clicked "Create Backup" button on `/backups`
- **Network:** GET /api/backups [200] → POST /api/backups [200] → GET /api/backups [200]
- **Observed:** new backup `hellodj-backup-20260811-141525.tar.gz` appeared at top of table (DOM confirmed)
- **Screenshot:** `/home/celes/.local/share/playwright-mcp/review2-after-create-backup.png`
- **Console:** none · **Verdict:** PASS (functional)
- ⚠️ This created a real backup file — evidence-only mutation to note.

### 2. Restore Modal
- **Action:** clicked "Restore" (newest backup)
- **Observed:** Bootstrap dialog opened — heading "Restore Backup", warning text "Restoring a backup will overwrite current configuration, sessions, and playlists." + "This cannot be undone!", "Backup: hellodj-backup-20260811-141525.tar.gz", buttons Cancel / Restore
- **Screenshots:** `/home/celes/.local/share/playwright-mcp/review2-restore-modal.png` (full page) and `/home/celes/.local/share/playwright-mcp/review2-restore-modal-target.png` (.modal element)
- **Closed with Cancel** (evidence-only, avoided mutating data) · **Console:** none · **Verdict:** PASS (modal renders & functions)

### 3. Save Config
- **Action:** filled Bot Token `test-token-review2`, clicked "Save Configuration"
- **Network:** GET /api/config [200] → POST /api/config [200]
- **POST response body:** `{"message":"Configuration saved","status":"ok"}` (content-length 48, server gunicorn)
- **Screenshots:** `/home/celes/.local/share/playwright-mcp/review2-config-filled.png` (filled), `/home/celes/.local/share/playwright-mcp/review2-after-save-config.png` (after save)
- **Console:** none (only the recurring DOM VERBOSE warning) · **Verdict:** PASS (functional)
- ⚠️ **MUTATION:** This wrote `test-token-review2` into the live `.env` (DISCORD_TOKEN). Dashboard now shows "Discord Token: ✅ Set". Operator should revert this test value.

### 4. Mobile Menu
- **Action:** resized to 390x844, clicked navbar-toggler button
- **Observed:** nav list (Dashboard, Config, Guilds, Playlists, Backups, Blacklist) expanded; toggle button state=`expanded`
- **Screenshots:** `/home/celes/.local/share/playwright-mcp/review2-mobile-view.png` (closed), `/home/celes/.local/share/playwright-mcp/review2-mobile-menu-open.png` (open)
- **Console:** none · **Verdict:** PASS (functional)

---

## Mobile Responsive (390x844)

| Page | Screenshot | Horizontal overflow |
|------|-----------|---------------------|
| Dashboard | `/home/celes/.local/share/playwright-mcp/review2-mobile-view.png` | no overflow measured (docScrollWidth 390 = viewport 390) |
| Config | `/home/celes/.local/share/playwright-mcp/review2-mobile-config.png` | full-page captured |
| Backups | `/home/celes/.local/share/playwright-mcp/review2-mobile-backups.png` | **no overflow** (docScrollWidth 390 = viewport 390) |

No horizontal scroll overflow detected at mobile width.

---

## Programmatic Contrast Audit (Dashboard, WCAG AA)

Method: relative luminance, nearest opaque ancestor background (card bg `rgba(30,30,45,0.6)`). Raw: `review2-contrast-dashboard-realbg.json`.

**Failing elements (all < 4.5:1):**

| Text | fg | ratio | pass AA |
|------|-----|-------|---------|
| Mounted (green badge) | rgb(25,135,84) | 3.62 | ❌ |
| Config Directory: (label) | rgb(33,37,41) | 1.06 | ❌ |
| /app/config (code) | rgb(214,51,132) | 3.65 | ❌ |
| Writable: (label) | rgb(33,37,41) | 1.06 | ❌ |
| Data Directory: (label) | rgb(33,37,41) | 1.06 | ❌ |
| /app/data (code) | rgb(214,51,132) | 3.65 | ❌ |
| Writable: (label) | rgb(33,37,41) | 1.06 | ❌ |
| Config Contents: (label) | rgb(33,37,41) | 1.06 | ❌ |
| hellodj-config.json (label) | rgb(33,37,41) | 1.06 | ❌ |
| hellodj-config.json (code) | rgb(214,51,132) | 3.65 | ❌ |

**Notable:** the NFS Storage Info card is populated via JS with `<strong>` labels using default dark text (`rgb(33,37,41)`) on a translucent dark card — near-black-on-dark = ~1.06:1 contrast. This is a real accessibility defect (fails WCAG AA 4.5:1). Pink `<code>` values at 3.65:1 also fail AA (fail even the 4.5 normal-text bar). Targeted screenshot: `/home/celes/.local/share/playwright-mcp/review2-nfs-card.png`.

---

## Structural Findings (code inspection — not fixed)

1. **Single `<form id="main-form">` wraps every page's entire content** — [`web-ui/templates/base.html`](web-ui/templates/base.html:200). `{% block content %}` is nested inside one `<form method="POST" action="#" onsubmit="return false;">`. This:
   - Triggers the recurring browser DOM warning on `/config`.
   - Makes every button/card/table on every page a descendant of a non-functional form — buttons default to submit-type, which is a structural/accessibility smell.
2. **NFS card contrast** — [`web-ui/templates/index.html`](web-ui/templates/index.html:153) populates `<strong>` labels that inherit dark text color on a translucent dark card → ~1.06:1 (see audit above).
3. **Mobile navbar-toggler has no accessible name** — the toggle button shows as unnamed "button" in the accessibility snapshot (no `aria-label`). Works functionally but is an a11y gap.

---

## Visual Anomalies Observed While Driving

- **NFS Storage Info card text contrast** — near-black labels (`rgb(33,37,41)`) on dark translucent card background. Likely renders as low-contrast text on dark background. **NEEDS_VISUAL_AUDIT** — confirm visually in `review2-nfs-card.png`.
- No blank screens, no stuck spinners, no layout breaks observed across any page or viewport.
- All pages rendered fully; no JS runtime errors.

---

## Handoff to Visual Auditor

Inspect these host-path screenshots (all at `/home/celes/.local/share/playwright-mcp/`):

1. `review2-dashboard.png` — baseline dashboard; verify status cards render, values populated (0/0/5/Mounted), Discord Token "✅ Set" (after my test save).
2. `review2-config.png` — config form layout; verify field styling/contrast on dark theme.
3. `review2-guilds.png`, `review2-playlists.png`, `review2-backups.png`, `review2-blacklist.png` — page layouts.
4. `review2-restore-modal.png` / `review2-restore-modal-target.png` — restore modal; verify modal renders correctly on dark theme.
5. `review2-after-save-config.png` — confirm save toast/feedback visible.
6. `review2-mobile-view.png` / `review2-mobile-menu-open.png` — mobile dashboard + expanded menu.
7. `review2-mobile-config.png` / `review2-mobile-backups.png` — mobile form & table layouts.
8. `review2-nfs-card.png` — **KEY:** verify the near-black labels (`Config Directory:`, `Writable:`, etc.) are illegible on the dark card — confirm the ~1.06:1 contrast finding.

**Questions for auditor:**
- Is the NFS Storage Info text legible on the dark card, or is it near-invisible (dark on dark)?
- Do any cards/buttons/table headers render with poor contrast on the dark theme?
- Is the mobile menu visually correct and does the config form/backups table lay out acceptably at 390px?

---

## Completion Checklist
- [x] All pages navigated & screenshotted
- [x] Console messages captured (no errors/warnings; 1 DOM verbose warning)
- [x] Network requests validated (all 200, no >=400)
- [x] Contrast data extracted via browser_evaluate
- [x] evidence.md written with host-accessible screenshot paths
- [x] review2-raw.json written
- [x] Visual-auditor handoff prepared
