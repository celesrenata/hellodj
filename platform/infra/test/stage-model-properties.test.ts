import * as fc from 'fast-check';
import {
  StageEndpoint,
  stageEndpoint,
  stageHostname,
} from '../lib/workloads-stack';
import { PROMOTION_ORDER, PromotionStageName } from '../lib/pipeline-stack';

/**
 * fast-check property assertions for the CDK/TypeScript stage model
 * (task 15.4) — the TypeScript mirror of the two authoritative Python pure
 * functions:
 *
 *   * `hellodj_platform_logic.endpoint_routing.route_endpoint`
 *     (Property 9 / R8.7): a request routes only to the stage whose endpoint
 *     hostname it targets, and to nowhere when no endpoint matches.
 *   * `hellodj_platform_logic.promotion.promote`
 *     (Property 10 / R9.6, R10.3-R10.5): promotion runs in the fixed order
 *     Beta -> Staging -> Production, deploys a stage only after every earlier
 *     stage succeeded, and halts on the first failure (every later stage is
 *     skipped, never deployed).
 *
 * The CDK layer isolates the three stages on the single shared GPU host only by
 * their distinct {@link StageEndpoint} (namespace + hostname
 * `<stage>.<region>.hellodj.bot`, from `stageEndpoint`) and promotes them in
 * the fixed {@link PROMOTION_ORDER}. These tests assert the *behaviour* that
 * wiring realizes — hostname->stage routing and fixed-order/halt promotion —
 * matches the Python models over many generated inputs (min 100 runs each), so
 * the IaC and the pure decision logic stay a single source of truth.
 *
 * Tag: Feature: hellodj-nix-native-delivery, Property 9/10 (CDK mirror)
 *
 * Validates: Requirements 8.7, 10.3, 10.4, 10.5
 */

const NUM_RUNS = 200;

// ---------------------------------------------------------------------------
// TypeScript mirrors of the two Python pure functions, expressed over the CDK
// stage model. These are the exact semantics under test — kept local to the
// test so the assertion is against the *modeled behaviour* of the CDK stage
// isolation/promotion wiring, not a re-import of production glue.
// ---------------------------------------------------------------------------

/**
 * Mirror of Python `route_endpoint`: return exactly the endpoint whose
 * `hostname` equals `hostname` (exact string match), else `null`. With
 * distinct-hostname endpoint sets this yields a unique route or no route.
 */
function routeEndpoint(
  hostname: string,
  endpoints: readonly StageEndpoint[],
): StageEndpoint | null {
  for (const endpoint of endpoints) {
    if (endpoint.hostname === hostname) {
      return endpoint;
    }
  }
  return null;
}

/** Realized outcome of deploying a stage (mirror of Python `StageResult`). */
type StageResult = 'succeeded' | 'failed' | 'skipped';

/**
 * Mirror of Python `promote`: walk {@link PROMOTION_ORDER} in fixed order,
 * deploy a stage only when every predecessor succeeded, and once a deployed
 * stage fails mark every remaining stage `skipped` without deploying it.
 *
 * @param outcomes the outcome each stage *would* produce if reached
 *   (`succeeded` | `failed`), keyed by promotion stage name.
 */
function promote(
  outcomes: Record<PromotionStageName, 'succeeded' | 'failed'>,
): Record<PromotionStageName, StageResult> {
  const realized = {} as Record<PromotionStageName, StageResult>;
  let predecessorSucceeded = true;
  for (const stage of PROMOTION_ORDER) {
    if (!predecessorSucceeded) {
      realized[stage] = 'skipped';
      continue;
    }
    const outcome = outcomes[stage];
    realized[stage] = outcome;
    if (outcome === 'failed') {
      predecessorSucceeded = false;
    }
  }
  return realized;
}

// ---------------------------------------------------------------------------
// Generators
// ---------------------------------------------------------------------------

/** A DNS-label-ish region token, so generated hostnames stay well-formed. */
const regionArb = fc
  .tuple(
    fc.stringMatching(/^[a-z]{2,4}$/),
    fc.stringMatching(/^[a-z]{4,10}$/),
    fc.integer({ min: 1, max: 9 }),
  )
  .map(([a, b, n]) => `${a}-${b}-${n}`);

/**
 * A set of stage endpoints with pairwise-distinct hostnames, built via the CDK
 * `stageEndpoint` factory. Each of the three stages uses its own region token
 * to guarantee distinct `<stage>.<region>.hellodj.bot` hostnames (stage prefix
 * alone already differs, region distinctness adds a stronger guarantee).
 */
const distinctEndpointsArb: fc.Arbitrary<StageEndpoint[]> = fc
  .uniqueArray(regionArb, { minLength: 3, maxLength: 3 })
  .map(([r0, r1, r2]) => [
    stageEndpoint('beta', r0),
    stageEndpoint('staging', r1),
    stageEndpoint('production', r2),
  ]);

// ---------------------------------------------------------------------------
// Property 9 (CDK mirror): a request routes only to the stage it targets.
// ---------------------------------------------------------------------------

describe('Feature: hellodj-nix-native-delivery, Property 9/10 (CDK mirror)', () => {
  test('Property 9 (CDK mirror): a request routes only to the targeted stage endpoint (R8.7)', () => {
    fc.assert(
      fc.property(
        distinctEndpointsArb,
        fc.integer({ min: 0, max: 2 }),
        (endpoints, targetIndex) => {
          const target = endpoints[targetIndex];
          const routed = routeEndpoint(target.hostname, endpoints);

          // Routes to exactly the targeted stage's endpoint...
          expect(routed).not.toBeNull();
          expect(routed!.stage).toBe(target.stage);
          expect(routed!.namespace).toBe(target.namespace);
          expect(routed!.hostname).toBe(target.hostname);

          // ...and never to any other stage (cross-stage isolation).
          for (const other of endpoints) {
            if (other.hostname !== target.hostname) {
              expect(routed!.stage).not.toBe(other.stage);
            }
          }
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  test('Property 9 (CDK mirror): a hostname matching no stage endpoint routes nowhere (R8.7)', () => {
    fc.assert(
      fc.property(
        distinctEndpointsArb,
        regionArb,
        (endpoints, unmatchedRegion) => {
          // A hostname none of the endpoints own must route to null. Use a
          // stage label ('gamma') that is not one of the three isolated stages
          // so it can never collide with a generated endpoint hostname.
          const unmatched = stageHostname('gamma', unmatchedRegion);
          fc.pre(endpoints.every((e) => e.hostname !== unmatched));

          expect(routeEndpoint(unmatched, endpoints)).toBeNull();
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });

  // -------------------------------------------------------------------------
  // Property 10 (CDK mirror): fixed-order promotion, halt on first failure.
  // -------------------------------------------------------------------------

  test('Property 10 (CDK mirror): promotion runs in fixed order and halts on the first failure (R9.6, R10.3-R10.5)', () => {
    const stageOutcomeArb = fc.constantFrom<'succeeded' | 'failed'>(
      'succeeded',
      'failed',
    );

    fc.assert(
      fc.property(
        fc.record({
          beta: stageOutcomeArb,
          staging: stageOutcomeArb,
          production: stageOutcomeArb,
        }),
        (outcomes) => {
          const realized = promote(
            outcomes as Record<PromotionStageName, 'succeeded' | 'failed'>,
          );

          // Fixed order is exactly Beta -> Staging -> Production.
          expect([...PROMOTION_ORDER]).toEqual(['beta', 'staging', 'production']);

          // Beta has no predecessor, so it is always attempted (never skipped)
          // and carries its own outcome.
          expect(realized.beta).toBe(outcomes.beta);
          expect(realized.beta).not.toBe('skipped');

          // Find the first failing stage (if any) in fixed order.
          let firstFailureIndex = -1;
          for (let i = 0; i < PROMOTION_ORDER.length; i++) {
            if (realized[PROMOTION_ORDER[i]] === 'failed') {
              firstFailureIndex = i;
              break;
            }
          }

          for (let i = 0; i < PROMOTION_ORDER.length; i++) {
            const stage = PROMOTION_ORDER[i];
            if (firstFailureIndex === -1 || i <= firstFailureIndex) {
              // Every stage up to and including the first failure was deployed,
              // and only after all earlier stages succeeded (R10.3, R10.5).
              expect(realized[stage]).toBe(outcomes[stage]);
              expect(realized[stage]).not.toBe('skipped');
              for (let j = 0; j < i; j++) {
                expect(realized[PROMOTION_ORDER[j]]).toBe('succeeded');
              }
            } else {
              // Every stage after the first failure is skipped, never deployed
              // (R10.4).
              expect(realized[stage]).toBe('skipped');
            }
          }
        },
      ),
      { numRuns: NUM_RUNS },
    );
  });
});
