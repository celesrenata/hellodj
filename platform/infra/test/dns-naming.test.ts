import { execFileSync } from 'node:child_process';
import * as path from 'node:path';
import {
  deriveEnvName,
  apexAliasTarget,
  isSubdomainOfZone,
  DeploymentStage,
  ZONE_NAME,
} from '../lib/config';

/**
 * Cross-check tests for the DNS environment-name derivation.
 *
 * These assert the TypeScript port in `lib/config.ts` reproduces the exact
 * naming shape defined by the authoritative Python
 * `hellodj_platform_logic.dns_naming` module (Requirement 12, design
 * Property 1), keeping IaC and runtime in sync (task 9.1).
 *
 * The final block additionally cross-checks the TS port against the *live*
 * Python output when a Python interpreter with the logic package is available;
 * if it is not (e.g. minimal CI image), that check is skipped rather than
 * failing the suite.
 */

const STAGES: DeploymentStage[] = [
  DeploymentStage.Beta,
  DeploymentStage.Staging,
  DeploymentStage.Production,
];

const REGIONS = ['us-east-1', 'eu-west-1', 'ap-southeast-2', 'us-west-2'];

describe('DNS environment-name derivation (TS port)', () => {
  test('non-production stages derive <stage>.<region>.hellodj.bot', () => {
    expect(deriveEnvName(DeploymentStage.Beta, 'us-east-1')).toBe(
      'beta.us-east-1.hellodj.bot',
    );
    expect(deriveEnvName(DeploymentStage.Staging, 'eu-west-1')).toBe(
      'staging.eu-west-1.hellodj.bot',
    );
  });

  test('production stage derives production.<region>.hellodj.bot', () => {
    expect(deriveEnvName(DeploymentStage.Production, 'us-east-1')).toBe(
      'production.us-east-1.hellodj.bot',
    );
  });

  test('every derived name is a strict subdomain of the zone', () => {
    for (const stage of STAGES) {
      for (const region of REGIONS) {
        const name = deriveEnvName(stage, region);
        expect(isSubdomainOfZone(name)).toBe(true);
        expect(name.endsWith(`.${ZONE_NAME}`)).toBe(true);
      }
    }
  });

  test('apex alias target is the bare zone', () => {
    expect(apexAliasTarget()).toBe('hellodj.bot');
    // The bare zone itself is NOT a strict subdomain of itself.
    expect(isSubdomainOfZone(ZONE_NAME)).toBe(false);
  });

  test('names are unique across stage/region combinations (no collisions)', () => {
    const names = new Set<string>();
    for (const stage of STAGES) {
      for (const region of REGIONS) {
        names.add(deriveEnvName(stage, region));
      }
    }
    expect(names.size).toBe(STAGES.length * REGIONS.length);
  });

  test('normalizes region casing/whitespace like the Python module', () => {
    expect(deriveEnvName(DeploymentStage.Beta, '  US-East-1 ')).toBe(
      'beta.us-east-1.hellodj.bot',
    );
  });

  test('rejects an invalid region label', () => {
    expect(() => deriveEnvName(DeploymentStage.Beta, 'us_east_1')).toThrow();
    expect(() => deriveEnvName(DeploymentStage.Beta, '')).toThrow();
  });
});

describe('DNS derivation cross-check against live Python (if available)', () => {
  // Locate the Python logic package: platform/components/
  const componentsDir = path.resolve(__dirname, '..', '..', 'components');

  function pythonDeriveAll(): Record<string, string> | null {
    const script = [
      'import json',
      'from hellodj_platform_logic.dns_naming import derive_env_name, apex_alias_target',
      'from hellodj_platform_logic.types import DeploymentStage',
      'out = {}',
      'for s in DeploymentStage:',
      '    for r in ["us-east-1", "eu-west-1", "ap-southeast-2", "us-west-2"]:',
      '        out[f"{s.value}|{r}"] = derive_env_name(s, r)',
      'out["__apex__"] = apex_alias_target()',
      'print(json.dumps(out))',
    ].join('\n');

    for (const py of ['python3', 'python']) {
      try {
        const stdout = execFileSync(py, ['-c', script], {
          cwd: componentsDir,
          encoding: 'utf-8',
          stdio: ['ignore', 'pipe', 'ignore'],
        });
        return JSON.parse(stdout) as Record<string, string>;
      } catch {
        // Try the next interpreter name / skip if unavailable.
      }
    }
    return null;
  }

  test('TS port matches Python output exactly (or is skipped)', () => {
    const pythonOut = pythonDeriveAll();
    if (pythonOut === null) {
      // No Python interpreter with the logic package available; the TS-only
      // shape tests above still guard the port.
      console.warn(
        'Skipping Python cross-check: no python interpreter with hellodj_platform_logic found.',
      );
      return;
    }

    expect(pythonOut['__apex__']).toBe(apexAliasTarget());

    for (const stage of STAGES) {
      for (const region of REGIONS) {
        const key = `${stage}|${region}`;
        expect(pythonOut[key]).toBe(deriveEnvName(stage, region));
      }
    }
  });
});
