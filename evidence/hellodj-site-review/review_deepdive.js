// Deep-dive: config save flow, CDN asset verification, blacklist/backups/playlists interactions
const { chromium } = require('/home/celes/mcp-browser-server/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const EVID_DIR = path.join(__dirname);
const BASE = 'https://hellodj.celestium.life';

const consoleLogs = [];
const pageErrors = [];
const failedRequests = [];
const allResponses = [];
let step = 0;
function shot(desc) {
  const n = String(step++).padStart(2, '0');
  return path.join(EVID_DIR, `${n}-${desc}.png`);
}

async function main() {
  const browser = await chromium.launch({
    executablePath: '/etc/profiles/per-user/celes/bin/chromium',
    headless: false,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();

  page.on('console', (m) => consoleLogs.push({ type: m.type(), text: m.text() }));
  page.on('pageerror', (e) => pageErrors.push(String(e)));
  page.on('requestfailed', (r) => failedRequests.push({ url: r.url(), method: r.method(), rt: r.resourceType(), err: r.failure()?.errorText }));
  page.on('response', (resp) => {
    const status = resp.status();
    const url = resp.url();
    const rt = resp.request().resourceType();
    // Log every response where status >= 400 OR is an external asset OR api
    if (status >= 400 || url.includes('/api/') || rt === 'stylesheet' || rt === 'script' || rt === 'image' || rt === 'font') {
      allResponses.push({ url, status, method: resp.request().method(), rt });
    }
  });

  // --- Config page deep dive ---
  await page.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: shot('10-config-page-loaded.png') });

  // Inspect config values loaded from API
  const configVals = await page.evaluate(() => {
    const get = (id) => { const el = document.getElementById(id); return el ? el.value : null; };
    return {
      discord_token_set: !!get('discord-token'),
      discord_token_val: get('discord-token'),
      lavalink_host: get('lavalink-host'),
      lavalink_port: get('lavalink-port'),
      voice_enabled_checked: document.getElementById('voice-enabled')?.checked,
      bot_default_source: get('bot-default-source'),
      password_inputs: Array.from(document.querySelectorAll('input[type=password]')).map(i => i.id),
      inside_form: Array.from(document.querySelectorAll('input[type=password]')).every(i => i.closest('form')),
    };
  });
  console.log('CONFIG_VALS', JSON.stringify(configVals, null, 2));

  // --- Test Save Configuration POST flow ---
  await page.evaluate(() => {
    window.__saved = null;
    // monkey-patch apiFetch to capture the config POST payload
    const orig = window.apiFetch;
    window.apiFetch = async function(url, opts) {
      if (url === '/api/config' && opts && opts.method === 'POST') {
        window.__saved = { url, body: opts.body };
      }
      return orig ? orig.apply(this, arguments) : fetch(url, opts);
    };
  });
  // Fill a couple of fields to ensure a non-empty save
  await page.locator('#discord-appid').fill('123456789');
  await page.locator('#discord-pubkey').fill('abc123pubkey');
  await page.screenshot({ path: shot('11-config-form-filled.png') });
  const saveBtn = page.locator('button:has-text("Save Configuration")');
  await saveBtn.click();
  await page.waitForTimeout(2500);
  const savedPayload = await page.evaluate(() => window.__saved);
  console.log('SAVE_PAYLOAD', JSON.stringify(savedPayload));
  await page.screenshot({ path: shot('12-after-save-config.png') });

  // --- Blacklist page: add a blacklist entry ---
  await page.goto(BASE + '/blacklist', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: shot('13-blacklist-page.png') });
  const blAdd = page.locator('input, textarea').first();
  console.log('BLACKLIST_INPUTS', await blAdd.count());
  const blBtn = page.locator('button:has-text("Add"), button:has-text("Block")').first();
  if (await blBtn.count()) {
    if (await blAdd.count()) await blAdd.fill('test.example.com');
    await blBtn.click();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: shot('14-blacklist-after-add.png') });
    console.log('CLICKED_BLACKLIST_ADD');
  }

  // --- Backups page: list + create ---
  await page.goto(BASE + '/backups', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: shot('15-backups-page.png') });
  const createBtn = page.locator('button:has-text("Create"), button:has-text("Backup")').first();
  if (await createBtn.count()) {
    await createBtn.click();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: shot('16-backups-after-create.png') });
    console.log('CLICKED_BACKUPS_CREATE');
  }

  // --- Playlists page ---
  await page.goto(BASE + '/playlists', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: shot('17-playlists-page.png') });

  // --- Guilds page ---
  await page.goto(BASE + '/guilds', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: shot('18-guilds-page.png') });

  // --- Dashboard: inspect status dots and quick actions ---
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: shot('19-dashboard-final.png') });
  const statusDots = await page.evaluate(() => {
    const dots = Array.from(document.querySelectorAll('.status-dot')).map(d => ({
      cls: d.className, color: getComputedStyle(d).backgroundColor,
    }));
    return { dots, statusTexts: Array.from(document.querySelectorAll('.status, .badge, .text-success, .text-danger, .text-warning')).map(e => e.textContent.trim()) };
  });
  console.log('STATUS_DOTS', JSON.stringify(statusDots));

  // --- Submit the main-form to check it does not navigate/reload ---
  await page.evaluate(() => {
    window.__formSubmit = null;
    const f = document.getElementById('main-form');
    f.addEventListener('submit', (e) => { e.preventDefault(); window.__formSubmit = 'submitted'; });
  });
  await page.locator('#main-form').evaluate((f) => f.dispatchEvent(new Event('submit', { cancelable: true })));
  await page.waitForTimeout(500);
  console.log('MAIN_FORM_SUBMIT', await page.evaluate(() => window.__formSubmit));

  const mainFormSubmit = await page.evaluate(() => window.__formSubmit);
  await browser.close();

  const evidence = { consoleLogs, pageErrors, failedRequests, allResponses, configVals, savePayload: savedPayload, statusDots, mainFormSubmit };
  fs.writeFileSync(path.join(EVID_DIR, 'raw-evidence-2.json'), JSON.stringify(evidence, null, 2));
  console.log('WROTE raw-evidence-2.json');
  console.log('CONSOLE_ERRORS', consoleLogs.filter(c => c.type === 'error').length);
  console.log('PAGE_ERRORS', pageErrors.length);
  console.log('FAILED', failedRequests.length);
  console.log('RESPONSES', allResponses.length);
  console.log('4xx/5xx responses:', allResponses.filter(r => r.status >= 400).map(r => `${r.status} ${r.url}`));
}

main().catch(e => { console.error('FATAL', e); process.exit(1); });
