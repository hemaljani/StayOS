# StayOS Shell — Unified Login + Feature Launcher

The StayOS shell is the single entry point for StayOS. It owns the site root
(`/`) and provides:

- **Login when unauthenticated** — one sign-in against the shared StayOS (LUMI)
  Amazon Cognito user pool.
- **Feature launcher when authenticated** — a grid that routes into the feature
  apps: **LUMI** at `/lumi/` and **PULSE** at `/pulse/` (plus "Coming Soon"
  tiles for future features).

## Screenshots

The shell renders a login view when unauthenticated and a feature launcher once
signed in (single sign-on carries the session into LUMI and PULSE).

<table>
  <tr>
    <td align="center" width="50%">
      <img src="frontend/assets/shell-login.png" alt="StayOS shell login view" width="100%" /><br/>
      <sub>Login (unauthenticated)</sub>
    </td>
    <td align="center" width="50%">
      <img src="frontend/assets/shell-launcher.png" alt="StayOS shell feature launcher grid" width="100%" /><br/>
      <sub>Feature launcher (authenticated)</sub>
    </td>
  </tr>
</table>

## Architecture

- **Framework:** Next.js 15 (App Router) + React 19 + Tailwind, static-export
  PWA (`output: 'export'`, `trailingSlash: true`), `basePath` unset so it is
  served from `/`. Mirrors the LUMI/PULSE frontend stack.
- **Auth:** consumes the shared `@stayos/auth` module (from `shared/auth/`) via a
  build-time TypeScript path alias. `src/lib/auth.ts` initializes it with the
  shell's env-derived Cognito config and re-exports the primitives.
- **Session:** the shared module stores tokens in `localStorage` under the
  `stayos.*` namespace on the shared origin, which is what enables SSO across
  the shell, LUMI, and PULSE.

## Structure

```
stayos-shell/frontend/
  src/
    app/
      layout.tsx          # minimal root layout (StayOS branding, no nav)
      page.tsx            # root "/" - login when unauthenticated, grid when authenticated
    components/
      LoginForm.tsx       # unauthenticated view
      FeatureGrid.tsx     # authenticated launcher (LUMI -> /lumi/, PULSE -> /pulse/)
    hooks/
      useShellAuth.ts     # reactive session state + login/logout over @stayos/auth
    lib/
      auth.ts             # thin adapter over @stayos/auth (initAuth + re-exports)
      constants.ts        # NEXT_PUBLIC_COGNITO_* env config
      features.ts         # the StayOS feature catalog
    styles/globals.css
  __tests__/              # shellPage + featureGrid property tests
```

## Configuration

Copy `frontend/.env.production.example` to `frontend/.env.production` and set the
**same** Cognito client ID and region as LUMI/PULSE:

```
NEXT_PUBLIC_COGNITO_CLIENT_ID=<shared LUMI app client id>
NEXT_PUBLIC_COGNITO_REGION=us-east-1
```

## Commands

**Prerequisites:** Node.js 18.18+ and npm (required by Next.js 15 / React 19).

```bash
cd frontend
npm install          # (.npmrc pins legacy-peer-deps for the React 19 / testing-library mix)
npm run dev          # local dev server
npm run test:run     # vitest (component + property tests)
npm run build        # static export to out/
```

## Deploy

The shell is static assets published to the **root** of the shared StayOS S3
bucket / CloudFront distribution, so `/` serves the shell while `/lumi/*` and
`/pulse/*` serve the features. Deploy after LUMI's stack exists (the shell reads
LUMI's shared Cognito for its build-time config):

```bash
# From the repo root (delegates to this feature's Makefile):
make shell-deploy [PROFILE=... REGION=...]
```

`shell-deploy` generates the shell's Cognito config from the LUMI stack outputs,
builds the static export, syncs it to the bucket root (excluding `/lumi/*` and
`/pulse/*`), and invalidates the shell's root cache entries. See the
[root README](../README.md#deployment) for the full-platform deploy order.

## Contributing

Contributions are welcome. See the [contributing guide](../CONTRIBUTING.md) for
PR conventions, coding standards, and how to propose changes.
