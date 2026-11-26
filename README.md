# Beacon

Beacon is a "What's that?" button for your city. You hear a siren or see a helicopter, tap once, and Beacon says what is going on, which crews are on it, roughly where, and when it cleared, with a source on every claim. Behind the button, Muse Ember listens to San Francisco's public fire and EMS dispatch radio, reads aircraft positions, alerts, and notices, and stitches them into one incident record per event.

Beacon is being built for the Meta Muse Ember Hackathon (Nov 23 to Dec 2, 2026). The rules, the $10 budget, and the submission checklist are in [HACKATHON.md](HACKATHON.md).

**Status:** scaffold and Ember client. The web app is an installable shell that shows whether the server is reachable, and the server answers a health check. The client every model call will go through is built, and no route uses it yet. **No real Ember call has been made:** there is no API key, so the client has run only against a mock of the Meta Model API in tests, and its request shapes come from Meta's documentation, not from an answer of Ember's. The audio token rate the budget rests on is [still a guess](#measure-the-audio-token-rate). Nothing listens to radio, and no incident exists.

## Layout

| Path | What it holds |
| --- | --- |
| [`web/`](web) | React + Vite + TypeScript PWA, installable on a phone and usable on a desktop. |
| [`server/`](server) | Python 3.12 + FastAPI server. Every route is under `/api`. |
| [`scripts/`](scripts) | Repository scripts: `check` runs what CI runs, `measure_audio_rate.py` measures what Ember bills for audio, and `generate-icons.sh` redraws the PWA icons from `web/public/favicon.svg`. |
| `data/` | Local runtime data. Ignored by Git, never committed. |

Inside `server/beacon/`, each package has one concern so pull requests stay apart: `api/` holds the HTTP routes, and `ember/` is the only code that calls the Meta Model API.

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

[`.env.example`](.env.example) lists the settings, and the Ember client is what reads them (`EmberClient.from_env`):

| Variable | What it does | When unset |
| --- | --- | --- |
| `REPLAY` | `1` answers from recorded Ember responses and never calls the API. `0` calls the API for a request with no recording. | `1` |
| `MODEL_API_KEY` | The Meta Model API key. Read only when `REPLAY=0`, which is refused without it. | No live calls. |
| `EMBER_STOP_USD` | The most the Ember ledger may hold in live spend, in dollars. | `3.00` |
| `EMBER_SPENT_BEFORE_USD` | What Meta's Usage page showed before Beacon's first call, in dollars. `0` is an answer; it has no default. | No live calls: a client that can spend is refused. |
| `BEACON_DATA_DIR` | Where runtime data goes. An absolute path; a relative one is refused. | The repository's `data/`. |

The server's routes do not build an Ember client yet, so today the settings are read by the two commands under [Ember client](#ember-client) and nothing else. Three places set any of them. `scripts/check`, and so CI, sets `REPLAY=1` and removes `MODEL_API_KEY`. Server pytest does the same, removes `EMBER_STOP_USD` and `EMBER_SPENT_BEFORE_USD`, and points `BEACON_DATA_DIR` at a temporary directory for each test. The orb's API service in [`.amp/services.yaml`](.amp/services.yaml) sets `REPLAY=1`.

## Ember client

Every model call goes through `EmberClient` in [`server/beacon/ember/`](server/beacon/ember), which calls `muse-ember-1.0` on the Meta Model API's Responses API through the OpenAI SDK. It is ported from the client of Forte, an earlier project by the same author for this hackathon.

```python
from beacon.ember import EmberClient

ember = EmberClient.from_env()
answer = ember.respond(
    task="extract_transmission",
    effort="low",
    max_output_tokens=1024,
    sensitive=True,  # the request or the answer can hold what Beacon must not store
    input="...",
)
```

| Mode | Selected by | What it does |
| --- | --- | --- |
| Replay-only | `REPLAY=1`, the default | Answers from recordings and never calls the API. A request with no recording fails. |
| Live | `REPLAY=0`, `MODEL_API_KEY`, and `EMBER_SPENT_BEFORE_USD` | Answers from a recording when there is one. Otherwise it calls the API and records the answer, so the same request costs $0 the next time. A [sensitive call](#what-is-stored) is never recorded and is paid for every time. |
| Fake | `EmberClient.fake(storage, responder)` | Answers from a function you supply, such as a `Script` of `fake_response(...)` objects. It needs no key and no recordings, and its calls are booked with the source `fake` at no spend. |

Around each call the client:

- **refuses a live call that could pass the spending stop** (see below), and one with more than **150,000** input tokens. It counts them with Meta's token-counting endpoint (`POST /v1/responses/input_tokens`), so the whole input, media included, is sent to Meta once to be counted before the request that generates an answer is sent or refused. The count is not billed;
- requires a `task` label, a reasoning effort, and `max_output_tokens` from 16 to 524,288 (Ember's largest output), because Ember reasons at `max` when no effort is sent and reasoning bills as output;
- requires `sensitive`, True or False, and keeps the content of a sensitive call out of everything it writes (see [What is stored](#what-is-stored));
- allows only function tools, because a hosted tool such as web search bills outside the token prices;
- takes media as inline bytes (a `data:` URL or base64, such as `input_audio`) or an uploaded `file_id`, and refuses a remote URL, because the bytes behind it can change after they were counted or recorded;
- writes one ledger row, from which the token trace is exported.

A failed call is not retried, with one exception: a client given a `Pacer` spaces its requests under a rate limit and tries a call again after a 429. A client given a `SpendScope` books its calls under the scope's name and holds them to a budget of their own under the stop, which is how one tap's investigation or the scale run gets a cap.

The client writes under `data/ember/` (or `$BEACON_DATA_DIR/ember/`), which Git ignores:

| File | What it holds |
| --- | --- |
| `ledger.sqlite3` | One `ember_calls` row per call: task, tokens, cached tokens, cost, latency, model, effort, scope, whether the call was `live`, `replay`, or `fake`, and its request and response, or for a sensitive call the sizes of them. Spend is the sum of the `live` rows. |
| `trace.jsonl` | The submission's token trace: every ledger row as one JSON line. It is written from the ledger on request, by the command below. |
| `replay/<request hash>.json` | Recorded responses, keyed by a SHA-256 of the request. A sensitive call has none. |

```sh
cd server
uv run python -m beacon.ember   # writes trace.jsonl and prints the live spend and the stop
```

### What is stored

Every call says whether it is `sensitive`: whether its request or its answer can hold what Beacon must not store, such as a patient's details, a name, or a house number in EMS radio traffic.

- **An ordinary call** (`sensitive=False`) is stored in full. The ledger, the trace, and the recordings hold the request's text and Ember's response as it wrote it, including the arguments of its tool calls, which can be free text. Audio, images, and files are replaced by a length and a digest in all three, so no radio audio is stored, but the words of a transcription or an extraction are.
- **A sensitive call** (`sensitive=True`) leaves its accounting and nothing of what was said. Its ledger row and trace line hold the task, tokens, cost, latency, model, effort, the response's id and status, the SHA-256 of the whole request, and the size of each field in place of its content. The answer is not recorded for replay and is never replayed, so the same call is paid for every time, and replay-only mode refuses it. The caller still gets the full answer, and what the caller stores from it is the caller's to strip.

**Any call whose input is EMS radio traffic, or anything else that can name a person or a home, must be made with `sensitive=True`.** The request's SHA-256 is still stored, and someone who holds the exact request can confirm it was made.

An API error is stored as its status and type without its message, for both kinds of call. Read the trace before sharing it. The client uses POSIX file locks, so it runs on Linux and macOS.

### The spending stop

The plan's hard stop is $3 above what Meta's Usage page showed on Nov 26, 2026, the day the ledger began. The ledger counts only Beacon's own calls, so that is **$3.00 of live spend in the ledger** (`EMBER_STOP_USD`), whatever the page showed. The client reserves each call's most before sending it, in one transaction, so calls made at once cannot pass the stop together.

The ledger cannot see what the account had spent before it began, and the credit can: the stop and the earlier spend together may not pass $9.00 of the $10.00 credit. So `EMBER_SPENT_BEFORE_USD` has no default. **Nobody has read that total off the Usage page yet**, and until someone sets the variable, to `0` if that is what the page shows, every client that can spend is refused when it is built. A guess of $0.00 would let an account that had already spent $8.00 spend $3.00 more. Replay-only and fake clients need neither the key nor this setting.

The stop is the environment's for every live client. `EmberClient.live` and the constructor read it as `EmberClient.from_env` does, and their `stop_nanousd` can lower it for one client and cannot raise it, so `EMBER_STOP_USD=0` stops all paid calls whichever way a client was built.

**Every process that can spend must use the same ledger file, and the file must outlive the process.** The stop is counted in one ledger: a second ledger is a second $3.00. That is why a relative `BEACON_DATA_DIR` is refused, as is a live client handed a relative ledger path (either would name a different file from each working directory), and why a deployment must put the data directory on storage that survives a restart.

A live call that was sent and did not come back with a usable answer, such as a timeout or any error response, may still have been billed, so the ledger keeps it at the most it could have cost. Only a call that never left the machine is booked at $0. That makes the ledger an upper bound on Beacon's spend; compare it with Meta's [Usage page](https://dev.meta.ai/usage) every evening.

The ledger is the only record of what was spent. When its file is deleted or replaced under a client that is already built, that client logs calls that spend nothing in the new file and refuses every paid call with `LedgerReplacedError`, because the stop would otherwise be counted from zero. It checks before a call's first request, when it reserves the call, and again immediately before the request that costs money. A file that goes while that request is out cannot be caught in time: the call may have been billed and is in no ledger, the client raises `LedgerReplacedError` saying so instead of returning the answer, and **Meta's Usage page has to be compared with the ledger by hand before any more paid calls are made**. A client built after the file was deleted counts from zero: deleting the ledger forgets its spend, so do not delete it during the hackathon.

### Measure the audio token rate

**Not done yet: it needs a key.** The budget rests on a guess of 25 to 30 input tokens per second of audio. This command makes the plan's first ten real calls to check it, one per clip of a known length, and fits the rate from what each was billed:

```sh
REPLAY=0 MODEL_API_KEY=... EMBER_SPENT_BEFORE_USD=... \
  uv run --project server python scripts/measure_audio_rate.py
```

Its calls are booked under one scope in the ledger, `measure-audio-rate`, which is the same on every run. `--budget` (default $0.05) is therefore the most the measurement may spend **over all its runs** in one ledger, not new money for each run: once the scope holds that much, raise `--budget` to measure again.

The clips are generated tones, which tell the rate only if Ember bills audio by its length. Their answers are recorded, so a rerun with the same clips replays them at no cost; a clip with no recording is a new paid call, and without `REPLAY=0` the command stops and says so. `--clips DIR` sends a folder of WAV files instead: use recordings of the kind Beacon will send, and only ones we have the rights to send. Because those can be radio traffic, their calls are sensitive ones: nothing of them is recorded, and every run pays for them again, under the same budget. A WAV file that is cut short of the length its header declares is refused.

## Rules Beacon is built under

- Every model call goes through Muse Ember on the Meta Model API, from one client that tracks tokens and spend and stops before the budget is passed.
- The answer to a tap is the product. The map is secondary.
- A photo only adds evidence to an incident. Beacon is not an image analyzer.
- EMS traffic is described as "medical response" with crews and a block-level location, never the patient.
- Beacon relays official orders and never tells anyone to stay or go on its own.
- Beacon publishes summaries, never raw radio audio, and uses only data it has the rights to use.

[AGENTS.md](AGENTS.md) holds the conventions for contributors and coding agents.
