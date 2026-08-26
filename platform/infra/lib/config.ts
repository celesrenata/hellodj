/**
 * Shared configuration for the HelloDJ CDK application.
 *
 * Parameterizes the region and deployment stage so the same infrastructure
 * code provisions Beta/Staging/Production across regions without redesign
 * (Requirements 1.1, 18.1, 18.3). Stack construction is added by later tasks;
 * this module establishes the shared config contract (task 1.1).
 */

/**
 * The three deployment stages of the delivery pipeline.
 *
 * The reconciled identifiers are `beta`, `staging`, `production` (Requirement
 * 9.1); they match `DeploymentStage` in the authoritative Python
 * `hellodj_platform_logic.types` module exactly so IaC and runtime agree on
 * every stage name (zero `gamma`/`prod`, R9.2).
 */
export enum DeploymentStage {
  Beta = 'beta',
  Staging = 'staging',
  Production = 'production',
}

/** The Route 53 hosted zone for the platform (Requirement 12.1). */
export const ZONE_NAME = 'hellodj.bot';

/** The default launch region (Requirement 18.1). */
export const DEFAULT_REGION = 'us-east-1';

/** Resolved configuration passed to CDK stacks. */
export interface PlatformConfig {
  readonly stage: DeploymentStage;
  readonly region: string;
  readonly account?: string;
}

/**
 * Resolve platform config from environment/context with safe defaults.
 */
export function resolveConfig(
  overrides: Partial<PlatformConfig> = {},
): PlatformConfig {
  const stage = (overrides.stage ??
    (process.env.HELLODJ_STAGE as DeploymentStage) ??
    DeploymentStage.Beta) as DeploymentStage;
  const region =
    overrides.region ??
    process.env.CDK_DEFAULT_REGION ??
    DEFAULT_REGION;
  const account = overrides.account ?? process.env.CDK_DEFAULT_ACCOUNT;
  return { stage, region, account };
}

// ---------------------------------------------------------------------------
// DNS environment-name derivation (single source of truth mirror)
// ---------------------------------------------------------------------------
//
// This is a TypeScript port of the Python
// `hellodj_platform_logic.dns_naming` module. The Python module is the
// authoritative source of truth consumed by the runtime components; this port
// lets the CDK edge stack derive identical names without a Node->Python bridge
// at synth time. The two are kept in sync by a cross-check test
// (`test/dns-naming.test.ts`) that asserts this port reproduces the exact
// shape the Python logic produces (and, when a Python interpreter is
// available, cross-checks against the live Python output).
//
// Naming scheme (Requirement 12 / design Property 1):
//   * Every stage           -> `<stage>.<region>.hellodj.bot`
//                              (`beta`/`staging`/`production`)
//   * Apex alias target      -> the bare zone `hellodj.bot`
//
// The scheme is region-parameterized so adding a region only introduces new,
// non-colliding names with no redesign (Requirement 18.3).

/**
 * A DNS label per RFC 1035: 1-63 chars, lowercase alphanumeric plus internal
 * hyphens. Mirrors `_DNS_LABEL` in the Python module exactly.
 */
const DNS_LABEL = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * Return the apex domain the production environment aliases to.
 *
 * The production environment name (`production.<region>.hellodj.bot`) is
 * aliased to this bare zone via a Route 53 alias/CNAME record (Requirement
 * 12.3).
 */
export function apexAliasTarget(): string {
  return ZONE_NAME;
}

/**
 * Validate and normalize an AWS region into a single DNS label.
 *
 * @throws if `region` is empty or not a valid DNS label.
 */
function normalizeRegion(region: string): string {
  const normalized = region.trim().toLowerCase();
  if (!normalized) {
    throw new Error('region must be a non-empty string');
  }
  if (!DNS_LABEL.test(normalized)) {
    throw new Error(`region is not a valid DNS label: ${JSON.stringify(region)}`);
  }
  return normalized;
}

/**
 * Return whether `name` is a strict subdomain of `zone`.
 *
 * A strict subdomain has at least one additional label to the left of the
 * zone (`beta.us-east-1.hellodj.bot` is a subdomain of `hellodj.bot`; the bare
 * zone itself is not). Mirrors `is_subdomain_of_zone` in the Python module.
 */
export function isSubdomainOfZone(name: string, zone: string = ZONE_NAME): boolean {
  const suffix = `.${zone}`;
  return name.endsWith(suffix) && name.length > suffix.length;
}

/**
 * Derive the DNS environment name for a stage in a region.
 *
 * Every stage resolves to `<stage>.<region>.hellodj.bot` — a strict subdomain
 * of {@link ZONE_NAME} that includes both the reconciled stage name and the
 * region (Requirements 12.2, 12.4, Property 1). This mirrors `derive_env_name`
 * in the Python `dns_naming` module exactly so IaC and runtime agree on every
 * name.
 *
 * @throws if `region` is not a valid DNS label.
 */
export function deriveEnvName(stage: DeploymentStage, region: string): string {
  const regionLabel = normalizeRegion(region);
  // `stage` values are validated lowercase labels
  // ("beta"/"staging"/"production"), matching DeploymentStage in the Python
  // `types` module.
  const name = `${stage}.${regionLabel}.${ZONE_NAME}`;

  // Invariant (Property 1): every derived name is a subdomain of the zone.
  if (!isSubdomainOfZone(name)) {
    throw new Error(
      `derived name ${JSON.stringify(name)} is not a subdomain of ${JSON.stringify(ZONE_NAME)}`,
    );
  }
  return name;
}
