import * as fs from 'node:fs';
import * as path from 'node:path';

/**
 * Cost-model doc-lint / example assertions (task 9.3).
 *
 * These assert that the `Idle_Cost_Model` recorded in `platform/infra/ARCHITECTURE.md`
 * satisfies Requirement 6:
 *
 *  - R6.1 — six itemized USD idle lines are present (EKS control plane, Node_Floor,
 *    single NAT, DAX, ALB, NLB).
 *  - R6.2 — the shared `transcode-gpu` NodePool contributes exactly $0 idle while
 *    scaled to zero.
 *  - R6.3 — the itemized total is at most 1.5× the recorded single-stage baseline.
 *  - R6.4 — the total idle cost is within the inclusive $180–220/mo target.
 *  - R6.5 — the region (`us-east-1`) and pricing-reference date (`2026-08-24`) are
 *    stated.
 *
 * The cost model lives in prose/table form (it is a documented estimate, not
 * synthesized infrastructure), so this suite reads the doc and parses the numbers
 * rather than asserting on a CDK template.
 */

const ARCHITECTURE_MD = path.resolve(__dirname, '..', 'ARCHITECTURE.md');

/** The itemized idle target range (inclusive), in USD/month (R6.4). */
const IDLE_TARGET_MIN = 180;
const IDLE_TARGET_MAX = 220;

/** The recorded single-stage foundation baseline, in USD/month. */
const SINGLE_STAGE_BASELINE_LOW = 340;

/** The six foundation lines that MUST each appear with a USD figure (R6.1). */
const SIX_ITEMIZED_LINES = [
  'EKS control plane',
  'Node_Floor',
  'NAT',
  'DAX',
  'Application Load Balancer',
  'Network Load Balancer',
] as const;

/** Extract the "## Idle cost model" section text from ARCHITECTURE.md. */
function readIdleCostSection(): string {
  const doc = fs.readFileSync(ARCHITECTURE_MD, 'utf-8');
  const start = doc.indexOf('## Idle cost model');
  expect(start).toBeGreaterThanOrEqual(0);
  // Section runs until the next top-level "## " heading (or EOF).
  const rest = doc.slice(start + '## Idle cost model'.length);
  const next = rest.indexOf('\n## ');
  return next >= 0 ? rest.slice(0, next) : rest;
}

/**
 * Parse the six itemized USD idle lines from the model, returning a map from a
 * human label to its dollar figure. Each line is expected as a markdown table
 * row whose last cell holds a bold `**$NN**` figure.
 */
function parseItemizedLines(section: string): Record<string, number> {
  const found: Record<string, number> = {};
  for (const label of SIX_ITEMIZED_LINES) {
    // Find a table row containing the label, then the bold **$NN** figure on it.
    const rowRe = new RegExp(
      `\\|[^\\n]*${label.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\$&')}[^\\n]*\\|`,
      'i',
    );
    const rowMatch = section.match(rowRe);
    expect(rowMatch).not.toBeNull();
    const row = rowMatch![0];
    const dollarMatch = row.match(/\*\*\$(\d+)\*\*/);
    expect(dollarMatch).not.toBeNull();
    found[label] = Number(dollarMatch![1]);
  }
  return found;
}

describe('Idle cost model (ARCHITECTURE.md doc-lint, Requirement 6)', () => {
  const section = readIdleCostSection();

  test('R6.1 — all six foundation lines are itemized with a USD figure', () => {
    const lines = parseItemizedLines(section);
    expect(Object.keys(lines).sort()).toEqual([...SIX_ITEMIZED_LINES].sort());
    for (const label of SIX_ITEMIZED_LINES) {
      expect(lines[label]).toBeGreaterThan(0);
    }
  });

  test('R6.1/R6.4 — the six itemized lines sum to a total within $180–220/mo', () => {
    const lines = parseItemizedLines(section);
    const total = Object.values(lines).reduce((a, b) => a + b, 0);
    expect(total).toBeGreaterThanOrEqual(IDLE_TARGET_MIN);
    expect(total).toBeLessThanOrEqual(IDLE_TARGET_MAX);
  });

  test('R6.4 — the stated total is within the inclusive $180–220/mo target', () => {
    // The doc states a "Total (itemized idle)" bold figure; parse it.
    const totalMatch = section.match(
      /Total \(itemized idle\)[^\n]*\*\*\$(\d+)\/mo\*\*/i,
    );
    expect(totalMatch).not.toBeNull();
    const statedTotal = Number(totalMatch![1]);
    expect(statedTotal).toBeGreaterThanOrEqual(IDLE_TARGET_MIN);
    expect(statedTotal).toBeLessThanOrEqual(IDLE_TARGET_MAX);

    // The stated total must equal the sum of the six itemized lines.
    const lines = parseItemizedLines(section);
    const sum = Object.values(lines).reduce((a, b) => a + b, 0);
    expect(statedTotal).toBe(sum);
  });

  test('R6.2 — the transcode-gpu NodePool is stated as $0 idle while scaled to zero', () => {
    expect(section).toMatch(/transcode-gpu/);
    // A row/line pairing the GPU NodePool with a $0 idle figure.
    const gpuZero = section.match(
      /transcode-gpu[^\n]*\*\*\$0\*\*|\*\*\$0\*\*[^\n]*(?:zero|transcode-gpu)/i,
    );
    expect(gpuZero).not.toBeNull();
  });

  test('R6.3 — the total is at most 1.5× the recorded single-stage baseline', () => {
    // The single-stage baseline (≈ $340–400/mo) must be recorded in the section.
    expect(section).toMatch(/\$340[–\-]?\$?400\/mo|\$340/);
    const lines = parseItemizedLines(section);
    const total = Object.values(lines).reduce((a, b) => a + b, 0);
    const oneAndAHalfX = 1.5 * SINGLE_STAGE_BASELINE_LOW; // 1.5 × $340 = $510
    expect(total).toBeLessThanOrEqual(oneAndAHalfX);
  });

  test('R6.5 — the region us-east-1 and pricing-reference date 2026-08-24 are stated', () => {
    expect(section).toMatch(/us-east-1/);
    expect(section).toMatch(/2026-08-24/);
  });
});
