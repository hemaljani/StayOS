# StayOS platform Makefile (root)
#
# Delegates to each feature's own Makefile (lumi/, pulse/, stayos-shell/,
# shared/data-orchestrator/) via prefixed pass-through targets, and adds two
# cross-feature orchestrators: `deploy-all` and `destroy-all`.
#
# Common targets:
#   make help          list available targets
#   make deploy-all    deploy the whole platform (preserves existing AppPassword)
#   make destroy-all   tear it all down (needs CONFIRM=DESTROY)
#   make test-all      run every feature's test suite
#   make <feature>-<target>   e.g. lumi-deploy, pulse-test, shell-deploy, data-deploy
#
# Full walkthrough (stages, parameters, failure recovery): docs/deployment-pipeline.md

# ─── Config (used by deploy-all/destroy-all) ─────────────────────────────────
REGION            ?= us-east-1
# PROFILE is canonical; AWS_PROFILE remains a backwards-compatible input.
PROFILE           ?=
ifeq ($(strip $(PROFILE)),)
PROFILE           := $(AWS_PROFILE)
endif
ifeq ($(strip $(PROFILE)),)
unexport AWS_PROFILE
else
override AWS_PROFILE := $(PROFILE)
export AWS_PROFILE
endif
ENVIRONMENT       ?= test
EXPECTED_ACCOUNT_ID ?=
# Some organizations require CloudFormation changes to be initiated through a
# guardrail-exempt role profile while all other calls continue to use PROFILE.
CLOUDFORMATION_PROFILE ?= $(PROFILE)
export CLOUDFORMATION_PROFILE
LUMI_STACK_PREFIX ?= stayos
PULSE_STACK_PREFIX ?= pulse
DATA_STACK_PREFIX ?= stayos-data
LUMI_STACK        := $(LUMI_STACK_PREFIX)-$(REGION)
PULSE_STACK       := $(PULSE_STACK_PREFIX)-$(REGION)
DATA_STACK        := $(DATA_STACK_PREFIX)-$(REGION)
AWS_PROFILE_FLAG  := $(if $(PROFILE),--profile $(PROFILE),)
AWS               := aws $(AWS_PROFILE_FLAG) --region $(REGION)
CFN_PROFILE_FLAG  := $(if $(CLOUDFORMATION_PROFILE),--profile $(CLOUDFORMATION_PROFILE),)
CFN_AWS           := aws $(CFN_PROFILE_FLAG) --region $(REGION)
# Creation / explicit replacement requires APP_PASSWORD; updates preserve it.
APP_PASSWORD      ?=
# Store the raw input without interpreting password dollars as Make variables.
override APP_PASSWORD := $(value APP_PASSWORD)
CHANGE_APP_PASSWORD ?= 0
export APP_PASSWORD CHANGE_APP_PASSWORD

.PHONY: help plan deployment-preflight deploy-all verify-deployment \
        deployment-summary deployment-summary-detailed destroy-all \
        tools-test auth-check test-all

help:
	@echo "StayOS platform Makefile — delegates to per-feature Makefiles."
	@echo ""
	@echo "  make lumi-<target>    run <target> in lumi/ (deploy, destroy, test, lint, reseed, ...)"
	@echo "  make pulse-<target>   run <target> in pulse/ (test, lint, validate, package, deploy, ...)"
	@echo "  make shell-<target>   run <target> in stayos-shell/ (test, lint, build-frontend, deploy, ...)"
	@echo "  make data-<target>    run <target> in shared/data-orchestrator/ (deploy, test, validate, destroy, ...)"
	@echo "  make plan             inspect config, account, stacks, and stages without changing AWS"
	@echo "  make lumi-voice-upgrade"
	@echo "                        upgrade only the existing voice policy and runtime; explicit profiles required"
	@echo "  make lumi-voice-rollback VOICE_BASELINE=..."
	@echo "                        restore the saved voice template and pinned image"
	@echo "  make deploy-all       deploy LUMI, then deploy PULSE with LUMI's outputs threaded in"
	@echo "                        skips APP_PASSWORD on updates; creation requires it in the environment"
	@echo "                        replacement requires CHANGE_APP_PASSWORD=1; honors PROFILE, REGION, ENVIRONMENT,"
	@echo "                        EXPECTED_ACCOUNT_ID, CLOUDFORMATION_PROFILE, and stack prefixes"
	@echo "                        concise by default; VERBOSE=1 streams diagnostics, NO_COLOR=1"
	@echo "                        disables color; full logs are written under logs/"
	@echo "  make deployment-summary-detailed"
	@echo "                        print runtime, artifact, API, and application details"
	@echo "  make destroy-all      tear down the WHOLE platform in reverse order (data -> shell -> PULSE -> LUMI)"
	@echo "                        requires CONFIRM=DESTROY; honors PROFILE=... REGION=... (default us-east-1)"
	@echo "  make test-all         run deployment-tool, shared-auth, shell, LUMI, PULSE, and data tests"
	@echo ""
	@echo "  The StayOS shell (login + launcher at /) publishes to the shared distribution root."
	@echo "  After LUMI's stack exists, run: make shell-deploy [PROFILE=... REGION=...]"
	@echo ""
	@echo "  The shared data orchestrator is deployed additively AFTER deploy-all:"
	@echo "  run: make data-deploy [PROFILE=... REGION=...] (does not re-seed live data)."
	@echo ""
	@echo "See lumi/Makefile and lumi/README.md for the full LUMI target list."

# Prefixed pass-throughs: `make <feature>-<target>` runs <target> in that
# feature's dir. Every child receives the same canonical deployment context so
# standalone commands target the same account, region, environment, and names.
lumi-%:
	$(MAKE) -C lumi $* PROFILE='$(PROFILE)' \
		CLOUDFORMATION_PROFILE='$(CLOUDFORMATION_PROFILE)' \
		REGION='$(REGION)' ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(LUMI_STACK_PREFIX)'

pulse-%:
	$(MAKE) -C pulse $* PROFILE='$(PROFILE)' \
		CLOUDFORMATION_PROFILE='$(CLOUDFORMATION_PROFILE)' \
		REGION='$(REGION)' ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(PULSE_STACK_PREFIX)' \
		LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)'

# Shell = static assets published to the distribution ROOT (/); run after LUMI exists.
shell-%:
	$(MAKE) -C stayos-shell $* PROFILE='$(PROFILE)' \
		REGION='$(REGION)' ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(LUMI_STACK_PREFIX)'

# Data Orchestrator (stayos-data): additive daily roll-forward + PULSE baseline.
# Deploy after deploy-all; it never re-seeds the live dataset. See its own Makefile.
data-%:
	$(MAKE) -C shared/data-orchestrator $* PROFILE='$(PROFILE)' \
		CLOUDFORMATION_PROFILE='$(CLOUDFORMATION_PROFILE)' \
		REGION='$(REGION)' ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(DATA_STACK_PREFIX)' \
		LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		PULSE_STACK_PREFIX='$(PULSE_STACK_PREFIX)'

plan:
	@PYTHONPATH=tools python3 -m stayos_deploy plan \
		--environment '$(ENVIRONMENT)' \
		--region '$(REGION)' \
		--expected-account-id '$(EXPECTED_ACCOUNT_ID)' \
		--cloudformation-profile '$(CLOUDFORMATION_PROFILE)' \
		--lumi-stack-prefix '$(LUMI_STACK_PREFIX)' \
		--pulse-stack-prefix '$(PULSE_STACK_PREFIX)' \
		--data-stack-prefix '$(DATA_STACK_PREFIX)' \
		$(if $(PROFILE),--profile '$(PROFILE)',)

# Reviewed existing-resource publish; use capture first, then the saved baseline.
FIX_PHASE ?= capture
FIX_BASELINE ?=
FIX_MANIFEST ?=
.PHONY: deploy-pending-fixes
deploy-pending-fixes:
	@PYTHONPATH=tools python3 -m stayos_deploy.pending '$(FIX_PHASE)' \
		--profile '$(PROFILE)' --cloudformation-profile '$(CLOUDFORMATION_PROFILE)' \
		--region '$(REGION)' --expected-account-id '$(EXPECTED_ACCOUNT_ID)' \
		--stack-prefix '$(LUMI_STACK_PREFIX)' \
		--pulse-stack-prefix '$(PULSE_STACK_PREFIX)' \
		$(if $(FIX_BASELINE),--baseline '$(FIX_BASELINE)',) \
		$(if $(FIX_MANIFEST),--manifest '$(FIX_MANIFEST)',)

deployment-preflight:
	@set -e; \
	account_id=$$($(AWS) sts get-caller-identity --query Account --output text); \
	cfn_account_id=$$($(CFN_AWS) sts get-caller-identity --query Account --output text); \
	if [ -z "$$account_id" ] || [ "$$account_id" = "None" ]; then \
		echo "ERROR: unable to resolve the active AWS account."; exit 1; \
	fi; \
	if [ -z "$$cfn_account_id" ] || [ "$$cfn_account_id" = "None" ]; then \
		echo "ERROR: unable to resolve the CloudFormation execution account."; exit 1; \
	fi; \
	if [ -n "$(EXPECTED_ACCOUNT_ID)" ] && [ "$$account_id" != "$(EXPECTED_ACCOUNT_ID)" ]; then \
		echo "ERROR: AWS account mismatch."; \
		echo "  Expected: $(EXPECTED_ACCOUNT_ID)"; \
		echo "  Resolved: $$account_id"; \
		echo "No deployment actions were started."; \
		exit 1; \
	fi; \
	if [ "$$cfn_account_id" != "$$account_id" ]; then \
		echo "ERROR: CloudFormation execution account mismatch."; \
		echo "  Target profile account:         $$account_id"; \
		echo "  CloudFormation profile account: $$cfn_account_id"; \
		echo "No deployment actions were started."; \
		exit 1; \
	fi; \
	echo "Deployment context validated:"; \
	echo "  Environment: $(ENVIRONMENT)"; \
	echo "  Account:     $$account_id"; \
	echo "  Region:      $(REGION)"; \
	echo "  Profile:     $${AWS_PROFILE:-<default credential chain>}"; \
	echo "  CFN profile: $(if $(CLOUDFORMATION_PROFILE),$(CLOUDFORMATION_PROFILE),<default credential chain>)"

# ─── deploy-all: deploy the whole platform, end to end ───────────────────────
# Deploys LUMI, then threads its outputs (Cognito pool, 5 DynamoDB stream ARNs,
# Gateway endpoint, Tool Lambda ARN) into PULSE, builds the Triage + Forecasting
# agent runtimes (re-deploying PULSE to wire their ARNs), publishes the PULSE PWA
# and the StayOS shell, and finally deploys the Data Orchestrator. 8 stages.
#
# The stage-by-stage walkthrough, every captured value, the WAF note, and
# per-stage failure recovery live in docs/deployment-pipeline.md.
deploy-all:
	@VERBOSE='$(VERBOSE)' NO_COLOR='$(NO_COLOR)' \
		PYTHONPATH=tools python3 -m stayos_deploy deploy \
		--environment '$(ENVIRONMENT)' \
		--region '$(REGION)' \
		--expected-account-id '$(EXPECTED_ACCOUNT_ID)' \
		--cloudformation-profile '$(CLOUDFORMATION_PROFILE)' \
		--lumi-stack-prefix '$(LUMI_STACK_PREFIX)' \
		--pulse-stack-prefix '$(PULSE_STACK_PREFIX)' \
		--data-stack-prefix '$(DATA_STACK_PREFIX)' \
		$(if $(PROFILE),--profile '$(PROFILE)',)

# Retained temporarily as a readable record of the pre-runner orchestration.
# deploy-all above is the supported entry point.
_deploy-all-legacy: deployment-preflight lumi-password-preflight
	@echo "══ [1/8] Deploying LUMI ($(LUMI_STACK)) ══"
	@$(MAKE) -C lumi deploy \
		PROFILE='$(PROFILE)' REGION='$(REGION)' \
		CLOUDFORMATION_PROFILE='$(CLOUDFORMATION_PROFILE)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(LUMI_STACK_PREFIX)'
	@echo ""
	@echo "══ [2/8] Capturing LUMI outputs and deploying the PULSE stack ══"
	@set -e; \
	out() { $(AWS) cloudformation describe-stacks --stack-name "$$1" \
		--query "Stacks[0].Outputs[?OutputKey=='$$2'].OutputValue" --output text; }; \
	required() { \
		if [ -z "$$2" ] || [ "$$2" = "None" ]; then \
			echo "ERROR: required deployment value '$$1' was not resolved."; exit 1; \
		fi; \
	}; \
	USER_POOL_ID=$$(out $(LUMI_STACK) UserPoolId); required UserPoolId "$$USER_POOL_ID"; \
	USER_POOL_CLIENT_ID=$$(out $(LUMI_STACK) UserPoolClientId); required UserPoolClientId "$$USER_POOL_CLIENT_ID"; \
	TOOL_LAMBDA_ARN=$$(out $(LUMI_STACK) ToolLambdaArn); required ToolLambdaArn "$$TOOL_LAMBDA_ARN"; \
	ACCOUNT_ID=$$($(AWS) sts get-caller-identity --query Account --output text); \
	required AccountId "$$ACCOUNT_ID"; \
	USER_POOL_ARN="arn:aws:cognito-idp:$(REGION):$$ACCOUNT_ID:userpool/$$USER_POOL_ID"; \
	GATEWAY_ENDPOINT_URL=$$($(AWS) ssm get-parameter \
		--name "/$(LUMI_STACK_PREFIX)/gateway/endpoint-url" \
		--query "Parameter.Value" --output text 2>/dev/null || echo ""); \
	required GatewayEndpointUrl "$$GATEWAY_ENDPOINT_URL"; \
	DATA_STACK=$$($(AWS) cloudformation describe-stack-resources \
		--stack-name $(LUMI_STACK) --logical-resource-id DataStack \
		--query "StackResources[0].PhysicalResourceId" --output text); \
	required DataStack "$$DATA_STACK"; \
	RESERVATIONS_STREAM_ARN=$$(out $$DATA_STACK ReservationsStreamArn); required ReservationsStreamArn "$$RESERVATIONS_STREAM_ARN"; \
	ROOMS_STREAM_ARN=$$(out $$DATA_STACK RoomsStreamArn); required RoomsStreamArn "$$ROOMS_STREAM_ARN"; \
	GUESTS_STREAM_ARN=$$(out $$DATA_STACK GuestsStreamArn); required GuestsStreamArn "$$GUESTS_STREAM_ARN"; \
	REVENUES_STREAM_ARN=$$(out $$DATA_STACK RevenuesStreamArn); required RevenuesStreamArn "$$REVENUES_STREAM_ARN"; \
	WORK_ORDERS_STREAM_ARN=$$(out $$DATA_STACK WorkOrdersStreamArn); required WorkOrdersStreamArn "$$WORK_ORDERS_STREAM_ARN"; \
	arn_of() { echo "$${1%%/stream/*}"; }; \
	RESERVATIONS_TABLE_ARN=$$(arn_of "$$RESERVATIONS_STREAM_ARN"); \
	ROOMS_TABLE_ARN=$$(arn_of "$$ROOMS_STREAM_ARN"); \
	GUESTS_TABLE_ARN=$$(arn_of "$$GUESTS_STREAM_ARN"); \
	REVENUES_TABLE_ARN=$$(arn_of "$$REVENUES_STREAM_ARN"); \
	WORK_ORDERS_TABLE_ARN=$$(arn_of "$$WORK_ORDERS_STREAM_ARN"); \
	tbl_name() { t="$${1##*:table/}"; echo "$${t%%/stream/*}"; }; \
	RESERVATIONS_TABLE_NAME=$$(tbl_name "$$RESERVATIONS_STREAM_ARN"); \
	ROOMS_TABLE_NAME=$$(tbl_name "$$ROOMS_STREAM_ARN"); \
	GUESTS_TABLE_NAME=$$(tbl_name "$$GUESTS_STREAM_ARN"); \
	echo "  Captured: UserPoolId=$$USER_POOL_ID  ToolLambdaArn=$$TOOL_LAMBDA_ARN"; \
	echo "  Streams:  reservations/rooms/guests/revenues/work-orders resolved from DataStack $$DATA_STACK"; \
	echo "  Tables:   $$RESERVATIONS_TABLE_ARN (+ rooms/guests/revenues/work-orders) derived for IAM scoping"; \
	pulse_deploy() { \
		$(MAKE) -C pulse deploy PROFILE='$(PROFILE)' REGION='$(REGION)' \
			CLOUDFORMATION_PROFILE='$(CLOUDFORMATION_PROFILE)' \
			ENVIRONMENT='$(ENVIRONMENT)' \
			EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
			STACK_PREFIX='$(PULSE_STACK_PREFIX)' \
			LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
			USER_POOL_ID="$$USER_POOL_ID" \
			USER_POOL_ARN="$$USER_POOL_ARN" \
			USER_POOL_CLIENT_ID="$$USER_POOL_CLIENT_ID" \
			GATEWAY_ENDPOINT_URL="$$GATEWAY_ENDPOINT_URL" \
			RESERVATIONS_STREAM_ARN="$$RESERVATIONS_STREAM_ARN" \
			ROOMS_STREAM_ARN="$$ROOMS_STREAM_ARN" \
			GUESTS_STREAM_ARN="$$GUESTS_STREAM_ARN" \
			REVENUES_STREAM_ARN="$$REVENUES_STREAM_ARN" \
			WORK_ORDERS_STREAM_ARN="$$WORK_ORDERS_STREAM_ARN" \
			RESERVATIONS_TABLE_ARN="$$RESERVATIONS_TABLE_ARN" \
			ROOMS_TABLE_ARN="$$ROOMS_TABLE_ARN" \
			GUESTS_TABLE_ARN="$$GUESTS_TABLE_ARN" \
			REVENUES_TABLE_ARN="$$REVENUES_TABLE_ARN" \
			WORK_ORDERS_TABLE_ARN="$$WORK_ORDERS_TABLE_ARN" \
			RESERVATIONS_TABLE_NAME="$$RESERVATIONS_TABLE_NAME" \
			ROOMS_TABLE_NAME="$$ROOMS_TABLE_NAME" \
			GUESTS_TABLE_NAME="$$GUESTS_TABLE_NAME" \
			TRIAGE_RUNTIME_ARN="$$1" \
			FORECAST_RUNTIME_ARN="$$2"; \
	}; \
	pulse_deploy "" \
		|| { echo ""; \
		     echo "ERROR: PULSE stack deploy (pass 1) failed. LUMI ($(LUMI_STACK)) is already"; \
		     echo "deployed and healthy - do NOT redeploy it. Fix the error above, then re-run"; \
		     echo "  make deploy-all PROFILE=$(PROFILE) REGION=$(REGION)"; \
		     echo ""; exit 1; }; \
	echo ""; \
	echo "══ [3/8] Registering PULSE tools on the shared StayOS Gateway ══"; \
	$(MAKE) -C pulse gateway-deploy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(PULSE_STACK_PREFIX)' LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		TOOL_LAMBDA_ARN="$$TOOL_LAMBDA_ARN" \
		|| { echo ""; \
		     echo "ERROR: PULSE Gateway tool registration failed. Both stacks are deployed."; \
		     echo "Re-run only this step: make pulse-gateway-deploy PROFILE=$(PROFILE) REGION=$(REGION) TOOL_LAMBDA_ARN=$$TOOL_LAMBDA_ARN"; \
		     echo ""; exit 1; }; \
	echo ""; \
	echo "══ [4/8] Building the Triage Agent runtime ══"; \
	$(MAKE) -C pulse triage-deploy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(PULSE_STACK_PREFIX)' LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: Triage Agent build/deploy failed (CodeBuild or AgentCore). Both stacks"; \
		     echo "are deployed; alerts are created but agentic triage will not fire until this"; \
		     echo "succeeds. Re-run: make pulse-triage-deploy PROFILE=$(PROFILE) REGION=$(REGION)"; \
		     echo ""; exit 1; }; \
	TRIAGE_RUNTIME_ARN=$$($(AWS) ssm get-parameter \
		--name "/$(PULSE_STACK_PREFIX)/triage/runtime-arn" \
		--query "Parameter.Value" --output text 2>/dev/null || echo ""); \
	if [ -z "$$TRIAGE_RUNTIME_ARN" ]; then \
		echo "ERROR: Triage runtime ARN not found in SSM after triage-deploy."; exit 1; \
	fi; \
	echo ""; \
	echo "══ [5/8] Building the Forecasting Agent runtime, then wiring both runtimes into PULSE ══"; \
	$(MAKE) -C pulse forecast-deploy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(PULSE_STACK_PREFIX)' LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: Forecasting Agent build/deploy failed (CodeBuild or AgentCore). Both"; \
		     echo "stacks are deployed and the Triage runtime is live; the daily oversell forecast"; \
		     echo "will not fire until this succeeds. Re-run: make pulse-forecast-deploy PROFILE=$(PROFILE) REGION=$(REGION)"; \
		     echo ""; exit 1; }; \
	FORECAST_RUNTIME_ARN=$$($(AWS) ssm get-parameter \
		--name "/$(PULSE_STACK_PREFIX)/forecast/runtime-arn" \
		--query "Parameter.Value" --output text 2>/dev/null || echo ""); \
	if [ -z "$$FORECAST_RUNTIME_ARN" ]; then \
		echo "ERROR: Forecast runtime ARN not found in SSM after forecast-deploy."; exit 1; \
	fi; \
	echo "  Re-deploying PULSE stack with TriageRuntimeArn=$$TRIAGE_RUNTIME_ARN and ForecastRuntimeArn=$$FORECAST_RUNTIME_ARN"; \
	pulse_deploy "$$TRIAGE_RUNTIME_ARN" "$$FORECAST_RUNTIME_ARN" \
		|| { echo ""; \
		     echo "ERROR: final PULSE stack deploy (with triage + forecast ARNs) failed. Both"; \
		     echo "runtimes exist (SSM /pulse/triage/runtime-arn, /pulse/forecast/runtime-arn);"; \
		     echo "retry with the same PROFILE, REGION, ENVIRONMENT, and stack prefixes."; \
		     echo ""; exit 1; }; \
	echo ""; \
	echo "══ [6/8] Publishing the PULSE PWA to /pulse on the shared LUMI CloudFront ══"; \
	$(MAKE) -C pulse deploy-frontend PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(PULSE_STACK_PREFIX)' LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		USER_POOL_CLIENT_ID="$$USER_POOL_CLIENT_ID" \
		COGNITO_REGION='$(REGION)' \
		|| { echo ""; \
		     echo "ERROR: PULSE frontend publish failed. Backend is fully deployed."; \
		     echo "Re-run only this step: make pulse-deploy-frontend PROFILE=$(PROFILE) REGION=$(REGION) USER_POOL_CLIENT_ID=<id>"; \
		     echo ""; exit 1; }; \
	echo ""; \
	echo "══ [7/8] Publishing the StayOS shell (login + launcher) to the distribution root ══"; \
	$(MAKE) -C stayos-shell deploy PROFILE='$(PROFILE)' \
		REGION='$(REGION)' ENVIRONMENT='$(ENVIRONMENT)' \
		EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: StayOS shell publish failed. LUMI + PULSE are fully deployed. The shell"; \
		     echo "owns the distribution ROOT (/) - without it, hitting / returns S3 AccessDenied."; \
		     echo "Re-run only this step: make shell-deploy PROFILE=$(PROFILE) REGION=$(REGION)"; \
		     echo ""; exit 1; }; \
	echo ""; \
	echo "══ [8/8] Deploying the shared Data Orchestrator (roll-forward + baseline) ══"; \
	$(MAKE) -C shared/data-orchestrator deploy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		CLOUDFORMATION_PROFILE='$(CLOUDFORMATION_PROFILE)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(DATA_STACK_PREFIX)' \
		LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' PULSE_STACK_PREFIX='$(PULSE_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: Data Orchestrator deploy failed. LUMI + PULSE are fully deployed"; \
		     echo "and healthy - do NOT redeploy them. The orchestrator is additive (it does"; \
		     echo "not re-seed live data). Re-run only this step:"; \
		     echo "  make data-deploy PROFILE=$(PROFILE) REGION=$(REGION) ENVIRONMENT=$(ENVIRONMENT) EXPECTED_ACCOUNT_ID=$(EXPECTED_ACCOUNT_ID)"; \
		     echo ""; exit 1; }
	@$(MAKE) verify-deployment PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' PULSE_STACK_PREFIX='$(PULSE_STACK_PREFIX)' \
		DATA_STACK_PREFIX='$(DATA_STACK_PREFIX)'
	@$(MAKE) deployment-summary PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' PULSE_STACK_PREFIX='$(PULSE_STACK_PREFIX)' \
		DATA_STACK_PREFIX='$(DATA_STACK_PREFIX)'

verify-deployment:
	@set -e; \
	required() { \
		if [ -z "$$2" ] || [ "$$2" = "None" ]; then \
			echo "ERROR: verification could not resolve '$$1'."; exit 1; \
		fi; \
	}; \
	stack_ok() { \
		status=$$($(AWS) cloudformation describe-stacks --stack-name "$$1" \
			--query "Stacks[0].StackStatus" --output text); \
		case "$$status" in \
			CREATE_COMPLETE|UPDATE_COMPLETE) echo "  PASS  $$1 ($$status)" ;; \
			*) echo "ERROR: stack $$1 is not healthy ($$status)."; exit 1 ;; \
		esac; \
	}; \
	echo "Verifying deployed resources..."; \
	stack_ok "$(LUMI_STACK)"; \
	stack_ok "$(PULSE_STACK)"; \
	stack_ok "$(DATA_STACK)"; \
	frontend_url=$$($(AWS) cloudformation describe-stacks --stack-name "$(LUMI_STACK)" \
		--query "Stacks[0].Outputs[?OutputKey=='FrontendUrl'].OutputValue" --output text); \
	api_url=$$($(AWS) cloudformation describe-stacks --stack-name "$(PULSE_STACK)" \
		--query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text); \
	triage_arn=$$($(AWS) ssm get-parameter --name "/$(PULSE_STACK_PREFIX)/triage/runtime-arn" \
		--query "Parameter.Value" --output text); \
	forecast_arn=$$($(AWS) ssm get-parameter --name "/$(PULSE_STACK_PREFIX)/forecast/runtime-arn" \
		--query "Parameter.Value" --output text); \
	voice_id=$$($(AWS) ssm get-parameter --name "/$(LUMI_STACK_PREFIX)/voice/runtime-id" \
		--query "Parameter.Value" --output text); \
	chat_id=$$($(AWS) ssm get-parameter --name "/$(LUMI_STACK_PREFIX)/chat/runtime-id" \
		--query "Parameter.Value" --output text); \
	gateway_url=$$($(AWS) ssm get-parameter --name "/$(LUMI_STACK_PREFIX)/gateway/endpoint-url" \
		--query "Parameter.Value" --output text); \
	required FrontendUrl "$$frontend_url"; \
	required PulseApiEndpoint "$$api_url"; \
	required VoiceRuntimeId "$$voice_id"; \
	required ChatRuntimeId "$$chat_id"; \
	required TriageRuntimeArn "$$triage_arn"; \
	required ForecastRuntimeArn "$$forecast_arn"; \
	required GatewayEndpointUrl "$$gateway_url"; \
	echo "  PASS  required stack outputs and runtime parameters"; \
	echo "Deployment verification passed."

deployment-summary:
	@set -e; \
	account_id=$$($(AWS) sts get-caller-identity --query Account --output text); \
	frontend_url=$$($(AWS) cloudformation describe-stacks --stack-name "$(LUMI_STACK)" \
		--query "Stacks[0].Outputs[?OutputKey=='FrontendUrl'].OutputValue" --output text); \
	pulse_api=$$($(AWS) cloudformation describe-stacks --stack-name "$(PULSE_STACK)" \
		--query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text); \
	realtime_url=$$($(AWS) cloudformation describe-stacks --stack-name "$(PULSE_STACK)" \
		--query "Stacks[0].Outputs[?OutputKey=='RealtimeHttpEndpoint'].OutputValue" --output text); \
	triage_arn=$$($(AWS) ssm get-parameter --name "/$(PULSE_STACK_PREFIX)/triage/runtime-arn" \
		--query "Parameter.Value" --output text); \
	forecast_arn=$$($(AWS) ssm get-parameter --name "/$(PULSE_STACK_PREFIX)/forecast/runtime-arn" \
		--query "Parameter.Value" --output text); \
	voice_id=$$($(AWS) ssm get-parameter --name "/$(LUMI_STACK_PREFIX)/voice/runtime-id" \
		--query "Parameter.Value" --output text); \
	chat_id=$$($(AWS) ssm get-parameter --name "/$(LUMI_STACK_PREFIX)/chat/runtime-id" \
		--query "Parameter.Value" --output text); \
	gateway_url=$$($(AWS) ssm get-parameter --name "/$(LUMI_STACK_PREFIX)/gateway/endpoint-url" \
		--query "Parameter.Value" --output text); \
	echo ""; \
	echo "════════════════ StayOS deployment summary ════════════════"; \
	echo "  Environment: $(ENVIRONMENT)"; \
	echo "  Account:     $$account_id"; \
	echo "  Region:      $(REGION)"; \
	echo ""; \
	echo "  Stacks:"; \
	echo "    LUMI:              $(LUMI_STACK)"; \
	echo "    PULSE:             $(PULSE_STACK)"; \
	echo "    Data Orchestrator: $(DATA_STACK)"; \
	echo ""; \
	echo "  Runtimes:"; \
	echo "    Voice:    arn:aws:bedrock-agentcore:$(REGION):$$account_id:runtime/$$voice_id"; \
	echo "    Chat:     arn:aws:bedrock-agentcore:$(REGION):$$account_id:runtime/$$chat_id"; \
	echo "    Triage:   $$triage_arn"; \
	echo "    Forecast: $$forecast_arn"; \
	echo "    Gateway:  $$gateway_url"; \
	echo ""; \
	echo "  Artifacts:"; \
	echo "    LUMI:  s3://$(LUMI_STACK_PREFIX)-deploy-$$account_id/functions/"; \
	echo "    PULSE: s3://$(PULSE_STACK_PREFIX)-deploy-$$account_id/functions/pulse-backend.zip"; \
	echo "    Data:  s3://$(LUMI_STACK_PREFIX)-deploy-$$account_id/functions/data-orchestrator.zip"; \
	echo "    Voice: $$account_id.dkr.ecr.$(REGION).amazonaws.com/$(LUMI_STACK_PREFIX)-voice-agent:latest"; \
	echo "    Chat:  $$account_id.dkr.ecr.$(REGION).amazonaws.com/$(LUMI_STACK_PREFIX)-chat-agent:latest"; \
	echo "    Triage: $$account_id.dkr.ecr.$(REGION).amazonaws.com/$(LUMI_STACK_PREFIX)-chat-agent:triage-latest"; \
	echo "    Forecast: $$account_id.dkr.ecr.$(REGION).amazonaws.com/$(LUMI_STACK_PREFIX)-chat-agent:forecaster-latest"; \
	echo ""; \
	echo "  URLs:"; \
	echo "    Shell:    $$frontend_url"; \
	echo "    LUMI:     $${frontend_url%/}/lumi/"; \
	echo "    PULSE:    $${frontend_url%/}/pulse/"; \
	echo "    API:      $$pulse_api"; \
	echo "    Realtime: $$realtime_url"; \
	echo ""; \
	echo "  Verification: PASSED"; \
	echo "═══════════════════════════════════════════════════════════"

deployment-summary-detailed: deployment-summary

# ─── destroy-all: tear the whole platform down (REVERSE deploy order) ────────
# Order: Data Orchestrator -> shell -> PULSE -> LUMI. LUMI goes LAST because the
# others reuse its shared foundation (Cognito pool, Gateway, ECR repo, frontend
# bucket + CloudFront). Each feature's `destroy` empties its buckets / purges ECR
# before deleting its stack, and waits for the delete to complete.
#
# DESTRUCTIVE and IRREVERSIBLE (deletes stacks, empties S3, purges ECR, removes
# the Cognito pool + all demo GM logins). Guarded by CONFIRM=DESTROY.
#
# Teardown order rationale + per-phase failure recovery: docs/deployment-pipeline.md
destroy-all: deployment-preflight
	@if [ "$(CONFIRM)" != "DESTROY" ]; then \
		echo ""; \
		echo "ERROR: destroy-all is destructive and irreversible. It deletes ALL StayOS"; \
		echo "stacks, empties S3 buckets, purges ECR images, and removes the Cognito user"; \
		echo "pool (all demo GM logins)."; \
		echo ""; \
		echo "Re-run with an explicit confirmation to proceed:"; \
		echo "  make destroy-all CONFIRM=DESTROY [PROFILE=... REGION=...]"; \
		echo ""; \
		exit 1; \
	fi
	@echo "══ [1/4] Destroying the shared Data Orchestrator ($(DATA_STACK)) ══"
	@$(MAKE) -C shared/data-orchestrator destroy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(DATA_STACK_PREFIX)' LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		PULSE_STACK_PREFIX='$(PULSE_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: Data Orchestrator delete-stack call failed. Re-run only this step:"; \
		     echo "  make data-destroy PROFILE=$(PROFILE) REGION=$(REGION) ENVIRONMENT=$(ENVIRONMENT)"; \
		     echo ""; exit 1; }
	@echo "  Waiting for $(DATA_STACK) delete to complete..."
	@$(AWS) cloudformation wait stack-delete-complete --stack-name $(DATA_STACK) \
		|| { echo ""; \
		     echo "ERROR: Data Orchestrator stack delete did not complete. Inspect events:"; \
		     echo "  aws cloudformation describe-stack-events --stack-name $(DATA_STACK)"; \
		     echo ""; exit 1; }
	@echo ""
	@echo "══ [2/4] Unpublishing the StayOS shell from the shared bucket root ══"
	@$(MAKE) -C stayos-shell destroy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: Shell teardown failed. Re-run only this step:"; \
		     echo "  make shell-destroy PROFILE=$(PROFILE) REGION=$(REGION) ENVIRONMENT=$(ENVIRONMENT)"; \
		     echo ""; exit 1; }
	@echo ""
	@echo "══ [3/4] Destroying PULSE (runtime, /pulse assets, stack) ══"
	@$(MAKE) -C pulse destroy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(PULSE_STACK_PREFIX)' LUMI_STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: PULSE teardown failed. LUMI is still deployed. Re-run only this step:"; \
		     echo "  make pulse-destroy PROFILE=$(PROFILE) REGION=$(REGION)"; \
		     echo "then re-run: make destroy-all CONFIRM=DESTROY PROFILE=$(PROFILE) REGION=$(REGION)"; \
		     echo ""; exit 1; }
	@echo ""
	@echo "══ [4/4] Destroying LUMI (shared foundation: buckets, CloudFront, Cognito, stack) ══"
	@$(MAKE) -C lumi destroy PROFILE='$(PROFILE)' REGION='$(REGION)' \
		ENVIRONMENT='$(ENVIRONMENT)' EXPECTED_ACCOUNT_ID='$(EXPECTED_ACCOUNT_ID)' \
		STACK_PREFIX='$(LUMI_STACK_PREFIX)' \
		|| { echo ""; \
		     echo "ERROR: LUMI teardown failed. Data/shell/PULSE are already gone. Re-run only"; \
		     echo "this step: make lumi-destroy PROFILE=$(PROFILE) REGION=$(REGION) ENVIRONMENT=$(ENVIRONMENT)"; \
		     echo ""; exit 1; }
	@echo ""
	@echo "════════════════════════════════════════════════════════════════"
	@echo "  StayOS torn down end to end: Data Orchestrator + shell + PULSE + LUMI."
	@echo "  All stacks deleted, shared buckets emptied, ECR images purged."
	@echo "════════════════════════════════════════════════════════════════"

tools-test:
	@python3 -m unittest discover -s tools/tests -v
	@PYTHONPATH=tools python3 -m pytest tools/tests/test_voice_upgrade.py -q

auth-check:
	@echo "Testing and type-checking shared/auth..."
	@cd shared/auth && npm ci --silent && npm run test:run -- --pool=threads && npm run typecheck

test-all: tools-test auth-check shell-test lumi-test pulse-test data-test
