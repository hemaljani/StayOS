<p align="center">
  <img src="stayos-shell/frontend/assets/stayos-logo.svg" alt="StayOS logo" width="96" height="96" />
</p>

# StayOS

**StayOS is the operating system for hotel General Managers and associates.** It is
an agentic, mobile-first platform that turns the data trapped across a property's disconnected
systems (PMS, Revenue Management, Loyalty/CRM, Facilities) into proactive,
AI-generated intelligence for the people who run the hotel.

> [!NOTE]
> This is a prototype, not a production deployment. It demonstrates the StayOS
> architecture using real AWS services and mock operational data. The prototype
> showcases the latest AWS agentic AI technologies, including
> [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html),
> the [Strands Agents BidiAgent](https://strandsagents.com/docs/user-guide/sdk/bidi/agent/),
> and [Amazon Nova 2.5 Sonic](https://aws.amazon.com/about-aws/whats-new/2026/10/amazon-nova-2.5-Sonic/).


## Product Vision

Every hotel runs on disconnected systems, so operational intelligence reaches associates late, in fragments, and only if they go looking for it. StayOS closes this gap by reading the property’s existing data through a unified API layer and transforming it into proactive, AI-generated intelligence—delivered on mobile, to the people who run the hotel, before they need it.

**StayOS is the shared intelligence platform. Its individually branded features are focused experiences, each designed around a distinct operational mission and moment in the hotel day.** This gives every feature a clear promise that users can recognize and trust, while preserving the benefits of one connected platform: shared identity, property context, data, and agentic capabilities.

StayOS currently includes two live features for the property General Manager:

- <img src="stayos-shell/frontend/assets/lumi-logo.svg" alt="LUMI icon" width="18" height="18" align="absmiddle" /> **LUMI — Start the day informed.** LUMI turns the property’s most important overnight and forward-looking signals into a daily brief covering KPIs, VIP arrivals, overbooking risk, and out-of-order rooms. The brief is available as a dashboard and a 60–90 second AI-generated audio summary, with voice and chat for deeper questions.

- <img src="stayos-shell/frontend/assets/pulse-logo.svg" alt="PULSE icon" width="18" height="18" align="absmiddle" /> **PULSE — Stay ahead throughout the day.** PULSE monitors hotel operations continuously and delivers tiered, real-time alerts as situations develop. Each alert is triaged by an AI agent that gathers the relevant property context and produces a decision-ready brief. With the GM’s approval, the agent can execute the response and close the loop. PULSE also looks ahead through predictive forecasting that identifies potential room oversell before it occurs.

**One platform. Distinct operational missions. A growing family of intelligent experiences.**

More features will join StayOS over time, each with its own recognizable identity and focused purpose—but all powered by the same connected intelligence foundation.

Each app lives in its own top-level directory:

| App | Directory | What it does |
|---|---|---|
| **StayOS shell** | [`stayos-shell/`](stayos-shell/README.md) | Unified login + feature launcher grid; establishes the shared session (SSO) |
| **LUMI** | [`lumi/`](lumi/README.md) | Daily AI-generated GM brief (KPIs, VIP arrivals, overbooking risk, OOO rooms) as a dashboard + 60-90s audio brief, plus voice/chat Q&A agents over the same dataset |
| **PULSE** | [`pulse/`](pulse/README.md) | Real-time throughout-the-day tiered alerting (walk risk, VIP room readiness, complaint escalation) with agentic AI triage and closed-loop resolution, plus a predictive Forecasting Agent for room-oversell warnings (advisory) |


## Example Screenshots

<p align="center">
  <img src="docs/demo/stayos-demo.gif" alt="StayOS demo — landing, sign in, and the LUMI &amp; PULSE experiences" width="320" />
</p>

## Repository Layout

StayOS separates the shared platform foundation from its individually branded
experiences:

```text
StayOS/
├── stayos-shell/  # Shared platform entry point: authentication and feature launcher
├── lumi/          # LUMI branded experience: daily intelligence, voice, and chat
├── pulse/         # PULSE branded experience: real-time alerts and forecasting
├── shared/        # Platform services used across StayOS experiences
│   ├── auth/                # Shared SSO session package
│   └── data-orchestrator/   # Daily property data roll-forward and PULSE baseline priming
├── docs/           # Platform-wide design, architecture, and deployment documentation
├── tools/          # Deployment planning and validation utilities
└── openapi.yaml    # Unified StayOS REST API specification for LUMI and PULSE
```

## Deployment

**Prerequisites** (one-time): Git, GNU Make, `zip`, AWS CLI v2.27 or later,
Python 3.12 or later with `pip`, Node.js with npm, and AWS credentials configured
for the target account. Node.js must be 22.22.2 or later within 22.x, 24.15.0 or
later within 24.x, or 26.0.0 or later, matching the committed frontend test
dependencies (`^22.22.2 || ^24.15.0 || >=26.0.0`). In the target region, the
deployment identity must be able to use Amazon Bedrock AgentCore and invoke
[Anthropic Claude Sonnet 4.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
and [Amazon Nova 2.5 Sonic](https://aws.amazon.com/about-aws/whats-new/2026/10/amazon-nova-2.5-Sonic/).
Complete Anthropic's first-time-use setup if it applies to the account. StayOS
also uses [Amazon Polly](https://docs.aws.amazon.com/polly/latest/dg/neural-voices.html)
for generated MP3 briefs; Polly is a separate AWS service, not a Bedrock model.
`REGION` defaults to `us-east-1`.

The commands below use the AWS CLI default credential chain. `PROFILE` is
optional; `CLOUDFORMATION_PROFILE` defaults to the same profile, and
`EXPECTED_ACCOUNT_ID` is an optional account-safety check. See
[deployment configuration](docs/deployment-pipeline.md#parameters-and-configuration)
when those overrides are needed.

```bash
git clone https://github.com/hemaljani/StayOS.git
cd StayOS

# First deployment only. Use at least 8 characters with uppercase,
# lowercase, and a number. Symbols are allowed but are not required.
printf 'New demo-user password: '; IFS= read -r -s APP_PASSWORD; printf '\n'; APP_PASSWORD="$APP_PASSWORD" make deploy-all; unset APP_PASSWORD
```

Password characters are intentionally invisible while you type; press Enter to
finish. The password is passed only to this deployment, is not written into the
command or shell history, and is removed from the current shell afterward.

For a later deployment of the same installation, do not enter the password
again. Run:

```bash
unset APP_PASSWORD
make deploy-all
```

The deployment detects the existing LUMI stack and preserves its `AppPassword`
CloudFormation parameter without retrieving the value. The defensive `unset`
prevents an old shell variable from being mistaken for an intentional password
change. To replace the stack parameter deliberately, both
`CHANGE_APP_PASSWORD=1` and a newly entered `APP_PASSWORD` are required; see
[deployment password handling](docs/deployment-pipeline.md#deployment-password).
Changing this parameter does not change passwords for Cognito users that already
exist.

Successful deployments are concise by default: the root pipeline prints eight
timed stages, bounded live progress such as CodeBuild, CloudFormation, Lambda,
Gateway, runtime, and frontend activity, and a short verification summary.
Complete command output is saved for every run at
`logs/deploy-<timestamp>.log`; set `VERBOSE=1` to stream that diagnostic output
to the console as well. `NO_COLOR=1` disables ANSI color, and CI environments
always receive stable line-oriented output. Use
`make deployment-summary-detailed` after deployment for runtime ARNs, artifact
locations, API endpoints, and application URLs.

LUMI's shared Python dependency layer is built remotely by a CodeBuild project
and Lambda-backed custom resource in the existing ComputeStack, using
Lambda-compatible Python 3.12, Amazon Linux, and `x86_64`. Developers therefore
produce the same target-compatible layer from macOS, Linux, or Windows without
local container tooling or another CloudFormation stack. The
build-input-fingerprinted layer artifact is reused until its requirements or
build definition changes.

## Deployment Pipeline

`make deploy-all` runs one ordered pipeline — each stage feeds the next.
**PULSE depends on LUMI's outputs**, which the root workflow captures and passes
to its deployment. In order, it deploys **LUMI → PULSE → Data Orchestrator**:

1. Deploy the **LUMI** stack (the shared foundation).
2. Capture LUMI's outputs (Cognito pool, the five operational-table stream ARNs,
   the shared Gateway endpoint, the Tool Lambda ARN) and deploy the **PULSE**
   stack with them threaded in.
3. Register PULSE's tools on the shared Gateway, build + deploy the Triage Agent,
   build + deploy the Forecasting Agent, wire both runtime ARNs into one final
   PULSE update, and publish the PULSE PWA to `/pulse`.
4. Publish the **StayOS shell** (login + feature launcher) to the distribution
   root (`/`).
5. Deploy the shared **Data Orchestrator** (`stayos-data`) — an additive
   roll-forward layer that primes today's data for every property.
6. Verify all stacks, required outputs, and runtime parameters, then print the
   deployed stacks, artifacts, runtimes, and real application URLs.

LUMI's voice and chat agents use ARM64 container images built on CodeBuild and
pushed to ECR. The Makefile creates or updates their AgentCore runtimes through
the AWS CLI. The chat runtime follows Gateway registration and receives
`GATEWAY_ENDPOINT_URL` from SSM.

PULSE's Triage and Forecasting agents use the shared build resources and ECR
repository. Their runtime ARNs are stored in SSM at
`/pulse/{triage,forecast}/runtime-arn` and wired into a final stack update.
The initial PULSE pass uses `deploy-initial`; the final ARN update uses
`deploy-runtime-wiring`. The `EnableDemoSimulator` CloudFormation parameter
controls whether demo simulator resources are enabled.

Frontend deployment generates production configuration from stack outputs and
SSM runtime IDs. The shell build uses LUMI's shared Cognito configuration,
publishes to the shared bucket root while excluding `lumi/*` and `pulse/*`,
and invalidates the shell's root cache entries.

Two behaviors worth calling out:

- **The Data Orchestrator is additive** — it rolls data forward and lays down the
  PULSE baseline. It never re-seeds or bulk-rewrites the live dataset.
- **The platform is populated on first run** — every GM has a current daily brief
  immediately after `deploy-all`, no manual step. A per-property EventBridge
  schedule then re-anchors the window at each property's local midnight. As a
  safety net, the VIP-arrivals tool falls back to a live reservations query if a
  brief is ever missing, so it never reports a false "no VIP arrivals".

📖 **Further reading:** [`docs/deployment-pipeline.md`](docs/deployment-pipeline.md)
walks through the full eight-stage pipeline with a diagram, the Makefile structure,
every parameter, and failure-recovery steps.

### Component redeployments

Use `make deploy-all` for a complete platform update. The following targets
support focused work on an existing installation. Use the same deployment
profiles, region, account, and stack prefixes as the original installation.
PULSE and the shell require LUMI's shared resources to exist.

| Command from the repository root | Purpose |
|---|---|
| `make lumi-gateway-deploy` | Update the shared Gateway and Tool Lambda target, and attempt WAF association |
| `make lumi-chat-build` | Build the chat image on CodeBuild and push it to ECR |
| `make lumi-chat-deploy` | Build and create/update the chat AgentCore Runtime |
| `make lumi-write-frontend-env` | Regenerate LUMI's `frontend/.env.production` from stack outputs and SSM |
| `make pulse-deploy` | Update the PULSE stack with explicitly supplied LUMI inputs |
| `make pulse-triage-deploy` | Build and create/update the Triage Agent runtime |
| `make pulse-forecast-deploy` | Build and create/update the Forecasting Agent runtime |
| `make pulse-gateway-deploy` | Register PULSE tools on the shared Gateway |
| `make pulse-deploy-frontend` | Build and publish PULSE assets at `/pulse` |
| `make shell-deploy` | Generate configuration, build, and publish the shell at `/` |

To upgrade only an existing voice deployment, use:

```bash
make lumi-voice-upgrade PROFILE=... CLOUDFORMATION_PROFILE=... \
  REGION=us-east-1 EXPECTED_ACCOUNT_ID=...
```

This preserves the existing password, other nested template references, and
runtime configuration. It builds with the existing CodeBuild project and
previews the voice IAM policy change before updating the parent stack and
runtime. See the
[focused upgrade and rollback procedure](docs/deployment-pipeline.md#focused-voice-upgrade).

Run `make help` for the complete target list. Feature READMEs document their
architecture, configuration, and local build/test commands.

## Teardown

`make destroy-all` is the mirror of `deploy-all` — it tears the whole platform
down in the **reverse** order (**Data Orchestrator → shell → PULSE → LUMI**, so
LUMI's shared foundation goes last). It is destructive and irreversible, so it is
guarded by an explicit confirmation:

```bash
# Tear down everything. CONFIRM=DESTROY is required (destructive, irreversible).
make destroy-all CONFIRM=DESTROY

# Target a specific AWS account / region:
make destroy-all CONFIRM=DESTROY PROFILE=my-other-account REGION=us-west-2
```

The root workflow performs these phases in order:

| Phase | Resources removed |
|---|---|
| Data Orchestrator | The shared roll-forward stack; waits for deletion to finish |
| Shell | Root assets in the shared bucket, excluding `lumi/*` and `pulse/*` |
| PULSE | Triage and Forecasting runtimes, `/pulse` assets, their shared ECR image tags, deploy-bucket contents, and the PULSE stack |
| LUMI | Voice/chat runtimes, the shared Gateway, frontend/audio/deploy-bucket contents, voice/chat ECR images, and the shared foundation stack |

LUMI owns the shared Cognito pool, Gateway, ECR repositories, frontend bucket,
and CloudFront distribution, so it is removed last. Gateway teardown waits for
every target to disappear before deleting and verifying the Gateway. Buckets
and repositories are emptied before CloudFormation deletion.

📖 **Further reading:** [`docs/deployment-pipeline.md`](docs/deployment-pipeline.md#teardown)
covers the full reverse-order teardown — the per-phase breakdown, a diagram, the
standalone `make <feature>-destroy` commands, and failure recovery.

## Data Model

All features read from **one shared DynamoDB operational layer** owned by LUMI:
5 read-only dataset tables (`stayos-guests`, `stayos-rooms`,
`stayos-reservations`, `stayos-work-orders`, `stayos-revenues`) plus 2 LUMI
application tables (`stayos-briefs`, `stayos-settings`). PULSE adds its own
`pulse-*` tables (see [`pulse/README.md`](pulse/README.md)). The 5 dataset
tables are partitioned by `propertyId`, which is the data-isolation boundary
between properties (`stayos-settings` is keyed by `gmAlias`). They seed once and
are read-only at runtime (`Query`/`GetItem`) — the only runtime write-back is
PULSE's GM-approved closed-loop Action Executor — and they stream changes
(`NEW_AND_OLD_IMAGES`), which is what PULSE's rule engine evaluates to fire
real-time alerts.

The shared **Data Orchestrator** (`shared/data-orchestrator/`) re-anchors this
dataset daily: one per-property EventBridge schedule fires at each property's
local midnight and rolls the deterministic 30-day window forward via idempotent
upsert (pausing PULSE evaluation during the rewrite so no alert storm fires),
then regenerates that day's brief. It is additive and never bulk-rewrites or
re-seeds the live tables. See
[`docs/data-model.md`](docs/data-model.md) for the full model.

**The canonical schema reference is [`docs/data-model.md`](docs/data-model.md)**
— full table schemas, keys/GSIs, enumerated values, relationships, and seed
volumes. It is not duplicated here to avoid drift.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).
