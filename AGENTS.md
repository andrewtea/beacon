# Beacon: guidance for agents

Beacon is a "What's that?" button for San Francisco, built for the Meta Muse Ember Hackathon. Read [HACKATHON.md](HACKATHON.md) first: the rules, the $10 budget, and the banned project types constrain every change. [README.md](README.md) says what exists today and how to run it.

## Layout

- `web/`: React + Vite + TypeScript PWA. Package manager is pnpm. Tests are Vitest with Testing Library, lint is oxlint, format is Prettier.
- `server/`: Python 3.12 + FastAPI, managed with uv. Tests are pytest, lint and format are ruff (config in the root `ruff.toml`). The package is `beacon`; every route is under `/api`.
- `scripts/`: repository scripts. Python scripts import `beacon` and run with `uv run --project server python scripts/<name>.py`. Keep their logic in `server/beacon/` so it is tested; the script is a thin entry point.
- `data/`: local runtime data (SQLite files, recordings, downloads), ignored by Git. The Ember client writes under `data/ember/`. `BEACON_DATA_DIR` moves it (`beacon.ember.client.default_data_dir`); code that stores data must honor it.

Each `server/beacon/` package has one concern, so parallel pull requests stay apart. Today: `api/` (HTTP routes, one module per resource, each exposing a `router` that `beacon.main` mounts under `/api`) and `ember/` (the Meta Model API client, its ledger, replay cache, and trace). When you add a package, add it to this list and to the README's layout table.

## Commands

```sh
scripts/check            # everything CI runs; run it before pushing
scripts/check server     # ruff format --check, ruff check, pytest
scripts/check web        # prettier, oxlint, vitest, tsc + vite build
amp orb services ensure  # in an Amp orb: start the API and the web dev server, print the portal URL
```

Run one server test with `cd server && uv run pytest tests/test_health.py -k name`, and one web test with `cd web && pnpm test src/App.test.tsx`. Server pytest sets `REPLAY=1` and removes `MODEL_API_KEY` itself; `pnpm test` run directly does not change the environment, and `scripts/check web` does. After changing dependencies, commit the lockfile (`server/uv.lock`, `web/pnpm-lock.yaml`): CI installs with the lockfiles frozen.

## Rules

- **Only `server/beacon/ember/` may call the Meta Model API.** Every model call goes through `EmberClient.respond`, which records tokens and cost for each call, replays recorded responses when `REPLAY=1`, and refuses a call past the spending stop. No other module imports `openai` or another model SDK, or sends a request to `api.meta.ai`. Do not raise the stop's default or the ceiling in `beacon/ember/pricing.py`, and do not delete `data/ember/ledger.sqlite3`: going over $10 disqualifies the entry. See [Using the Ember client](#using-the-ember-client).
- **Tests and CI never call the Meta Model API or a live data source.** Tests replay recorded or hand-written responses. `server/tests/conftest.py` removes `MODEL_API_KEY`, sets `REPLAY=1`, and patches Python's `socket.connect` and `socket.connect_ex` to raise `NetworkBlockedError` for an address that is not this machine. The patch is a backstop: a test that catches the error still passes, and native code that opens its own sockets is not covered, so the rule is yours to keep. Do not weaken the guard; give the code under test a fake transport instead.
- **Never commit secrets, raw radio audio, or downloaded source data.** The repository is public. Keys come from the environment (`MODEL_API_KEY`). Runtime data stays under the Git-ignored `data/`. Beacon publishes summaries, never raw radio audio.
- **Say what is mocked.** Until real keys and permissions exist, features run on mock data. Anything a stand-in produced is labeled as such in the API reply and in the app, and the README's status says so. Never present a stand-in's output as Ember's.
- **Product lines that keep Beacon inside the hackathon rules** (see HACKATHON.md, "Constraints for Beacon"):
  - The answer to a tap is the product. The map is secondary and opened from an incident card.
  - A photo only adds evidence to a tap and its incident. There is no "analyze this photo" feature.
  - EMS traffic is "medical response" with crews and a block-level location only. Strip patient details, names, and house numbers before anything is stored or shown.
  - Beacon relays official orders and never tells anyone to stay or go on its own. Official notices outrank radio traffic, and every claim carries its source and a confidence label.
  - Use only data we have the rights to use.

## Using the Ember client

```python
from beacon.ember import EmberClient, SpendScope

ember = EmberClient.from_env()                       # replay-only unless REPLAY=0 and MODEL_API_KEY
answer = ember.respond(
    task="extract_transmission",                     # labels the call in the ledger and the trace
    effort="low",                                    # required; low unless an eval shows a gain
    max_output_tokens=1024,                          # required; reasoning tokens count against it
    instructions="...",                              # the stable prefix first, so it is cached
    input=[{"role": "user", "content": [...]}],
    text={"format": {"type": "json_schema", ...}},   # structured output
)
answer.output_text, answer.usage, answer.cost_usd, answer.source  # "live", "replay", or "fake"
```

- Arguments are the Responses API fields of the same names. Media goes in as inline bytes (`input_audio` with base64 `data`, a `data:` URL, or `file_data`) or a `file_id`; a remote URL is refused, so fetch a PDF or image yourself and send its bytes. Only function tools are allowed, and there is no `tool_choice`.
- A tool loop sends `answer.output` back unchanged as input on its next round, followed by one `function_call_output` item per function call.
- Give a piece of work a cap of its own with `EmberClient.from_env(scope=SpendScope("tap:<id>", budget_nanousd))`. Money is whole nano-dollars (`beacon.ember.pricing`).
- Catch `beacon.ember.EmberError` and its subclasses (`BudgetExceededError`, `ReplayMissError`, `InputTooLargeError`, `EmberCallError`, ...), never an SDK error. Do not retry a failed call yourself: it may have been billed.
- Build a client once per process and share it; it is safe across threads.
- **In tests**, use `EmberClient.fake(Storage.under(tmp_path), Script([fake_response(text=..., function_calls=[...])]))` from `beacon.ember`. It checks requests the way a live client does and spends nothing. `server/tests/test_ember_audio_rate.py` is a short example.
- **With no key**, a feature runs on a responder passed to `EmberClient.fake`. Its answers have `source == "fake"`: carry that to the API reply and the app, and label the result as a stand-in's.
- No real Ember call has been made yet, so the request shapes are from Meta's documentation and unverified. When a key exists, run `scripts/measure_audio_rate.py` first (README, "Measure the audio token rate").

## Conventions

- Match the code that is there: a new route copies the shape of `server/beacon/api/health.py` and its test; a new web API module copies `web/src/api/health.ts` and uses `fetchJson`.
- A route that fails says why in a fixed sentence in `detail`, and never repeats what the client sent (see `_validation_error` in `server/beacon/main.py`): a tap can carry a location, a photo, or a microphone clip.
- Deterministic code decides and is tested; Ember writes and judges. Keep model calls out of code that groups, measures, or filters.
- Comments say why, in plain sentences. Test names are sentences that state the behavior.
