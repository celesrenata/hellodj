# AWS Platform Is Pre-Production — No Users, No Data To Migrate

inclusion: auto

## The Fact

The AWS EKS deployment (beta / staging / production stages at
`*.us-east-1.hellodj.bot` and the apex `hellodj.bot`) has **NO real users and no
production data yet**. It is a pre-production build-out. Nobody depends on it.

## What This Means For How We Fix Things

When a change requires replacing or recreating an AWS resource, **do NOT design
elaborate zero-downtime data migrations or careful cross-stack ownership-transfer
dances.** Prefer the simplest correct end state:

- **Tear down and let the pipeline / CDK recreate.** If a resource is in a wedged
  state (`ROLLBACK_COMPLETE`, a cross-stack ownership collision, an
  `AWS::EarlyValidation::ResourceExistenceCheck` conflict, etc.), it is fine to
  **delete the physical resource** (SES identities, Cognito pools, DynamoDB
  items, Secrets Manager secrets, CloudFormation stacks) and let the correct
  source (AuthStack / WorkloadsStack / EdgeStack / the pipeline) recreate it from
  scratch.
- **Cognito pools may be deleted.** There are no real accounts to preserve. The
  `deletionProtection: true` + `RemovalPolicy.RETAIN` on the pools is a durable
  guard for LATER (once real users exist) — during pre-prod it is acceptable to
  flip protection off and delete a pool to unblock a clean rebuild. The
  admin-bootstrap credential (`admin` / `Wkh3llodj<stage>!`) is re-seeded by the
  AuthStack seeder on every (re)create, so a deleted pool comes back with a
  working admin automatically.
- **Secrets, SES identities, DKIM records, DynamoDB tables/items** can be deleted
  and recreated freely. No backfill, no export, no PITR restore needed.
- **Still fix the SOURCE.** "No users" means we skip *data migration*, NOT that
  we skip building it correctly. Changes still flow through the real
  build/deploy path (CDK source → CodeCommit push → pipeline; or an explicit
  `cdk deploy <foundation-stack>` for foundation stacks). We just don't have to
  preserve live state while doing it.

## When This Stops Being True

The moment real users onboard (real Cognito accounts, real guild data, real
source credentials), this steering must be revisited and removed/narrowed. Until
then, optimize for the simplest correct rebuild, not for state preservation.
