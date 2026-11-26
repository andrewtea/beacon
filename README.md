# Beacon

Beacon is a "What's that?" button for your city. You hear a siren or see a helicopter, tap once, and Beacon says what is going on, which crews are on it, roughly where, and when it cleared, with a source on every claim. Behind the button, Muse Ember listens to San Francisco's public fire and EMS dispatch radio, reads aircraft positions, alerts, and notices, and stitches them into one incident record per event.

Beacon is being built for the Meta Muse Ember Hackathon (Nov 23 to Dec 2, 2026). The rules, the $10 budget, and the submission checklist are in [HACKATHON.md](HACKATHON.md).

**Status:** scaffold only. The web app is an installable shell that shows whether the server is reachable, and the server answers a health check. Nothing listens to radio, no incident exists, and no model has been called.

## Layout

| Path | What it holds |
| --- | --- |
| [`web/`](web) | React + Vite + TypeScript PWA, installable on a phone and usable on a desktop. |
| [`server/`](server) | Python 3.12 + FastAPI server. Every route is under `/api`. |
| [`scripts/`](scripts) | Repository scripts: `check` runs what CI runs, and `generate-icons.sh` redraws the PWA icons from `web/public/favicon.svg`. |
| `data/` | Local runtime data. Ignored by Git, never committed. |

Inside `server/beacon/`, each package has one concern so pull requests stay apart: `api/` holds the HTTP routes.

## Run locally

You need [uv](https://docs.astral.sh/uv/), Node.js 26, and pnpm 12. uv installs Python 3.12 itself.

```sh
# The server, on http://127.0.0.1:8000.
cd server
uv sync
uv run uvicorn beacon.main:app --reload

# The web app, in a second terminal. It proxies /api to the server.
cd web
pnpm install
pnpm dev
```

In an Amp orb, `amp orb services ensure` starts both and prints the portal URL.

Both of those serve the web app with Vite's dev server, which leaves out the web app manifest and the service worker, so the app cannot be installed from it. To try the installable build, keep the server running and serve the production build, then open it on `localhost` or over HTTPS:

```sh
cd web
pnpm build && pnpm preview
```

Run a Python script from `scripts/` with the server's environment: `uv run --project server python scripts/<name>.py`.

## Check

```sh
scripts/check          # everything CI runs
scripts/check web      # format, lint, tests, type check, build
scripts/check server   # format, lint, tests
```

Tests never call the Meta Model API or a live data source. Two entry points enforce the environment for that: `scripts/check`, which CI also runs, sets `REPLAY=1` and removes `MODEL_API_KEY` before it checks either side, and server pytest does the same however it is started, because `server/tests/conftest.py` does it. CI holds no key. A web test run directly with `pnpm test` keeps whatever environment the shell has; nothing in `web/` reads the key. As a backstop for the server tests, `server/tests/conftest.py` patches Python's `socket.connect` and `socket.connect_ex` in the test process to raise `NetworkBlockedError` for any address that is not this machine. It is a backstop and not a sandbox: a test that catches that error still passes, and the patch does not reach DNS lookups, subprocesses, or native code that opens sockets without Python's `socket` module.

## Configuration

Settings come from the environment. The server does not load the repository-root `.env` file: export a variable in your shell or put it in front of the command. (Vite loads `web/.env` for the web app, as it does by default, and exposes only names that start with `VITE_` to the browser. Beacon has no such file and no such setting.)

The scaffold reads none of them yet. [`.env.example`](.env.example) lists the three names that are reserved, and the Ember client, which is the second part of Phase 1, is what gives them effect: `MODEL_API_KEY` (the Meta Model API key), `REPLAY` (`1` to answer from recorded Ember responses and call nothing), and `BEACON_DATA_DIR` (where runtime data goes instead of the repository's `data/`). Three places set any of them today. `scripts/check`, and so CI, sets `REPLAY=1` and removes `MODEL_API_KEY`. Server pytest does the same and also points `BEACON_DATA_DIR` at a temporary directory for each test. The orb's API service in [`.amp/services.yaml`](.amp/services.yaml) sets `REPLAY=1`.

## Rules Beacon is built under

- Every model call goes through Muse Ember on the Meta Model API, from one client that tracks tokens and spend and stops before the budget is passed. That client is not built yet, and until it is, nothing in the repository calls a model.
- The answer to a tap is the product. The map is secondary.
- A photo only adds evidence to an incident. Beacon is not an image analyzer.
- EMS traffic is described as "medical response" with crews and a block-level location, never the patient.
- Beacon relays official orders and never tells anyone to stay or go on its own.
- Beacon publishes summaries, never raw radio audio, and uses only data it has the rights to use.

[AGENTS.md](AGENTS.md) holds the conventions for contributors and coding agents.
