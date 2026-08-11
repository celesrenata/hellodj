// Backups restore-modal flow + delete/restore API verification (non-destructive)
const { chromium } = require('/home/celes/mcp-browser-server/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const EVID_DIR = path.join(__dirname);
const BASE = 'https://hellodj.celestium.life';

const consoleLogs = [];
const pageErrors = [];
const failedRequests = [];
const apiCalls = [];
let step = 0;
let modalVisible = null;
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
  page.on('requestfailed', (r) => failedRequests.push({ url: r.url(), method: r.method(), err: r.failure()?.errorText }));
  page.on('response', async (resp) => {
    const url = resp.url();
    if (url.includes('/api/')) {
      let body = '';
      try { body = (await resp.body()).toString().slice(0, 300); } catch (e) {}
      apiCalls.push({ url, status: resp.status(), method: resp.request().method(), body });
    }
  });

  // placeholder to avoid unused
  void 0;

  await page.goto(BASE + '/backups', { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: shot('20-backups-loaded.png') });

  // Count restore/delete buttons
  const restoreBtns = page.locator('button:has-text("Restore")');
  const deleteBtns = page.locator('button.btn-outline-danger');
  console.log('RESTORE_BTNS', await restoreBtns.count(), 'DELETE_BTNS', await deleteBtns.count());

  // Open restore modal (non-destructive - just verify it opens and renders)
  if (await restoreBtns.count()) {
    await restoreBtns.first().click();
    await page.waitForTimeout(1200);
    await page.screenshot({ path: shot('21-restore-modal-open.png') });
    modalVisible = await page.evaluate(() => {
      const m = document.getElementById('restoreModal');
      return { visible: m && m.classList.contains('show'), restoreName: document.getElementById('restore-name')?.textContent };
    });
    console.log('MODAL', JSON.stringify(modalVisible));
    // Close modal without confirming
    await page.locator('#restoreModal .btn-secondary, #restoreModal [data-bs-dismiss="modal"]').first().click();
    await page.waitForTimeout(600);
  }

  // Verify restore/delete API endpoints respond (non-destructive: use nonexistent backup names)
  const restoreResp = await page.request.post(`${BASE}/api/backups/nonexistent-backup-xyz.tar.gz/restore`);
  console.log('RESTORE_NONEXIST', restoreResp.status(), (await restoreResp.body()).toString().slice(0, 200));
  const deleteResp = await page.request.delete(`${BASE}/api/backups/nonexistent-backup-xyz.tar.gz`);
  console.log('DELETE_NONEXIST', deleteResp.status(), (await deleteResp.body()).toString().slice(0, 200));

  await browser.close();
  fs.writeFileSync(path.join(EVID_DIR, 'raw-evidence-3.json'), JSON.stringify({ consoleLogs, pageErrors, failedRequests, apiCalls, modal: modalVisible, restoreNonexist: restoreResp.status(), deleteNonexist: deleteResp.status() }, null, 2));
  console.log('WROTE raw-evidence-3.json');
  console.log('CONSOLE_ERR', consoleLogs.filter(c => c.type === 'error').length, 'PAGE_ERR', pageErrors.length, 'FAILED', failedRequests.length);
}
main().catch(e => { console.error('FATAL', e); process.exit(1); });
