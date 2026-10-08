# Changelog

All notable changes to **StayOS** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.1] - 2026-10-08

### Changed
- LUMI voice uses Amazon Nova Sonic 2.5 (`amazon.nova-2-5-sonic`) with the
  GA Strands BidiAgent, preserving its voice, property-scoped tools, prompts,
  and browser message contract. The GA provider replaces custom Nova event
  handling and renews connections with conversation history.
- Added a focused `make lumi-voice-upgrade` path with account checks,
  change-set scope validation, digest-pinned runtime updates, and rollback.
- Shortened the voice-stack description and added `make lumi-voice-description`
  for reviewed metadata-only updates that preserve parameters, permissions,
  and the deployed runtime.
- Deployment output is concise by default, with timed progress, per-run logs,
  `VERBOSE=1` diagnostics, `NO_COLOR=1`, and stable line-oriented CI output.
- LUMI builds its Python 3.12 `x86_64` dependency layer in AWS CodeBuild,
  producing Lambda-compatible artifacts from macOS, Linux, or Windows without
  requiring Docker.
- PULSE runtime wiring now uses a CloudFormation-only update, avoiding a second
  backend package build, Lambda refresh, and seed run.

## [1.2.0] - 2026-09-09

Proactive intelligence for PULSE: adds the **Predictive / Forecasting Agent**, a
second PULSE agent that runs *ahead* of the reactive rule engine to warn GMs of
likely room oversell before it happens. Additive and advisory-only (human stays
in the loop); no breaking changes. Also folds in the frontend dependency
security fixes.

### Added
- **PULSE Predictive / Forecasting Agent** (`pulse/backend/services/forecast-agent/`) —
  a containerized Strands agent on Amazon Bedrock AgentCore Runtime that runs on
  a per-property daily schedule (EventBridge Scheduler at each property's local
  midnight) and forecasts room **oversell / walk risk** over a 1-14 day horizon.
  A **pure, deterministic Forecast Engine** owns every number (oversell
  classification, rooms oversold, integer 0-100 confidence) — the model never
  generates figures; Claude Sonnet only *authors* the remediation narrative
  around those fixed numbers, with a deterministic template fallback when the
  model is unavailable. It reads forward-looking signals (`get_occupancy`,
  `get_revenue`, `get_sister_property_availability`) through the shared StayOS
  AgentCore Gateway (MCP), then emits an advisory `FORECAST_OVERSELL` alert into
  `pulse-alerts` and publishes `ALERT_UPDATED` / `ALERT_RESOLVED` over the
  existing realtime + Web Push delivery path.
- **Advisory-only posture** — `FORECAST_OVERSELL` is deliberately excluded from
  the Action Executor's resolvable types, so a forecast can never auto-execute a
  write-back; the PWA renders it as a heads-up (advisory footer, no
  approve/reject controls) even at WARNING severity.
- **Forecaster invoker Lambda** (`pulse/backend/functions/forecast-invoker/`) — a
  thin EventBridge Scheduler target that starts the `pulse-forecaster` AgentCore
  Runtime session for a given `propertyId`.
- **`make forecast-build` / `forecast-deploy` / `forecast-destroy`** — out-of-band
  build/deploy/teardown for the forecaster runtime, mirroring the Triage Agent
  (shared CodeBuild + shared ECR under a distinct `forecaster-latest` tag);
  `make destroy` now tears the forecaster runtime down as well.

### Changed
- Added the `FORECAST_OVERSELL` alert type and its `forecast` payload to the
  PULSE backend model and the PWA (`types.ts`, `TriageModal.tsx`, `format.ts`).
- Extended the existing PULSE nested stacks (`pulse-api`, `pulse-pipeline`,
  `pulse-observability`) with the forecaster runtime role, invoker, per-property
  schedules, and forecast metric filters / alarms / dashboard widget — no new
  nested stack and no new DynamoDB table. Threaded `ForecastModelId` /
  `ForecastRuntimeArn` through the root stack.

### Security
- **Frontend dependency vulnerabilities** — resolved all open `npm audit`
  findings across the three frontends (`stayos-shell`, `lumi`, `pulse`):
  - Bumped `next` `^15.5.23` → `^15.5.24` to patch a critical unauthenticated
    RCE (GHSA-p293-qw3h-jr36 / CVE-2026-75604, GHSA-2xp9-vwfh-vxw4).
  - Added npm `overrides` to force patched transitive versions of `postcss`
    (`^8.5.26`) and `sharp` (`^0.35.3`) over Next.js's vulnerable nested copies,
    and `js-yaml` (`^4.3.2`) to patch a high CPU-DoS (GHSA-2883-xcg3-v3hh).
  - Bumped `vitest` `^4.1.10` → `^4.1.11` (lumi) to patch a moderate path
    traversal (GHSA-82fw-gwwq-j7x9).
  - Added `lumi/frontend/.npmrc` (`legacy-peer-deps=true`) to regenerate the
    lockfile past an `@testing-library/react` peer conflict, mirroring pulse.
  - Verified: `npm audit` → 0 vulnerabilities, build and tests pass on all three.

## [1.1.0] - 2026-09-04

Platform reliability and reach: adds the shared Unified Data Orchestrator so the
demo dataset stays current on its own, a public marketing landing page for the
StayOS shell, and a set of correctness and security fixes. No breaking changes.

### Added
- **Unified Data Orchestrator** (`shared/data-orchestrator/`, `StackPrefix`
  `stayos-data`) — a Step Functions state machine (Quiesce → Generate →
  Reconcile → UnQuiesce → RegenerateBrief → PrimeBaseline) plus one per-property
  EventBridge Scheduler rule that re-anchors the deterministic 30-day window at
  each property's local midnight via idempotent upsert (pausing PULSE evaluation
  during the rewrite so no alert storm fires), then regenerates that day's brief.
  It is additive and never bulk-rewrites or re-seeds the live tables.
- **Prime-on-deploy** — the orchestrator deploy step primes today's data for
  every pilot property (idempotent, failure-isolated), so each GM has a current
  daily brief immediately after `make deploy-all` with no manual step.
- **StayOS marketing landing page** — a public landing page at the shell root
  (`/`) that explains StayOS and its two live features (LUMI and PULSE) before
  sign-in; a Sign In call-to-action reveals the login form, and authenticated
  visitors still land on the feature launcher grid (SSO preserved).

### Changed
- `make deploy-all` now also deploys the shared Data Orchestrator, wired to the
  live LUMI table names and PULSE rule-evaluator stream mappings.
- Shortened PULSE CloudFormation stack descriptions to under 25 words.

### Fixed
- **VIP arrivals** — `get_vip_guests` now falls back to a live reservations
  query when a brief for the current date is missing, so it never reports a
  false "no VIP arrivals"; the fallback is deduped and capped to match the brief.
- **PULSE triage** — fixed an out-of-order (OOO) `triageBrief` placeholder leak
  by threading the real block id.
- **LUMI voice agent** — corrected `get_revenue` parameter names to match the
  tool schema.

### Security
- Merged security fixes (dependency bumps and verified secret-scan allowlists).
- Removed a real AWS account ID from `docs/data-model.md` and stopped tracking
  local working-notes docs.

## [1.0.0] - 2026-08-21

Initial version 1 — the StayOS reference implementation (prototype / customer
demo): the operating system for hotel General Managers. Two live features on one
shared platform (unified login shell, one Amazon Cognito pool, one CloudFront
origin, one AWS WAF, one shared DynamoDB operational data layer), serverless-first
on AWS (`us-east-1`).

### Platform
- **StayOS shell** — unified login + feature launcher served at the site root
  `/`. A GM signs in once and both features trust the shared session (SSO) via
  the shared `@stayos/auth` module (`stayos.*` `localStorage` on the shared
  origin). Includes a StayOS logo mark/lockup and a first-login onboarding tour
  (LUMI → PULSE coachmark) plus a manual "Take a tour" replay.
- **Shared data layer** — 5 read-only operational dataset tables + 2 LUMI
  application tables (`stayos-*`), seeded with ~24k items of deterministic hotel
  operations data across 5 pilot properties; `NEW_AND_OLD_IMAGES` DynamoDB
  Streams feed PULSE. See [`docs/data-model.md`](docs/data-model.md).
- **Shared StayOS AgentCore Gateway** — one MCP tool layer (read-only hotel-ops
  tools) consumed by both LUMI's chat agent and PULSE's triage agent.
- **Root `make deploy-all`** — one-command platform deploy: LUMI, then PULSE
  wired to LUMI's outputs (Cognito, stream ARNs, Gateway endpoint, Tool Lambda),
  Gateway tool registration, Triage Agent build, and PULSE PWA publish.

### LUMI (Feature 1) — Daily GM Intelligence Brief
- Daily AI-generated brief (KPIs, VIP arrivals, overbooking/walk risk, OOO rooms)
  as a mobile dashboard plus a 60–90s Amazon Polly audio brief (multi-language).
- **Voice agent** (Amazon Nova Sonic, WebSocket push-to-talk) and **chat agent**
  (Strands + Claude Sonnet via the shared Gateway over MCP) for Q&A over the same
  dataset, both on Amazon Bedrock AgentCore Runtime.
- Brief history, per-GM EventBridge Scheduler delivery, AWS WAF, CloudWatch/X-Ray
  observability. Served at `/lumi`.

### PULSE (Feature 2) — Real-Time Situational Awareness
- Event-driven **Rule Engine** over the operational-table streams producing
  tiered alerts (CRITICAL / WARNING / INFO).
- Agentic **Triage Agent** (Strands + Claude Sonnet on AgentCore Runtime, shared
  Gateway) that attaches a schema-validated `triageBrief` (summary, confidence,
  ranked options) asynchronously.
- **Escalation Service** (GM → AGM → MOD chain), **dual-channel delivery**
  (AppSync Events realtime + Web Push), and a **closed-loop Action Executor**
  ("human approves, agent executes", EU AI Act Article 14) that writes the
  resolving action back and resolves the originating alert.
- PWA tabs (PULSE / VIPs / Ops / Kitchen), a demo scenario simulator, a
  30-minute alert auto-resolve sweeper, and 4 CloudFormation nested stacks.
  Served at `/pulse`.

[Unreleased]: https://github.com/hemaljani/StayOS/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/hemaljani/StayOS/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/hemaljani/StayOS/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/hemaljani/StayOS/releases/tag/v1.1.0
[1.0.0]: https://github.com/hemaljani/StayOS/releases/tag/v1.0.0
