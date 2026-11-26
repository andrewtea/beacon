# Beacon: guidance for agents

Beacon is a "What's that?" button for San Francisco, built for the Meta Muse Ember Hackathon. Read [HACKATHON.md](HACKATHON.md) first: the rules, the $10 budget, and the banned project types constrain every change. [README.md](README.md) says what exists today and how to run it.

## Layout

- `web/`: React + Vite + TypeScript PWA. Package manager is pnpm. Tests are Vitest with Testing Library, lint is oxlint, format is Prettier.
- `server/`: Python 3.12 + FastAPI, managed with uv. Tests are pytest, lint and format are ruff (config in the root `ruff.toml`). The package is `beacon`; every route is under `/api`.
- `scripts/`: repository scripts. Python scripts import `beacon` and run with `uv run --project server python scripts/<name>.py`. Keep their logic in `server/beacon/` so it is tested; the script is a thin entry point.
- `data/`: local runtime data (SQLite files, recordings, downloads), ignored by Git. The Ember client writes under `data/ember/`. `BEACON_DATA_DIR` moves it and must be an absolute path (`beacon.ember.client.default_data_dir`); code that stores data must honor it.

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

- **Only `server/beacon/ember/` may call the Meta Model API.** Every model call goes through `EmberClient.respond`, which records tokens and cost for each call, replays recorded responses when `REPLAY=1`, and refuses a call past the spending stop. No other module imports `openai` or another model SDK, or sends a request to `api.meta.ai`. Do not raise the stop's default or the ceiling in `beacon/ember/pricing.py`, do not give `EMBER_SPENT_BEFORE_USD` a default, and do not delete `data/ember/ledger.sqlite3` or point two processes at different ledgers: going over $10 disqualifies the entry. See [Using the Ember client](#using-the-ember-client).
- **Tests and CI never call the Meta Model API or a live data source.** Tests replay recorded or hand-written responses. `server/tests/conftest.py` removes `MODEL_API_KEY`, sets `REPLAY=1`, and patches Python's `socket.connect` and `socket.connect_ex` to raise `NetworkBlockedError` for an address that is not this machine. The patch is a backstop: a test that catches the error still passes, and native code that opens its own sockets is not covered, so the rule is yours to keep. Do not weaken the guard; give the code under test a fake transport instead.
- **Never commit secrets, raw radio audio, or downloaded source data.** The repository is public. Keys come from the environment (`MODEL_API_KEY`). Runtime data stays under the Git-ignored `data/`. Beacon publishes summaries, never raw radio audio.
- **Say what is mocked.** Until real keys and permissions exist, features run on mock data. Anything a stand-in produced is labeled as such in the API reply and in the app, and the README's status says so. Never present a stand-in's output as Ember's.
- **Product lines that keep Beacon inside the hackathon rules** (see HACKATHON.md, "Constraints for Beacon"):
  - The answer to a tap is the product. The map is secondary and opened from an incident card.
  - A photo only adds evidence to a tap and its incident. There is no "analyze this photo" feature.
  - EMS traffic is "medical response" with crews and a block-level location only. Strip patient details, names, and house numbers before anything is stored or shown. That includes what the Ember client stores: a call whose input is EMS traffic is made with `sensitive=True`.
  - Beacon relays official orders and never tells anyone to stay or go on its own. Official notices outrank radio traffic, and every claim carries its source and a confidence label.
  - Use only data we have the rights to use.

## Using the Ember client

```python
import base64

from beacon.ember import EmberClient

schema = {
    "type": "object",
    "properties": {"units": {"type": "array", "items": {"type": "string"}}},
    "required": ["units"],
    "additionalProperties": False,
}


def extract_transmission(ember: EmberClient, wav: bytes) -> str:
    answer = ember.respond(
        task="extract_transmission",  # labels the call in the ledger and the trace
        effort="low",  # required; low unless an eval shows a gain
        max_output_tokens=1024,  # required, 16 to 524,288; reasoning tokens count against it
        sensitive=True,  # required; True when the request or answer can hold what must not be stored
        instructions="You log dispatch radio.",  # the stable prefix first, so it is cached
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Which units were dispatched?"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"},
                    },
                ],
            }
        ],
        text={  # structured output
            "format": {
                "type": "json_schema",
                "name": "transmission",
                "strict": True,
                "schema": schema,
            }
        },
    )
    # answer.usage, answer.cost_usd, and answer.source ("live", "replay", or "fake") are there too.
    return answer.output_text


# Replay-only unless REPLAY=0, with MODEL_API_KEY and EMBER_SPENT_BEFORE_USD set.
ember = EmberClient.from_env()
```

- **`sensitive` is required on every call.** `True` when the request or the answer can hold what Beacon must not store: any EMS radio traffic, and anything else that can name a person or a home. The ledger and the trace then keep the call's accounting and none of its text, and the answer is not recorded or replayed, so the call is paid for every time and replay-only mode refuses it. `False` stores the request's text and the answer in full, with media replaced by a digest. The client returns the full answer either way; stripping what your own code stores is still yours to do. An error from a sensitive call is one of the client's own with a fixed text and nothing chained under it, so its message can be logged; never log a traceback's local variables, which hold the request.
- `task` and a `SpendScope` label are stored and printed as given, for sensitive calls too. They name the kind of work or its id (`extract_transmission`, `tap:1842`), never an address, a person, or anything read from a request.
- Arguments are the Responses API fields of the same names. Media goes in as inline bytes (`input_audio` with base64 `data`, a `data:` URL, or `file_data`) or a `file_id`; a remote URL is refused, so fetch a PDF or image yourself and send its bytes. Only function tools are allowed, and there is no `tool_choice`.
- A tool loop sends `answer.output` back unchanged as input on its next round, followed by one `function_call_output` item per function call.
- Give a piece of work a cap of its own with `EmberClient.from_env(scope=SpendScope("tap:<id>", budget_nanousd))`, with `SpendScope` from `beacon.ember`. A scope's budget is the most its label may hold in the ledger over every run that uses the label. Money is whole nano-dollars (`beacon.ember.pricing`).
- Build clients with `EmberClient.from_env`. A live client keeps the stop of the process's environment however it is built, and is refused until `EMBER_SPENT_BEFORE_USD` is set there. Both spending settings are read from `os.environ` only; do not add a way to pass them in. Before it sends the request that generates, a live client sends the same input to Meta's token-counting endpoint.
- Catch `beacon.ember.EmberError` and its subclasses (`BudgetExceededError`, `ReplayMissError`, `InputTooLargeError`, `EmberCallError`, ...), never an SDK error. Do not retry a failed call yourself: it may have been billed. `LedgerReplacedError` (a `BudgetExceededError`) means the ledger file was deleted or replaced under the process: stop making paid calls and tell the operator, who has to compare Meta's Usage page with the ledger.
- Build a client once per process and share it; it is safe across threads. It reads the spending settings when it is built, so a change to them needs a restart.
- To point at a call's ledger row later (a tap's token count, say), keep `answer.ledger_ref` and read it back with `ember.ledger_calls([ref])`. A row id alone does not name a call.
- **In tests**, use `EmberClient.fake(Storage.under(tmp_path), Script([fake_response(text="...")]))` from `beacon.ember`; `fake_response` also takes `function_calls` and token counts. It checks requests the way a live client does and spends nothing. `server/tests/test_ember_audio_rate.py` is a short example.
- **With no key**, a feature runs on a responder passed to `EmberClient.fake`. Its answers have `source == "fake"`: carry that to the API reply and the app, and label the result as a stand-in's.
- No real Ember call has been made yet, so the request shapes are from Meta's documentation and unverified. When a key exists, run `scripts/measure_audio_rate.py` first (README, "Measure the audio token rate").

## Conventions

- Match the code that is there: a new route copies the shape of `server/beacon/api/health.py` and its test; a new web API module copies `web/src/api/health.ts` and uses `fetchJson`.
- A route that fails says why in a fixed sentence in `detail`, and never repeats what the client sent (see `_validation_error` in `server/beacon/main.py`): a tap can carry a location, a photo, or a microphone clip.
- Deterministic code decides and is tested; Ember writes and judges. Keep model calls out of code that groups, measures, or filters.
- Comments say why, in plain sentences. Test names are sentences that state the behavior.
