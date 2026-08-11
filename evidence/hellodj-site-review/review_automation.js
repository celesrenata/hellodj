// Comprehensive HelloDJ site review automation
// Launches Chromium, captures console/network/DOM evidence + screenshots.
// Evidence saved to evidence/hellodj-site-review/

const { chromium } = require('/home/celes/mcp-browser-server/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const EVID_DIR = path.join(__dirname);
const BASE = 'https://hellodj.celestium.life';

const consoleLogs = [];
const pageErrors = [];
const failedRequests = [];
const apiResponses = [];
let step = 0;

function shotName(desc) {
  const n = String(step++).padStart(2, '0');
  return `${n}-${desc}.png`;
}

async function main() {
  const browser = await chromium.launch({
    executablePath: '/etc/profiles/per-user/celes/bin/chromium',
    headless: false,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
  });
  const page = await context.newPage();

  page.on('console', (msg) => {
    let loc = '';
    try {
      const l = msg.location;
      if (l && l.url) loc = `${l.url.split('/').pop()}:${l.lineNumber}`;
    } catch (e) { loc = ''; }
    consoleLogs.push({ type: msg.type(), text: msg.text(), location: loc, at: 'console' });
  });
  page.on('pageerror', (err) => {
    pageErrors.push({ text: String(err), at: 'pageerror' });
  });
  page.on('requestfailed', (req) => {
    failedRequests.push({
      url: req.url(),
      method: req.method(),
      resourceType: req.resourceType(),
      failure: req.failure() ? req.failure().errorText : 'unknown',
      at: 'requestfailed',
    });
  });
  page.on('response', (resp) => {
    const status = resp.status();
    const url = resp.url();
    const rt = resp.request().resourceType();
    // Record all responses; flag non-2xx
    if (status >= 400 || (url.startsWith(BASE) && url.includes('/api/'))) {
      apiResponses.push({
        url, status, method: resp.request().method(), resourceType: rt, at: 'response',
      });
    }
  });

  // --- Step 1: Baseline landing page ---
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(EVID_DIR, shotName('00-baseline.png')) });
  console.log('BASELINE_URL', page.url());

  // Dump nav links
  const links = await page.evaluate(() => {
    const nav = document.querySelectorAll('nav a, .nav-link, a');
    return Array.from(new Set(Array.from(nav).map(a => a.getAttribute('href')))).filter(Boolean);
  });
  console.log('LINKS', JSON.stringify(links));

  // --- Step 2: Click "Create Backup" if present ---
  const backupBtn = page.locator('button:has-text("Create Backup"), .btn:has-text("Create Backup")');
  if (await backupBtn.count()) {
    await backupBtn.first().click();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(EVID_DIR, shotName('01-after-create-backup.png')) });
    console.log('CLICKED_CREATE_BACKUP');
  }

  // --- Step 3: Navigate pages via nav links ---
  const pagesToVisit = [
    ['Config', '/config'],
    ['Guilds', '/guilds'],
    ['Playlists', '/playlists'],
    ['Backups', '/backups'],
    ['Blacklist', '/blacklist'],
  ];
  for (const [label, href] of pagesToVisit) {
    const link = page.locator(`a[href="${href}"]`).first();
    if (await link.count()) {
      await link.click();
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1200);
      const slug = label.toLowerCase().replace(/\s+/g, '-');
      await page.screenshot({ path: path.join(EVID_DIR, shotName(`02-${slug}-page.png`)) });
      console.log('PAGE', label, 'URL', page.url());
    } else {
      console.log('PAGE_MISSING_LINK', label, href);
    }
  }

  // --- Step 4: Edit Config page (config editor) ---
  const editBtn = page.locator('.btn-hellodj:has-text("Edit Config"), button:has-text("Edit Config")').first();
  if (await editBtn.count()) {
    await editBtn.click();
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(1200);
    await page.screenshot({ path: path.join(EVID_DIR, shotName('03-edit-config-page.png')) });
    console.log('EDIT_CONFIG_URL', page.url());
  }

  // --- Step 5: Mobile viewport + hamburger menu ---
  await page.setViewportSize({ width: 375, height: 667 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(EVID_DIR, shotName('04-mobile-view.png')) });
  const toggler = page.locator('.navbar-toggler').first();
  if (await toggler.count()) {
    await toggler.click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(EVID_DIR, shotName('05-mobile-menu-open.png')) });
    console.log('TOGGLED_MOBILE_MENU');
  }

  // --- Step 6: Restore desktop and go to dashboard ---
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.waitForTimeout(800);
  await page.locator('a[href="/"]').first().click();
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: path.join(EVID_DIR, shotName('06-dashboard-return.png')) });
  console.log('RETURN_URL', page.url());

  // --- Step 7: Extract all form inputs and interactive elements ---
  const domInfo = await page.evaluate(() => {
    const forms = Array.from(document.querySelectorAll('form')).map(f => ({
      action: f.action, method: f.method, id: f.id,
    }));
    const inputs = Array.from(document.querySelectorAll('input, textarea, select')).map(i => ({
      tag: i.tagName, type: i.type, name: i.name, id: i.id, placeholder: i.placeholder,
    }));
    const buttons = Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim()).filter(Boolean);
    // broken images
    const brokenImgs = Array.from(document.images).filter(img => img.naturalWidth === 0).map(img => img.src);
    const brokenCss = Array.from(document.styleSheets).filter(s => {
      try { return s.href && s.cssRules.length === 0; } catch (e) { return true; }
    }).map(s => s.href).filter(Boolean);
    return { forms, inputs, buttons, brokenImgs, brokenCss, title: document.title };
  });
  console.log('DOM_INFO', JSON.stringify(domInfo, null, 2));

  // --- Step 8: Direct REST API verification via page requests ---
  const apiChecks = await (async () => {
    const results = {};
    for (const ep of ['/api/status', '/api/nfs-status', '/api/backups', '/api/guilds', '/api/playlists']) {
      try {
        const r = await page.request.get(BASE + ep);
        results[ep] = { status: r.status(), body: (await r.body()).toString().slice(0, 400) };
      } catch (e) {
        results[ep] = { error: String(e) };
      }
    }
    return results;
  })();
  console.log('API_CHECKS', JSON.stringify(apiChecks, null, 2));

  // --- Step 9: favicon and static asset checks ---
  const assetChecks = await (async () => {
    const results = {};
    for (const a of ['/favicon.ico', '/static/favicon.ico']) {
      try {
        const r = await page.request.get(BASE + a);
        results[a] = { status: r.status(), type: r.headers()['content-type'] || '' };
      } catch (e) {
        results[a] = { error: String(e) };
      }
    }
    return results;
  })();
  console.log('ASSET_CHECKS', JSON.stringify(assetChecks, null, 2));

  // --- Full-page screenshot of dashboard ---
  await page.screenshot({ path: path.join(EVID_DIR, shotName('07-fullpage-dashboard.png')), fullPage: true });

  await browser.close();

  // --- Write evidence summary ---
  const evidence = {
    baselineUrl: BASE,
    consoleLogs,
    pageErrors,
    failedRequests,
    apiResponses,
    apiChecks,
    assetChecks,
    domInfo,
  };
  fs.writeFileSync(path.join(EVID_DIR, 'raw-evidence.json'), JSON.stringify(evidence, null, 2));
  console.log('WROTE raw-evidence.json');
  console.log('CONSOLE_ERRORS', consoleLogs.filter(c => c.type === 'error').length);
  console.log('PAGE_ERRORS', pageErrors.length);
  console.log('FAILED_REQUESTS', failedRequests.length);
  console.log('API_RESPONSES', apiResponses.length);
}

main().catch(e => {
  console.error('FATAL', e);
  process.exit(1);
});
