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

## Conventions

- **PYQUALITY**: complete type hints, Google-style docstrings, Powertools
  structured logging (no `print`), specific exception handling, boto3 clients
  created once at module level with an adaptive-retry `Config`, and all
  resource identifiers sourced from environment variables.
- **NAMING**: `snake_case` Python, `PascalCase` classes, `UPPER_SNAKE_CASE`
  constants, Lambda handlers named `lambda_handler`. DynamoDB item attributes
  are `camelCase`; the mapping from each `snake_case` model field is documented
  inline in `common/models.py`.
