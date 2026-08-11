# Live Review — HelloDJ Web UI (`https://hellodj.celestium.life`)
Session: 2026-08-11T19:18-20:56Z | Mode: visual-debug (gathering) + visual-auditor (interpretation)

## Screenshot inventory (host-accessible)
All screenshots copied into the workspace at `evidence/live-review/` AND present on host mount at `/home/celes/.local/share/playwright-mcp/`.

| # | File | State captured |
|---|------|----------------|
| 00 | `evidence/live-review/00-baseline.png` | Dashboard (index) full page, desktop |
| 01 | `evidence/live-review/01-config-page.png` | Config page, full form, desktop |
| 02 | `evidence/live-review/02-guilds-page.png` | Guilds page (empty table), desktop |
| 03 | `evidence/live-review/03-playlists-page.png` | Playlists page (empty), desktop |
| 04 | `evidence/live-review/04-backups-page.png` | Backups list, desktop |
| 05 | `evidence/live-review/05-backups-after-create.png` | Backups after Create Backup (POST 200) |
| 06 | `evidence/live-review/06-restore-modal-open.png` | Restore modal open (backup 204537) |
| 07 | `evidence/live-review/07-blacklist-page.png` | Blacklist page (empty), desktop |
| 08 | `evidence/live-review/08-mobile-view.png` | Dashboard @ 375x667 (hamburger visible) |
| 09 | `evidence/live-review/09-mobile-menu-open.png` | Mobile nav menu expanded |
| 10 | `evidence/live-review/10-mobile-config.png` | Config page @ 375x667 |
| 11 | `evidence/live-review/11-dashboard-desktop.png` | Dashboard final, desktop 1280x720 |

Host-mount equivalents (for visual-auditor): `/home/celes/.local/share/playwright-mcp/00-baseline.png` ... `11-dashboard-desktop.png`

## Functional evidence
- **All API calls returned 200** — no 4xx/5xx observed (see `raw-evidence-network.json`).
- **No console errors or warnings** on any page (see `raw-evidence-console.json`).
- **Create Backup**: `POST /api/backups` → 200 `{"backup":"hellodj-backup-20260811-193233.tar.gz","message":"...created","status":"ok"}`. New row appears.
- **Save Configuration**: `POST /api/config` → 200 `{"message":"Configuration saved","status":"ok"}` (default_source changed to SoundCloud, persisted).
- **Restore modal**: Opens correctly with Cancel/Restore buttons; Cancel closes cleanly.
- **Static assets** (Bootstrap CSS/JS, icons, jQuery from CDNs) all load 200.

## Critical contrast finding (programmatic)
The site uses a **dark theme** (card bg `rgba(30,30,45,0.6)`, headers `rgb(37,37,55)`, body text `#e0e0e0`) but **Bootstrap's default light-theme text color `#212529` (rgb(33,37,41)) is left on many elements**:
- Dashboard NFS storage card labels/values: contrast **1.06:1** (near-black on near-black) — FAIL.
- Config page **every form label**: contrast **1.06:1** — FAIL (labels effectively invisible).
- Code text `#d63384` on dark card: **3.65:1** — FAIL for normal text.
- Restore buttons `#bb86fc` on white: **2.65:1** — FAIL.
- Modal warning "This cannot be undone!" `#dc3545` on `rgb(45,45,68)`: **2.95:1** — FAIL.
Full table in `raw-evidence-contrast.json`.

## Accessibility findings (programmatic)
- **Unnamed buttons** with no accessible text/aria-label: `.navbar-toggler` (mobile hamburger), 8× `.btn.btn-sm.btn-outline-danger` (delete buttons on backups), `.btn-close` (modal close). See `raw-evidence-contrast.json`.

## Steps to verify visually (visual-auditor)
1. Confirm dark theme renders with **dark-on-dark unreadable text** in NFS card (00/11) and **invisible form labels** on config (01/10).
2. Confirm stat cards (Active Guilds=0, Playlists=0) show correct numbers on dark cards.
3. Confirm backups delete buttons (outline-danger, unnamed) render as intended small trash icons — are they visible/clickable?
4. Confirm mobile menu toggle (09) renders and menu items visible.
5. Confirm restore modal (06) layout/contrast of warning text and buttons.
