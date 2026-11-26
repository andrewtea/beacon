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

Run a Python script from `scripts/` with the server's environment: `uv run --project server python scripts/<name>.py`.

## Check

```sh
scripts/check          # everything CI runs
scripts/check web      # format, lint, tests, type check, build
scripts/check server   # format, lint, tests
```

Tests never call the Meta Model API or a live data source. They run with `REPLAY=1` and with `MODEL_API_KEY` removed from their environment, and CI runs the same script and holds no key. As a backstop, a server test that opens a TCP connection to another machine fails. That guard covers sockets in the test process only, not DNS lookups or subprocesses.

## Configuration

The server reads its settings from the environment; [`.env.example`](.env.example) lists them. `MODEL_API_KEY` is the Meta Model API key, and `REPLAY=1` (the default for development) replays recorded Ember responses at no cost. `BEACON_DATA_DIR` is where runtime data goes; it defaults to the repository's `data/`.

## Rules Beacon is built under

- Every model call goes through Muse Ember on the Meta Model API, from one client that tracks tokens and spend and stops before the budget is passed.
- The answer to a tap is the product. The map is secondary.
- A photo only adds evidence to an incident. Beacon is not an image analyzer.
- EMS traffic is described as "medical response" with crews and a block-level location, never the patient.
- Beacon relays official orders and never tells anyone to stay or go on its own.
- Beacon publishes summaries, never raw radio audio, and uses only data it has the rights to use.

[AGENTS.md](AGENTS.md) holds the conventions for contributors and coding agents.
