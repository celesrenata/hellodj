# HelloDJ Web UI — Fix Tasklist

**Date:** 2026-08-11
**Target:** https://hellodj.celestium.life (Flask app in `web-ui/`, templates in `web-ui/templates/`)
**Grounding:** Two independent, mutually-agreeing reviews — [`plans/live-review-findings.md`](./live-review-findings.md) (visual-debug) and [`plans/live-review-auditor.md`](./live-review-auditor.md) (visual-auditor). Both confirm identical findings with no discrepancies. No functional/backend defects were found (all API calls 200, no console errors; Create Backup, Save Config, and Restore modal all work).
**Status:** Plan only — no code/templates modified.

> **Scope note:** This tasklist encodes **only** the verified findings from the two grounding reports. No new or speculative defects are included. The old contents of this file (from a different evidence set) are superseded.

---

## Context — Root Cause (from evidence, not speculation)

The theme override in `base.html` sets dark backgrounds (card bg `rgba(30,30,45,0.6)`, body gradient `rgb(15,15,25)→rgb(25,25,40)`, `color:#e0e0e0` on `body`) but **fails to override Bootstrap's default light-theme text color `#212529`** on elements that rely on Bootstrap defaults — `--bs-body-color`, `.form-label`, `.card`, `.btn`. The single dominant defect is therefore a **dark theme + light-theme text mismatch** producing near-black text on near-black backgrounds (~1.06:1), which drives most P0 and P1 items.

---

## P0 — Dark theme + light-theme text (critical, unreadable)

Target WCAG AA: **4.5:1** for all normal text.

### P0-1 — Global dark-theme text palette (root cause fix)

- **Files:** [`web-ui/templates/base.html`](../web-ui/templates/base.html:9) (inline `<style>` block)
- **Problem:** The dark theme sets dark backgrounds but leaves Bootstrap's default `--bs-body-color: #212529` active on elements like `.form-label`, `.card`, `.btn`, producing dark-on-dark text at **1.06:1** (WCAG AA requires 4.5:1).
- **Changes (in the `:root` / `body` / component rules of the `<style>` block):**
  1. Override Bootstrap's text-color variable: set `--bs-body-color: #e0e0e0` (and `--bs-body-color-rgb` to its RGB components) so Bootstrap defaults inherit light text.
  2. Add explicit overrides for the components that rely on the default: `.form-label`, `.form-check-label`, `label`, `.card`, `.card-body`, `.card-header`, `.btn` — all text colors set to the light palette family (`#e0e0e0` / `rgb(200,200,215)` matching the existing `.text-muted` override).
  3. Keep the existing `body { color:#e0e0e0 }` and the `.text-muted` light override at line 123–125.
- **Target:** Every default-theme text element on a dark background computes **>= 4.5:1** against its nearest opaque ancestor background.

### P0-2 — Config page: form labels (all sections)

- **Files:** [`web-ui/templates/config.html`](../web-ui/templates/config.html:22) (all `.form-label` lines: 22, 26, 30, 46, 50, 54, 67, 71, 82, 102, 106, 114, 122, 126, 139, 143, 147, 151, 155, 171, 179, 187; plus `.form-check-label` line 99)
- **Problem:** Every form label (Bot Token, Application ID, Public Key, Host, Port, Password, Client ID, Client Secret, API Key, wake-word checkbox, model path, STT, TTS, LLM keys, source/autoplay/repeat) renders near-black `#212529` on the dark card `rgba(30,30,45,0.6)` → **1.06:1**, effectively invisible (confirmed by visual auditor on `01-config-page.png`, `10-mobile-config.png`).
- **Changes:** This is fully covered by the global P0-1 `.form-label` / `label` override in `base.html`; no per-field edits needed. If a scoped fallback is preferred instead, add `.config-page .form-label, .config-page .form-check-label { color:#e0e0e0 }` to the style block.
- **Target:** Every config form label computes **>= 4.5:1** against `rgba(30,30,45,0.6)`.

### P0-3 — Dashboard NFS Storage Info card: labels/values

- **Files:** [`web-ui/templates/index.html`](../web-ui/templates/index.html:153) (JS-populated `$('#nfs-info').html(...)` block, lines 153–165)
- **Problem:** The `<strong>` labels ("Config Directory:", "Writable:", "Data Directory:", "Config Contents:", "hellodj-config.json") render near-black `#212529` on the dark card → **1.06:1**, effectively invisible (confirmed on `11-dashboard-desktop.png`).
- **Changes:** Add a scoped CSS rule in `base.html` (or inline `style="color:#e0e0e0"`) so the NFS card labels use a light tone — e.g. `#nfs-info strong { color:#e0e0e0 }` (or `rgb(200,200,215)` to match `.text-muted`).
- **Target:** Every NFS-card label computes **>= 4.5:1** against the dark card background.

---

## P1 — Accessibility / contrast failures

Target WCAG AA: **4.5:1** for normal text; **accessible names** for icon-only buttons.

### P1-1 — Restore buttons: `#bb86fc` text contrast FAIL (2.65:1)

- **Files:** [`web-ui/templates/base.html`](../web-ui/templates/base.html:72) (`.btn-outline-hellodj` color at line 74), [`web-ui/templates/backups.html`](../web-ui/templates/backups.html:90) (Restore button markup)
- **Problem:** Restore button text `#bb86fc` on white measures **2.65:1** — FAIL vs 4.5:1 (confirmed on `04-backups-page.png`, `05-backups-after-create.png`).
- **Changes:** Override the `.btn-outline-hellodj` text color (line 74) to a tone that reaches >= 4.5:1 against its actual background. On the white table/card area, darken the purple (e.g. a deeper violet like `#5f3dc4` / `#6a3fb5`); on dark backgrounds a lighter value like `#d4b3ff` may be needed — apply one value that passes on both, or scope per-container overrides.
- **Target:** Restore button text computes **>= 4.5:1** against its real background.

### P1-2 — Code text `#d63384` contrast FAIL (3.65:1)

- **Files:** [`web-ui/templates/base.html`](../web-ui/templates/base.html:9) (add `code` rule to the `<style>` block), [`web-ui/templates/index.html`](../web-ui/templates/index.html:151) (NFS `code` values, lines 156–158), [`web-ui/templates/config.html`](../web-ui/templates/config.html) (code blocks)
- **Problem:** Pink code snippet values `rgb(214,51,132)` on the dark background measure **3.65:1** — FAIL vs 4.5:1.
- **Changes:** Add a global `code { color: ... }` override in `base.html` to a lighter pink (e.g. `#f48fb1` / `#ff9ec4`) so effective contrast on the dark card/body reaches >= 4.5:1. Apply consistently to all `<code>` snippet values (NFS card and config).
- **Target:** Every pink code snippet computes **>= 4.5:1** against its background.

### P1-3 — Restore modal warning "This cannot be undone!" contrast FAIL (2.95:1)

- **Files:** [`web-ui/templates/backups.html`](../web-ui/templates/backups.html:54) (warning line), [`web-ui/templates/base.html`](../web-ui/templates/base.html:96) (`.modal-content` bg `#2d2d44`)
- **Problem:** Red warning text `#dc3545` (`.text-danger`) on the dark modal `rgb(45,45,68)` measures **2.95:1** — FAIL vs 4.5:1 (confirmed on `06-restore-modal-open.png`).
- **Changes:** Add a scoped override in `base.html` for this element — e.g. `.modal .text-danger, #restoreModal .text-danger { color:#ff6b6b }` (or a lighter red like `#ef9a9a`) so contrast on `#2d2d44` reaches >= 4.5:1.
- **Target:** The warning text computes **>= 4.5:1** against the modal background.

### P1-4 — Stat-card numbers lack sufficient contrast

- **Files:** [`web-ui/templates/index.html`](../web-ui/templates/index.html:20) (stat numbers: lines 20, 29, 38, 47 — `h2.display-4` `#stat-*`)
- **Problem:** The dashboard stat-card numbers (Active Guilds, Playlists, Backups, NFS Status) lack sufficient contrast against the dark cards (confirmed by visual auditor on `00-baseline.png`, `11-dashboard-desktop.png`).
- **Changes:** Give the stat numbers an explicit bright light color — e.g. `h2.display-4, #stat-guilds, #stat-playlists, #stat-backups, #stat-nfs { color:#f5f5ff }` (or `#ffffff`) — rather than relying on the inherited default.
- **Target:** Each stat-card number computes **>= 4.5:1** against `rgba(30,30,45,0.6)`.

### P1-5 — Unnamed icon-only buttons: no accessible name

- **Files:**
  - Mobile hamburger: [`web-ui/templates/base.html`](../web-ui/templates/base.html:159) (`.navbar-toggler`)
  - Delete buttons (8×): [`web-ui/templates/backups.html`](../web-ui/templates/backups.html:93) (`.btn.btn-sm.btn-outline-danger` with only `<i class="bi bi-trash">`)
  - Modal close: [`web-ui/templates/backups.html`](../web-ui/templates/backups.html:50) (`.btn-close`)
- **Problem:** These icon-only buttons have no text and no `aria-label`/`title`, so screen readers report them as unnamed "buttons" (confirmed in the accessibility snapshot).
- **Changes:**
  - Line 159: add `aria-label="Toggle navigation"` to the `.navbar-toggler` button.
  - Line 93: add `aria-label="Delete backup"` (or `title="Delete ${b.name}"`) to each delete button in the JS-rendered row template.
  - Line 50: add `aria-label="Close"` to the modal `.btn-close`.
- **Target:** Every icon-only button has an accessible name (accessible snapshot reports a named button, not an unnamed one). Mobile menu still expands/collapses; delete + modal-close still function.

---

## P2/P3 — Responsive & visual polish (minor)

### P2-1 — Delete buttons: thin, low-contrast red outlines on dark bg

- **Files:** [`web-ui/templates/backups.html`](../web-ui/templates/backups.html:93) (`.btn-outline-danger` delete button), [`web-ui/templates/base.html`](../web-ui/templates/base.html:9) (style block)
- **Problem:** Delete buttons render as thin red outlines with poor contrast on the dark background — hard to identify/click (confirmed on `04-backups-page.png`, `05-backups-after-create.png`).
- **Changes:** Give delete buttons a high-contrast, clearly identifiable treatment — e.g. a filled red button with white text (`.btn-danger`-style), or a stronger border + brighter red (`#ff6b6b`) with a solid fill on hover. Add the rule in `base.html` (e.g. `.btn-outline-danger { border-color:#ff6b6b; color:#ff8a8a }` + `:hover` fill).
- **Target:** Delete buttons are visually distinct and legible (**>= 4.5:1** text contrast where text is shown) and have a clear hover/active affordance on dark background.

### P2-2 — Missing hover states on interactive elements

- **Files:** [`web-ui/templates/base.html`](../web-ui/templates/base.html:9) (style block; existing hover rules at 34–37, 67–71, 76–79, 90–92, 143–146)
- **Problem:** Interactive elements (buttons/links) lack confirmed hover-state styling — e.g. `.btn-outline-hellodj`, `.btn-outline-danger`, `.navbar-toggler`, `.btn-close`.
- **Changes:** Add consistent `:hover` (and `:focus-visible`) states for the outlined buttons and the toggler/close controls — brighten the text/border and/or add a background fill — matching the hover treatment already present on `.btn-hellodj`, `.btn-outline-hellodj`, `.nav-link`, and table rows.
- **Target:** Every interactive element shows a clear hover (and keyboard-focus) state.

### P3-1 — Mobile config form: tight label spacing

- **Files:** [`web-ui/templates/config.html`](../web-ui/templates/config.html:21) (all `.mb-3` field groups with `.form-label`)
- **Problem:** On mobile the config form labels lack breathing room / tight label spacing (confirmed PARTIAL on `08-mobile-view.png`, `09-mobile-menu-open.png`, `10-mobile-config.png`; layout itself is readable).
- **Changes:** Add a small media-query rule in `base.html` (e.g. `@media (max-width: 576px) { .form-label { margin-bottom: 0.5rem; } .mb-3 { margin-bottom: 1rem; } }`) to give labels more vertical separation on small screens.
- **Target:** Config form labels have clear separation and remain readable on mobile without altering the layout.

---

## Verification (closing step)

After applying P0-1…P0-3, P1-1…P1-5, P2-1…P2-2, and P3-1, re-run the **visual-debug + visual-auditor** pass against https://hellodj.celestium.life and capture fresh screenshots. Use the existing baselines in [`evidence/live-review/`](../evidence/live-review/) (`00-baseline.png`, `01-config-page.png`, `04-backups-page.png`, `05-backups-after-create.png`, `06-restore-modal-open.png`, `08-mobile-view.png`, `09-mobile-menu-open.png`, `10-mobile-config.png`, `11-dashboard-desktop.png`) as the before-state reference.

Confirm each group:

- **P0 — Theme/contrast:** re-run the relative-luminance contrast audit against nearest opaque ancestor backgrounds. Every previously failing element now computes **>= 4.5:1**: config form labels (P0-2), NFS-card labels (P0-3), and the general `--bs-body-color` / `.form-label` / `.card` / `.btn` defaults (P0-1). Visual audit shows legible light labels on dark cards on both config and dashboard.
- **P1 — Contrast + accessible names:** Restore button text (P1-1) >= 4.5:1, code text (P1-2) >= 4.5:1, modal warning (P1-3) >= 4.5:1, stat-card numbers (P1-4) >= 4.5:1. Accessibility snapshot shows named buttons for the hamburger toggle, the 8 delete buttons, and the modal close (P1-5).
- **P2/P3 — Polish:** delete buttons are distinct/legible with hover (P2-1, P2-2); mobile config labels have clear spacing (P3-1).
- **No regressions:** all pages still render; all API calls still return 200; no console errors. Create Backup, Save Config, and Restore modal still work (functional flows unchanged).
- **Evidence:** save fresh screenshots + updated contrast/console/network data under `evidence/live-review/` (or a new `evidence/live-review-fixes/` subfolder) and record expected-vs-observed results in `evidence/live-review/evidence.md`.

**Pass/fail gate:** the review is complete only when (1) the re-run contrast audit shows no failing element (all >= 4.5:1), (2) every icon-only button has an accessible name, and (3) no new functional/console/network errors appear.

---

## Task ordering rationale

- **P0 first** — near-invisible dark-on-dark text (~1.06:1) is the most severe failure and is the shared root cause; the global theme fix (P0-1) also resolves P0-2 and P0-3.
- **P1 second** — WCAG AA contrast failures (restore, code, modal warning, stat numbers) and the missing accessible names on icon-only buttons.
- **P2/P3 last** — visual polish / minor responsive spacing; non-blocking.
