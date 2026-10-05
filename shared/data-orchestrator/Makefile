# Makefile — StayOS Unified Data Orchestrator (shared) Build & Deploy
#
# Packages the single orchestrator Lambda zip (all step handlers share one
# artifact; each Lambda points at the same S3Key with a different Handler),
# uploads it, and deploys the stayos-data-orchestrator CloudFormation stack.
#
# DESIGN NOTE (Option 1 — additive): this deploy is ADDITIVE. It owns only the
# per-property roll-forward layer + PULSE baseline priming; it does NOT re-seed
# the live dataset. LumiSeedLambdaArn/PulseSeedLambdaArn stay empty, so the
# legacy stayos-seed-data custom resource remains the initial seeder. Roll-forward
# schedules use their own group (not LUMI's stayos-briefs), so GM-customized
# brief-delivery schedules are untouched. See docs/deployment-pipeline.md for the full flow.
#
# Prerequisites: AWS CLI v2, Python 3.12, zip.
# Usage:
#   make deploy                          # uses current AWS profile account
#   make deploy PROFILE=my-profile       # explicit profile
#   make validate                        # cfn-lint-free template validation (no deploy)
#   make test                            # run the orchestrator unit/integration tests
#   make destroy                         # tear down the orchestrator stack

# ─── Config ───────────────────────────────────────────────────
# Matches the template default and repo NAMING convention: stayos-data.
STACK_PREFIX ?= stayos-data
PROFILE ?=
ifeq ($(strip $(PROFILE)),)
PROFILE := $(AWS_PROFILE)
endif
ifeq ($(strip $(PROFILE)),)
unexport AWS_PROFILE
else
override AWS_PROFILE := $(PROFILE)
export AWS_PROFILE
endif
ENVIRONMENT ?= test
EXPECTED_ACCOUNT_ID ?=
CLOUDFORMATION_PROFILE ?= $(PROFILE)
REGION ?= us-east-1
ORCHESTRATED ?= 0
step = $(if $(filter 1,$(ORCHESTRATED)),  - $(2),[$(1)] $(2))
AWS_REGION := $(REGION)
AWS_DEFAULT_REGION := $(REGION)
export AWS_REGION AWS_DEFAULT_REGION
ACCOUNT_ID ?= $(shell aws $(if $(PROFILE),--profile $(PROFILE),) --region $(REGION) sts get-caller-identity --query Account --output text 2>/dev/null)
# Stack name mirrors the LUMI/PULSE convention: ${StackPrefix}-${Region}.
STACK_NAME := $(STACK_PREFIX)-$(REGION)
# Reuse LUMI's deploy bucket so we don't create a second artifacts bucket for
# one small zip. Override DEPLOY_BUCKET to use a different bucket.
LUMI_STACK_PREFIX ?= stayos
DEPLOY_BUCKET ?= $(LUMI_STACK_PREFIX)-deploy-$(ACCOUNT_ID)
LAMBDA_S3_KEY := functions/data-orchestrator.zip

# ─── Option 1 knobs (additive deploy) ─────────────────────────
# Empty on purpose: disables the orchestrator's seed path (see DESIGN NOTE).
# Set these only for a future full cutover.
LUMI_SEED_LAMBDA_ARN ?=
PULSE_SEED_LAMBDA_ARN ?=
# Bump to force the first-deploy seed custom resource to re-run (idempotent,
# upsert-only). Kept at the template default for an additive deploy.
SEED_TRIGGER ?= 1

# ─── LUMI resource names threaded into the orchestrator (AI-1/AI-2/AI-3) ──────
# Generate/Reconcile/RegenerateBrief target the live LUMI tables + settings +
# orchestrator. Derived from the LUMI StackPrefix (today stayos-*). Overridable.
LUMI_RESERVATIONS_TABLE ?= $(LUMI_STACK_PREFIX)-reservations
LUMI_ROOMS_TABLE        ?= $(LUMI_STACK_PREFIX)-rooms
LUMI_GUESTS_TABLE       ?= $(LUMI_STACK_PREFIX)-guests
LUMI_REVENUES_TABLE     ?= $(LUMI_STACK_PREFIX)-revenues
LUMI_WORK_ORDERS_TABLE  ?= $(LUMI_STACK_PREFIX)-work-orders
LUMI_SETTINGS_TABLE     ?= $(LUMI_STACK_PREFIX)-settings
LUMI_ORCHESTRATOR_FN    ?= $(LUMI_STACK_PREFIX)-orchestrator

# PULSE rule-evaluator function whose DynamoDB-stream ESMs the Quiesce step
# toggles (AI-4). ESM UUIDs are resolved LIVE at deploy time (they change on
# PULSE redeploy) - see the deploy target.
PULSE_STACK_PREFIX      ?= pulse
PULSE_RULE_EVALUATOR_FN ?= $(PULSE_STACK_PREFIX)-rule-evaluator
PULSE_ALERTS_TABLE      ?= $(PULSE_STACK_PREFIX)-alerts

# Pilot estate (matches the template EstatePropertyIds default). Used by the
# post-deploy `prime` step so a fresh deploy has today's brief for every
# property out of the box (no waiting for local midnight).
ESTATE_PROPERTY_IDS ?= ALOHA-CHI-001 ALOHA-MIA-001 ALOHA-TYO-001 ALOHA-MAD-001 ALOHA-BOM-001

# All step-Lambda handlers + starters live in handlers/ and bundle into one zip.
# AWS profile flag threaded into every aws call.
AWS_PROFILE_FLAG := $(if $(PROFILE),--profile $(PROFILE),)
AWS := aws $(AWS_PROFILE_FLAG) --region $(REGION)
CFN_PROFILE_FLAG := $(if $(CLOUDFORMATION_PROFILE),--profile $(CLOUDFORMATION_PROFILE),)
CFN_AWS := aws $(CFN_PROFILE_FLAG) --region $(REGION)
CFN_ACCOUNT_ID ?= $(shell $(CFN_AWS) sts get-caller-identity --query Account --output text 2>/dev/null)

.PHONY: all assert-account deploy package ensure-bucket package-infra deploy-infra \
        update-lambda-code prime validate test lint clean destroy

# ─── Full Deploy (single command) ─────────────────────────────
all: deploy

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

deploy: assert-account ensure-bucket package deploy-infra update-lambda-code prime
	@if [ "$(ORCHESTRATED)" != "1" ]; then \
		echo ""; \
		echo "Data Orchestrator deployment complete (additive / Option 1)."; \
		echo "Stack: $(STACK_NAME)  Account: $(ACCOUNT_ID)  Region: $(REGION)"; \
		echo "The live dataset was preserved and today's estate was primed."; \
	fi

# ─── Step 1: Ensure the deploy bucket exists ──────────────────
# Reuses LUMI's deploy bucket by default; create it only if missing.
ensure-bucket:
	@echo "$(call step,1/4,Ensuring deploy bucket exists ($(DEPLOY_BUCKET))...)"
	@$(AWS) s3api head-bucket --bucket $(DEPLOY_BUCKET) 2>/dev/null || \
		$(AWS) s3 mb s3://$(DEPLOY_BUCKET) > /dev/null
	@echo "    done"

# ─── Step 2: Package the orchestrator Lambda zip ──────────────
# All handlers share one artifact. aws-lambda-powertools (Logger, per PYQUALITY)
# isn't in the base runtime, so it's vendored into the zip alongside the handler
# modules; everything else is boto3/stdlib.
#
# QUIESCE SEAM (AI-4): the Quiesce/UnQuiesce steps import the PULSE-owned
# `pulse.rule_engine.quiesce` seam, so we vendor the PULSE `pulse` package into
# the zip (shim also honors PULSE_BACKEND_SRC_PATH). Without it, quiesce no-ops.
PULSE_BACKEND_SRC := ../../pulse/backend/src
LUMI_SEED_DATA_SRC := ../../lumi/backend/functions/seed-data
package:
	@echo "$(call step,2/4,Packaging orchestrator Lambda...)"
	@mkdir -p build
	@find build -mindepth 1 -delete
	@mkdir -p build/pkg
	@cp handlers/*.py build/pkg/
	@python3 -m pip install --ignore-installed \
		-r requirements.txt -t build/pkg/ \
		--disable-pip-version-check \
		--no-warn-conflicts
	@# Vendor the LUMI dataset_generator package so `import dataset_generator`
	@# (via dataset_generator_shim) resolves at runtime - Generate/Reconcile reuse it.
	@if [ -d "$(LUMI_SEED_DATA_SRC)/dataset_generator" ]; then \
		cp -R "$(LUMI_SEED_DATA_SRC)/dataset_generator" build/pkg/dataset_generator; \
	else \
		echo "    ERROR: LUMI dataset_generator not found at $(LUMI_SEED_DATA_SRC)"; exit 1; \
	fi
	@# Vendor the PULSE `pulse` package so the quiesce seam import resolves.
	@if [ -d "$(PULSE_BACKEND_SRC)/pulse" ]; then \
		cp -R "$(PULSE_BACKEND_SRC)/pulse" build/pkg/pulse; \
	else \
		echo "    WARNING: PULSE package not found at $(PULSE_BACKEND_SRC)/pulse - quiesce will no-op"; \
	fi
	@cd build/pkg && zip -r ../data-orchestrator.zip . \
		-x "*.pyc" "__pycache__/*" "*.dist-info/*" "*.egg-info/*" "*/tests/*" "tests/*" > /dev/null
	@$(AWS) s3 cp build/data-orchestrator.zip s3://$(DEPLOY_BUCKET)/$(LAMBDA_S3_KEY) --quiet
	@echo "    done"

# ─── Step 3: Deploy the CloudFormation stack ──────────────────
# Points CloudFormation at the packaged artifact. Seed Lambda ARNs pass through
# (empty by default for Option 1). LUMI table/settings/orchestrator names and the
# live PULSE ESM UUIDs (resolved below) are threaded in so the steps target real
# resources. CAPABILITY_NAMED_IAM: the template creates named IAM roles.
deploy-infra:
	@echo "$(call step,3/4,Deploying orchestrator stack...)"
	@# Resolve the live PULSE rule-evaluator ESM UUIDs (AI-4). They change on a
	@# PULSE redeploy, so query them now rather than hardcoding. Empty (PULSE not
	@# deployed / no mappings) → quiesce safely no-ops.
	@ESM_UUIDS=$$($(AWS) lambda list-event-source-mappings \
		--function-name $(PULSE_RULE_EVALUATOR_FN) \
		--query "EventSourceMappings[].UUID" --output text 2>/dev/null | tr '\t' ',' || echo ""); \
	echo "    rule-evaluator ESM UUIDs: $${ESM_UUIDS:-<none - quiesce no-ops>}"; \
	$(CFN_AWS) cloudformation deploy \
		--stack-name $(STACK_NAME) \
		--template-file infrastructure/data-orchestrator.yaml \
		--parameter-overrides \
			StackPrefix=$(STACK_PREFIX) \
			DeployBucket=$(DEPLOY_BUCKET) \
			LambdaCodeS3Key=$(LAMBDA_S3_KEY) \
			SeedTrigger=$(SEED_TRIGGER) \
			LumiSeedLambdaArn=$(LUMI_SEED_LAMBDA_ARN) \
			PulseSeedLambdaArn=$(PULSE_SEED_LAMBDA_ARN) \
			ReservationsTableName=$(LUMI_RESERVATIONS_TABLE) \
			RoomsTableName=$(LUMI_ROOMS_TABLE) \
			GuestsTableName=$(LUMI_GUESTS_TABLE) \
			RevenuesTableName=$(LUMI_REVENUES_TABLE) \
			WorkOrdersTableName=$(LUMI_WORK_ORDERS_TABLE) \
			SettingsTableName=$(LUMI_SETTINGS_TABLE) \
			LumiOrchestratorFunctionName=$(LUMI_ORCHESTRATOR_FN) \
			PulseAlertsTableName=$(PULSE_ALERTS_TABLE) \
			RuleEngineEsmUuids="$$ESM_UUIDS" \
		--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
		--no-fail-on-empty-changeset
	@echo "    done"

# ─── Step 4: Force Lambda code refresh ────────────────────────
# CloudFormation keys Lambda code by a static S3 key, so it can't tell when the
# zip content changes. Force an update on each Lambda so the latest handler code
# runs after a deploy. Every function is required after CloudFormation completes.
update-lambda-code:
	@echo "$(call step,4/4,Refreshing Lambda function code...)"
	@set -e; \
	log="build/lambda-update.log"; \
	: > "$$log"; \
	for fn in quiesce generate reconcile unquiesce regenerate-brief prime-baseline seed-starter rollforward-starter; do \
		full_name="$(STACK_PREFIX)-$$fn"; \
		echo "  updating $$full_name"; \
		if ! $(AWS) lambda update-function-code \
			--function-name "$$full_name" \
			--s3-bucket $(DEPLOY_BUCKET) \
			--s3-key $(LAMBDA_S3_KEY) >> "$$log" 2>&1; then \
			echo "ERROR: required Lambda update failed for $$full_name."; \
			echo "Last 40 log lines ($$log):"; tail -n 40 "$$log"; \
			echo "Retry: make update-lambda-code PROFILE=$(PROFILE) REGION=$(REGION) STACK_PREFIX=$(STACK_PREFIX)"; \
			exit 1; \
		fi; \
		if ! $(AWS) lambda wait function-updated-v2 \
			--function-name "$$full_name" >> "$$log" 2>&1; then \
			echo "ERROR: Lambda $$full_name did not become ready after its code update."; \
			echo "Last 40 log lines ($$log):"; tail -n 40 "$$log"; \
			exit 1; \
		fi; \
	done
	@echo "    done"

# ─── Post-deploy: prime today's data for every property ───────
# A fresh deploy's seeded window is anchored to a reference date that may not be
# today, so a "today" brief may not exist until the local-midnight schedule
# fires. This runs an immediate roll-forward for every pilot property so the app
# is populated out of the box. It is:
#   - idempotent: roll-forward is upsert-only; the concurrency guard skips a
#     duplicate same-property/day execution (ExecutionAlreadyExists);
#   - serialized: every property toggles the same rule-engine event mappings, so
#     one roll-forward must fully unquiesce before the next begins;
#   - verified: a failed execution or zero-item PULSE baseline fails this stage
#     instead of reporting a successful but incomplete deployment.
prime:
	@echo "$(call step,prime,Priming today's data for the estate...)"
	@SM=$$($(AWS) cloudformation describe-stacks --stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='StateMachineArn'].OutputValue" \
		--output text 2>/dev/null); \
	if [ -z "$$SM" ] || [ "$$SM" = "None" ]; then \
		echo "    ERROR: state machine ARN not found; cannot prime"; exit 1; \
	else \
		PRIME_FAILED=0; \
		for P in $(ESTATE_PROPERTY_IDS); do \
			E=$$($(AWS) stepfunctions start-execution --state-machine-arn "$$SM" \
				--name "prime-$$P-$$(date -u +%Y%m%dT%H%M%S)" \
				--input "{\"mode\":\"roll-forward\",\"propertyId\":\"$$P\"}" \
				--query "executionArn" --output text 2>/dev/null); \
			if [ -n "$$E" ] && [ "$$E" != "None" ]; then \
				echo "    started roll-forward for $$P"; \
			else \
				echo "    ERROR: could not start roll-forward for $$P"; \
				PRIME_FAILED=1; continue; \
			fi; \
			S=""; \
			for i in $$(seq 1 30); do \
				S=$$($(AWS) stepfunctions describe-execution --execution-arn "$$E" \
					--query "status" --output text 2>/dev/null); \
				case "$$S" in \
					SUCCEEDED) break;; \
					FAILED|TIMED_OUT|ABORTED) \
						echo "    ERROR: prime execution for $$P ended $$S"; \
						PRIME_FAILED=1; break;; \
					*) sleep 6;; \
				esac; \
			done; \
			if [ "$$S" != "SUCCEEDED" ]; then \
				echo "    ERROR: prime execution for $$P did not complete successfully"; \
				PRIME_FAILED=1; continue; \
			fi; \
			OUTPUT=$$($(AWS) stepfunctions describe-execution --execution-arn "$$E" \
				--query "output" --output text 2>/dev/null); \
			if ! echo "$$OUTPUT" | grep -Eq '"baselineAlertsPrimed":[1-9][0-9]*'; then \
				echo "    ERROR: prime execution for $$P wrote no PULSE baseline alerts"; \
				PRIME_FAILED=1; \
			else \
				echo "    verified roll-forward and PULSE baseline for $$P"; \
			fi; \
		done; \
		if [ "$$PRIME_FAILED" -ne 0 ]; then \
			echo "    ERROR: one or more estate prime verifications failed"; exit 1; \
		fi; \
		echo "    done"; \
	fi

# ─── Template validation (no deploy) ──────────────────────────
# Validates the template with CloudFormation's validator. Creates/changes nothing.
validate:
	@echo "Validating orchestrator template..."
	@$(AWS) cloudformation validate-template \
		--template-body file://infrastructure/data-orchestrator.yaml > /dev/null
	@echo "Template is valid."

# ─── Testing ──────────────────────────────────────────────────
# Runs the orchestrator handler unit + integration tests (moto-mocked; no AWS
# calls). Note python3 (not python) per repo convention.
test:
	@echo "Running data-orchestrator tests..."
	@python3 -m pytest tests/ -v

lint:
	@echo "Linting orchestrator handlers..."
	@python3 -m ruff check handlers/ tests/

# ─── Cleanup ──────────────────────────────────────────────────
clean:
	@rm -rf build

# ─── Destroy ──────────────────────────────────────────────────
# Tears down the orchestrator stack ONLY - does not touch LUMI, PULSE, or any
# operational data (it holds no tables and, in Option 1, doesn't own the seed).
# Destructive: run deliberately.
destroy: assert-account
	@echo "Deleting orchestrator stack $(STACK_NAME)..."
	@$(CFN_AWS) cloudformation delete-stack --stack-name $(STACK_NAME)
	@echo "Waiting for stack deletion to complete..."
	@$(CFN_AWS) cloudformation wait stack-delete-complete --stack-name $(STACK_NAME)
	@echo "Orchestrator stack deleted."
