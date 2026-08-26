# HelloDJ AWS Platform

Monorepo root for the AWS re-platform of HelloDJ (spec: `aws-saas-replatform`).

## Layout

```
platform/
├── infra/                        # AWS CDK app (TypeScript) — all infrastructure as code
│   ├── cdk.json
│   ├── package.json
│   ├── tsconfig.json
│   ├── bin/                      # CDK app entry points
│   └── lib/                      # CDK stacks/constructs
├── components/                   # Per-component Python packages (independently deployable)
│   └── hellodj_platform_logic/   # Shared pure decision functions (single source of truth)
└── pyproject.toml                # Workspace tooling: ruff (PEP 8), max-line-count, test deps
```

## Principles

- **Single source of truth for decisions.** Pure decision/derivation logic (DNS naming, auth
  routing, GPU strategy/placement, dependency gate, base-image gate, autoscaling, draining,
  promotion, migration filter, Hive keys, cost model, Tidal refresh, hybrid GPU controller)
  lives once in `components/hellodj_platform_logic/` and is imported by both the CDK layer and
  the runtime components.
- **PEP 8 + bounded files.** `ruff` enforces PEP 8; every Python source file stays within the
  500-line maximum (see `pyproject.toml`).
- **Independently deployable components.** Each component under `components/` is its own package,
  versioned and deployed independently.

## Tooling

```bash
# Lint (PEP 8) + line-count check
python -m ruff check .
python tools/check_line_count.py

# Tests (unit + Hypothesis property tests)
python -m pytest
```

## Nix-native delivery — reproducible verification path

The end-to-end, copy-runnable command set that reproduces the full build-and-deploy path
(push to `hellodj` → Nix build with no paid build server → publish to the S3 cache + ECR →
promote Beta → Staging → Production on the single GPU host) lives in
[`docs/nix-native-delivery-verification.md`](docs/nix-native-delivery-verification.md)
(spec `hellodj-nix-native-delivery`, Requirement 12).
