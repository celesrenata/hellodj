---
inclusion: manual
---

# Visual Debug — Playwright Web Debugging

## Overview

Visual Debug mode uses Playwright (via MCP tools) for comprehensive website debugging through console, network, DOM-based gathering, and screenshot capture.

## Environment

The Playwright MCP server provides browser automation. Screenshots are accessible at:
- Container path: `/home/node/.playwright-mcp/`
- Host path: `/home/celes/.local/share/playwright-mcp/`

Always translate container paths to host paths when referencing screenshots.

## Available Playwright MCP Tools

Use the `mcp_playwright_*` tools directly:

- `mcp_playwright_browser_navigate` — Navigate to a URL
- `mcp_playwright_browser_take_screenshot` — Capture screenshot (filename, fullPage, type, scale, target)
- `mcp_playwright_browser_snapshot` — Get page accessibility snapshot (DOM tree with roles/names/refs)
- `mcp_playwright_browser_click` — Click element (target = ref from snapshot or CSS selector)
- `mcp_playwright_browser_fill_form` — Fill form fields
- `mcp_playwright_browser_type` — Type text into element
- `mcp_playwright_browser_evaluate` — Run JavaScript on the page
- `mcp_playwright_browser_console_messages` — Get console messages (level: error/warning/info/debug)
- `mcp_playwright_browser_network_requests` — List network requests
- `mcp_playwright_browser_network_request` — Get full details of one request (index from list)
- `mcp_playwright_browser_wait_for` — Wait for text/textGone/time
- `mcp_playwright_browser_resize` — Resize viewport (width, height)
- `mcp_playwright_browser_tabs` — Manage tabs (list/new/close/select)
- `mcp_playwright_browser_hover` — Hover over element
- `mcp_playwright_browser_select_option` — Select dropdown option
- `mcp_playwright_browser_press_key` — Press keyboard key

## Workflow

### 1. Navigate and Establish Baseline

1. `browser_navigate` to target URL
2. `browser_console_messages` level="error" — check for load errors
3. `browser_snapshot` — get page structure
4. `browser_take_screenshot` filename="00-baseline.png" fullPage=true
5. `browser_network_requests` — see what loaded

### 2. Interaction Execution (per step)

1. `browser_snapshot` — find element to interact with (get its ref)
2. `browser_click`/`browser_type`/`browser_fill_form` — perform the action
3. `browser_wait_for` — wait for result
4. `browser_take_screenshot` — capture new state
5. `browser_console_messages` — check for new errors
6. `browser_network_requests` — check for new API calls

### 3. Computed Style Extraction

Use `browser_evaluate` with JavaScript to extract CSS values:

```javascript
() => {
  const elements = document.querySelectorAll('h1, h2, h3, p, a, button, .card');
  return Array.from(elements).slice(0, 50).map(el => {
    const s = getComputedStyle(el);
    return {
      tag: el.tagName, class: el.className.substring(0, 40),
      color: s.color, backgroundColor: s.backgroundColor,
      fontSize: s.fontSize, fontWeight: s.fontWeight, opacity: s.opacity
    };
  });
}
```

### 4. WCAG Contrast Audit

Use `browser_evaluate` with the luminance/contrast calculation to find failing text elements.

### 5. Evidence Compilation

Write findings to `evidence/{slug}/evidence.md` with:
- Action performed
- Expected vs observed result
- Screenshot HOST path
- Console errors/warnings
- Network request results
- Computed style data
- Verdict: PASS / FAIL / NEEDS_VISUAL_AUDIT

## Common Patterns

### Full Page Audit
Navigate → screenshot each page → extract contrast data → check console for each

### Form Interaction
Snapshot → fill form → screenshot → submit → wait → screenshot → check network

### Responsive Testing
Screenshot at desktop → resize to 768×1024 (tablet) → resize to 375×667 (mobile) → check hamburger menu → restore desktop

### Auth Flow Testing
Navigate to /login → snapshot (find fields) → fill credentials → submit → wait for redirect → screenshot → check network for tokens

## Key Rules

- **NEVER** write Node.js scripts — use MCP tool calls directly
- **ALWAYS** `browser_snapshot` before interacting (get element refs)
- **ALWAYS** screenshot after every state-changing interaction
- **ALWAYS** use host paths (`/home/celes/.local/share/playwright-mcp/`) in evidence
- Use CSS selectors (`target="css=button.submit"`) as fallback when refs are stale

## Error Recovery

- **Tool failure**: Check page is loaded (`browser_snapshot`), re-navigate if crashed
- **Element not found**: Re-run `browser_snapshot` for fresh refs (page may have changed)
- **Stale refs**: After navigation or async updates, always re-snapshot
- **Large output**: Use `browser_evaluate` with `filename` param to save to file
