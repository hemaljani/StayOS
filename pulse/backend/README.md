# PULSE Backend

Serverless-first Python 3.12 backend for **PULSE** (StayOS Feature #2), the
real-time, AI-triaged alert pipeline that extends the StayOS PWA. This package
is deployed as a set of AWS Lambda functions wired to DynamoDB Streams, Amazon
Bedrock, EventBridge Scheduler, AWS AppSync Events, and Web Push.

## Layout (src layout)

```
backend/
├── pyproject.toml            # deps + ruff/black/pytest config
├── src/pulse/
│   ├── common/               # shared models, config, logging, aws, dynamo, errors
│   ├── rule_engine/          # DynamoDB Streams -> alert rule evaluation
│   ├── triage/               # Amazon Bedrock triage agent
│   ├── escalation/           # escalation-trigger hierarchy + routing chain
│   ├── delivery/             # AppSync Events realtime + Web Push + INFO batcher
│   ├── action_executor/      # GM-approved write-backs (closed loop)
│   ├── demo_simulator/       # deterministic operational-data mutations (demo)
│   ├── forecast_invoker.py   # Scheduler -> validated AgentCore forecast result
│   └── api/                  # REST API business logic
└── tests/                    # pytest + Hypothesis, mirrors src/pulse/
```

## Development

Create a virtual environment and install the dev extras:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Common commands

```sh
ruff check src tests        # lint (incl. import order + docstrings)
black src tests             # format
pytest                      # run the test suite
```

The `src` layout plus `pythonpath = ["src"]` in `pyproject.toml` means `pytest`
resolves `import pulse` without a build step; an editable install (`pip install
-e .`) additionally makes `python -c "import pulse"` work anywhere.

### Forecast invocation validation

The shared Lambda archive includes `pulse.forecast_invoker.lambda_handler`.
CodeBuild imports this handler from the final ZIP before uploading it, so an
omitted module fails the build. The handler initializes its AWS client on first
invocation, then reuses it across warm invocations.

`FORECAST_AGENT_RUNTIME_ARN` selects the runtime; an empty value explicitly skips
forecasting during initial deployment. The invocation role grants access to
that exact runtime and its `DEFAULT` endpoint once configured. The handler
consumes the JSON response and raises on runtime failures, rejected requests,
invalid summaries, or incomplete streams. A completed result includes the
property, unique session ID, UTC completion timestamp, and forecast counts.
`completedAt` records invocation completion; it does not define the forecast's
business date.

Run the forecast and infrastructure checks from this directory:

```sh
pytest tests/forecast tests/smoke -q
```

The invoker emits `Forecast invocation completed` or `Forecast run failure`
into the pipeline log group. The forecast dashboard displays completion and
failure counts. A completed no-alert run verifies the invocation path; forecast
data quality still depends on the runtime's data contract.

Use the repository's documented deployment entry point to apply the package,
handler configuration, IAM permissions, timeout, and observability template
together. These local checks do not invoke the deployed runtime or prove that
the deployed configuration has changed.

## Conventions

- **PYQUALITY**: complete type hints, Google-style docstrings, Powertools
  structured logging (no `print`), specific exception handling, boto3 clients
  created once at module level with an adaptive-retry `Config`, and all
  resource identifiers sourced from environment variables.
- **NAMING**: `snake_case` Python, `PascalCase` classes, `UPPER_SNAKE_CASE`
  constants, Lambda handlers named `lambda_handler`. DynamoDB item attributes
  are `camelCase`; the mapping from each `snake_case` model field is documented
  inline in `common/models.py`.
