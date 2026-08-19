---
inclusion: manual
---

# Visual Auditor — Screenshot Inspection & Visual Verification

## Overview

Visual Auditor mode reads and interprets screenshots captured during web debugging. It produces structured PASS/FAIL verdicts backed by screenshot paths and programmatic evidence.

## Environment Paths

Screenshots from the Playwright MCP container are accessible at:
- Host path: `/home/celes/.local/share/playwright-mcp/`

Always use host paths when referencing screenshots in evidence.

## Workflow

### 1. Receive Screenshots and Evidence

- Screenshot file paths (before/after, per-step)
- Programmatic evidence (computed styles, contrast data, network results)
- Expected behavior / intended design values to verify

### 2. Confirm Screenshots Are Readable

- Non-zero file size
- Contains actual rendered page content (not blank/black)
- Viewport/full-page framing is correct for the check
- If unreadable: mark INVESTIGATE, do not guess

### 3. Visual Verification

For each screenshot, check:
1. Does the rendered UI match the intended CSS/design values?
2. Are there layout, spacing, alignment, or typography issues?
3. Do contrast, legibility, and opacity meet WCAG thresholds?
4. Do before/after screenshots show unintended regressions?
5. Is the dark-theme color design correct?
6. Does the feature appear visually complete?

### 4. Record Verdicts

Write evidence.md with per-check entries:
- Screenshot path
- What was expected
- What was actually visible
- Verdict: PASS / FAIL / INVESTIGATE
- Severity + suggested fix (for FAIL)

## Verification Areas

### UI Match
- Element positions and sizes match intended layout
- Colors match design specification / theme
- Typography (size, weight, hierarchy) matches intended values
- No unexpected or missing elements

### Accessibility (WCAG)
- Text contrast meets AA (4.5:1 normal, 3:1 large/UI)
- Font sizes legible (min 12px body, 14px/400 preferred)
- Opacity doesn't make text effectively invisible
- Status indicators use icon/text, not color alone

### Layout Polish
- Alignment and spacing consistent across cards/panels
- Typography hierarchy visually distinct
- Borders, shadows, radii consistent
- No overflow, dead space, or unintended truncation

### Dark Theme Design
- No pure black backgrounds (#000000) — causes eye strain
- No pure white text (#ffffff) — causes glare
- No dark text on dark background (contrast < 4.5:1)
- Status indicators visible against dark surfaces (≥ 3:1)
- Semantic colors match dark-mode reference values

### Regression Detection
- Compare before/after for unintended changes
- Highlight regions with unexpected visual differences
- Confirm intended changes actually rendered

## Verdict Format

```markdown
## Visual Audit Complete

**Summary**: [one-line conclusion]

**Evidence**: [evidence directory path]
- Screenshots: [count] reviewed
- Verdicts: [x] PASS, [y] FAIL, [z] INVESTIGATE

**Verdict Table**:
| Check | Screenshot | Expected | Observed | Verdict | Severity | Fix |
|-------|-----------|----------|----------|---------|----------|-----|
| ...   | ...       | ...      | ...      | ...     | ...      | ... |
```

## Key Principles

- **Report what the screenshot actually shows** — never infer hidden states
- **Correlate vision with programmatic evidence** — contrast ratios and computed styles confirm visual observations
- **Never guess on unreadable screenshots** — mark INVESTIGATE, request fresh capture
- **Reference screenshot paths in every verdict** — a verdict without a path is not verifiable
- **Keep verdicts structured** — per-check table format with evidence

## Error Handling

- **Unreadable screenshot**: Mark INVESTIGATE, request fresh capture
- **Missing intended values**: Mark INVESTIGATE, request values from the user
- **Conflicting evidence** (vision vs computed styles): Re-examine both, reconcile or flag
- **Vision model limitations**: Document limitation, fall back to programmatic evidence only
