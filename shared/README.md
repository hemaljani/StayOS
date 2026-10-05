# Shared (cross-feature layer)

This directory holds code genuinely shared across more than one StayOS feature.
It is populated only when a second real consumer exists - not speculatively.

## `auth/` - the shared StayOS authentication module (`@stayos/auth`)

`shared/auth/` is the single source of truth for StayOS client-side
authentication. It exists because there are now **three** consumers of one
session:

- the **StayOS shell** (`stayos-shell/`, served at `/`) - where a GM signs in;
- **LUMI** (`lumi/`, served at `/lumi`);
- **PULSE** (`pulse/`, served at `/pulse`).

All three are served from one CloudFront origin and share a single browser
session, so a GM logs in once at the shell and both features open with no second
login (single sign-on). Keeping the auth logic in one place guarantees the shell
and both features agree on exactly what a session is.

### What it provides

- `signIn`, `refreshSession`, `signOut`, `getAccessToken`, `getIdToken`,
  `getCurrentUser`, `isAuthenticated` - the auth primitives (Cognito
  `USER_PASSWORD_AUTH` + `REFRESH_TOKEN_AUTH`, JWT claim decoding).
- `initAuth({ cognitoClientId, cognitoRegion })` - each app injects its own
  env-derived Cognito config; the module hardcodes no resource identifiers.
- A `localStorage` session under the shared `stayos.*` key namespace, visible
  across the whole origin. This is the mechanism that makes SSO work (per-tab
  `sessionStorage` would not survive the cross-app navigation).

### How it is consumed (mechanism)

Not an npm workspace package. Each app resolves `@stayos/auth` to
`shared/auth/src/index.ts` via a **build-time TypeScript path alias**, wired in
three places per app so build, typecheck, and test all agree:

- `tsconfig.json` -> `compilerOptions.paths["@stayos/auth"]`
- `next.config.js` -> `transpilePackages: ['@stayos/auth']` + a webpack alias
- `vitest.config.ts` -> `resolve.alias['@stayos/auth']`

Each app's `src/lib/auth.ts` is a thin adapter: it calls `initAuth()` with that
app's Cognito config and re-exports the primitives, so existing
`@/lib/auth` imports keep working unchanged.

`shared/auth/` builds and tests in isolation (`cd shared/auth && npm test`);
its `src/__tests__/auth.test.ts` includes a fast-check property asserting all
consumers agree on a session. The root `make test-all` target also runs these
tests and `tsc --noEmit`, so shared authentication is part of the platform-wide
quality gate.

## Infrastructure carve-out - still deferred

The originally planned shared **infrastructure** layer (the Cognito user pool and
WAF web ACL that LUMI defines and PULSE reuses) remains deliberately deferred:

- LUMI's Cognito stack (`lumi/infrastructure/nested-stacks/auth.yaml`) is
  self-contained and safe to move later; it exports only through standard
  nested-stack `Outputs`, with no `Fn::ImportValue` coupling.
- LUMI's WAF web ACL is embedded in
  `lumi/infrastructure/nested-stacks/data.yaml`, consumed directly by
  `chat.yaml` and `compute.yaml`.

Move `auth.yaml` (and, if it turns out to be genuinely shared, the WAF web ACL)
into `shared/infrastructure/` when a second consumer's own stacks make the real
dependency shape concrete - the same "wait for a real second consumer" rule that
now justifies `shared/auth/`.
