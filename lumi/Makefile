# Makefile — LUMI build & deploy. See docs/deployment-pipeline.md for the full flow.
# Prereqs: AWS CLI v2, Python 3.12, Node.js 18+, zip, md5sum/md5
# Usage:
#   make deploy                         # uses current AWS profile account
#   make deploy ACCOUNT_ID=123456789012 # explicit account

STACK_PREFIX ?= stayos
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
AWS_REGION := $(REGION)
AWS_DEFAULT_REGION := $(REGION)
export AWS_REGION AWS_DEFAULT_REGION
AWS_PROFILE_FLAG := $(if $(PROFILE),--profile $(PROFILE),)
CFN_PROFILE_FLAG := $(if $(CLOUDFORMATION_PROFILE),--profile $(CLOUDFORMATION_PROFILE),)
CFN_AWS := aws $(CFN_PROFILE_FLAG) --region $(REGION)
ACCOUNT_ID ?= $(shell aws $(AWS_PROFILE_FLAG) --region $(REGION) sts get-caller-identity --query Account --output text 2>/dev/null)
CFN_ACCOUNT_ID ?= $(shell $(CFN_AWS) sts get-caller-identity --query Account --output text 2>/dev/null)
STACK_NAME := $(STACK_PREFIX)-$(REGION)
DEPLOY_BUCKET := $(STACK_PREFIX)-deploy-$(ACCOUNT_ID)
FRONTEND_BUCKET := $(STACK_PREFIX)-frontend-$(ACCOUNT_ID)
LEGACY_DEPENDENCY_BUILD_STACK := $(STACK_PREFIX)-dependencies-build
DEPENDENCY_SOURCE_KEY := layer-source/dependencies-layer-source.zip
DEPENDENCY_BUILD_HASH := $(shell python3 -c "import hashlib,pathlib; paths=('backend/layers/dependencies/requirements.txt','backend/layers/dependencies/buildspec.yml','infrastructure/nested-stacks/compute.yaml'); print(hashlib.sha256(b''.join(pathlib.Path(path).read_bytes() for path in paths)).hexdigest()[:16])")
DEPENDENCY_LAYER_S3_KEY := layers/stayos-dependencies-$(DEPENDENCY_BUILD_HASH).zip
# Audio bucket holds Polly-generated MP3 brief files. Must be emptied on destroy
# or CloudFormation cannot delete it (non-empty bucket -> DataStack DELETE_FAILED).
AUDIO_BUCKET := $(STACK_PREFIX)-audio-$(ACCOUNT_ID)
BEDROCK_MODEL_ID ?= us.anthropic.claude-sonnet-4-6
APP_PASSWORD ?=
ORCHESTRATED ?= 0
GATEWAY_DELETE_MAX_ATTEMPTS ?= 60
GATEWAY_DELETE_POLL_SECONDS ?= 5

step = $(if $(filter 1,$(ORCHESTRATED)),  - $(2),[$(1)] $(2))

.PHONY: all assert-account deploy ensure-buckets package-layer-source \
        remove-legacy-layer-build-stack package-backend package-infra \
        deploy-infra update-lambda-code write-frontend-env build-frontend \
        deploy-frontend invalidate-cdn preflight clean test lint \
        test-backend test-frontend \
        voice-build voice-deploy voice-destroy reseed \
        gateway-deploy gateway-destroy \
        chat-build chat-deploy chat-destroy generate-briefs \
        destroy

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

gateway-deploy voice-deploy chat-deploy deploy-frontend destroy: assert-account

deploy: assert-account preflight ensure-buckets package-backend deploy-infra update-lambda-code gateway-deploy voice-deploy chat-deploy write-frontend-env build-frontend deploy-frontend invalidate-cdn generate-briefs
	@if [ "$(ORCHESTRATED)" != "1" ]; then \
		echo ""; \
		echo "══════════════════════════════════════════════════════════════"; \
		echo "  LUMI deployment complete"; \
		echo "  Stack: $(STACK_NAME)  Account: $(ACCOUNT_ID)  Region: $(REGION)"; \
		echo "══════════════════════════════════════════════════════════════"; \
	fi

# ─── Pre-flight: Verify Bedrock model access ─────────────────
preflight:
	@if [ -z "$(APP_PASSWORD)" ]; then \
		echo ""; \
		echo "ERROR: APP_PASSWORD is not set."; \
		echo ""; \
		echo "This password protects the LUMI application login."; \
		echo "Set it in your environment or pass it to make:"; \
		echo ""; \
		echo "  export APP_PASSWORD=your-secure-password"; \
		echo "  make deploy"; \
		echo ""; \
		echo "  — or —"; \
		echo ""; \
		echo "  make deploy APP_PASSWORD=your-secure-password"; \
		echo ""; \
		exit 1; \
	fi
	@echo "$(call step,prerequisite,Verifying AgentCore CLI support...)"
	@ERR=$$(aws bedrock-agentcore-control list-gateways --region $(REGION) 2>&1 >/dev/null); \
	if [ $$? -ne 0 ]; then \
		if echo "$$ERR" | grep -qiE "Invalid choice|argument command|not.*subcommand|Unknown command|available commands"; then \
			echo "" && \
			echo "ERROR: AWS CLI does not support 'bedrock-agentcore-control'." && \
			echo "" && \
			echo "This subcommand requires AWS CLI version 2.27 or later." && \
			echo "" && \
			echo "To update your AWS CLI:" && \
			echo "" && \
			echo "  macOS:  brew upgrade awscli" && \
			echo "  Linux:  curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o 'awscliv2.zip' && unzip awscliv2.zip && sudo ./aws/install --update" && \
			echo "" && \
			echo "  Verify: aws --version" && \
			echo "" && \
			exit 1; \
		else \
			echo "" && \
			echo "ERROR: 'bedrock-agentcore-control list-gateways' failed (not a CLI-version issue):" && \
			echo "$$ERR" && \
			echo "" && \
			exit 1; \
		fi; \
	fi
	@echo "    done"
	@echo "$(call step,prerequisite,Verifying Bedrock model access ($(BEDROCK_MODEL_ID))...)"
	@aws bedrock-runtime converse \
		--model-id $(BEDROCK_MODEL_ID) \
		--messages '[{"role":"user","content":[{"text":"hi"}]}]' \
		--inference-config '{"maxTokens":1}' \
		--region $(REGION) > /dev/null 2>&1 \
	|| (echo "" && \
		echo "ERROR: Bedrock model $(BEDROCK_MODEL_ID) is not accessible." && \
		echo "" && \
		echo "To enable it, follow these steps:" && \
		echo "" && \
		echo "  1. Open the AWS Console in region $(REGION):" && \
		echo "     https://$(REGION).console.aws.amazon.com/bedrock/home?region=$(REGION)#/modelaccess" && \
		echo "" && \
		echo "  2. Click 'Manage model access' (top right)" && \
		echo "" && \
		echo "  3. Check the box next to 'Anthropic Claude Sonnet 4.6'" && \
		echo "     (under Anthropic > Claude)" && \
		echo "" && \
		echo "  4. Click 'Save changes' and wait for status to show 'Access granted'" && \
		echo "     (typically takes 1-2 minutes)" && \
		echo "" && \
		echo "  5. Re-run: make deploy" && \
		echo "" && \
		echo "Note: If your account uses an AWS Organization, model access may" && \
		echo "require approval from your org administrator." && \
		echo "" && \
		exit 1)
	@echo "    done"
	@echo "$(call step,prerequisite,Verifying Bedrock model access (amazon.nova-2-sonic-v1:0)...)"
	@aws bedrock get-foundation-model \
		--model-identifier amazon.nova-2-sonic-v1:0 \
		--region $(REGION) > /dev/null 2>&1 \
	|| (echo "" && \
		echo "ERROR: Amazon Nova Sonic (amazon.nova-2-sonic-v1:0) is not accessible." && \
		echo "" && \
		echo "To enable it, follow these steps:" && \
		echo "" && \
		echo "  1. Open the AWS Console in region $(REGION):" && \
		echo "     https://$(REGION).console.aws.amazon.com/bedrock/home?region=$(REGION)#/modelaccess" && \
		echo "" && \
		echo "  2. Click 'Manage model access' (top right)" && \
		echo "" && \
		echo "  3. Check the box next to 'Amazon Nova Sonic'" && \
		echo "     (under Amazon > Nova)" && \
		echo "" && \
		echo "  4. Click 'Save changes' and wait for status to show 'Access granted'" && \
		echo "     (typically takes 1-2 minutes)" && \
		echo "" && \
		echo "  5. Re-run: make deploy" && \
		echo "" && \
		echo "Note: If your account uses an AWS Organization, model access may" && \
		echo "require approval from your org administrator." && \
		echo "" && \
		exit 1)
	@echo "    done"

# ─── Step 1: Ensure S3 buckets exist ──────────────────────────
ensure-buckets:
	@echo "$(call step,1/8,Creating S3 buckets (if needed)...)"
	@aws s3api head-bucket --bucket $(DEPLOY_BUCKET) 2>/dev/null || \
		aws s3 mb s3://$(DEPLOY_BUCKET) --region $(REGION) > /dev/null
	@echo "    done"

# ─── Step 2: Package backend ──────────────────────────────────
# The dependency-layer input is prepared locally, but ComputeStack owns and
# invokes its Lambda-compatible CodeBuild project. This keeps the artifact
# Python 3.12 / Amazon Linux / x86_64 on every developer workstation without
# requiring local Docker or another CloudFormation stack.
package-layer-source: ensure-buckets
	@echo "  [layer] Packaging dependency build input..."
	@mkdir -p build
	@find build -maxdepth 1 -name dependencies-layer-source.zip -delete
	@cd backend/layers/dependencies && \
		zip -j ../../../build/dependencies-layer-source.zip \
			requirements.txt buildspec.yml > /dev/null
	@aws s3 cp build/dependencies-layer-source.zip \
		s3://$(DEPLOY_BUCKET)/$(DEPENDENCY_SOURCE_KEY) --quiet

remove-legacy-layer-build-stack:
	@STATUS=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(LEGACY_DEPENDENCY_BUILD_STACK) \
		--query 'Stacks[0].StackStatus' --output text 2>/dev/null || true); \
	if [ -n "$$STATUS" ] && [ "$$STATUS" != "None" ]; then \
		echo "  [layer] Removing legacy dependency-builder stack..." && \
		$(CFN_AWS) cloudformation delete-stack \
			--stack-name $(LEGACY_DEPENDENCY_BUILD_STACK) && \
		$(CFN_AWS) cloudformation wait stack-delete-complete \
			--stack-name $(LEGACY_DEPENDENCY_BUILD_STACK); \
	fi

package-backend: package-layer-source
	@echo "$(call step,2/8,Packaging backend...)"
	@mkdir -p build
	@cd backend/functions/api && zip -r ../../../build/stayos-api.zip . -x "*.pyc" "__pycache__/*" > /dev/null
	@cd backend/functions/orchestrator && zip -r ../../../build/stayos-orchestrator.zip . -x "*.pyc" "__pycache__/*" > /dev/null
	@cd backend/functions/seed-data && zip -r ../../../build/stayos-seed-data.zip . -x "*.pyc" "__pycache__/*" > /dev/null
	@cd backend/functions/tools && zip -r ../../../build/stayos-tools.zip . -x "*.pyc" "__pycache__/*" > /dev/null
	@aws s3 cp build/stayos-api.zip s3://$(DEPLOY_BUCKET)/functions/ --quiet
	@aws s3 cp build/stayos-orchestrator.zip s3://$(DEPLOY_BUCKET)/functions/ --quiet
	@aws s3 cp build/stayos-seed-data.zip s3://$(DEPLOY_BUCKET)/functions/ --quiet
	@aws s3 cp build/stayos-tools.zip s3://$(DEPLOY_BUCKET)/functions/ --quiet
	@echo "    done"

# ─── Step 3: Deploy infrastructure ────────────────────────────
package-infra: package-layer-source remove-legacy-layer-build-stack
	@$(CFN_AWS) cloudformation package \
		--template-file infrastructure/root-stack.yaml \
		--s3-bucket $(DEPLOY_BUCKET) \
		--s3-prefix cfn-templates \
		--output-template-file build/packaged-root-stack.yaml > /dev/null

deploy-infra: package-infra
	@echo "  [layer] ComputeStack will build or reuse the Python 3.12 x86_64 layer..."
	@echo "$(call step,3/8,Deploying infrastructure (this may take several minutes)...)"
	@$(CFN_AWS) cloudformation deploy \
		--stack-name $(STACK_NAME) \
		--template-file build/packaged-root-stack.yaml \
		--parameter-overrides \
			StackPrefix=$(STACK_PREFIX) \
			AccountId=$(ACCOUNT_ID) \
			BedrockModelId=$(BEDROCK_MODEL_ID) \
			DependenciesLayerS3Key=$(DEPENDENCY_LAYER_S3_KEY) \
			AppPassword=$(APP_PASSWORD) \
		--capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
		--no-fail-on-empty-changeset
	@echo "    done"

# ─── Step 4: Update Lambda code ───────────────────────────────
# CFN uses a static S3 key, so it can't detect zip content changes.
# Force a code update on every function after each deploy.
update-lambda-code:
	@echo "$(call step,4/8,Updating Lambda function code...)"
	@set -e; \
	log="build/lambda-update.log"; \
	: > "$$log"; \
	for spec in \
		"$(STACK_PREFIX)-api:functions/stayos-api.zip" \
		"$(STACK_PREFIX)-orchestrator:functions/stayos-orchestrator.zip" \
		"$(STACK_PREFIX)-seed-data:functions/stayos-seed-data.zip" \
		"$(STACK_PREFIX)-tools:functions/stayos-tools.zip"; do \
		fn="$${spec%%:*}"; key="$${spec#*:}"; \
		echo "  updating $$fn"; \
		if ! aws lambda update-function-code \
			--function-name "$$fn" \
			--s3-bucket $(DEPLOY_BUCKET) \
			--s3-key "$$key" \
			--region $(REGION) >> "$$log" 2>&1; then \
			echo "ERROR: required Lambda update failed for $$fn."; \
			echo "Last 40 log lines ($$log):"; tail -n 40 "$$log"; \
			echo "Retry: make update-lambda-code PROFILE=$(PROFILE) REGION=$(REGION) STACK_PREFIX=$(STACK_PREFIX)"; \
			exit 1; \
		fi; \
		if ! aws lambda wait function-updated-v2 \
			--function-name "$$fn" --region $(REGION) >> "$$log" 2>&1; then \
			echo "ERROR: Lambda $$fn did not become ready after its code update."; \
			echo "Last 40 log lines ($$log):"; tail -n 40 "$$log"; \
			exit 1; \
		fi; \
	done
	@echo "    done"

# ─── Step 5: Generate frontend config ─────────────────────────
write-frontend-env:
	@echo "$(call step,5/8,Generating frontend config from stack outputs...)"
	@API_URL=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='ApiGatewayUrl'].OutputValue" \
		--output text) && \
	POOL_ID=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
		--output text) && \
	CLIENT_ID=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
		--output text) && \
	IDENTITY_POOL_ID=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='VoiceIdentityPoolId'].OutputValue" \
		--output text) && \
	VOICE_RUNTIME_ARN="" && \
	VOICE_RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/voice/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	if [ "$$VOICE_RUNTIME_ID" != "None" ] && [ -n "$$VOICE_RUNTIME_ID" ]; then \
		VOICE_RUNTIME_ARN="arn:aws:bedrock-agentcore:$(REGION):$(ACCOUNT_ID):runtime/$$VOICE_RUNTIME_ID"; \
	fi && \
	CHAT_RUNTIME_ARN="" && \
	CHAT_RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/chat/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	if [ "$$CHAT_RUNTIME_ID" != "None" ] && [ -n "$$CHAT_RUNTIME_ID" ]; then \
		CHAT_RUNTIME_ARN="arn:aws:bedrock-agentcore:$(REGION):$(ACCOUNT_ID):runtime/$$CHAT_RUNTIME_ID"; \
	fi && \
	required() { \
		if [ -z "$$2" ] || [ "$$2" = "None" ]; then \
			echo "ERROR: required frontend value '$$1' was not resolved from $(STACK_NAME)."; exit 1; \
		fi; \
	} && \
	required ApiGatewayUrl "$$API_URL" && \
	required UserPoolId "$$POOL_ID" && \
	required UserPoolClientId "$$CLIENT_ID" && \
	required VoiceIdentityPoolId "$$IDENTITY_POOL_ID" && \
	required VoiceRuntimeArn "$$VOICE_RUNTIME_ARN" && \
	required ChatRuntimeArn "$$CHAT_RUNTIME_ARN" && \
	printf '%s\n' \
		"# Auto-generated by make deploy - do not edit manually" \
		"NEXT_PUBLIC_API_URL=$$API_URL" \
		"NEXT_PUBLIC_COGNITO_USER_POOL_ID=$$POOL_ID" \
		"NEXT_PUBLIC_COGNITO_CLIENT_ID=$$CLIENT_ID" \
		"NEXT_PUBLIC_COGNITO_REGION=$(REGION)" \
		"NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID=$$IDENTITY_POOL_ID" \
		"NEXT_PUBLIC_AWS_REGION=$(REGION)" \
		"NEXT_PUBLIC_AGENTCORE_RUNTIME_ARN=$$VOICE_RUNTIME_ARN" \
		"NEXT_PUBLIC_CHAT_RUNTIME_ARN=$$CHAT_RUNTIME_ARN" \
		> frontend/.env.production
	@echo "    done"

# ─── Step 6: Build frontend ───────────────────────────────────
build-frontend:
	@echo "$(call step,6/8,Building frontend...)"
	@cd frontend && npm ci --legacy-peer-deps && npm run build
	@echo "    done"

# ─── Step 7: Deploy frontend ──────────────────────────────────
# LUMI is served from /lumi/ on the shared StayOS bucket + CloudFront
# (shell owns "/", PULSE owns /pulse/). --delete is scoped to /lumi/,
# so it never touches the shell or PULSE assets.
deploy-frontend:
	@echo "$(call step,7/8,Deploying frontend to S3 (/lumi prefix)...)"
	@aws s3 sync frontend/out/ s3://$(FRONTEND_BUCKET)/lumi/ --delete --quiet
	@echo "    done"

# ─── Step 8: Invalidate CDN ───────────────────────────────────
# Only invalidate /lumi/* so a LUMI redeploy leaves the shell (root)
# and PULSE cache entries on the shared distribution intact.
invalidate-cdn:
	@echo "$(call step,8/8,Invalidating CDN cache (/lumi/*)...)"
	@aws cloudfront create-invalidation \
		--distribution-id $$($(CFN_AWS) cloudformation describe-stacks \
			--stack-name $(STACK_NAME) \
			--query "Stacks[0].Outputs[?OutputKey=='FrontendDistributionId'].OutputValue" \
			--output text) \
		--paths "/lumi/*" > /dev/null
	@echo "    done"

# ─── Testing ──────────────────────────────────────────────────
test: test-backend test-frontend
	@echo "All tests passed"

test-backend:
	@echo "Running backend tests..."
	@cd backend && python3 -m pytest tests/ -v

test-frontend:
	@echo "Running frontend tests..."
	@cd frontend && npm test

lint:
	@echo "Linting backend..."
	@cd backend && python3 -m flake8 functions/ --max-line-length 120
	@cd backend && python3 -m mypy functions/ --ignore-missing-imports
	@echo "Linting frontend..."
	@cd frontend && npm run lint

# ─── Generate AI Briefs ───────────────────────────────────────
# Triggers the orchestrator to generate today's brief for all GMs.
# Async (fire-and-forget); ~2-3 min per property. Enriches the CFN
# seed's synthetic briefs with real AI narratives, Polly audio, VIP data.
generate-briefs:
	@echo "$(call step,async,Generating AI briefs for all properties...)"
	@aws lambda invoke \
		--function-name $(STACK_PREFIX)-orchestrator \
		--invocation-type Event \
		--cli-binary-format raw-in-base64-out \
		--payload '{"source":"scheduler","action":"generate-all"}' \
		--region $(REGION) \
		/tmp/orchestrator-async.json > /dev/null 2>&1 && \
	echo "    done (briefs will be ready in ~2-3 minutes)"

# ─── AgentCore Gateway Deploy ──────────────────────────────────
# Creates/updates the AgentCore Gateway exposing the Tool Lambda's
# read-only hotel ops tools as MCP tools. IAM-only auth, MCP response
# streaming on. Gateway ID + endpoint URL tracked in SSM. No CFN
# resource type exists yet, so the Gateway and its IAM role are CLI-managed.
#
# Usage:
#   make gateway-deploy   # create/update Gateway + register Lambda target

GATEWAY_NAME := $(STACK_PREFIX)-gateway
GATEWAY_TARGET_NAME := tools
GATEWAY_ROLE_NAME := $(STACK_PREFIX)-gateway-role-$(REGION)

gateway-deploy:
	@echo "  [gateway] Fetching Tool Lambda ARN and WAF WebACL ARN from stack outputs..."
	@TOOL_LAMBDA_ARN=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='ToolLambdaArn'].OutputValue" \
		--output text) && \
	WAF_WEB_ACL_ARN=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='GatewayWafWebAclArn'].OutputValue" \
		--output text) && \
	echo "  [gateway] Ensuring Gateway IAM role exists ($(GATEWAY_ROLE_NAME))..." && \
	if aws iam get-role --role-name $(GATEWAY_ROLE_NAME) > /dev/null 2>&1; then \
		echo "  [gateway] Role already exists"; \
	else \
		aws iam create-role \
			--role-name $(GATEWAY_ROLE_NAME) \
			--assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},"Action":"sts:AssumeRole"}]}' > /dev/null && \
		echo "  [gateway] Role created - waiting for IAM propagation..." && \
		sleep 10; \
	fi && \
	GATEWAY_ROLE_ARN=$$(aws iam get-role --role-name $(GATEWAY_ROLE_NAME) --query 'Role.Arn' --output text) && \
	aws iam put-role-policy \
		--role-name $(GATEWAY_ROLE_NAME) \
		--policy-name InvokeToolLambda \
		--policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"$$TOOL_LAMBDA_ARN\"}]}" > /dev/null && \
	echo "  [gateway] Checking for existing Gateway..." && \
	if GATEWAY_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/gateway/gateway-id" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	   [ "$$GATEWAY_ID" != "None" ] && [ -n "$$GATEWAY_ID" ]; then \
		echo "  [gateway] Updating existing Gateway: $$GATEWAY_ID" && \
		aws bedrock-agentcore-control update-gateway \
			--gateway-identifier "$$GATEWAY_ID" \
			--name "$(GATEWAY_NAME)" \
			--role-arn "$$GATEWAY_ROLE_ARN" \
			--protocol-type MCP \
			--protocol-configuration '{"mcp":{"streamingConfiguration":{"enableResponseStreaming":true}}}' \
			--authorizer-type AWS_IAM \
			--exception-level DEBUG > /dev/null ; \
	else \
		echo "  [gateway] Creating new Gateway..." && \
		GATEWAY_ID=$$(aws bedrock-agentcore-control create-gateway \
			--name "$(GATEWAY_NAME)" \
			--description "StayOS AgentCore Gateway - MCP tool endpoint for hotel operations data" \
			--role-arn "$$GATEWAY_ROLE_ARN" \
			--protocol-type MCP \
			--protocol-configuration '{"mcp":{"streamingConfiguration":{"enableResponseStreaming":true}}}' \
			--authorizer-type AWS_IAM \
			--exception-level DEBUG \
			--query 'gatewayId' --output text) && \
		aws ssm put-parameter \
			--name "/$(STACK_PREFIX)/gateway/gateway-id" \
			--value "$$GATEWAY_ID" \
			--type String \
			--overwrite > /dev/null && \
		echo "  [gateway] Gateway created: $$GATEWAY_ID" && \
		echo "  [gateway] Stored in SSM: /$(STACK_PREFIX)/gateway/gateway-id" ; \
	fi && \
	GATEWAY_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/gateway/gateway-id" \
		--query "Parameter.Value" --output text) && \
	echo "  [gateway] Waiting for Gateway READY status..." && \
	while true; do \
		STATUS=$$(aws bedrock-agentcore-control get-gateway \
			--gateway-identifier "$$GATEWAY_ID" \
			--query 'status' --output text); \
		if [ "$$STATUS" = "READY" ]; then \
			echo "  [gateway] Gateway is READY"; \
			break; \
		elif [ "$$STATUS" = "FAILED" ] || [ "$$STATUS" = "UPDATE_UNSUCCESSFUL" ]; then \
			echo "" && \
			echo "  ERROR: AgentCore Gateway entered $$STATUS state." && \
			echo "" && \
			exit 1; \
		else \
			sleep 10; \
		fi; \
	done && \
	GATEWAY_URL=$$(aws bedrock-agentcore-control get-gateway \
		--gateway-identifier "$$GATEWAY_ID" --query 'gatewayUrl' --output text) && \
	GATEWAY_ARN=$$(aws bedrock-agentcore-control get-gateway \
		--gateway-identifier "$$GATEWAY_ID" --query 'gatewayArn' --output text) && \
	aws ssm put-parameter \
		--name "/$(STACK_PREFIX)/gateway/endpoint-url" \
		--value "$$GATEWAY_URL" \
		--type String \
		--overwrite > /dev/null && \
	echo "  [gateway] Endpoint URL stored in SSM: /$(STACK_PREFIX)/gateway/endpoint-url" && \
	echo "  [gateway] Reading tool schema and registering Gateway Target..." && \
	TARGET_CONFIG=$$(python3 -c "import json, sys; tools = json.load(open('backend/tools/tool-schema.json'))['tools']; print(json.dumps({'mcp': {'lambda': {'lambdaArn': sys.argv[1], 'toolSchema': {'inlinePayload': tools}}}}))" "$$TOOL_LAMBDA_ARN") && \
	TARGET_ID=$$(aws bedrock-agentcore-control list-gateway-targets \
		--gateway-identifier "$$GATEWAY_ID" \
		--query "items[?name=='$(GATEWAY_TARGET_NAME)'].targetId | [0]" --output text) && \
	if [ "$$TARGET_ID" != "None" ] && [ -n "$$TARGET_ID" ]; then \
		echo "  [gateway] Updating existing Gateway Target: $$TARGET_ID" && \
		aws bedrock-agentcore-control update-gateway-target \
			--gateway-identifier "$$GATEWAY_ID" \
			--target-id "$$TARGET_ID" \
			--name "$(GATEWAY_TARGET_NAME)" \
			--target-configuration "$$TARGET_CONFIG" \
			--credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' > /dev/null ; \
	else \
		echo "  [gateway] Registering Tool Lambda as Gateway Target..." && \
		TARGET_ID="" && \
		ATTEMPT=1 && \
		while [ $$ATTEMPT -le 6 ]; do \
			CREATE_ERR=$$(aws bedrock-agentcore-control create-gateway-target \
				--gateway-identifier "$$GATEWAY_ID" \
				--name "$(GATEWAY_TARGET_NAME)" \
				--description "LUMI read-only hotel operations tools (Tool Lambda)" \
				--target-configuration "$$TARGET_CONFIG" \
				--credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
				--query 'targetId' --output text 2>&1) && \
			{ TARGET_ID="$$CREATE_ERR"; break; } || \
			if echo "$$CREATE_ERR" | grep -q "lacks permission to invoke Lambda"; then \
				echo "  [gateway] IAM policy not yet propagated (attempt $$ATTEMPT/6); retrying in 15s..." && \
				sleep 15 && \
				ATTEMPT=$$((ATTEMPT+1)); \
			else \
				echo "$$CREATE_ERR" && exit 1; \
			fi; \
		done && \
		if [ -z "$$TARGET_ID" ] || [ "$$TARGET_ID" = "None" ]; then \
			echo "  ERROR: Gateway Target creation failed after retries (IAM propagation)." && exit 1; \
		fi && \
		echo "  [gateway] Target created: $$TARGET_ID" ; \
	fi && \
	echo "  [gateway] Waiting for Gateway Target READY status..." && \
	while true; do \
		TSTATUS=$$(aws bedrock-agentcore-control get-gateway-target \
			--gateway-identifier "$$GATEWAY_ID" \
			--target-id "$$TARGET_ID" \
			--query 'status' --output text); \
		if [ "$$TSTATUS" = "READY" ]; then \
			echo "  [gateway] Gateway Target is READY"; \
			break; \
		elif [ "$$TSTATUS" = "FAILED" ] || [ "$$TSTATUS" = "UPDATE_UNSUCCESSFUL" ] || [ "$$TSTATUS" = "SYNCHRONIZE_UNSUCCESSFUL" ]; then \
			echo "" && \
			echo "  ERROR: Gateway Target entered $$TSTATUS state." && \
			echo "" && \
			exit 1; \
		else \
			sleep 10; \
		fi; \
	done && \
	echo "  [gateway] Associating WAF WebACL with Gateway..." && \
	if aws wafv2 associate-web-acl \
		--web-acl-arn "$$WAF_WEB_ACL_ARN" \
		--resource-arn "$$GATEWAY_ARN" > /dev/null 2>&1; then \
		echo "  [gateway] WAF WebACL associated"; \
	else \
		echo ""; \
		echo "  WARNING: Could not associate WAF WebACL with the Gateway (commonly"; \
		echo "  WAFUnavailableEntityException). This has been observed as an AWS-side gap in the"; \
		echo "  WAF <-> AgentCore Gateway integration rather than a config issue on our end - it is"; \
		echo "  not fixed by waiting/retrying immediately. Continuing without failing the deploy:"; \
		echo "  per AWS docs, a Gateway with no web ACL associated has zero WAF overhead and simply"; \
		echo "  runs without that extra protection layer in the meantime."; \
		echo ""; \
		echo "  Retry manually once resolved with:"; \
		echo "    aws wafv2 associate-web-acl --web-acl-arn \"$$WAF_WEB_ACL_ARN\" --resource-arn \"$$GATEWAY_ARN\""; \
		echo ""; \
	fi && \
	echo "  [gateway] Gateway ID:  $$GATEWAY_ID" && \
	echo "  [gateway] Gateway URL: $$GATEWAY_URL"

# ─── AgentCore Gateway Destroy ─────────────────────────────────
# Disassociates WAF, deletes all Targets, deletes the Gateway, removes
# SSM params. The CLI-managed IAM role is left in place so a later
# gateway-deploy can reuse it.
#
# Usage:
#   make gateway-destroy   # tear down the AgentCore Gateway only

gateway-destroy:
	@set -e; \
	GATEWAY_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/gateway/gateway-id" \
		--query "Parameter.Value" --output text 2>/dev/null || true); \
	if [ "$$GATEWAY_ID" != "None" ] && [ -n "$$GATEWAY_ID" ]; then \
		GATEWAY_ARN=$$(aws bedrock-agentcore-control get-gateway \
			--gateway-identifier "$$GATEWAY_ID" \
			--query 'gatewayArn' --output text 2>/dev/null || true); \
		if [ -n "$$GATEWAY_ARN" ] && [ "$$GATEWAY_ARN" != "None" ]; then \
			echo "  [gateway] Disassociating WAF WebACL from Gateway..." && \
			aws wafv2 disassociate-web-acl --resource-arn "$$GATEWAY_ARN" > /dev/null 2>&1 || true; \
		fi; \
		echo "  [gateway] Deleting Gateway Targets..."; \
		TARGET_IDS=$$(aws bedrock-agentcore-control list-gateway-targets \
			--gateway-identifier "$$GATEWAY_ID" \
			--query 'items[].targetId' --output text); \
		for TARGET_ID in $$TARGET_IDS; do \
			aws bedrock-agentcore-control delete-gateway-target \
				--gateway-identifier "$$GATEWAY_ID" \
				--target-id "$$TARGET_ID" > /dev/null; \
		done; \
		echo "  [gateway] Waiting for all Gateway Targets to be removed..."; \
		ATTEMPT=1; \
		while true; do \
			REMAINING=$$(aws bedrock-agentcore-control list-gateway-targets \
				--gateway-identifier "$$GATEWAY_ID" \
				--query 'length(items)' --output text); \
			if [ "$$REMAINING" = "0" ]; then \
				echo "  [gateway] Verified all Gateway Targets are removed"; \
				break; \
			fi; \
			if [ "$$ATTEMPT" -ge "$(GATEWAY_DELETE_MAX_ATTEMPTS)" ]; then \
				echo "ERROR: $$REMAINING Gateway Target(s) remain after waiting."; \
				echo "Retry: make gateway-destroy PROFILE=$(PROFILE) REGION=$(REGION) STACK_PREFIX=$(STACK_PREFIX)"; \
				exit 1; \
			fi; \
			sleep $(GATEWAY_DELETE_POLL_SECONDS); \
			ATTEMPT=$$((ATTEMPT + 1)); \
		done; \
		echo "  [gateway] Deleting Gateway: $$GATEWAY_ID"; \
		aws bedrock-agentcore-control delete-gateway \
			--gateway-identifier "$$GATEWAY_ID" > /dev/null; \
		echo "  [gateway] Waiting for Gateway deletion to complete..."; \
		ATTEMPT=1; \
		while true; do \
			if GATEWAY_STATUS=$$(aws bedrock-agentcore-control get-gateway \
				--gateway-identifier "$$GATEWAY_ID" \
				--query 'status' --output text 2>&1); then \
				if [ "$$ATTEMPT" -ge "$(GATEWAY_DELETE_MAX_ATTEMPTS)" ]; then \
					echo "ERROR: Gateway is still $$GATEWAY_STATUS after waiting."; \
					echo "Retry: make gateway-destroy PROFILE=$(PROFILE) REGION=$(REGION) STACK_PREFIX=$(STACK_PREFIX)"; \
					exit 1; \
				fi; \
				sleep $(GATEWAY_DELETE_POLL_SECONDS); \
				ATTEMPT=$$((ATTEMPT + 1)); \
			else \
				case "$$GATEWAY_STATUS" in \
					*ResourceNotFoundException*) \
						echo "  [gateway] Verified Gateway deletion"; \
						break ;; \
					*) \
						echo "ERROR: Unable to verify Gateway deletion:"; \
						echo "$$GATEWAY_STATUS"; \
						exit 1 ;; \
				esac; \
			fi; \
		done; \
		aws ssm delete-parameters \
			--names "/$(STACK_PREFIX)/gateway/gateway-id" \
				"/$(STACK_PREFIX)/gateway/endpoint-url" > /dev/null; \
		echo "  [gateway] Gateway deleted and SSM parameters removed"; \
	else \
		echo "  [gateway] No Gateway ID found in SSM - nothing to delete"; \
	fi

# ─── Voice Agent Build (CodeBuild) ────────────────────────────
# Packages voice agent source, uploads to S3, and runs a remote ARM64
# container build via CodeBuild (Finch/CodeBuild, not Docker).
# CodeBuild pushes to ECR with :latest + timestamped tag.
#
# Usage:
#   make voice-build    # trigger remote ARM64 container build

VOICE_BUILD_PROJECT := $(STACK_PREFIX)-voice-agent-build
VOICE_ECR_URI := $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/$(STACK_PREFIX)-voice-agent

voice-build:
	@echo "  [voice] Packaging voice agent source..."
	@mkdir -p build
	@cd backend/services/voice-agent && zip -r ../../../build/voice-agent-source.zip . \
		-x "tests/*" "__pycache__/*" "*.pyc" "agentcore/*" ".pytest_cache/*" > /dev/null
	@echo "  [voice] Uploading source to S3..."
	@aws s3 cp build/voice-agent-source.zip \
		s3://$(DEPLOY_BUCKET)/voice-agent-source/voice-agent-source.zip --quiet
	@echo "  [voice] Starting CodeBuild (ARM64 container build)..."
	@BUILD_ID=$$(aws codebuild start-build \
		--project-name $(VOICE_BUILD_PROJECT) \
		--query 'build.id' --output text) && \
	echo "  [voice] Build started: $$BUILD_ID" && \
	echo "  [voice] Waiting for build to complete..." && \
	while true; do \
		STATUS=$$(aws codebuild batch-get-builds --ids $$BUILD_ID \
			--query 'builds[0].buildStatus' --output text); \
		if [ "$$STATUS" = "SUCCEEDED" ]; then \
			echo "  [voice] Build succeeded"; \
			echo "  [voice] Image: $(VOICE_ECR_URI):latest"; \
			break; \
		elif [ "$$STATUS" = "IN_PROGRESS" ]; then \
			sleep 10; \
		else \
			LOG_URL=$$(aws codebuild batch-get-builds --ids $$BUILD_ID \
				--query 'builds[0].logs.deepLink' --output text); \
			echo ""; \
			echo "  CodeBuild failed with status: $$STATUS"; \
			echo ""; \
			echo "  View build logs: $$LOG_URL"; \
			echo ""; \
			exit 1; \
		fi; \
	done

# ─── Voice Agent Deploy (AgentCore Runtime) ───────────────────
# Creates the runtime on first deploy; later deploys update the
# container URI. State tracked in SSM at /${StackPrefix}/voice/runtime-id.
#
# Usage:
#   make voice-deploy   # build + deploy to AgentCore

voice-deploy: voice-build
	@echo "  [voice] Deploying to AgentCore Runtime..."
	@ROLE_ARN=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='VoiceAgentCoreRoleArn'].OutputValue" \
		--output text) && \
	SETTINGS_TABLE="$(STACK_PREFIX)-settings" && \
	USER_POOL_ID=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
		--output text) && \
	if RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/voice/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	   [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [voice] Updating existing runtime: $$RUNTIME_ID" && \
		aws bedrock-agentcore-control update-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(VOICE_ECR_URI):latest\"}}" \
			--environment-variables "{\"SETTINGS_TABLE_NAME\":\"$$SETTINGS_TABLE\",\"RESERVATIONS_TABLE_NAME\":\"$(STACK_PREFIX)-reservations\",\"ROOMS_TABLE_NAME\":\"$(STACK_PREFIX)-rooms\",\"GUESTS_TABLE_NAME\":\"$(STACK_PREFIX)-guests\",\"REVENUES_TABLE_NAME\":\"$(STACK_PREFIX)-revenues\",\"WORK_ORDERS_TABLE_NAME\":\"$(STACK_PREFIX)-work-orders\",\"BRIEFS_TABLE_NAME\":\"$(STACK_PREFIX)-briefs\",\"COGNITO_USER_POOL_ID\":\"$$USER_POOL_ID\",\"AWS_DEFAULT_REGION\":\"$(REGION)\",\"UNIFIED_TRACES_DESTINATION_ENABLED\":\"true\"}" ; \
	else \
		echo "  [voice] Creating new AgentCore Runtime..." && \
		RUNTIME_ID=$$(aws bedrock-agentcore-control create-agent-runtime \
			--agent-runtime-name "$(STACK_PREFIX)_voice_agent" \
			--description "LUMI Voice Agent - Nova Sonic bidirectional streaming" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(VOICE_ECR_URI):latest\"}}" \
			--environment-variables "{\"SETTINGS_TABLE_NAME\":\"$$SETTINGS_TABLE\",\"RESERVATIONS_TABLE_NAME\":\"$(STACK_PREFIX)-reservations\",\"ROOMS_TABLE_NAME\":\"$(STACK_PREFIX)-rooms\",\"GUESTS_TABLE_NAME\":\"$(STACK_PREFIX)-guests\",\"REVENUES_TABLE_NAME\":\"$(STACK_PREFIX)-revenues\",\"WORK_ORDERS_TABLE_NAME\":\"$(STACK_PREFIX)-work-orders\",\"BRIEFS_TABLE_NAME\":\"$(STACK_PREFIX)-briefs\",\"COGNITO_USER_POOL_ID\":\"$$USER_POOL_ID\",\"AWS_DEFAULT_REGION\":\"$(REGION)\",\"UNIFIED_TRACES_DESTINATION_ENABLED\":\"true\"}" \
			--query 'agentRuntimeId' --output text) && \
		aws ssm put-parameter \
			--name "/$(STACK_PREFIX)/voice/runtime-id" \
			--value "$$RUNTIME_ID" \
			--type String \
			--overwrite > /dev/null && \
		echo "  [voice] Runtime created: $$RUNTIME_ID" && \
		echo "  [voice] Stored in SSM: /$(STACK_PREFIX)/voice/runtime-id" ; \
	fi && \
	RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/voice/runtime-id" \
		--query "Parameter.Value" --output text) && \
	echo "  [voice] Waiting for runtime READY status..." && \
	while true; do \
		STATUS=$$(aws bedrock-agentcore-control get-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" \
			--query 'status' --output text); \
		if [ "$$STATUS" = "READY" ]; then \
			echo "  [voice] Runtime is READY"; \
			echo "  [voice] Runtime ID: $$RUNTIME_ID"; \
			break; \
		elif [ "$$STATUS" = "FAILED" ]; then \
			echo ""; \
			echo "  ERROR: AgentCore Runtime entered FAILED state."; \
			echo ""; \
			echo "  Check CloudWatch logs: /$(STACK_PREFIX)/agentcore/voice-agent"; \
			echo ""; \
			exit 1; \
		else \
			sleep 10; \
		fi; \
	done

# ─── Voice Agent Destroy (AgentCore Runtime) ──────────────────
# Deletes the runtime and SSM param. CFN handles ECR + CodeBuild deletion.
#
# Usage:
#   make voice-destroy   # tear down AgentCore Runtime only

voice-destroy:
	@RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/voice/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null); \
	if [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [voice] Deleting AgentCore Runtime: $$RUNTIME_ID" && \
		aws bedrock-agentcore-control delete-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" && \
		aws ssm delete-parameter \
			--name "/$(STACK_PREFIX)/voice/runtime-id" > /dev/null && \
		echo "  [voice] Runtime deleted and SSM parameter removed"; \
	else \
		echo "  [voice] No runtime ID found in SSM - nothing to delete"; \
	fi

# ─── Chat Agent Build (CodeBuild) ─────────────────────────────
# Packages chat agent source, uploads to S3, and runs a remote ARM64
# container build via CodeBuild (Finch/CodeBuild, not Docker).
# CodeBuild pushes to ECR with :latest + timestamped tag.
#
# Usage:
#   make chat-build    # trigger remote ARM64 container build

CHAT_BUILD_PROJECT := $(STACK_PREFIX)-chat-agent-build
CHAT_ECR_URI := $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/$(STACK_PREFIX)-chat-agent

chat-build:
	@echo "  [chat] Packaging chat agent source..."
	@mkdir -p build
	@cd backend/services/chat-agent && zip -r ../../../build/chat-agent-source.zip . \
		-x "tests/*" "__pycache__/*" "*.pyc" "agentcore/*" ".pytest_cache/*" > /dev/null
	@echo "  [chat] Uploading source to S3..."
	@aws s3 cp build/chat-agent-source.zip \
		s3://$(DEPLOY_BUCKET)/chat-agent-source/chat-agent-source.zip --quiet
	@echo "  [chat] Starting CodeBuild (ARM64 container build)..."
	@BUILD_ID=$$(aws codebuild start-build \
		--project-name $(CHAT_BUILD_PROJECT) \
		--query 'build.id' --output text) && \
	echo "  [chat] Build started: $$BUILD_ID" && \
	echo "  [chat] Waiting for build to complete..." && \
	while true; do \
		STATUS=$$(aws codebuild batch-get-builds --ids $$BUILD_ID \
			--query 'builds[0].buildStatus' --output text); \
		if [ "$$STATUS" = "SUCCEEDED" ]; then \
			echo "  [chat] Build succeeded"; \
			echo "  [chat] Image: $(CHAT_ECR_URI):latest"; \
			break; \
		elif [ "$$STATUS" = "IN_PROGRESS" ]; then \
			sleep 10; \
		else \
			LOG_URL=$$(aws codebuild batch-get-builds --ids $$BUILD_ID \
				--query 'builds[0].logs.deepLink' --output text); \
			echo ""; \
			echo "  CodeBuild failed with status: $$STATUS"; \
			echo ""; \
			echo "  View build logs: $$LOG_URL"; \
			echo ""; \
			exit 1; \
		fi; \
	done

# ─── Chat Agent Deploy (AgentCore Runtime) ────────────────────
# Creates the runtime on first deploy; later deploys update the
# container URI. State tracked in SSM at /${StackPrefix}/chat/runtime-id.
#
# Usage:
#   make chat-deploy   # build + deploy to AgentCore

chat-deploy: chat-build
	@echo "  [chat] Deploying to AgentCore Runtime..."
	@ROLE_ARN=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='ChatAgentCoreRoleArn'].OutputValue" \
		--output text) && \
	GATEWAY_ENDPOINT_URL=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/gateway/endpoint-url" \
		--query "Parameter.Value" --output text) && \
	USER_POOL_ID=$$($(CFN_AWS) cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
		--output text) && \
	if RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/chat/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null) && \
	   [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [chat] Updating existing runtime: $$RUNTIME_ID" && \
		aws bedrock-agentcore-control update-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(CHAT_ECR_URI):latest\"}}" \
			--environment-variables "{\"GATEWAY_ENDPOINT_URL\":\"$$GATEWAY_ENDPOINT_URL\",\"COGNITO_USER_POOL_ID\":\"$$USER_POOL_ID\",\"AWS_DEFAULT_REGION\":\"$(REGION)\"}" ; \
	else \
		echo "  [chat] Creating new AgentCore Runtime..." && \
		RUNTIME_ID=$$(aws bedrock-agentcore-control create-agent-runtime \
			--agent-runtime-name "$(STACK_PREFIX)_chat_agent" \
			--description "LUMI Chat Agent - text-based conversational agent via AgentCore Gateway" \
			--role-arn "$$ROLE_ARN" \
			--network-configuration '{"networkMode":"PUBLIC"}' \
			--agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"$(CHAT_ECR_URI):latest\"}}" \
			--environment-variables "{\"GATEWAY_ENDPOINT_URL\":\"$$GATEWAY_ENDPOINT_URL\",\"COGNITO_USER_POOL_ID\":\"$$USER_POOL_ID\",\"AWS_DEFAULT_REGION\":\"$(REGION)\"}" \
			--query 'agentRuntimeId' --output text) && \
		aws ssm put-parameter \
			--name "/$(STACK_PREFIX)/chat/runtime-id" \
			--value "$$RUNTIME_ID" \
			--type String \
			--overwrite > /dev/null && \
		echo "  [chat] Runtime created: $$RUNTIME_ID" && \
		echo "  [chat] Stored in SSM: /$(STACK_PREFIX)/chat/runtime-id" ; \
	fi && \
	RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/chat/runtime-id" \
		--query "Parameter.Value" --output text) && \
	echo "  [chat] Waiting for runtime READY status..." && \
	while true; do \
		STATUS=$$(aws bedrock-agentcore-control get-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" \
			--query 'status' --output text); \
		if [ "$$STATUS" = "READY" ]; then \
			echo "  [chat] Runtime is READY"; \
			echo "  [chat] Runtime ID: $$RUNTIME_ID"; \
			break; \
		elif [ "$$STATUS" = "FAILED" ]; then \
			echo ""; \
			echo "  ERROR: AgentCore Runtime entered FAILED state."; \
			echo ""; \
			echo "  Check CloudWatch logs: /$(STACK_PREFIX)/agentcore/chat-agent"; \
			echo ""; \
			exit 1; \
		else \
			sleep 10; \
		fi; \
	done

# ─── Chat Agent Destroy (AgentCore Runtime) ───────────────────
# Deletes the runtime and SSM param. CFN handles ECR + CodeBuild deletion.
#
# Usage:
#   make chat-destroy   # tear down AgentCore Runtime only

chat-destroy:
	@RUNTIME_ID=$$(aws ssm get-parameter \
		--name "/$(STACK_PREFIX)/chat/runtime-id" \
		--query "Parameter.Value" --output text 2>/dev/null); \
	if [ "$$RUNTIME_ID" != "None" ] && [ -n "$$RUNTIME_ID" ]; then \
		echo "  [chat] Deleting AgentCore Runtime: $$RUNTIME_ID" && \
		aws bedrock-agentcore-control delete-agent-runtime \
			--agent-runtime-id "$$RUNTIME_ID" && \
		aws ssm delete-parameter \
			--name "/$(STACK_PREFIX)/chat/runtime-id" > /dev/null && \
		echo "  [chat] Runtime deleted and SSM parameter removed"; \
	else \
		echo "  [chat] No runtime ID found in SSM - nothing to delete"; \
	fi

# ─── Re-seed Data ─────────────────────────────────────────────
# Clears all dataset tables and regenerates 30 days of fresh mock data.
# Safe to run anytime — stale data is replaced.
reseed:
	@echo ""
	@echo "╔══════════════════════════════════════════════╗"
	@echo "║         Re-seeding StayOS mock data         ║"
	@echo "╚══════════════════════════════════════════════╝"
	@echo ""
	@echo "  [1/3] Invoking seed-data Lambda (Force mode)..."
	@echo "        This clears all 5 dataset tables and regenerates 30 days"
	@echo "        of hotel operations data (~24,500 items across 5 properties)."
	@echo ""
	@echo "        Tables being cleared:"
	@echo "          • lumi-reservations"
	@echo "          • lumi-rooms"
	@echo "          • lumi-guests"
	@echo "          • lumi-revenues"
	@echo "          • lumi-work-orders"
	@echo ""
	@echo "        Invoking Lambda: $(STACK_PREFIX)-seed-data..."
	@aws lambda invoke \
		--function-name $(STACK_PREFIX)-seed-data \
		--cli-binary-format raw-in-base64-out \
		--payload '{"RequestType":"Create","ResourceProperties":{},"Force":true}' \
		--region $(REGION) \
		/tmp/reseed-response.json > /tmp/reseed-invoke.json
	@STATUS=$$(python3 -c "import json; r=json.load(open('/tmp/reseed-invoke.json')); print(r.get('FunctionError','SUCCESS'))"); \
	if [ "$$STATUS" = "SUCCESS" ]; then \
		echo "  ✅ Seed data regenerated successfully"; \
		echo ""; \
		echo "  [2/3] Generating AI briefs for all GMs..."; \
		echo "        Invoking Lambda: $(STACK_PREFIX)-orchestrator"; \
		echo "        Payload: {action: generate-all}"; \
		echo "        This generates Bedrock narratives + Polly audio for each GM."; \
		echo "        (async — runs in background, takes ~2-3 min per property)"; \
		echo ""; \
		aws lambda invoke \
			--function-name $(STACK_PREFIX)-orchestrator \
			--invocation-type Event \
			--cli-binary-format raw-in-base64-out \
			--payload '{"source":"scheduler","action":"generate-all"}' \
			--region $(REGION) \
			/tmp/orchestrator-response.json > /dev/null 2>&1; \
		echo "  ✅ Orchestrator invoked (async — briefs ready in ~2-3 min)"; \
		echo ""; \
		echo "  [3/3] Summary"; \
		echo ""; \
		echo "        Tables refreshed:"; \
		echo "          • lumi-rooms          (~1,840 items)"; \
		echo "          • lumi-guests         (~250 items)"; \
		echo "          • lumi-revenues       (~150 items)"; \
		echo "          • lumi-reservations   (~21,500 items)"; \
		echo "          • lumi-work-orders    (~750 items)"; \
		echo ""; \
		echo "        Coverage: today-29 through today+1 (30 days)"; \
		echo "        Briefs:  generating async for 5 properties"; \
		echo ""; \
		echo "  ✅ Done — reload the dashboard in 2-3 minutes for fresh briefs."; \
	else \
		echo "  ❌ Seed failed: $$STATUS"; \
		echo ""; \
		echo "  Lambda response:"; \
		cat /tmp/reseed-response.json; \
	fi
	@echo ""

# ─── Cleanup ──────────────────────────────────────────────────
clean:
	@rm -rf build/ frontend/out/ frontend/.next/ backend/layers/dependencies/python/
	@echo "Clean"

# ─── Full Teardown (single command) ───────────────────────────
# Tears down ALL LUMI infra in reverse of `deploy`. See
# docs/deployment-pipeline.md for the full flow. Key ordering gotchas:
#   - CLI-managed AgentCore Runtimes (voice, chat) + Gateway go first;
#     they are not in the CFN stack and hold refs to its IAM roles/ECR.
#   - Empty the shared frontend bucket and deploy bucket; a non-empty
#     bucket blocks delete-stack (AUDIO_BUCKET too, or DataStack DELETE_FAILED).
#   - Purge voice + chat ECR repos; a repo with images blocks its own delete.
#   - delete-stack last, then wait so callers know teardown finished.
#
# WARNING: destructive and irreversible. This deletes the shared StayOS
# bucket + CloudFront + Cognito user pool that PULSE and the shell also
# depend on — the LUMI-owned shared foundation is destroyed last, so this
# MUST run last in a platform teardown.
#
# Usage:
#   make destroy   # tear down all of LUMI (invoked last by root destroy-all)
destroy: voice-destroy chat-destroy gateway-destroy
	@$(MAKE) remove-legacy-layer-build-stack \
		PROFILE=$(PROFILE) CLOUDFORMATION_PROFILE=$(CLOUDFORMATION_PROFILE) \
		REGION=$(REGION) STACK_PREFIX=$(STACK_PREFIX)
	@echo "  [lumi] Emptying frontend bucket ($(FRONTEND_BUCKET))..."
	@aws s3 rm s3://$(FRONTEND_BUCKET) --recursive --quiet 2>/dev/null || true
	@echo "  [lumi] Emptying deploy bucket ($(DEPLOY_BUCKET))..."
	@aws s3 rm s3://$(DEPLOY_BUCKET) --recursive --quiet 2>/dev/null || true
	@echo "  [lumi] Emptying audio bucket ($(AUDIO_BUCKET))..."
	@aws s3 rm s3://$(AUDIO_BUCKET) --recursive --quiet 2>/dev/null || true
	@echo "  [lumi] Purging voice + chat ECR repositories..."
	@for REPO in $(STACK_PREFIX)-voice-agent $(STACK_PREFIX)-chat-agent; do \
		IMAGE_IDS=$$(aws ecr list-images --repository-name $$REPO --region $(REGION) \
			--query 'imageIds[*]' --output json 2>/dev/null); \
		if [ -n "$$IMAGE_IDS" ] && [ "$$IMAGE_IDS" != "[]" ] && [ "$$IMAGE_IDS" != "null" ]; then \
			aws ecr batch-delete-image --repository-name $$REPO --region $(REGION) \
				--image-ids "$$IMAGE_IDS" > /dev/null 2>&1 || true; \
		fi; \
	done
	@echo "  [lumi] Deleting CloudFormation stack ($(STACK_NAME))..."
	@$(CFN_AWS) cloudformation delete-stack --stack-name $(STACK_NAME)
	@echo "  [lumi] Waiting for stack delete to complete (CloudFront removal can take 15+ min)..."
	@$(CFN_AWS) cloudformation wait stack-delete-complete --stack-name $(STACK_NAME) \
		|| { echo ""; \
		     echo "  ERROR: LUMI stack delete did not complete. Common cause: a resource"; \
		     echo "  (bucket/ECR repo) was not empty, or a dependency is still attached."; \
		     echo "  Inspect events: aws cloudformation describe-stack-events --stack-name $(STACK_NAME)"; \
		     echo ""; exit 1; }
	@echo "  [lumi] LUMI teardown complete."
