# PULSE — Architecture

> Standalone architecture diagram for **PULSE** (StayOS Feature 2): a real-time,
> tiered, AI-triaged alerting layer with a human-approved closed-loop resolution
> path. This diagram is the canonical system view from
> [`../.kiro/specs/initial-pulse-project/design.md`](../.kiro/specs/initial-pulse-project/design.md)
> and is kept in sync with it. A rendered `architecture.png` sits alongside this
> file (mirroring LUMI's `lumi/docs/architecture.png` convention).

PULSE reuses LUMI's platform primitives — the shared PWA shell, the Cognito user
pool, the AWS WAF web ACL, the DynamoDB operational tables (now with Streams
enabled), and the shared StayOS AgentCore Gateway tool layer — and adds an
event-driven Rule Engine, an agentic Triage Agent, a dual-channel delivery layer
(AppSync Events realtime + Web Push background wake), and a closed-loop Action
Executor that writes approved resolutions back to the operational tables.

## System diagram

```mermaid
graph TD
    subgraph Client["GM Mobile Browser — StayOS PWA"]
        PWA["PULSE / VIPs / Ops / Kitchen tabs"]
        SW["Service Worker (Web Push)"]
    end

    subgraph Edge["Edge / Auth (reuse LUMI)"]
        CF["CloudFront + WAF"]
        COG["Cognito User Pool"]
    end

    subgraph API["API Layer"]
        APIGW["API Gateway (REST)"]
        APILAM["pulse-api Lambda<br/>(alerts, rules, approvals, push subs, kitchen)"]
        OPSREAD["pulse-ops-read Lambda<br/>(/vips, /ops via Gateway MCP)"]
    end

    subgraph Op["LUMI Operational Tables (source + write-back target)"]
        RES["stayos-reservations<br/>Streams: NEW_AND_OLD_IMAGES"]
        RM["stayos-rooms<br/>Streams: NEW_AND_OLD_IMAGES"]
        GST["stayos-guests<br/>Streams: NEW_AND_OLD_IMAGES"]
        WO["stayos-work-orders<br/>Streams: NEW_AND_OLD_IMAGES"]
        REV["stayos-revenues<br/>Streams: NEW_AND_OLD_IMAGES"]
    end

    subgraph Demo["Demo / Closed-Loop"]
        SIM["pulse-demo-simulator Lambda<br/>(on-demand + optional ambient schedule)"]
        EXEC["pulse-action-executor Lambda<br/>(approved-action write-back)"]
    end

    subgraph Engine["PULSE Rule + Triage Pipeline"]
        STREAM["DynamoDB Streams"]
        RULE["pulse-rule-evaluator Lambda"]
        TRIAGE["pulse-triage-agent<br/>(AgentCore Runtime: Strands + Claude Sonnet)"]
        BR["Amazon Bedrock<br/>(Claude Sonnet via TRIAGE_MODEL_ID)"]
        GW["StayOS AgentCore Gateway<br/>(MCP: shared hotel-ops tools + PULSE tools)"]
    end

    subgraph Deliver["Delivery + Escalation"]
        PUBLISH["pulse-push-service Lambda<br/>(dual-channel: realtime publish + Web Push)"]
        BATCH["pulse-info-batcher Lambda<br/>(EventBridge Scheduler)"]
        ESC["pulse-escalation-service Lambda<br/>(EventBridge Scheduler)"]
        APPSYNC["AWS AppSync Events API<br/>(namespace: pulse, Cognito auth,<br/>OnSubscribe property-scope guard,<br/>property broadcast + per-user unicast)"]
        WEBPUSH["Web Push endpoints (VAPID)"]
    end

    subgraph Data["PULSE Tables"]
        ALERTS["pulse-alerts"]
        RULES["pulse-rules"]
        HIST["pulse-alert-history"]
        SUBS["pulse-push-subscriptions"]
        KIT["pulse-kitchen"]
    end

    subgraph Obs["Observability"]
        CW["CloudWatch (metrics, logs, dashboard)"]
        XR["X-Ray traces"]
    end

    PWA --> CF --> APIGW --> APILAM
    APIGW --> OPSREAD
    OPSREAD -->|MCP tool calls| GW
    PWA -. auth .-> COG
    APILAM --> ALERTS
    APILAM --> RULES
    APILAM --> SUBS
    APILAM --> HIST
    APILAM --> KIT

    SIM -->|scripted deterministic mutations| RES & RM & GST & WO & REV
    RES & RM & GST & WO & REV --> STREAM --> RULE
    RULE --> RULES
    RULE -->|CRITICAL/WARNING async invoke| TRIAGE
    TRIAGE --> BR
    TRIAGE -->|MCP tool calls| GW
    GW -->|read-only queries| RES & RM & GST & WO & REV
    TRIAGE -->|attach brief + publish ALERT_UPDATED| ALERTS
    RULE --> ALERTS
    RULE --> PUBLISH
    ALERTS -->|status change stream| HIST
    ALERTS -->|status change stream| ESC
    PUBLISH -->|background wake VAPID| WEBPUSH --> SW --> PWA
    PUBLISH -->|foreground realtime publish| APPSYNC
    APPSYNC -.->|wss realtime events| PWA
    PWA -. subscribe Cognito auth, OnSubscribe scope .-> APPSYNC
    APPSYNC -. auth .-> COG
    BATCH -->|INFO batch| PUBLISH
    ESC --> PUBLISH

    APILAM -->|GM-approved option| EXEC
    EXEC -->|write-back clears condition| RES & RM & GST & WO & REV
    EXEC -->|set RESOLVED transactionally| ALERTS
    EXEC -->|publish RESOLVED realtime| APPSYNC

    RULE & TRIAGE & PUBLISH & ESC & APILAM & OPSREAD & SIM & EXEC & APPSYNC & GW --> CW
    RULE & TRIAGE & PUBLISH & EXEC --> XR
```

## The closed loop

```
operational data change  ->  Stream  ->  Rule Engine  ->  Alert + Triage
        ^                                                     |
        |                                                     v
   Action Executor  <--  GM approves ranked option  <--  Push to GM
   (write-back)
        |
        +-->  operational data change clears the condition  ->  Stream
                                                                 |
                                                                 v
                                          Rule Engine re-evaluates -> originating alert -> RESOLVED
```

A change in operational data is picked up, triaged by the agent, the GM approves
a resolving action, and that action **writes back** to the operational data. The
write-back flows through Streams like any other change; the Rule Engine
re-evaluates, sees the triggering condition no longer holds, and transitions the
**originating** alert to RESOLVED (correlated by `sourceEntityRef` / `dedupeKey`)
rather than emitting a new one.

## Legend

- **Reused from LUMI (shared StayOS platform):** CloudFront + WAF, Cognito user
  pool, the five `lumi-*` operational tables, and the StayOS AgentCore Gateway
  tool layer.
- **New in PULSE:** the Rule Engine (`pulse-rule-evaluator`), the agentic Triage
  Agent (AgentCore Runtime), the dual-channel delivery layer (AppSync Events +
  Web Push), the Escalation Service, and the closed-loop Demo Simulator +
  Action Executor.
- **Solid arrows** = data/control flow; **dotted arrows** = auth and realtime
  WebSocket (AppSync Events) paths.

For the full requirement/property mapping and design decisions, see
[`../.kiro/specs/initial-pulse-project/design.md`](../.kiro/specs/initial-pulse-project/design.md).
