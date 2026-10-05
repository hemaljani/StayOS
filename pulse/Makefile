# Makefile - PULSE (StayOS Feature #2) build, test, validate & deploy
#
# Build/test/lint locally; deploy is a manual step. Mirrors LUMI's conventions.
# Container builds use CodeBuild (ARM64), never Docker; `finch` is the local
# equivalent. See pulse/README.md#deploy for the full flow and out-of-band
# prerequisites (AWS CLI v2 >= 2.27, Python 3.12, Node 18+, zip, cfn-lint, finch).
#
# Quick start:
#   make help          # list all targets
#   make test          # backend + frontend test suites
#   make lint          # backend + frontend linters
#   make validate      # cfn-lint the CloudFormation templates
#   make deploy USER_POOL_ID=... USER_POOL_ARN=... RESERVATIONS_STREAM_ARN=... (see below)

# ─── Config ───────────────────────────────────────────────────
STACK_PREFIX ?= pulse
REGION       ?= us-east-1
PROFILE      ?=
ifeq ($(strip $(PROFILE)),)
PROFILE      := $(AWS_PROFILE)
endif
ifeq ($(strip $(PROFILE)),)
unexport AWS_PROFILE
else
override AWS_PROFILE := $(PROFILE)
export AWS_PROFILE
endif
ENVIRONMENT  ?= test
EXPECTED_ACCOUNT_ID ?=
CLOUDFORMATION_PROFILE ?= $(PROFILE)
AWS_REGION := $(REGION)
AWS_DEFAULT_REGION := $(REGION)
export AWS_REGION AWS_DEFAULT_REGION

# Directory layout (override only if the tree changes).
BACKEND_DIR  ?= backend
FRONTEND_DIR ?= frontend
INFRA_DIR    ?= infrastructure
BUILD_DIR    ?= build

# Derived AWS invocation: add `--profile` only when PROFILE is set.
AWS_PROFILE_FLAG := $(if $(PROFILE),--profile $(PROFILE),)
AWS := aws $(AWS_PROFILE_FLAG) --region $(REGION)
CFN_PROFILE_FLAG := $(if $(CLOUDFORMATION_PROFILE),--profile $(CLOUDFORMATION_PROFILE),)
CFN_AWS := aws $(CFN_PROFILE_FLAG) --region $(REGION)

# Account + stack identity.
ACCOUNT_ID ?= $(shell $(AWS) sts get-caller-identity --query Account --output text 2>/dev/null)
CFN_ACCOUNT_ID ?= $(shell $(CFN_AWS) sts get-caller-identity --query Account --output text 2>/dev/null)
STACK_NAME := $(STACK_PREFIX)-$(REGION)

# S3 deploy bucket for the shared Lambda package + packaged nested templates.
# Account-scoped for global S3 uniqueness (NAMING-02). Threaded into nested
# stacks via the root stack's DeployBucket parameter.
DEPLOY_BUCKET     ?= pulse-deploy-$(ACCOUNT_ID)
LAMBDA_CODE_S3_KEY ?= functions/pulse-backend.zip
LAMBDA_ZIP        := $(BUILD_DIR)/pulse-backend.zip
# Shared Lambda zip builds on CodeBuild (x86_64 Linux), not locally, so native
# wheels (e.g. pydantic_core from [ops-read]) match the Lambda runtime.
BACKEND_BUILD_PROJECT ?= $(STACK_PREFIX)-backend-build
BACKEND_SOURCE_KEY    ?= backend-source/backend-source.zip
PACKAGED_TEMPLATE := $(BUILD_DIR)/packaged-root-stack.yaml

# Triage Agent (AgentCore Runtime). Built out-of-band on LUMI's EXISTING shared
# CodeBuild project and pushed to LUMI's EXISTING shared ECR repo (no new
# CodeBuild/ECR for PULSE) under a distinct 'triage-latest' tag to avoid
# colliding with the chat agent's ':latest'. Override only if the shared owner changes.
TRIAGE_MODEL_ID        ?= us.anthropic.claude-sonnet-4-6
# Shared LUMI build resources reused by PULSE (owned by LUMI's voice/chat stacks).
SHARED_BUILD_PROJECT   ?= $(LUMI_STACK_PREFIX)-chat-agent-build
SHARED_DEPLOY_BUCKET   ?= $(LUMI_STACK_PREFIX)-deploy-$(ACCOUNT_ID)
SHARED_AGENT_ECR_REPO  ?= $(LUMI_STACK_PREFIX)-chat-agent
TRIAGE_IMAGE_TAG       ?= triage-latest
# Upload under chat-agent-source/: the shared build role's S3 read policy is
# scoped to that exact prefix, so reuse needs no IAM change.
TRIAGE_SOURCE_KEY      ?= chat-agent-source/triage-agent-source.zip
# Deferred (=) so LUMI_STACK_PREFIX (defined below) resolves at use-time.
TRIAGE_ECR_URI          = $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/$(SHARED_AGENT_ECR_REPO)
TRIAGE_ROLE_NAME       := $(STACK_PREFIX)-triage-agentcore-role-$(REGION)
TRIAGE_RUNTIME_NAME    := $(STACK_PREFIX)_triage_agent
TRIAGE_RUNTIME_SSM     := /$(STACK_PREFIX)/triage/runtime-arn

# Forecasting Agent (AgentCore Runtime). Additive PULSE agent that runs AHEAD of
# the reactive Rule Engine. Like Triage, built out-of-band on LUMI's shared
# CodeBuild + ECR (no new resources) under a distinct 'forecaster-latest' tag.
FORECAST_MODEL_ID        ?= us.anthropic.claude-sonnet-4-6
FORECAST_IMAGE_TAG       ?= forecaster-latest
# chat-agent-source/ prefix so the shared build role's S3 read policy needs no
# IAM change (mirrors triage).
FORECAST_SOURCE_KEY      ?= chat-agent-source/forecast-agent-source.zip
# Deferred (=) so LUMI_STACK_PREFIX (defined below) resolves at use-time.
FORECAST_ECR_URI          = $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/$(SHARED_AGENT_ECR_REPO)
# Region-suffixed execution role created by pulse-api.yaml (PulseForecasterAgentCoreRole).
FORECAST_ROLE_NAME       := $(STACK_PREFIX)-forecaster-role-$(REGION)
FORECAST_RUNTIME_NAME    := $(STACK_PREFIX)_forecaster
FORECAST_RUNTIME_SSM     := /$(STACK_PREFIX)/forecast/runtime-arn
FORECAST_RUNTIME_ID_SSM  := /$(STACK_PREFIX)/forecast/runtime-id
# Comma-delimited forecasting-enabled propertyIds (invocation gate, Req 1.4/1.5):
# the 5 pilot properties with per-property daily schedules in pulse-pipeline.
FORECAST_ENABLED_PROPERTY_IDS ?= ALOHA-CHI-001,ALOHA-MIA-001,ALOHA-TYO-001,ALOHA-MAD-001,ALOHA-BOM-001

# Shared StayOS AgentCore Gateway (owned by LUMI; PULSE adds tools to it). The
# Gateway id/endpoint live in SSM under LUMI's namespace, so these paths key off
# LUMI_STACK_PREFIX, not STACK_PREFIX.
LUMI_STACK_PREFIX     ?= stayos
GATEWAY_ID_SSM        := /$(LUMI_STACK_PREFIX)/gateway/gateway-id
GATEWAY_ENDPOINT_SSM  := /$(LUMI_STACK_PREFIX)/gateway/endpoint-url
GATEWAY_TARGET_NAME   ?= tools
# PULSE tool schema + shared Tool Lambda ARN the Gateway target invokes.
# Deploy-time inputs (see pulse/README.md#deploy prerequisite 3); override on CLI.
GATEWAY_TOOL_SCHEMA   ?= ../lumi/backend/tools/tool-schema.json
TOOL_LAMBDA_ARN       ?=

# ─── Deploy-time parameters (threaded to the root stack) ──────
# Values known only at deploy time; pass on the CLI or export. The five stream
# ARNs and the Cognito user pool ARN/ID have no template default and are
# required by `make deploy` (guarded below). See pulse/README.md#deploy.
USER_POOL_ID            ?=
USER_POOL_ARN           ?=
USER_POOL_CLIENT_ID     ?=
WAF_WEB_ACL_ARN         ?=
VAPID_PUBLIC_KEY        ?=
RESERVATIONS_STREAM_ARN ?=
ROOMS_STREAM_ARN        ?=
GUESTS_STREAM_ARN       ?=
REVENUES_STREAM_ARN     ?=
WORK_ORDERS_STREAM_ARN  ?=
# Operational-table ARNs (IAM scoping for rule-engine OperationalRead + API
# OperationalWriteBack) and names (runtime env for Action Executor / Simulator).
# Same five LUMI tables as the stream ARNs; required by `make deploy` (guarded)
# so the IAM policies never get an empty Resource (BUG-006).
RESERVATIONS_TABLE_ARN  ?=
ROOMS_TABLE_ARN         ?=
GUESTS_TABLE_ARN        ?=
REVENUES_TABLE_ARN      ?=
WORK_ORDERS_TABLE_ARN   ?=
RESERVATIONS_TABLE_NAME ?=
ROOMS_TABLE_NAME        ?=
GUESTS_TABLE_NAME       ?=
GATEWAY_ENDPOINT_URL    ?=
TRIAGE_RUNTIME_ARN      ?=
FORECAST_RUNTIME_ARN    ?=
ENABLE_DEMO_SIMULATOR   ?= true
ORCHESTRATED            ?= 0

# ─── Shared frontend hosting (Option A: /pulse path on LUMI CloudFront) ───────
# PULSE's static export is served from the /pulse/ prefix of the SHARED StayOS
# (LUMI) S3 bucket + CloudFront distribution - one origin, one Cognito session,
# one WAF. deploy-frontend syncs out/ into s3://<lumi-frontend>/pulse/ and
# invalidates /pulse/*. Cross-feature READ only (no LUMI files modified).
LUMI_STACK          := $(LUMI_STACK_PREFIX)-$(REGION)
LUMI_FRONTEND_BUCKET ?= $(LUMI_STACK_PREFIX)-frontend-$(ACCOUNT_ID)
# NEXT_PUBLIC_* build-time inputs for the PULSE PWA. API URL defaults to the
# PULSE stack's ApiEndpoint when unset; Cognito comes from the shared LUMI pool.
PULSE_API_URL       ?=
COGNITO_CLIENT_ID   ?= $(USER_POOL_CLIENT_ID)
COGNITO_REGION      ?= $(REGION)

.PHONY: help assert-account test test-backend test-frontend lint lint-backend lint-frontend \
        build-frontend write-frontend-env deploy-frontend validate ensure-bucket \
        package package-backend package-infra ensure-code-placeholder \
        update-lambda-code deploy deploy-initial deploy-stack \
        deploy-runtime-wiring _require-deploy-vars \
        gateway-deploy triage-build triage-deploy clean kitchen-reseed seed-run \
        forecast-build forecast-deploy forecast-destroy \
        triage-destroy destroy

.DEFAULT_GOAL := help

# ─── Help ─────────────────────────────────────────────────────
help: ## List available targets
	@echo "PULSE (StayOS Feature #2) - make targets:"
	@echo ""
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Config: STACK_PREFIX=$(STACK_PREFIX) REGION=$(REGION) DEPLOY_BUCKET=$(DEPLOY_BUCKET)"
	@echo "        PROFILE=$(if $(PROFILE),$(PROFILE),<default>)  STACK_NAME=$(STACK_NAME)"

# ─── Test ─────────────────────────────────────────────────────
test: test-backend test-frontend ## Run backend + frontend test suites
	@echo "All PULSE tests passed"

test-backend: ## Run backend pytest (unit + Hypothesis property tests)
	@echo "Running backend tests..."
	@cd $(BACKEND_DIR) && python3 -m pytest

test-frontend: ## Run frontend vitest (unit + fast-check) once
	@echo "Running frontend tests..."
	@cd $(FRONTEND_DIR) && npx vitest run

# ─── Lint ─────────────────────────────────────────────────────
lint: lint-backend lint-frontend ## Lint backend + frontend
	@echo "Lint clean"

lint-backend: ## Lint backend with ruff (PEP 8, isort, pydocstyle)
	@echo "Linting backend..."
	@cd $(BACKEND_DIR) && ruff check src tests

lint-frontend: ## Lint frontend with next lint / eslint
	@echo "Linting frontend..."
	@cd $(FRONTEND_DIR) && npm run lint

# ─── Frontend build ───────────────────────────────────────────
build-frontend: ## Build the Next.js PWA (static export to out/)
	@echo "Building frontend..."
	@# Install first so a clean checkout has all build dependencies. The root
	@# deployment runner captures this output in its per-run log.
	@cd $(FRONTEND_DIR) && npm ci --legacy-peer-deps && npm run build

# Generate frontend/.env.production from stack outputs + shared Cognito. API URL
# defaults to the PULSE stack's ApiEndpoint.
write-frontend-env: ## Write frontend/.env.production from stack outputs + Cognito
	@echo "Generating PULSE frontend env..."
	@API_URL="$(PULSE_API_URL)"; \
	if [ -z "$$API_URL" ]; then \
		API_URL=$$($(AWS) cloudformation describe-stacks --stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
			--output text 2>/dev/null); \
	fi; \
	if [ -z "$$API_URL" ] || [ "$$API_URL" = "None" ]; then \
		echo "ERROR: required PULSE ApiEndpoint was not resolved from $(STACK_NAME)."; exit 1; \
	fi; \
	if [ -z "$(COGNITO_CLIENT_ID)" ] || [ "$(COGNITO_CLIENT_ID)" = "None" ]; then \
		echo "ERROR: COGNITO_CLIENT_ID is required to build the PULSE frontend."; exit 1; \
	fi; \
	printf '%s\n' \
		"# Auto-generated by make write-frontend-env - do not edit manually" \
		"NEXT_PUBLIC_API_URL=$$API_URL" \
		"NEXT_PUBLIC_COGNITO_CLIENT_ID=$(COGNITO_CLIENT_ID)" \
		"NEXT_PUBLIC_COGNITO_REGION=$(COGNITO_REGION)" \
		"NEXT_PUBLIC_AWS_REGION=$(REGION)" \
		> $(FRONTEND_DIR)/.env.production
	@echo "Wrote $(FRONTEND_DIR)/.env.production"

# Publish the PULSE static export to the /pulse/ prefix of the SHARED LUMI
# frontend bucket, then invalidate /pulse/* on the shared distribution (no LUMI
# infra change; next.config.js basePath=/pulse). Requires LUMI to be deployed.
deploy-frontend: write-frontend-env build-frontend ## Publish PULSE PWA to /pulse on the shared LUMI CloudFront
	@echo "Deploying PULSE frontend to s3://$(LUMI_FRONTEND_BUCKET)/pulse/ ..."
	@if ! $(AWS) s3api head-bucket --bucket $(LUMI_FRONTEND_BUCKET) 2>/dev/null; then \
		echo "ERROR: shared LUMI frontend bucket $(LUMI_FRONTEND_BUCKET) not found."; \
		echo "Deploy LUMI first (make -C ../lumi deploy) or set LUMI_FRONTEND_BUCKET."; \
		exit 1; \
	fi
	@$(AWS) s3 sync $(FRONTEND_DIR)/out/ s3://$(LUMI_FRONTEND_BUCKET)/pulse/ --delete --quiet
	@echo "Invalidating /pulse/* on the shared LUMI distribution..."
	@DIST_ID=$$($(AWS) cloudformation describe-stacks --stack-name $(LUMI_STACK) \
		--query "Stacks[0].Outputs[?OutputKey=='FrontendDistributionId'].OutputValue" \
		--output text 2>/dev/null) && \
	if [ -n "$$DIST_ID" ] && [ "$$DIST_ID" != "None" ]; then \
		$(AWS) cloudfront create-invalidation --distribution-id "$$DIST_ID" \
			--paths "/pulse/*" > /dev/null && \
		echo "Invalidated /pulse/* on $$DIST_ID"; \
	else \
		echo "WARNING: could not resolve LUMI FrontendDistributionId; skipped invalidation."; \
	fi
	@FRONTEND_URL=$$($(AWS) cloudformation describe-stacks --stack-name $(LUMI_STACK) \
		--query "Stacks[0].Outputs[?OutputKey=='FrontendUrl'].OutputValue" \
		--output text 2>/dev/null); \
	if [ -z "$$FRONTEND_URL" ] || [ "$$FRONTEND_URL" = "None" ]; then \
		echo "ERROR: PULSE was published, but LUMI FrontendUrl could not be resolved."; exit 1; \
	fi; \
	echo "PULSE frontend live at $${FRONTEND_URL%/}/pulse/"

# ─── IaC validation ───────────────────────────────────────────
validate: ## cfn-lint the root + nested CloudFormation templates
	@echo "Validating CloudFormation templates..."
	@cd $(INFRA_DIR) && cfn-lint root-stack.yaml nested-stacks/*.yaml
	@echo "Templates valid"

# ─── Packaging ────────────────────────────────────────────────
# Create the S3 deploy bucket if needed (safe to re-run).
ensure-bucket: ## Create the S3 deploy bucket if it does not exist
	@$(AWS) s3api head-bucket --bucket $(DEPLOY_BUCKET) 2>/dev/null \
		|| $(AWS) s3 mb s3://$(DEPLOY_BUCKET) > /dev/null
	@echo "Deploy bucket ready: s3://$(DEPLOY_BUCKET)"

# Build the single shared Lambda zip (pulse-backend.zip) on CodeBuild and upload
# it to the key the nested stacks reference. The [ops-read] extra pulls in the
# MCP client stack used only by the pulse-ops-read facade (lazy-imported).
package-backend: ensure-bucket ## Build the shared Lambda zip on CodeBuild (x86_64 Linux) + upload
	@echo "Packaging backend Lambda deployment zip (via CodeBuild, x86_64 Linux)..."
	@# Build the zip on CodeBuild rather than locally so native wheels (e.g.
	@# pydantic_core, pulled by the [ops-read] MCP/strands extra) match the Lambda
	@# runtime (x86_64 / python3.12). Building on a non-Linux host packages host
	@# wheels that fail at runtime with
	@# "No module named 'pydantic_core._pydantic_core'" (BUG-017). No Docker/Finch.
	@echo "  [backend] Staging backend source (pyproject + src + README)..."
	@mkdir -p $(BUILD_DIR)/backend-src
	@find $(BUILD_DIR)/backend-src -mindepth 1 -delete
	@find $(BUILD_DIR) -maxdepth 1 -name backend-source.zip -delete
	@cp -R $(BACKEND_DIR)/pyproject.toml $(BACKEND_DIR)/src $(BUILD_DIR)/backend-src/
	@cp $(BACKEND_DIR)/README.md $(BUILD_DIR)/backend-src/ 2>/dev/null || true
	@cd $(BUILD_DIR)/backend-src && zip -r ../backend-source.zip . \
		-x "*.pyc" "__pycache__/*" "*.egg-info/*" > /dev/null
	@echo "  [backend] Uploading source to s3://$(DEPLOY_BUCKET)/$(BACKEND_SOURCE_KEY)..."
	@$(AWS) s3 cp $(BUILD_DIR)/backend-source.zip s3://$(DEPLOY_BUCKET)/$(BACKEND_SOURCE_KEY) --quiet
	@echo "  [backend] Starting CodeBuild project $(BACKEND_BUILD_PROJECT)..."
	@BUILD_ID=$$($(AWS) codebuild start-build --project-name $(BACKEND_BUILD_PROJECT) \
		--query 'build.id' --output text) && \
	echo "  [backend] Build started: $$BUILD_ID" && \
	while true; do \
		STATUS=$$($(AWS) codebuild batch-get-builds --ids $$BUILD_ID \
			--query 'builds[0].buildStatus' --output text); \
		if [ "$$STATUS" = "SUCCEEDED" ]; then \
			echo "  [backend] Build succeeded: s3://$(DEPLOY_BUCKET)/$(LAMBDA_CODE_S3_KEY)"; break; \
		elif [ "$$STATUS" = "IN_PROGRESS" ]; then sleep 10; \
		else \
			LOG_URL=$$($(AWS) codebuild batch-get-builds --ids $$BUILD_ID \
				--query 'builds[0].logs.deepLink' --output text); \
			echo "  [backend] CodeBuild failed ($$STATUS). Logs: $$LOG_URL"; exit 1; \
		fi; \
	done

# Force a Lambda code refresh on all PULSE functions from the freshly-built zip.
# CloudFormation references a STATIC S3 key so it cannot detect content changes;
# this pushes the latest code to every function. Safe to run repeatedly.
update-lambda-code: ## Refresh all PULSE Lambda code from the shared zip
	@echo "Refreshing PULSE Lambda function code from s3://$(DEPLOY_BUCKET)/$(LAMBDA_CODE_S3_KEY)..."
	@set -e; \
	log="$(BUILD_DIR)/lambda-update.log"; \
	: > "$$log"; \
	for FN in $(STACK_PREFIX)-api $(STACK_PREFIX)-rule-evaluator $(STACK_PREFIX)-triage-invoker \
	           $(STACK_PREFIX)-alert-timeout-sweeper \
	           $(STACK_PREFIX)-escalation-service \
	           $(STACK_PREFIX)-action-executor $(STACK_PREFIX)-history-writer $(STACK_PREFIX)-info-batcher \
	           $(STACK_PREFIX)-push-service $(STACK_PREFIX)-ops-read $(STACK_PREFIX)-demo-simulator \
	           $(STACK_PREFIX)-forecaster-invoker \
	           $(STACK_PREFIX)-kitchen-seed $(STACK_PREFIX)-rules-seed; do \
		if [ "$$FN" = "$(STACK_PREFIX)-demo-simulator" ] && [ "$(ENABLE_DEMO_SIMULATOR)" != "true" ]; then \
			echo "  WARNING: optional $$FN is disabled; skipping code update"; \
			continue; \
		fi; \
		echo "  updating $$FN"; \
		if ! $(AWS) lambda update-function-code \
			--function-name "$$FN" \
			--s3-bucket $(DEPLOY_BUCKET) \
			--s3-key $(LAMBDA_CODE_S3_KEY) >> "$$log" 2>&1; then \
			echo "ERROR: required Lambda update failed for $$FN."; \
			echo "Last 40 log lines ($$log):"; tail -n 40 "$$log"; \
			echo "Retry: make update-lambda-code PROFILE=$(PROFILE) REGION=$(REGION) STACK_PREFIX=$(STACK_PREFIX)"; \
			exit 1; \
		fi; \
		if ! $(AWS) lambda wait function-updated-v2 \
			--function-name "$$FN" >> "$$log" 2>&1; then \
			echo "ERROR: Lambda $$FN did not become ready after its code update."; \
			echo "Last 40 log lines ($$log):"; tail -n 40 "$$log"; \
			exit 1; \
		fi; \
	done
	@echo "Lambda code refresh complete."

# `aws cloudformation package` uploads nested templates to S3 and rewrites the
# root template's TemplateURLs into build/packaged-root-stack.yaml.
package-infra: ensure-bucket ## Package nested templates for deploy
	@echo "Packaging CloudFormation nested templates..."
	@mkdir -p $(BUILD_DIR)
	@$(CFN_AWS) cloudformation package \
		--template-file $(INFRA_DIR)/root-stack.yaml \
		--s3-bucket $(DEPLOY_BUCKET) \
		--s3-prefix cfn-templates \
		--output-template-file $(PACKAGED_TEMPLATE) > /dev/null
	@echo "Wrote $(PACKAGED_TEMPLATE)"

# Full packaging: nested templates + ensure a Lambda-code object exists. The
# REAL backend zip is built on CodeBuild AFTER the stack exists (deploy ->
# package-backend -> update-lambda-code), since that CodeBuild project is itself
# created by the stack.
package: ensure-code-placeholder package-infra ## Package templates (+ ensure Lambda-code object)
	@echo "Packaging complete"

# Ensure a Lambda-code object exists at the deploy key so first-deploy CFN can
# create the Lambdas (CFN requires the S3 object to exist). Leaves a real build
# in place; otherwise uploads a tiny placeholder that package-backend later
# overwrites.
ensure-code-placeholder: ensure-bucket
	@if $(AWS) s3api head-object --bucket $(DEPLOY_BUCKET) --key $(LAMBDA_CODE_S3_KEY) >/dev/null 2>&1; then \
		echo "Lambda-code object present: s3://$(DEPLOY_BUCKET)/$(LAMBDA_CODE_S3_KEY)"; \
	else \
		echo "Uploading placeholder Lambda-code object (first deploy)..."; \
		mkdir -p $(BUILD_DIR)/placeholder/pulse/seed && \
		: > $(BUILD_DIR)/placeholder/pulse/__init__.py && \
		: > $(BUILD_DIR)/placeholder/pulse/seed/__init__.py && \
		printf '%s\n' \
			'"""First-deploy placeholder for the shared PULSE Lambda package."""' \
			'import json, urllib.request' \
			'' \
			'' \
			'def _cfn_ok(event, context):' \
			'    """Respond SUCCESS to a CloudFormation custom resource (no-op).' \
			'' \
			'    The real code is built on CodeBuild AFTER the stack exists, then a' \
			'    second deploy with bumped seed Triggers runs the real seeds. This' \
			'    placeholder must NOT raise at import or invoke, or the seed custom' \
			'    resources would hang the first stack create (BUG: No module pulse).' \
			'    """' \
			'    if "ResponseURL" not in event:' \
			'        return' \
			'    body = json.dumps({' \
			'        "Status": "SUCCESS",' \
			'        "PhysicalResourceId": event.get("PhysicalResourceId")' \
			'            or (context.log_stream_name if context else "placeholder"),' \
			'        "StackId": event.get("StackId"),' \
			'        "RequestId": event.get("RequestId"),' \
			'        "LogicalResourceId": event.get("LogicalResourceId"),' \
			'        "Data": {"placeholder": True},' \
			'    }).encode("utf-8")' \
			'    req = urllib.request.Request(' \
			'        url=event["ResponseURL"], data=body, method="PUT",' \
			'        headers={"content-type": "", "content-length": str(len(body))})' \
			'    urllib.request.urlopen(req)  # noqa: S310 (fixed CFN URL)' \
			'' \
			'' \
			'def lambda_handler(event, context):' \
			'    """Placeholder handler: succeed as a CFN no-op; error on direct invoke."""' \
			'    if isinstance(event, dict) and "RequestType" in event:' \
			'        _cfn_ok(event, context)' \
			'        return' \
			'    raise RuntimeError("placeholder - run package-backend to build real code")' \
			> $(BUILD_DIR)/placeholder/pulse/seed/kitchen_seed.py && \
		cp $(BUILD_DIR)/placeholder/pulse/seed/kitchen_seed.py \
			$(BUILD_DIR)/placeholder/pulse/seed/rules_seed.py && \
		printf 'def lambda_handler(event, context):\n    raise RuntimeError("placeholder - run package-backend")\n' \
			> $(BUILD_DIR)/placeholder/lambda_function.py && \
		(cd $(BUILD_DIR)/placeholder && zip -q -r ../placeholder.zip lambda_function.py pulse) && \
		$(AWS) s3 cp $(BUILD_DIR)/placeholder.zip s3://$(DEPLOY_BUCKET)/$(LAMBDA_CODE_S3_KEY) --quiet; \
	fi

# ─── Deploy (manual step) ─────────────────────────────────────
assert-account:
	@if [ -z "$(ACCOUNT_ID)" ]; then \
		echo "ERROR: unable to resolve the active AWS account."; exit 1; \
	fi
	@if [ -n "$(EXPECTED_ACCOUNT_ID)" ] && [ "$(ACCOUNT_ID)" != "$(EXPECTED_ACCOUNT_ID)" ]; then \
		echo "ERROR: AWS account mismatch (expected $(EXPECTED_ACCOUNT_ID), resolved $(ACCOUNT_ID))."; \
		exit 1; \
	fi
	@if [ -z "$(CFN_ACCOUNT_ID)" ] || [ "$(CFN_ACCOUNT_ID)" != "$(ACCOUNT_ID)" ]; then \
		echo "ERROR: CloudFormation profile account mismatch (target $(ACCOUNT_ID), resolved $(CFN_ACCOUNT_ID))."; \
		exit 1; \
	fi

gateway-deploy triage-deploy forecast-deploy deploy-frontend destroy: assert-account

# Guard the parameters that have no template default; fail fast with a clear
# message if any are missing (see pulse/README.md#deploy).
_require-deploy-vars:
	@missing=""; \
	for pair in \
		"USER_POOL_ID=$(USER_POOL_ID)" \
		"USER_POOL_ARN=$(USER_POOL_ARN)" \
		"RESERVATIONS_STREAM_ARN=$(RESERVATIONS_STREAM_ARN)" \
		"ROOMS_STREAM_ARN=$(ROOMS_STREAM_ARN)" \
		"GUESTS_STREAM_ARN=$(GUESTS_STREAM_ARN)" \
		"REVENUES_STREAM_ARN=$(REVENUES_STREAM_ARN)" \
		"WORK_ORDERS_STREAM_ARN=$(WORK_ORDERS_STREAM_ARN)" \
		"RESERVATIONS_TABLE_ARN=$(RESERVATIONS_TABLE_ARN)" \
		"ROOMS_TABLE_ARN=$(ROOMS_TABLE_ARN)" \
		"GUESTS_TABLE_ARN=$(GUESTS_TABLE_ARN)" \
		"REVENUES_TABLE_ARN=$(REVENUES_TABLE_ARN)" \
		"WORK_ORDERS_TABLE_ARN=$(WORK_ORDERS_TABLE_ARN)"; do \
		val="$${pair#*=}"; \
		if [ -z "$$val" ]; then missing="$$missing $${pair%%=*}"; fi; \
	done; \
	if [ -n "$$missing" ]; then \
		echo ""; \
		echo "ERROR: missing required deploy parameters:$$missing"; \
		echo ""; \
		echo "These come from the LUMI platform (Cognito user pool + the five"; \
		echo "operational-table DynamoDB Stream ARNs). See pulse/README.md#deploy."; \
		echo ""; \
		echo "Example:"; \
		echo "  make deploy \\"; \
		echo "    USER_POOL_ID=us-east-1_xxxx USER_POOL_ARN=arn:aws:cognito-idp:... \\"; \
		echo "    RESERVATIONS_STREAM_ARN=arn:... ROOMS_STREAM_ARN=arn:... \\"; \
		echo "    GUESTS_STREAM_ARN=arn:... REVENUES_STREAM_ARN=arn:... \\"; \
		echo "    WORK_ORDERS_STREAM_ARN=arn:..."; \
		echo ""; \
		exit 1; \
	fi

# Deploy the packaged root stack. CloudFormation provisions the nested stacks in
# dependency order: pulse-data -> pulse-pipeline -> pulse-api -> pulse-observability.
deploy-stack: assert-account _require-deploy-vars package
	@echo "Deploying $(STACK_NAME) (this may take several minutes)..."
	@$(CFN_AWS) cloudformation deploy \
		--stack-name $(STACK_NAME) \
		--template-file $(PACKAGED_TEMPLATE) \
		--parameter-overrides \
			StackPrefix=$(STACK_PREFIX) \
			DeployBucket=$(DEPLOY_BUCKET) \
			TriageModelId=$(TRIAGE_MODEL_ID) \
			TriageRuntimeArn=$(TRIAGE_RUNTIME_ARN) \
			ForecastModelId=$(FORECAST_MODEL_ID) \
			ForecastRuntimeArn=$(FORECAST_RUNTIME_ARN) \
			GatewayEndpointUrl=$(GATEWAY_ENDPOINT_URL) \
			EnableDemoSimulator=$(ENABLE_DEMO_SIMULATOR) \
			UserPoolId=$(USER_POOL_ID) \
			UserPoolArn=$(USER_POOL_ARN) \
			UserPoolClientId=$(USER_POOL_CLIENT_ID) \
			VapidPublicKey=$(VAPID_PUBLIC_KEY) \
			WafWebAclArn=$(WAF_WEB_ACL_ARN) \
			ReservationsStreamArn=$(RESERVATIONS_STREAM_ARN) \
			RoomsStreamArn=$(ROOMS_STREAM_ARN) \
			GuestsStreamArn=$(GUESTS_STREAM_ARN) \
			RevenuesStreamArn=$(REVENUES_STREAM_ARN) \
			ReservationsTableArn=$(RESERVATIONS_TABLE_ARN) \
			RoomsTableArn=$(ROOMS_TABLE_ARN) \
			GuestsTableArn=$(GUESTS_TABLE_ARN) \
			RevenuesTableArn=$(REVENUES_TABLE_ARN) \
			WorkOrdersTableArn=$(WORK_ORDERS_TABLE_ARN) \
			ReservationsTableName=$(RESERVATIONS_TABLE_NAME) \
			RoomsTableName=$(ROOMS_TABLE_NAME) \
			GuestsTableName=$(GUESTS_TABLE_NAME) \
			WorkOrdersStreamArn=$(WORK_ORDERS_STREAM_ARN) \
			--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
			--no-fail-on-empty-changeset

# Initial deployment owns all heavyweight backend work exactly once:
# CloudFormation, CodeBuild package, Lambda refresh, and idempotent seeds.
deploy-initial: deploy-stack
	@echo "Building the shared Lambda zip and refreshing function code..."
	@$(MAKE) package-backend STACK_PREFIX=$(STACK_PREFIX) REGION=$(REGION) \
		PROFILE=$(PROFILE) ORCHESTRATED=$(ORCHESTRATED)
	@$(MAKE) update-lambda-code STACK_PREFIX=$(STACK_PREFIX) REGION=$(REGION) \
		PROFILE=$(PROFILE) ORCHESTRATED=$(ORCHESTRATED)
	@echo "Running PULSE data seeds against the deployed package..."
	@$(MAKE) seed-run STACK_PREFIX=$(STACK_PREFIX) REGION=$(REGION) \
		PROFILE=$(PROFILE) ORCHESTRATED=$(ORCHESTRATED)
	@if [ "$(ORCHESTRATED)" != "1" ]; then \
		echo ""; \
		echo "PULSE initial deployment complete: $(STACK_NAME)"; \
		echo "Next: register Gateway tools and deploy the Triage and Forecast runtimes."; \
	fi

# Standalone compatibility: `make deploy` retains the original full initial
# deployment behavior.
deploy: deploy-initial ## Deploy PULSE infrastructure, backend package, code, and seeds

# Final runtime wiring is intentionally CloudFormation-only. It must never
# rebuild the backend package, refresh Lambdas, or invoke seeds.
deploy-runtime-wiring: assert-account _require-deploy-vars package-infra
	@if [ -z "$(TRIAGE_RUNTIME_ARN)" ] || [ -z "$(FORECAST_RUNTIME_ARN)" ]; then \
		echo "ERROR: TRIAGE_RUNTIME_ARN and FORECAST_RUNTIME_ARN are required."; \
		exit 1; \
	fi
	@echo "Updating $(STACK_NAME) runtime wiring..."
	@$(CFN_AWS) cloudformation deploy \
		--stack-name $(STACK_NAME) \
		--template-file $(PACKAGED_TEMPLATE) \
		--parameter-overrides \
			StackPrefix=$(STACK_PREFIX) \
			DeployBucket=$(DEPLOY_BUCKET) \
			TriageModelId=$(TRIAGE_MODEL_ID) \
			TriageRuntimeArn=$(TRIAGE_RUNTIME_ARN) \
			ForecastModelId=$(FORECAST_MODEL_ID) \
			ForecastRuntimeArn=$(FORECAST_RUNTIME_ARN) \
			GatewayEndpointUrl=$(GATEWAY_ENDPOINT_URL) \
			EnableDemoSimulator=$(ENABLE_DEMO_SIMULATOR) \
			UserPoolId=$(USER_POOL_ID) \
			UserPoolArn=$(USER_POOL_ARN) \
			UserPoolClientId=$(USER_POOL_CLIENT_ID) \
			VapidPublicKey=$(VAPID_PUBLIC_KEY) \
			WafWebAclArn=$(WAF_WEB_ACL_ARN) \
			ReservationsStreamArn=$(RESERVATIONS_STREAM_ARN) \
			RoomsStreamArn=$(ROOMS_STREAM_ARN) \
			GuestsStreamArn=$(GUESTS_STREAM_ARN) \
			RevenuesStreamArn=$(REVENUES_STREAM_ARN) \
			ReservationsTableArn=$(RESERVATIONS_TABLE_ARN) \
			RoomsTableArn=$(ROOMS_TABLE_ARN) \
			GuestsTableArn=$(GUESTS_TABLE_ARN) \
			RevenuesTableArn=$(REVENUES_TABLE_ARN) \
			WorkOrdersTableArn=$(WORK_ORDERS_TABLE_ARN) \
			ReservationsTableName=$(RESERVATIONS_TABLE_NAME) \
			RoomsTableName=$(ROOMS_TABLE_NAME) \
			GuestsTableName=$(GUESTS_TABLE_NAME) \
			WorkOrdersStreamArn=$(WORK_ORDERS_STREAM_ARN) \
		--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
		--no-fail-on-empty-changeset
	@if [ "$(ORCHESTRATED)" != "1" ]; then \
		echo "PULSE runtime wiring update complete: $(STACK_NAME)"; \
	fi

# ─── Shared Gateway tool registration (out-of-band) ───────────
# Registers/updates PULSE tools on the SHARED StayOS AgentCore Gateway target.
# Gateway has no CloudFormation resource type, so this runs out-of-band via the
# AWS CLI; PULSE additions are additive to the shared target.
#
# Required (deploy-time inputs, see pulse/README.md#deploy prerequisite 3):
#   TOOL_LAMBDA_ARN     - ARN of the shared Tool Lambda the target invokes
#   GATEWAY_TOOL_SCHEMA - path to the tool schema JSON (inline payload)
# The shared Gateway id is read from SSM ($(GATEWAY_ID_SSM)).
gateway-deploy: ## Register PULSE tools on the shared StayOS Gateway target
	@if [ -z "$(TOOL_LAMBDA_ARN)" ]; then \
		echo "ERROR: TOOL_LAMBDA_ARN is required (shared Tool Lambda ARN)."; \
		echo "See pulse/README.md#deploy prerequisite 3."; \
		exit 1; \
	fi
	@echo "  [gateway] Reading shared Gateway id from SSM $(GATEWAY_ID_SSM)..."
	@GATEWAY_ID=$$($(AWS) ssm get-parameter --name "$(GATEWAY_ID_SSM)" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	if [ -z "$$GATEWAY_ID" ] || [ "$$GATEWAY_ID" = "None" ]; then \
		echo "  ERROR: no shared Gateway id in SSM $(GATEWAY_ID_SSM)."; \
		echo "  Deploy the shared StayOS Gateway (LUMI gateway-deploy) first."; \
		exit 1; \
	fi && \
	echo "  [gateway] Building target config from $(GATEWAY_TOOL_SCHEMA)..." && \
	TARGET_CONFIG=$$(python3 -c "import json, sys; tools = json.load(open(sys.argv[1]))['tools']; print(json.dumps({'mcp': {'lambda': {'lambdaArn': sys.argv[2], 'toolSchema': {'inlinePayload': tools}}}}))" "$(GATEWAY_TOOL_SCHEMA)" "$(TOOL_LAMBDA_ARN)") && \
	TARGET_ID=$$($(AWS) bedrock-agentcore-control list-gateway-targets \
		--gateway-identifier "$$GATEWAY_ID" \
		--query "items[?name=='$(GATEWAY_TARGET_NAME)'].targetId | [0]" --output text) && \
	if [ "$$TARGET_ID" != "None" ] && [ -n "$$TARGET_ID" ]; then \
		echo "  [gateway] Updating shared target $$TARGET_ID with PULSE tools..." && \
		$(AWS) bedrock-agentcore-control update-gateway-target \
			--gateway-identifier "$$GATEWAY_ID" \
			--target-id "$$TARGET_ID" \
			--name "$(GATEWAY_TARGET_NAME)" \
			--target-configuration "$$TARGET_CONFIG" \
			--credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' > /dev/null && \
		echo "  [gateway] Shared target updated"; \
	else \
		echo "  ERROR: shared target '$(GATEWAY_TARGET_NAME)' not found on Gateway $$GATEWAY_ID."; \
		echo "  The shared target is created by LUMI's gateway-deploy."; \
		exit 1; \
	fi

# ─── Triage Agent container build (CodeBuild, ARM64) ──────────
# Stages backend/src/pulse into the build context, zips the triage-agent source,
# uploads to S3, and triggers the SHARED CodeBuild project to build+push the
# ARM64 image to ECR. Local equivalent uses AWS Finch:
#   finch build --platform linux/arm64 -t $(STACK_PREFIX)-triage-agent \
#     -f Containerfile .
triage-build: ## Build the Triage Agent ARM64 container via the SHARED CodeBuild project
	@echo "  [triage] Staging build context (triage-agent + pulse package)..."
	@mkdir -p $(BUILD_DIR)/triage-context
	@find $(BUILD_DIR)/triage-context -mindepth 1 -delete
	@find $(BUILD_DIR) -maxdepth 1 -name triage-agent-source.zip -delete
	@cp -R $(BACKEND_DIR)/services/triage-agent/. $(BUILD_DIR)/triage-context/
	@cp -R $(BACKEND_DIR)/src/pulse $(BUILD_DIR)/triage-context/pulse
	@echo "  [triage] Zipping source..."
	@cd $(BUILD_DIR)/triage-context && zip -r ../triage-agent-source.zip . \
		-x "tests/*" "__pycache__/*" "*.pyc" "agentcore.yaml" ".pytest_cache/*" > /dev/null
	@echo "  [triage] Uploading source to the SHARED deploy bucket ($(SHARED_DEPLOY_BUCKET))..."
	@$(AWS) s3 cp $(BUILD_DIR)/triage-agent-source.zip \
		s3://$(SHARED_DEPLOY_BUCKET)/$(TRIAGE_SOURCE_KEY) --quiet
	@echo "  [triage] Starting SHARED CodeBuild project $(SHARED_BUILD_PROJECT) (source+env+buildspec overrides)..."
	@echo "  [triage] Reusing ECR repo $(SHARED_AGENT_ECR_REPO); pushing tag '$(TRIAGE_IMAGE_TAG)' (no new resources)..."
	@BUILDSPEC=$$(printf '%b' 'version: 0.2\nphases:\n  pre_build:\n    commands:\n      - aws ecr get-login-password --region $$AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $$AWS_ACCOUNT_ID.dkr.ecr.$$AWS_DEFAULT_REGION.amazonaws.com\n  build:\n    commands:\n      - docker build --platform linux/arm64 -t $$ECR_REPO_URI:$$IMAGE_TAG -f Containerfile .\n  post_build:\n    commands:\n      - docker push $$ECR_REPO_URI:$$IMAGE_TAG\n      - echo "Pushed $$ECR_REPO_URI:$$IMAGE_TAG"\n'); \
	BUILD_ID=$$($(AWS) codebuild start-build --project-name $(SHARED_BUILD_PROJECT) \
		--source-location-override "$(SHARED_DEPLOY_BUCKET)/$(TRIAGE_SOURCE_KEY)" \
		--buildspec-override "$$BUILDSPEC" \
		--environment-variables-override \
			"name=ECR_REPO_URI,value=$(TRIAGE_ECR_URI),type=PLAINTEXT" \
			"name=IMAGE_TAG,value=$(TRIAGE_IMAGE_TAG),type=PLAINTEXT" \
			"name=AWS_ACCOUNT_ID,value=$(ACCOUNT_ID),type=PLAINTEXT" \
		--query 'build.id' --output text) && \
	echo "  [triage] Build started: $$BUILD_ID" && \
	while true; do \
		STATUS=$$($(AWS) codebuild batch-get-builds --ids $$BUILD_ID \
			--query 'builds[0].buildStatus' --output text); \
		if [ "$$STATUS" = "SUCCEEDED" ]; then \
			echo "  [triage] Build succeeded: $(TRIAGE_ECR_URI):$(TRIAGE_IMAGE_TAG)"; break; \
		elif [ "$$STATUS" = "IN_PROGRESS" ]; then sleep 10; \
		else \
			LOG_URL=$$($(AWS) codebuild batch-get-builds --ids $$BUILD_ID \
				--query 'builds[0].logs.deepLink' --output text); \
			echo "  CodeBuild failed ($$STATUS). Logs: $$LOG_URL"; exit 1; \
		fi; \
	done

# Create or update the Triage Agent AgentCore Runtime and write its ARN to SSM
# $(TRIAGE_RUNTIME_SSM) (consumed by the Rule Engine to InvokeAgentRuntime).
# Env vars come from SSM + stack outputs (never hardcoded).
triage-deploy: triage-build ## Build + deploy the Triage Agent to AgentCore Runtime
	@echo "  [triage] Resolving runtime configuration..."
	@ROLE_ARN=$$($(AWS) iam get-role --role-name $(TRIAGE_ROLE_NAME) \
		--query 'Role.Arn' --output text) && \
	GATEWAY_ENDPOINT_URL=$$($(AWS) ssm get-parameter --name "$(GATEWAY_ENDPOINT_SSM)" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	ALERTS_TABLE_NAME=$$($(AWS) ssm get-parameter \
		--name "/$(STACK_PREFIX)/triage/alerts-table-name" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	REALTIME_HTTP_ENDPOINT=$$($(AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='RealtimeHttpEndpoint'].OutputValue" \
		--output text 2>/dev/null) && \
	ENV_VARS="{\"GATEWAY_ENDPOINT_URL\":\"$$GATEWAY_ENDPOINT_URL\",\"TRIAGE_MODEL_ID\":\"$(TRIAGE_MODEL_ID)\",\"ALERTS_TABLE_NAME\":\"$$ALERTS_TABLE_NAME\",\"REALTIME_HTTP_ENDPOINT\":\"$$REALTIME_HTTP_ENDPOINT\",\"AWS_DEFAULT_REGION\":\"$(REGION)\",\"LOG_LEVEL\":\"INFO\"}" && \
	if RUNTIME_ID=$$($(AWS) ssm get-parameter --name "/$(STACK_PREFIX)/triage/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	   [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [triage] Updating existing runtime: $$RUNTIME_ID" && \
		$(AWS) bedrock-agentcore-control update-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(TRIAGE_ECR_URI):$(TRIAGE_IMAGE_TAG)\"}}" \
			--environment-variables "$$ENV_VARS" > /dev/null ; \
	else \
		echo "  [triage] Creating new AgentCore Runtime..." && \
		RUNTIME_ID=$$($(AWS) bedrock-agentcore-control create-agent-runtime \
			--agent-runtime-name "$(TRIAGE_RUNTIME_NAME)" \
			--description "PULSE Triage Agent - async alert triage via shared StayOS Gateway" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(TRIAGE_ECR_URI):$(TRIAGE_IMAGE_TAG)\"}}" \
			--environment-variables "$$ENV_VARS" \
			--query 'agentRuntimeId' --output text) && \
		$(AWS) ssm put-parameter --name "/$(STACK_PREFIX)/triage/runtime-id" \
			--value "$$RUNTIME_ID" --type String --overwrite > /dev/null && \
		echo "  [triage] Runtime created: $$RUNTIME_ID" ; \
	fi && \
	RUNTIME_ID=$$($(AWS) ssm get-parameter --name "/$(STACK_PREFIX)/triage/runtime-id" \
		--query "Parameter.Value" --output text) && \
	RUNTIME_ARN="arn:aws:bedrock-agentcore:$(REGION):$(ACCOUNT_ID):runtime/$$RUNTIME_ID" && \
	$(AWS) ssm put-parameter --name "$(TRIAGE_RUNTIME_SSM)" \
		--value "$$RUNTIME_ARN" --type String --overwrite > /dev/null && \
	echo "  [triage] Runtime ARN stored in SSM $(TRIAGE_RUNTIME_SSM):" && \
	echo "           $$RUNTIME_ARN" && \
	if [ "$(ORCHESTRATED)" != "1" ]; then \
		echo "  [triage] Thread it into the stack as TriageRuntimeArn on the next deploy."; \
	fi

# ─── Forecasting Agent container build (CodeBuild, ARM64) ─────
# Mirrors triage-build: stages backend/src/pulse, zips forecast-agent source,
# uploads to the SHARED deploy bucket, and triggers the SHARED CodeBuild project
# to build+push an ARM64 image under the distinct '$(FORECAST_IMAGE_TAG)' tag.
# Local equivalent uses AWS Finch:
#   finch build --platform linux/arm64 -t $(STACK_PREFIX)-forecast-agent \
#     -f Containerfile .
forecast-build: ## Build the Forecasting Agent ARM64 container via the SHARED CodeBuild project
	@echo "  [forecast] Staging build context (forecast-agent + pulse package)..."
	@mkdir -p $(BUILD_DIR)/forecast-context
	@find $(BUILD_DIR)/forecast-context -mindepth 1 -delete
	@find $(BUILD_DIR) -maxdepth 1 -name forecast-agent-source.zip -delete
	@cp -R $(BACKEND_DIR)/services/forecast-agent/. $(BUILD_DIR)/forecast-context/
	@cp -R $(BACKEND_DIR)/src/pulse $(BUILD_DIR)/forecast-context/pulse
	@echo "  [forecast] Zipping source..."
	@cd $(BUILD_DIR)/forecast-context && zip -r ../forecast-agent-source.zip . \
		-x "tests/*" "__pycache__/*" "*.pyc" "agentcore.yaml" ".pytest_cache/*" > /dev/null
	@echo "  [forecast] Uploading source to the SHARED deploy bucket ($(SHARED_DEPLOY_BUCKET))..."
	@$(AWS) s3 cp $(BUILD_DIR)/forecast-agent-source.zip \
		s3://$(SHARED_DEPLOY_BUCKET)/$(FORECAST_SOURCE_KEY) --quiet
	@echo "  [forecast] Starting SHARED CodeBuild project $(SHARED_BUILD_PROJECT) (source+env+buildspec overrides)..."
	@echo "  [forecast] Reusing ECR repo $(SHARED_AGENT_ECR_REPO); pushing tag '$(FORECAST_IMAGE_TAG)' (no new resources)..."
	@BUILDSPEC=$$(printf '%b' 'version: 0.2\nphases:\n  pre_build:\n    commands:\n      - aws ecr get-login-password --region $$AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $$AWS_ACCOUNT_ID.dkr.ecr.$$AWS_DEFAULT_REGION.amazonaws.com\n  build:\n    commands:\n      - docker build --platform linux/arm64 -t $$ECR_REPO_URI:$$IMAGE_TAG -f Containerfile .\n  post_build:\n    commands:\n      - docker push $$ECR_REPO_URI:$$IMAGE_TAG\n      - echo "Pushed $$ECR_REPO_URI:$$IMAGE_TAG"\n'); \
	BUILD_ID=$$($(AWS) codebuild start-build --project-name $(SHARED_BUILD_PROJECT) \
		--source-location-override "$(SHARED_DEPLOY_BUCKET)/$(FORECAST_SOURCE_KEY)" \
		--buildspec-override "$$BUILDSPEC" \
		--environment-variables-override \
			"name=ECR_REPO_URI,value=$(FORECAST_ECR_URI),type=PLAINTEXT" \
			"name=IMAGE_TAG,value=$(FORECAST_IMAGE_TAG),type=PLAINTEXT" \
			"name=AWS_ACCOUNT_ID,value=$(ACCOUNT_ID),type=PLAINTEXT" \
		--query 'build.id' --output text) && \
	echo "  [forecast] Build started: $$BUILD_ID" && \
	while true; do \
		STATUS=$$($(AWS) codebuild batch-get-builds --ids $$BUILD_ID \
			--query 'builds[0].buildStatus' --output text); \
		if [ "$$STATUS" = "SUCCEEDED" ]; then \
			echo "  [forecast] Build succeeded: $(FORECAST_ECR_URI):$(FORECAST_IMAGE_TAG)"; break; \
		elif [ "$$STATUS" = "IN_PROGRESS" ]; then sleep 10; \
		else \
			LOG_URL=$$($(AWS) codebuild batch-get-builds --ids $$BUILD_ID \
				--query 'builds[0].logs.deepLink' --output text); \
			echo "  CodeBuild failed ($$STATUS). Logs: $$LOG_URL"; exit 1; \
		fi; \
	done

# Create or update the Forecasting Agent AgentCore Runtime and write its ARN to
# SSM $(FORECAST_RUNTIME_SSM) (consumed by pulse-forecaster-invoker to
# InvokeAgentRuntime). Env vars come from SSM + stack outputs. Mirrors triage-deploy.
forecast-deploy: forecast-build ## Build + deploy the Forecasting Agent to AgentCore Runtime
	@echo "  [forecast] Resolving runtime configuration..."
	@ROLE_ARN=$$($(AWS) iam get-role --role-name $(FORECAST_ROLE_NAME) \
		--query 'Role.Arn' --output text) && \
	GATEWAY_ENDPOINT_URL=$$($(AWS) ssm get-parameter --name "$(GATEWAY_ENDPOINT_SSM)" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	ALERTS_TABLE_NAME=$$($(AWS) ssm get-parameter \
		--name "/$(STACK_PREFIX)/triage/alerts-table-name" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	REALTIME_HTTP_ENDPOINT=$$($(AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='RealtimeHttpEndpoint'].OutputValue" \
		--output text 2>/dev/null) && \
	ENV_VARS="{\"GATEWAY_ENDPOINT_URL\":\"$$GATEWAY_ENDPOINT_URL\",\"FORECAST_MODEL_ID\":\"$(FORECAST_MODEL_ID)\",\"ALERTS_TABLE_NAME\":\"$$ALERTS_TABLE_NAME\",\"REALTIME_HTTP_ENDPOINT\":\"$$REALTIME_HTTP_ENDPOINT\",\"FORECAST_ENABLED_PROPERTY_IDS\":\"$(FORECAST_ENABLED_PROPERTY_IDS)\",\"POWERTOOLS_SERVICE_NAME\":\"$(STACK_PREFIX)-forecaster\",\"AWS_DEFAULT_REGION\":\"$(REGION)\",\"LOG_LEVEL\":\"INFO\"}" && \
	if RUNTIME_ID=$$($(AWS) ssm get-parameter --name "$(FORECAST_RUNTIME_ID_SSM)" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	   [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [forecast] Updating existing runtime: $$RUNTIME_ID" && \
		$(AWS) bedrock-agentcore-control update-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(FORECAST_ECR_URI):$(FORECAST_IMAGE_TAG)\"}}" \
			--environment-variables "$$ENV_VARS" > /dev/null ; \
	else \
		echo "  [forecast] Creating new AgentCore Runtime..." && \
		RUNTIME_ID=$$($(AWS) bedrock-agentcore-control create-agent-runtime \
			--agent-runtime-name "$(FORECAST_RUNTIME_NAME)" \
			--description "PULSE Forecasting Agent - daily per-property oversell forecast via shared StayOS Gateway" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(FORECAST_ECR_URI):$(FORECAST_IMAGE_TAG)\"}}" \
			--environment-variables "$$ENV_VARS" \
			--query 'agentRuntimeId' --output text) && \
		$(AWS) ssm put-parameter --name "$(FORECAST_RUNTIME_ID_SSM)" \
			--value "$$RUNTIME_ID" --type String --overwrite > /dev/null && \
		echo "  [forecast] Runtime created: $$RUNTIME_ID" ; \
	fi && \
	RUNTIME_ID=$$($(AWS) ssm get-parameter --name "$(FORECAST_RUNTIME_ID_SSM)" \
		--query "Parameter.Value" --output text) && \
	RUNTIME_ARN="arn:aws:bedrock-agentcore:$(REGION):$(ACCOUNT_ID):runtime/$$RUNTIME_ID" && \
	$(AWS) ssm put-parameter --name "$(FORECAST_RUNTIME_SSM)" \
		--value "$$RUNTIME_ARN" --type String --overwrite > /dev/null && \
	echo "  [forecast] Runtime ARN stored in SSM $(FORECAST_RUNTIME_SSM):" && \
	echo "           $$RUNTIME_ARN" && \
	if [ "$(ORCHESTRATED)" != "1" ]; then \
		echo "  [forecast] Thread it into the stack as ForecastRuntimeArn on the next deploy."; \
	fi

# ─── Seed (post-deploy demo data) ─────────────────────────────
# Re-run the kitchen snapshot seed on demand. Kitchen seeding is a CFN custom
# resource that runs automatically on `make deploy`; this target only re-invokes
# the seed Lambda directly (no ResponseURL -> idempotent put, skips CFN response)
# for a manual refresh. Bump KitchenSeedTrigger on a deploy for a CFN-driven re-run.
kitchen-reseed: ## Re-invoke the kitchen seed Lambda directly (manual refresh)
	@echo "Re-invoking $(STACK_PREFIX)-kitchen-seed Lambda (direct, no ResponseURL)..."
	@$(AWS) lambda invoke \
		--function-name $(STACK_PREFIX)-kitchen-seed \
		--cli-binary-format raw-in-base64-out \
		--payload '{"RequestType":"Create","ResourceProperties":{}}' \
		/tmp/pulse-kitchen-reseed.json > /dev/null
	@echo "Kitchen seed invoked; response: $$(cat /tmp/pulse-kitchen-reseed.json)"

# Run BOTH data seeds via direct Lambda invoke (no ResponseURL -> idempotent put).
# Used by `deploy` right after update-lambda-code so the real seed code runs
# against the real package (the first stack-create seeds ran against the placeholder).
seed-run: ## Invoke kitchen + rules seed Lambdas directly against real code
	@for SEED in kitchen-seed rules-seed; do \
		FN="$(STACK_PREFIX)-$$SEED"; \
		echo "  [seed] Invoking $$FN (direct, no ResponseURL)..."; \
		$(AWS) lambda invoke \
			--function-name "$$FN" \
			--cli-binary-format raw-in-base64-out \
			--payload '{"RequestType":"Create","ResourceProperties":{}}' \
			/tmp/pulse-$$SEED-run.json > /dev/null \
			&& echo "  [seed] $$FN response: $$(cat /tmp/pulse-$$SEED-run.json)" \
			|| { echo "  [seed] ERROR: $$FN invoke failed"; exit 1; }; \
	done
	@echo "PULSE data seeds complete."

# ─── Cleanup ──────────────────────────────────────────────────
clean: ## Remove build artifacts (build/, frontend out/.next, __pycache__)
	@rm -rf $(BUILD_DIR) $(FRONTEND_DIR)/out $(FRONTEND_DIR)/.next
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean"

# ─── Triage Agent Destroy (AgentCore Runtime) ─────────────────
# Deletes the PULSE Triage Agent AgentCore Runtime (CLI-managed, not in the CFN
# stack) and removes its SSM parameters. The 'triage-latest' image in the shared
# ECR repo is purged by `destroy` below, not here.
#
# Usage:
#   make triage-destroy   # tear down the Triage Agent runtime only
triage-destroy: ## Tear down the Triage Agent AgentCore Runtime only
	@RUNTIME_ID=$$($(AWS) ssm get-parameter --name "/$(STACK_PREFIX)/triage/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null); \
	if [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [triage] Deleting AgentCore Runtime: $$RUNTIME_ID" && \
		$(AWS) bedrock-agentcore-control delete-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" > /dev/null 2>&1 || true; \
		$(AWS) ssm delete-parameter --name "/$(STACK_PREFIX)/triage/runtime-id" > /dev/null 2>&1 || true; \
		$(AWS) ssm delete-parameter --name "$(TRIAGE_RUNTIME_SSM)" > /dev/null 2>&1 || true; \
		echo "  [triage] Runtime deleted and SSM parameters removed"; \
	else \
		echo "  [triage] No runtime ID found in SSM - nothing to delete"; \
	fi

# Deletes the PULSE Forecasting Agent AgentCore Runtime (CLI-managed, not in the
# CFN stack) and removes its SSM parameters. Mirrors triage-destroy. The
# '$(FORECAST_IMAGE_TAG)' image in the shared ECR repo is purged by `destroy`, not here.
#
# Usage:
#   make forecast-destroy   # tear down the Forecasting Agent runtime only
forecast-destroy: ## Tear down the Forecasting Agent AgentCore Runtime only
	@RUNTIME_ID=$$($(AWS) ssm get-parameter --name "$(FORECAST_RUNTIME_ID_SSM)" \
		--query "Parameter.Value" --output text 2>/dev/null); \
	if [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [forecast] Deleting AgentCore Runtime: $$RUNTIME_ID" && \
		$(AWS) bedrock-agentcore-control delete-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" > /dev/null 2>&1 || true; \
		$(AWS) ssm delete-parameter --name "$(FORECAST_RUNTIME_ID_SSM)" > /dev/null 2>&1 || true; \
		$(AWS) ssm delete-parameter --name "$(FORECAST_RUNTIME_SSM)" > /dev/null 2>&1 || true; \
		echo "  [forecast] Runtime deleted and SSM parameters removed"; \
	else \
		echo "  [forecast] No runtime ID found in SSM - nothing to delete"; \
	fi

# ─── Full Teardown (single command) ───────────────────────────
# Tears down ALL PULSE infrastructure in the reverse order of `deploy`, removing
# ONLY PULSE-owned resources and leaving LUMI's shared foundation (Cognito,
# Gateway, ECR repo, CloudFront/S3) intact. See docs/deployment-pipeline.md for
# the full flow.
#
# Order: (1) Triage + Forecasting Agent Runtimes; (2) remove PULSE's /pulse/
# assets from the shared LUMI bucket; (3) delete only the 'triage-latest' and
# 'forecaster-latest' image tags from the shared ECR repo; (4) empty PULSE's own
# deploy bucket; (5) delete-stack + wait.
#
# NOTE: PULSE's Gateway tools live on LUMI's SHARED target; LUMI's gateway-destroy
# removes the whole shared Gateway (and PULSE's tools with it), so they are not
# separately deregistered here.
#
# WARNING: Destructive and irreversible.
#
# Usage:
#   make destroy   # tear down all of PULSE (invoked before LUMI by destroy-all)
destroy: triage-destroy forecast-destroy ## Tear down all PULSE infrastructure (runtime, /pulse assets, stack)
	@echo "  [pulse] Removing PULSE frontend assets from shared bucket (s3://$(LUMI_FRONTEND_BUCKET)/pulse/)..."
	@$(AWS) s3 rm s3://$(LUMI_FRONTEND_BUCKET)/pulse/ --recursive --quiet 2>/dev/null || true
	@echo "  [pulse] Deleting the '$(TRIAGE_IMAGE_TAG)' image tag from the shared ECR repo ($(SHARED_AGENT_ECR_REPO))..."
	@$(AWS) ecr batch-delete-image --repository-name $(SHARED_AGENT_ECR_REPO) --region $(REGION) \
		--image-ids imageTag=$(TRIAGE_IMAGE_TAG) > /dev/null 2>&1 || true
	@echo "  [pulse] Deleting the '$(FORECAST_IMAGE_TAG)' image tag from the shared ECR repo ($(SHARED_AGENT_ECR_REPO))..."
	@$(AWS) ecr batch-delete-image --repository-name $(SHARED_AGENT_ECR_REPO) --region $(REGION) \
		--image-ids imageTag=$(FORECAST_IMAGE_TAG) > /dev/null 2>&1 || true
	@echo "  [pulse] Emptying PULSE deploy bucket ($(DEPLOY_BUCKET))..."
	@$(AWS) s3 rm s3://$(DEPLOY_BUCKET) --recursive --quiet 2>/dev/null || true
	@echo "  [pulse] Deleting CloudFormation stack ($(STACK_NAME))..."
	@$(CFN_AWS) cloudformation delete-stack --stack-name $(STACK_NAME)
	@echo "  [pulse] Waiting for stack delete to complete..."
	@$(CFN_AWS) cloudformation wait stack-delete-complete --stack-name $(STACK_NAME) \
		|| { echo ""; \
		     echo "  ERROR: PULSE stack delete did not complete. Inspect events:"; \
		     echo "  aws cloudformation describe-stack-events --stack-name $(STACK_NAME)"; \
		     echo ""; exit 1; }
	@echo "  [pulse] PULSE teardown complete."
