# Contributing to StayOS

Thanks for taking a look. StayOS is a demo/prototype platform, organized as
one repo hosting multiple independent features — see the root
[`README.md`](README.md) for the current feature list and layout.

## Scope your change to one feature

Each feature directory (`lumi/`, `pulse/`) is self-contained. A PR that
touches only `lumi/` should not need to touch `pulse/` or `shared/`, and
vice versa. If your change needs to touch more than one feature directory,
say why in the PR description — it usually means something belongs in
`shared/` instead.

## Before opening a PR

1. Read the feature's own `README.md` for prerequisites and local setup
   (AWS CLI, container runtime, Node/Python versions).
2. Run that feature's test suite: `make <feature>-test` from the repo root
   (e.g. `make lumi-test`).
3. Run lint: `make <feature>-lint`.
4. Keep the diff scoped — see above.

## Commit messages

Use imperative mood ("Add X", "Fix Y", not "Added X" or "Fixes Y").

## Branch naming

`kebab-case`, prefixed by type: `feature/...`, `fix/...`, `refactor/...`,
`docs/...`.

## Reporting issues

Open a GitHub issue with: what you expected, what happened, and the AWS
region/account context if it's deployment-related (redact account IDs).
