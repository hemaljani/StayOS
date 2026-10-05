# StayOS — Deployment Pipeline Reference

How the whole StayOS platform gets from `git clone` to a fully populated,
running system with **one command**: `make deploy-all`.

This document explains what that command does, in what order, why the order
matters, and how the pieces are wired together. For the data these stacks read
and write, see [`data-model.md`](data-model.md).

---

## Contents

- [TL;DR](#tldr)
- [Why a single orchestrated command](#why-a-single-orchestrated-command)
- [The pipeline, stage by stage](#the-pipeline-stage-by-stage)
  - [Stage 1 — Deploy LUMI](#stage-1--deploy-lumi)
  - [Stage 2 — Capture LUMI outputs and deploy PULSE initially](#stage-2--capture-lumi-outputs-and-deploy-pulse-initially)
  - [Stage 3 — Register PULSE tools on the shared Gateway](#stage-3--register-pulse-tools-on-the-shared-gateway)
  - [Stage 4 — Build the Triage Agent](#stage-4--build-the-triage-agent)
  - [Stage 5 — Build the Forecasting Agent and wire runtimes](#stage-5--build-the-forecasting-agent-and-wire-runtimes)
  - [Stage 6 — Publish the PULSE PWA](#stage-6--publish-the-pulse-pwa)
  - [Stage 7 — Publish the StayOS shell](#stage-7--publish-the-stayos-shell)
  - [Stage 8 — Deploy the shared Data Orchestrator](#stage-8--deploy-the-shared-data-orchestrator)
- [First run is populated automatically](#first-run-is-populated-automatically)
- [Parameters and configuration](#parameters-and-configuration)
- [Deployment output and logs](#deployment-output-and-logs)
- [How the root Makefile is structured](#how-the-root-makefile-is-structured)
- [Failure recovery](#failure-recovery)
- [Teardown](#teardown)
  - [Why the order reverses](#why-the-order-reverses)
  - [The teardown, phase by phase](#the-teardown-phase-by-phase)
    - [Phase 1 — Data Orchestrator](#phase-1--data-orchestrator)
    - [Phase 2 — Shell](#phase-2--shell)
    - [Phase 3 — PULSE](#phase-3--pulse)
    - [Phase 4 — LUMI (last)](#phase-4--lumi-last)
  - [Why buckets and ECR are emptied first](#why-buckets-and-ecr-are-emptied-first)
  - [Failure recovery](#failure-recovery-1)
- [Related docs](#related-docs)

---

## TL;DR

```bash
make deploy-all APP_PASSWORD=YourSecurePassword123!
```

That single command deploys three things in a fixed order — **LUMI → PULSE →
Data Orchestrator** — because each stage produces values the next stage needs.
PULSE can never be deployed standalone from a clean account: it consumes
outputs produced by the LUMI deploy. The final stage primes today's data, so
every GM has a live daily brief the moment the command finishes.

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## Why a single orchestrated command

StayOS is a multi-feature platform, not one stack:

| Component | Stack prefix | Owns |
|-----------|-------------|------|
| **LUMI** | `stayos` | The shared foundation: Cognito user pool, the 5 read-only operational DynamoDB tables (+ their streams), the shared AgentCore Gateway, the shared Tool Lambda, and the CloudFront distribution served at `/`. |
| **PULSE** | `pulse` | The real-time alerting feature: rule evaluator, Triage Agent + Forecasting Agent (AgentCore Runtimes), Action Executor, and the PULSE PWA served at `/pulse`. |
| **Data Orchestrator** | `stayos-data` | The shared, additive roll-forward + PULSE-baseline layer that keeps each property's data current. |

The dependency runs one direction: **PULSE depends on LUMI**, and the **Data
Orchestrator depends on both**. PULSE needs concrete values that only exist
*after* LUMI is deployed:

- the **Cognito user pool** (id, ARN, client id) — for authenticating PULSE users
- the **five operational-table DynamoDB stream ARNs** — the PULSE rule evaluator
  subscribes to these streams to fire alerts
- the **shared AgentCore Gateway endpoint** — PULSE registers its tools here
- the **shared Tool Lambda ARN** — the target PULSE's Gateway tools invoke

Deploying PULSE by hand would mean copying all of those values out of the LUMI
stack outputs and passing them in correctly. `make deploy-all` does exactly that
capture-and-thread step for you, which is why it is the supported path from a
clean account.

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## The pipeline, stage by stage

`make deploy-all` runs eight numbered stages (echoed as `[n/8]` in the
build log). The flow is a single ordered chain — each stage's output feeds the
next:

```mermaid
flowchart TB
    Start(["make deploy-all<br/>APP_PASSWORD=..."])

    subgraph LUMI["🔵 Stage 1 — LUMI"]
        L1["Deploy LUMI stack<br/>(stayos-us-east-1)"]
    end

    subgraph CAPTURE["🔵 Stage 2 — Capture + PULSE stack"]
        C1["Read LUMI outputs:<br/>Cognito pool · 5 stream ARNs<br/>Gateway endpoint · Tool Lambda ARN"]
        C2["Deploy PULSE infrastructure<br/>package · Lambda refresh · seeds"]
        C1 --> C2
    end

    subgraph GATEWAY["🔴 Stage 3 — Gateway tools"]
        G1["Register PULSE tools on the<br/>shared StayOS Gateway"]
    end

    subgraph TRIAGE["🔴 Stage 4 — Triage Agent"]
        T1["Build + deploy Triage Agent<br/>to AgentCore Runtime"]
    end

    subgraph FORECAST["🔴 Stage 5 — Forecasting Agent"]
        F1["Build + deploy Forecasting Agent<br/>to AgentCore Runtime"]
        F2["CloudFormation-only wiring update<br/>(thread both runtime ARNs)"]
        F1 --> F2
    end

    subgraph PWA["🔴 Stage 6 — PULSE PWA"]
        W1["Publish PULSE PWA to /pulse<br/>on the shared CloudFront"]
    end

    subgraph SHELL["🔵 Stage 7 — StayOS shell"]
        S1["Publish shell (login + launcher)<br/>to the distribution root /"]
    end

    subgraph DATA["🟢 Stage 8 — Data Orchestrator"]
        D1["Deploy stayos-data<br/>(additive roll-forward + baseline)"]
        D2["Prime today's data for<br/>every pilot property"]
        D1 --> D2
    end

    Start --> LUMI
    LUMI -->|outputs threaded in| CAPTURE
    CAPTURE --> GATEWAY --> TRIAGE --> FORECAST --> PWA --> SHELL --> DATA
    DATA --> Done(["✅ Every GM has a live brief"])

    classDef lumi fill:#e3f2fd,stroke:#1565c0,color:#0d47a1;
    classDef pulse fill:#fce4ec,stroke:#c2185b,color:#880e4f;
    classDef data fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef edge fill:#fff,stroke:#616161,color:#212121;
    class L1,C1,C2,S1 lumi;
    class G1,T1,F1,F2,W1 pulse;
    class D1,D2 data;
    class Start,Done edge;
```

### Stage 1 — Deploy LUMI

```
make -C lumi deploy APP_PASSWORD=... PROFILE=... REGION=... \
    ENVIRONMENT=test EXPECTED_ACCOUNT_ID=...
```

Deploys the LUMI root stack (`stayos-<region>`, e.g. `stayos-us-east-1`) and its
nested stacks — including the `DataStack` that owns the 5 operational tables and
their DynamoDB streams. `APP_PASSWORD` sets the login password for the 5 demo GM
accounts. The existing ComputeStack owns the dependency-layer CodeBuild project
and a Lambda-backed custom resource that waits for the build before creating
the layer version. CodeBuild packages the shared layer on Lambda-compatible
Python 3.12 / Amazon Linux / `x86_64` compute, requires binary wheels,
validates its native imports, and writes it to a build-input-fingerprinted S3
key. This produces the same target-compatible Lambda layer from macOS, Linux,
or Windows without a local Docker dependency or another CloudFormation stack.
This is the foundation everything else builds on.

### Stage 2 — Capture LUMI outputs and deploy PULSE initially

The Makefile reads values back out of the deployed LUMI stack using the
CloudFormation `Outputs[?OutputKey=='X'].OutputValue` query pattern:

- `UserPoolId`, `UserPoolClientId`, `ToolLambdaArn` — from the LUMI **root** stack
- the five `*StreamArn` outputs — from the nested **`DataStack`** (its physical
  name is resolved via its fixed logical id `DataStack`)
- `UserPoolArn` — **not** a stack output, so it is derived deterministically from
  `UserPoolId` + region + account id
- the Gateway endpoint URL — read from SSM parameter
  `/stayos/gateway/endpoint-url`

From each stream ARN the Makefile also derives the table ARN and table name
(string-trimming `/stream/...` and `:table/...`) so PULSE can scope its IAM
policies to exactly those tables. All of these are passed into:

```
make -C pulse deploy-initial USER_POOL_ID=... \
    RESERVATIONS_STREAM_ARN=... (…and the rest)
```

This initial PULSE deploy creates or updates infrastructure, builds the backend
package on CodeBuild, refreshes all Lambda functions, and runs the idempotent
seeds. The Triage and Forecast runtimes do not exist yet, so both runtime ARNs
are passed empty.

### Stage 3 — Register PULSE tools on the shared Gateway

```
make -C pulse gateway-deploy TOOL_LAMBDA_ARN=...
```

Registers PULSE's tools on the shared StayOS AgentCore Gateway, pointing them at
LUMI's shared Tool Lambda.

### Stage 4 — Build the Triage Agent

```
make -C pulse triage-deploy
```

Builds and deploys the PULSE Triage Agent to AgentCore Runtime (via CodeBuild —
no local Docker/Finch needed) and writes its runtime ARN to SSM parameter
`/pulse/triage/runtime-arn`. The Makefile validates the parameter and retains
the ARN for the final PULSE update after the Forecasting Agent is also ready.

### Stage 5 — Build the Forecasting Agent and wire runtimes

```
make -C pulse forecast-deploy
```

Builds and deploys the PULSE Forecasting Agent to AgentCore Runtime (via the
same shared CodeBuild project — no local Docker/Finch needed) and writes its
runtime ARN to SSM parameter `/pulse/forecast/runtime-arn`. The deployment
runner reads that ARN back, then invokes the CloudFormation-only
`deploy-runtime-wiring` target, threading in **both** `TRIAGE_RUNTIME_ARN` and
`FORECAST_RUNTIME_ARN` so the per-property
EventBridge scheduler and its `pulse-forecaster-invoker` Lambda can invoke the
Forecasting Agent. The Forecasting Agent is additive and advisory-only: it runs
ahead of the reactive Rule Engine to warn of likely room oversell, and its
runtime execution role (`pulse-forecaster-role-<region>`) is created by the
PULSE stack, so it already exists by the time this stage runs. Waiting until
both runtimes are ready reduces the deployment to two CloudFormation passes.
The wiring pass does **not** rerun CodeBuild, refresh Lambda code, invoke seeds,
or print standalone next-step guidance.

### Stage 6 — Publish the PULSE PWA

```
make -C pulse deploy-frontend USER_POOL_CLIENT_ID=... COGNITO_REGION=...
```

Publishes the PULSE progressive web app to `/pulse` on the **shared LUMI
CloudFront distribution** (PULSE does not own its own distribution).

### Stage 7 — Publish the StayOS shell

```
make -C stayos-shell deploy PROFILE=... REGION=...
```

Builds the StayOS shell (the login + feature-launcher app) and publishes it to
the **root** of the shared distribution (`/`), the front door that ties LUMI and
PULSE together. The sync excludes `lumi/*` and `pulse/*` so it never touches the
feature assets. The shell self-sources its config (Cognito client id, CloudFront
distribution id) from the LUMI stack outputs, so it only needs LUMI to exist —
true by this point. Without this stage the distribution root serves an S3
`AccessDenied` (nothing is published at `/`).

### Stage 8 — Deploy the shared Data Orchestrator

```
make -C shared/data-orchestrator deploy PROFILE=... REGION=... \
    LUMI_STACK_PREFIX=stayos PULSE_STACK_PREFIX=pulse
```

Deploys the shared `stayos-data` stack, wired to the live LUMI table names and
PULSE rule-evaluator stream mappings, and primes today's data for every pilot
property.

**This stage is additive.** It does *not* re-seed or bulk-rewrite the live
dataset — the original seed data and any runtime changes are left untouched. The
priming step is an idempotent, failure-isolated roll-forward: a failure priming
one property does not block the others.

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## First run is populated automatically

Because Stage 8 primes today's data, **every GM has a current daily brief the
moment `make deploy-all` finishes** — there is no manual "generate the first
brief" step.

Thereafter, one **per-property EventBridge schedule** re-anchors each property's
data window at that property's local midnight, so briefs stay current day to
day.

As a safety net, the VIP-arrivals tool also falls back to a live reservations
query if a brief for the current date is ever missing, so it never reports a
false "no VIP arrivals".

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## Parameters and configuration

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `APP_PASSWORD` | **Yes** | — | Login password for the 5 demo GM accounts (forwarded to the LUMI deploy). |
| `PROFILE` | No | default credential chain | Canonical AWS CLI profile / target account. `AWS_PROFILE` remains accepted for compatibility; `PROFILE` wins if both are set. |
| `CLOUDFORMATION_PROFILE` | No | `PROFILE` | Optional role-chain profile used only for CloudFormation operations. It must resolve to the same target account and is useful when organization guardrails exempt a deployment role. |
| `REGION` | No | `us-east-1` | Target AWS region. |
| `ENVIRONMENT` | No | `test` | Logical deployment environment included in planning, reporting, and retries. Existing stack names remain unchanged. |
| `EXPECTED_ACCOUNT_ID` | No | — | Twelve-digit safety guard. Deployment stops before mutations when the resolved account differs. |
| `LUMI_STACK_PREFIX` | No | `stayos` | Stack prefix for LUMI; the LUMI stack name is `${LUMI_STACK_PREFIX}-${REGION}`. |
| `PULSE_STACK_PREFIX` | No | `pulse` | Stack and resource prefix for PULSE. |
| `DATA_STACK_PREFIX` | No | `stayos-data` | Stack and resource prefix for the shared Data Orchestrator. |

```bash
# Default account, us-east-1
make deploy-all APP_PASSWORD=YourSecurePassword123!

# A different account / region
make deploy-all APP_PASSWORD=YourSecurePassword123! PROFILE=my-other-account \
  REGION=us-west-2 ENVIRONMENT=test EXPECTED_ACCOUNT_ID=123456789012

# Target-account profile plus a same-account CloudFormation execution profile
make deploy-all APP_PASSWORD=YourSecurePassword123! PROFILE=my-target-account \
  CLOUDFORMATION_PROFILE=my-target-cfn-role \
  REGION=us-east-1 ENVIRONMENT=test EXPECTED_ACCOUNT_ID=123456789012

# Read-only account, stack, and stage inspection
make plan PROFILE=my-other-account REGION=us-west-2 \
  ENVIRONMENT=test EXPECTED_ACCOUNT_ID=123456789012
```

**Prerequisites** (one-time): AWS CLI v2.27+, Python 3.12+, Node.js 18+, and
Amazon Bedrock model access enabled in the target account/region (Claude Sonnet,
Nova Sonic, Polly).

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## Deployment output and logs

`make deploy-all` uses concise output by default. The root runner exclusively
owns the eight numbered stage headings and translates feature output into
bounded live progress without exposing IDs, ARNs, raw JSON, or upload chatter.
A successful run prints timed stage results, stack verification, application
URLs, asynchronous brief status, total duration, and the log path.

```text
[1/8] LUMI
  … Building or reusing the x86_64 layer in ComputeStack
  … Updating LUMI CloudFormation infrastructure
  … Refreshing LUMI Lambda functions (1/4)
  … Configuring the shared AgentCore Gateway
  … Building the voice runtime image on CodeBuild
  ✓ Foundation, Gateway, runtimes, and frontend ready (8m 42s)
```

Every run writes complete child-command output to
`logs/deploy-<timestamp>.log`. Output modes:

| Setting | Behavior |
|---------|----------|
| default | Concise live progress, timed stages, and summary; diagnostics go to the run log. |
| `VERBOSE=1` | Stream child-command output and include runtime, API, Gateway, and artifact details in the summary. |
| `NO_COLOR=1` | Disable ANSI color. |
| `CI` set | Use stable line-oriented output with no interactive rendering. |

On failure, the runner preserves the failed command's exit code and prints the
stage, the most useful error line, a targeted recovery command, and the run-log
path. Successful commands that emit an unresolved warning are shown as warnings,
not as completed-without-qualification results.

For detailed deployment information outside a run:

```bash
make deployment-summary-detailed PROFILE=... REGION=...
```

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## How the root Makefile is structured

The root `Makefile` is a **delegator** — it does not build feature code itself.
It forwards prefixed targets to each feature's own Makefile. For `deploy-all`,
it invokes the Python runner under `tools/stayos_deploy`, which owns stage
formatting, logging, output capture, recovery messages, and final verification:

| Pattern | Delegates to | Example |
|---------|-------------|---------|
| `make lumi-<target>` | `make -C lumi <target>` | `make lumi-deploy`, `make lumi-test` |
| `make pulse-<target>` | `make -C pulse <target>` | `make pulse-deploy`, `make pulse-triage-deploy` |
| `make shell-<target>` | `make -C stayos-shell <target>` | `make shell-deploy` |
| `make data-<target>` | `make -C shared/data-orchestrator <target>` | `make data-deploy`, `make data-test` |
| `make plan` | read-only configuration and AWS-state inspection | validates account, region, stack names, and stage order |
| `make deploy-all` | orchestrates LUMI → PULSE → Data Orchestrator | (this document) |
| `make deployment-summary-detailed` | reads runtime, artifact, API, and URL details | read-only AWS inspection |
| `make test-all` | deployment-tool, shared-auth, and feature tests | includes shared-auth tests and type checking |

Run `make help` from the repo root for the full target list. Each feature's own
`README.md` and `Makefile` document its individual targets and internals.

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## Failure recovery

`deploy-all` is designed so a failure in a later stage does **not** require
redeploying the earlier ones. Each stage prints a targeted re-run command on
failure. In general:

- **LUMI is deployed once** and is healthy after Stage 1 — later failures never
  require redeploying it.
- **PULSE stack failures** can be retried by re-running `make deploy-all` (LUMI
  is skipped-safe because it is already deployed).
- **Individual PULSE steps** (Gateway registration, Triage build, frontend
  publish) each have a standalone re-run command, e.g.
  `make pulse-gateway-deploy PROFILE=... REGION=... TOOL_LAMBDA_ARN=...`.
- **Data Orchestrator failures** are re-runnable with `make data-deploy` and are
  safe because the stage is additive (it does not touch live data).

Every deploy accepts the same canonical context: `PROFILE`, `REGION`,
`CLOUDFORMATION_PROFILE`, `ENVIRONMENT`, `EXPECTED_ACCOUNT_ID`, and stack-prefix
overrides. Keep that context unchanged when retrying. Both profiles are checked
against the same account before deployment; `EXPECTED_ACCOUNT_ID` prevents a
retry from targeting a different account. The successful pipeline ends with
observed stack/runtime/output verification plus a concrete deployment summary.

If CloudFormation is denied by an organization SCP while ordinary target-account
calls work, configure a role-chain profile whose `role_arn` is the approved
same-account deployment role and whose `source_profile` can assume it. Pass that
name as `CLOUDFORMATION_PROFILE`; do not replace `PROFILE`, because the latter
remains the canonical target-account identity for all other deployment calls.

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## Teardown

`make destroy-all` is the mirror of `make deploy-all`: it tears the whole
platform down with one command, in the **reverse** of the deploy order. It is
destructive and irreversible, so it is guarded by an explicit confirmation.

```bash
# Tear down everything. CONFIRM=DESTROY is required.
make destroy-all CONFIRM=DESTROY

# A different account / region
make destroy-all CONFIRM=DESTROY PROFILE=my-other-account REGION=us-west-2
```

### Why the order reverses

Deploy runs **LUMI → PULSE → Data Orchestrator** because the dependency points
that way (PULSE depends on LUMI; the orchestrator depends on both). Teardown
must therefore run the other way — **Data Orchestrator → shell → PULSE → LUMI** —
so nothing is pulled out from under a still-live consumer. LUMI owns the shared
foundation (Cognito user pool, AgentCore Gateway, shared ECR repos, and the
shared frontend bucket + CloudFront), so it is destroyed **last**.

```mermaid
flowchart TB
    Start(["make destroy-all<br/>CONFIRM=DESTROY"])

    subgraph DATA["🟢 Phase 1 — Data Orchestrator"]
        D1["Delete stayos-data stack<br/>+ wait for completion"]
    end

    subgraph SHELL["⚪ Phase 2 — Shell"]
        S1["Remove shell root objects from the<br/>shared bucket (keeps /lumi, /pulse)"]
    end

    subgraph PULSE["🔴 Phase 3 — PULSE"]
        P1["Delete Triage Agent runtime"]
        P2["Remove /pulse assets · purge triage<br/>image tag · empty PULSE deploy bucket"]
        P3["Delete PULSE stack + wait"]
        P1 --> P2 --> P3
    end

    subgraph LUMI["🔵 Phase 4 — LUMI (shared foundation, last)"]
        L1["Delete voice/chat runtimes + Gateway"]
        L2["Empty shared buckets · purge ECR repos"]
        L3["Delete LUMI stack + wait<br/>(CloudFront removal can take 15+ min)"]
        L1 --> L2 --> L3
    end

    Start --> DATA --> SHELL --> PULSE --> LUMI
    LUMI --> Done(["✅ Platform fully removed"])

    classDef lumi fill:#e3f2fd,stroke:#1565c0,color:#0d47a1;
    classDef pulse fill:#fce4ec,stroke:#c2185b,color:#880e4f;
    classDef data fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20;
    classDef shell fill:#f5f5f5,stroke:#616161,color:#212121;
    classDef edge fill:#fff,stroke:#616161,color:#212121;
    class D1 data;
    class S1 shell;
    class P1,P2,P3 pulse;
    class L1,L2,L3 lumi;
    class Start,Done edge;
```

### The teardown, phase by phase

`make destroy-all` runs four numbered phases (echoed as `══ [n/4] ══`).

#### Phase 1 — Data Orchestrator

```
make -C shared/data-orchestrator destroy PROFILE=... REGION=...
```

Deletes the additive `stayos-data` stack (deployed last, so removed first). The
orchestrator owns no tables, so this is a safe standalone delete. `destroy-all`
adds an explicit `wait stack-delete-complete` after it, because the
orchestrator's own `destroy` target does not wait.

#### Phase 2 — Shell

```
make -C stayos-shell destroy PROFILE=... REGION=...
```

The shell has no infrastructure of its own — it is static files at the **root**
of the shared bucket. So teardown removes only the shell's root objects,
**excluding `/lumi/*` and `/pulse/*`** (the same prefixes deploy excludes), then
invalidates the root cache. In a full teardown this is largely redundant (LUMI's
phase empties the whole bucket next), but it keeps the shell teardown correct for
standalone use.

#### Phase 3 — PULSE

```
make -C pulse destroy PROFILE=... REGION=...
```

Removes only PULSE-owned resources, leaving LUMI's shared foundation intact:

- deletes the **Triage Agent** AgentCore Runtime (CLI-managed, tracked in SSM)
- removes PULSE's **`/pulse` frontend assets** from the shared LUMI bucket
- deletes only the **`triage-latest` image tag** from the shared
  `stayos-chat-agent` ECR repo — never the repo or the chat agent's `:latest`
- empties PULSE's own **deploy bucket** (`pulse-deploy-<account>`)
- deletes the **PULSE stack** and waits for completion

PULSE's Gateway tools live on LUMI's **shared** Gateway target and are not
deregistered here — LUMI's `gateway-destroy` (Phase 4) removes the whole shared
Gateway, and PULSE's tools with it.

#### Phase 4 — LUMI (last)

```
make -C lumi destroy PROFILE=... REGION=...
```

Tears down the shared foundation, in the order CloudFormation needs:

- deletes the **voice + chat AgentCore runtimes** and the shared **AgentCore
  Gateway** (CLI-managed, not part of the stack); Gateway teardown waits until
  every target is absent before requesting Gateway deletion, then verifies the
  Gateway itself is absent before removing its SSM parameters
- **empties the shared frontend + deploy buckets** and **purges the voice/chat
  ECR repos** — CloudFormation cannot delete a non-empty bucket or repository
- deletes the **LUMI stack** and waits for completion (the CloudFront
  distribution removal can take 15+ minutes)

### Why buckets and ECR are emptied first

CloudFormation refuses to delete an S3 bucket that still holds objects or an ECR
repository that still holds images. Each feature's `destroy` therefore empties
its buckets and purges its images **before** calling `delete-stack`, so the stack
delete is not blocked mid-run.

### Failure recovery

Like `deploy-all`, each phase prints a targeted re-run command on failure, and
every phase waits for its stack delete to finish before the next begins:

- **Individual features** can be torn down on their own:
  `make data-destroy`, `make shell-destroy`, `make pulse-destroy`,
  `make lumi-destroy` (honoring each feature's `PROFILE`/`AWS_PROFILE` +
  `REGION`).
- **Order matters for standalone destroys** — `make lumi-destroy` removes the
  shared foundation, so run it only after PULSE and the shell are gone (or use
  `make destroy-all`, which sequences this for you).
- **A stalled stack delete** is usually a non-empty bucket/repo or a lingering
  dependency; inspect it with
  `aws cloudformation describe-stack-events --stack-name <stack>`.

<div align="right"><a href="#contents">↑ Back to top</a></div>

---

## Related docs

- [`data-model.md`](data-model.md) — the shared DynamoDB data model these stacks read and write
- [`../README.md`](../README.md) — platform overview and quick start
- [`../lumi/README.md`](../lumi/README.md) — LUMI internals and per-feature targets
- [`../pulse/README.md`](../pulse/README.md) — PULSE internals and per-feature targets

<div align="right"><a href="#contents">↑ Back to top</a></div>
