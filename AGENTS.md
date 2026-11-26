# Beacon: guidance for agents

Beacon is a "What's that?" button for San Francisco, built for the Meta Muse Ember Hackathon. Read [HACKATHON.md](HACKATHON.md) first: the rules, the $10 budget, and the banned project types constrain every change. [README.md](README.md) says what exists today and how to run it.

## Layout

- `web/`: React + Vite + TypeScript PWA. Package manager is pnpm. Tests are Vitest with Testing Library, lint is oxlint, format is Prettier.
- `server/`: Python 3.12 + FastAPI, managed with uv. Tests are pytest, lint and format are ruff (config in the root `ruff.toml`). The package is `beacon`; every route is under `/api`.
- `scripts/`: repository scripts. Python scripts import `beacon` and run with `uv run --project server python scripts/<name>.py`. Keep their logic in `server/beacon/` so it is tested; the script is a thin entry point.
- `data/`: local runtime data (SQLite files, recordings, downloads), ignored by Git. The Ember client writes under `data/ember/`. `BEACON_DATA_DIR` moves it and must be an absolute path (`beacon.ember.client.default_data_dir`); code that stores data must honor it.

Each `server/beacon/` package has one concern, so parallel pull requests stay apart. When you add a package, add it to this list and to the README's layout table.

- `api/`: HTTP routes, one module per resource, each exposing a `router` that `beacon.main` mounts under `/api`.
- `ember/`: the Meta Model API client, its ledger, replay cache, and trace.
- `evidence/`: the evidence store. Every poller writes to it and every reader of what a source said reads from it. See [Using the evidence store](#using-the-evidence-store).
- `incident/`: the incident engine, which reads evidence rows as incidents and stores nothing. `calls` decides which radio calls are one incident, `build` attaches what else was said about the place and words every line, `inforce` says what is in force, `aircraft` makes tracks and says what circles, `places` is the list of blocks that gives a radio call's place a point, and `engine` is the reader (`open_engine`). See [Using the incident engine](#using-the-incident-engine).
- `ingest/`: one module per source (`nws`, `opensky`, `traffic511`, `openmhz`, `notices`), the `Runner` that polls them, `paid` (what a poller that pays Ember for each item keeps to), and `setup`, which reads `BEACON_LIVE_SOURCES` once to open the right store (`open_store`) and make the pollers (`build`).
- `notice/`: what is kept of an official notice (`Notice`), and the Ember call that reads one from its PDF (`read_notice` in `read`).
- `radio/`: the closed vocabulary a radio call is kept in and the lists a unit or a street has to be on (`vocabulary`), the Ember call that hears one (`extract_transmission` in `extract`), and the in-memory, confined decoding of its audio (`audio`), whose `Ffmpeg` also draws a tap's photo again.
- `scenario/`: the mock scenario, an invented San Francisco fire served to the pollers in each source's own format, with its radio calls and the stand-in for Ember that "hears" them (`radio`), and its notices, written as PDFs (`pdf`), with the stand-in that "reads" them (`notices`).
- `taps/`: taking a tap. `desk` checks one, rounds its place, and stores its row (`Taps.take`); `media` has a photo drawn again as a new JPEG and a clip written out again as its samples, and keeps them for an hour (`Taps.media` reads them back).

`beacon/errors.py` is a module beside them, with `raise_bare`: how an error about something private is raised with nothing chained under it.

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
- **Say what is mocked.** Until real keys and permissions exist, features run on mock data. Anything a stand-in produced is labeled as such in the API reply and in the app, and the README's status says so. Never present a stand-in's output as Ember's. An evidence row says it itself: `row.stand_in` is true when it came from the mock scenario (`origin == "mock"`) or a fake Ember wrote it (`model == "fake"`), and whatever is built from such a row is a stand-in's too. The live evidence store refuses those rows; do not add a way around that.
- **Mock data is invented, never copied.** The repository is public and holds no real radio traffic, transcript, recording, or notice. What the mock scenario says is made up and says so in its text (`beacon.scenario.INVENTED`).
- **Nothing a radio call said is stored as text, and its audio is never written to disk.** This is what ingest guarantees, and later code builds on it and must not undo it. A call reaches the evidence store only as a `beacon.radio.Transmission`: tags from fixed lists, a hundred block, and unit ids and street names that are on the lists the caller gives (`beacon.radio.Known`; a shape alone is not enough, and with no list none is kept). Do not add a transcript, a summary, a note, or any other free-text field to it, to its form (`beacon.radio.extract.SCHEMA`), or to a radio row's `data`; a new thing worth keeping is a new tag in `beacon/radio/vocabulary.py`. A medical call is kept as `"medical response"` with its units and block, and no status, details, or alarm; so is every call on an EMS-only talkgroup. Do not write radio audio to a file, a temporary one included, or put it in a log or an error, and do not run a decoder on it outside the confinement in `beacon/radio/audio.py`, or widen what that confinement shows (`_SYSTEM_DIRS`). `extract_transmission` is always a `sensitive=True` call. Two limits are known and stated in `vocabulary.py`: a listed name is kept whatever Ember meant by it, and nothing checks that Ember heard the call right.
- **A notice's words are kept only when it is a notice to the public, and its document is never stored.** A notice reaches the evidence store only as a `beacon.notice.Notice`. Everything a model could write freely (`issuer`, `title`, `areas`, `streets`, `instruction`, `summary`) is kept for the types in `beacon.notice.TEXT_TYPES` and left empty for a press release and for anything Ember does not take for a notice, which is where a victim is named and a home described. A text is kept whole or left out and named in `incomplete`, never cut: a cut instruction can say the opposite. A notice row is a model's reading, so its confidence is `high` and never `official`. Do not widen `TEXT_TYPES` to those types, do not add a field that holds the document's text for them, do not cut a text to make it fit, and do not store, cache, or log a PDF or parse one in Beacon's process: Ember reads it, from bytes Beacon fetched from a host the operator named, in a `sensitive=True` call.
- **Work that is paid for per item claims the item first.** `EvidenceStore.claim(source, ref)` is True for one caller only, for good. Claim before the paid request, never ask about an item whose claim is not yours, and give a claim up (`release`) only when the paid request is known not to have been sent (`EmberClosedError`, `ReplayMissError`, or a `BudgetExceededError` that is not a `LedgerReplacedError`). The rules that follow once Ember has been asked (when a claim may be given up, the hold after a failure, the stop after a replaced ledger, `close` and `drain`) are written once, in `beacon/ingest/paid.py` (`PaidDesk`): a new paying poller uses it and does not write them again. `beacon/ingest/openmhz.py` and `notices.py` are the examples; their clients have no pacer, because each retry is a paid request and a claim covers one. Each names the label of its own `SpendScope` (`SCOPE` in its module, `ingest:<source>`), and `PaidDesk` refuses a live client that pays under none or under another poller's. A poller that pays also offers `close` and `drain` (`beacon.ingest.runner.PaidWork`), which `Ingest.stop` uses so that shutting down admits no new paid work and stores the answer to work that is out. Only `stop` answering True is a complete stop.
- **A poller's request goes through `beacon.ingest.http.fetch`,** which caps what is read, follows no redirect, and fails with a `SourceError` whose text is fixed. Never log or raise a request's address (511's holds the token), a reply's body, or an exception from the HTTP library, and never leave one chained under your own error: `raise ... from None` only hides it from a printed traceback and keeps it in `__context__`. Raise outside the `except` block, with `beacon.errors.raise_bare`, which also drops an error your *caller* is handling. The same goes for any error about what a source or a person sent: the parsers and the evidence store raise that way.
- **A source is polled for real only with the right to.** OpenSky's terms require a written license for calling its REST API from a running service, so `setup.source_pollers` refuses it without `BEACON_OPENSKY_PERMITTED=1`. Do not remove that check, and give a new source one like it when its terms ask for permission. A key is not permission.
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
- Catch `beacon.ember.EmberError` and its subclasses (`BudgetExceededError`, `ReplayMissError`, `InputTooLargeError`, `EmberCallError`, `EmberClosedError`, ...), never an SDK error. Do not retry a failed call yourself: it may have been billed. `LedgerReplacedError` (a `BudgetExceededError`) means the ledger file was deleted or replaced under the process: stop making paid calls and tell the operator, who has to compare Meta's Usage page with the ledger.
- Build a client once per process and share it; it is safe across threads. It reads the spending settings when it is built, so a change to them needs a restart. When the process shuts down, call `ember.close()`: from then on no call is admitted, and one that had not been raises `EmberClosedError`, having cost nothing. A call that was already admitted, at the last check before its paid request, still sends it, possibly after `close` returns; only `ember.drain(timeout)` answering True says nothing more is sent. `ember.mode` says what a client is: `live`, `replay`, or `fake`. The client follows no HTTP redirect, so a request's audio or document goes to Meta's address and nowhere else. A refusal (`BudgetExceededError` that is not a `LedgerReplacedError`, `ReplayMissError`, `EmberClosedError`) means no paid request of the call was sent, for a client with a pacer too.
- To point at a call's ledger row later (a tap's token count, say), keep `answer.ledger_ref` and read it back with `ember.ledger_calls([ref])`. A row id alone does not name a call.
- **In tests**, use `EmberClient.fake(Storage.under(tmp_path), Script([fake_response(text="...")]))` from `beacon.ember`; `fake_response` also takes `function_calls` and token counts. It checks requests the way a live client does and spends nothing. `server/tests/test_ember_audio_rate.py` is a short example.
- **With no key**, a feature runs on a responder passed to `EmberClient.fake`. Its answers have `source == "fake"`: carry that to the API reply and the app, and label the result as a stand-in's.
- No real Ember call has been made yet, so the request shapes are from Meta's documentation and unverified. When a key exists, run `scripts/measure_audio_rate.py` first (README, "Measure the audio token rate").

## Using the evidence store

```python
import os
from datetime import timedelta

from beacon.evidence import EvidenceStore, NewEvidence, Point
from beacon.ingest import open_store

store = open_store(os.environ)  # the mock store unless BEACON_LIVE_SOURCES names a source

# Read: what any source said near a place in the last ten minutes, plus rows with no
# point, such as a city-wide alert. Oldest first.
rows = store.search(
    kinds=["radio", "aircraft", "alert", "traffic", "notice"],
    start=now - timedelta(minutes=10),
    end=now,
    near=Point(37.7766, -122.4236),
    radius_m=500,
    include_unlocated=True,
)
for row in rows:
    row.kind, row.source, row.ref  # ("alert", "nws", "urn:oid:...")
    row.occurred_at, row.until  # aware datetimes in UTC; until is None for most kinds
    row.point, row.place  # Point | None, and the place in words | None
    row.confidence  # "official", "high", "medium", or "low"
    row.stand_in  # True for the mock scenario's rows and a fake Ember's
    row.data  # a dict whose shape belongs to the row's kind and source

# Write: only a poller or an ingest route does. The same source and ref is stored once.
store.add([NewEvidence(kind="tap", source="app", ref=tap_id, occurred_at=now,
                       confidence="low", origin="live", point=where)])  # as `Taps.take` does
```

- **A row is one thing one source said, and it is never changed.** A revision is a new row: an NWS update names the alerts it replaces in `data["replaces"]`, and each version of a 511 event is a row whose `data["event_id"]` is the same.
- **`search` returns history, not what is in force.** The store applies no revision for you, and the rows that end something can lie outside your window or your radius: an alert issued an hour ago is found by its span long after a later row said it was replaced, cancelled, or no longer listed. To decide what is in force at a moment, take the candidate rows and then look up, with no upper bound on age and no distance filter, the later rows of the same source that name them: `data["replaces"]` (NWS), a later version with the same `data["event_id"]` (511), an NWS row whose `data["message_type"]` is `Cancel`, and rows with `data["change"]`.
- **An item that stops being listed has not been officially ended.** NWS and 511 answer with what is active now, and mostly an alert or a closure that is over is just missing from the next answer. When an item has been missing from two complete answers in a row, the poller stores a row with `data["change"] == "not_listed"` and `data["about"]` holding the `ref` of the row it is about. It records what Beacon saw, at `medium` confidence: it is not an official all-clear, and must not be shown as one. If the item is listed again, a `"listed_again"` row follows, and the latest change row about a `ref` is the one that holds. An alert that simply ran to its `until` gets no change row. What *is* official is an explicit cancellation: an NWS row whose `data["message_type"]` is `Cancel` is the authority saying the alerts in its `data["replaces"]` are over, and it stays `official`.
- **Rows carry what the source said, not a judgment.** Grouping rows into incidents, deciding that an aircraft is circling, and weighing a notice against radio are the incident engine's, and stay deterministic and tested.
- `search` finds a row with an `until` when its span touches the window, and any other row when it happened inside it. Pass `include_unlocated=True` with `near` unless you want only rows that have a point. Aircraft are one row per position, about every 30 seconds each, so give an aircraft query a short window or a larger `limit` (default 500).
- **A `radio` row** (`source == "openmhz"`) is one call. `data` is `Transmission.as_data()` plus the call's `system`, `talkgroup`, `seconds`, and `emergency`: `event` (`"medical response"` for a medical call, else the `category`), `category`, `clarity` (`clear`, `partial`, `unintelligible`), `status`, `details` (a list of tags), `units` (ids such as `E36`), `alarm` (1 to 5 or null), `street`, `block` (the hundred block), and `cross_street`. Units and streets are only ones on a list (`beacon.radio.Known`): the story's own in the mock scenario (`beacon.scenario.radio.KNOWN`), and for real radio the operator's files, without which a row has no units, no street, and so no `place`. The lists of categories, statuses, and details are `CATEGORIES`, `STATUSES`, and `DETAILS` in `beacon/radio/vocabulary.py`. `row.place` is the place in words (`400 block of Hayes St`) and `row.point` is always None, so a `near` search finds radio only with `include_unlocated=True`: placing a block on the map is not done in ingest. `row.model` says which Ember heard it, and `confidence` is `medium` for a call heard `clear` and `low` otherwise. Units are how calls of one incident are tied together; a call often names units and no place.
- **A `notice` row** (`source == "notices"`) is **Ember's reading** of one document an authority published (`beacon.notice.read_notice`), and not the document. `row.url` is the PDF, which is the authority speaking; `row.model` says which Ember read it (`live` or `fake`); and `row.confidence` is `high`, not `official`, for that reason (`low` when Ember says the document is no notice). `row.occurred_at` is when the feed says it was published, and `row.until` is `effective_until` when the document gives one. `data` is `Notice.as_data()` plus `page` (the feed item's page, or null): `notice_type` (`evacuation_order`, `evacuation_warning`, `shelter_in_place`, `road_closure`, `all_clear`, `advisory`, `press_release`, `other`), `hazard`, `issuer`, `title`, `issued_at` and `effective_until` (ISO 8601 with an offset, or null), `areas`, `streets` (street names without numbers), `instruction`, `summary`, and `incomplete`. `beacon.notice.ORDERS` are the types that tell people to do something or that they may stop. How to use one:
  - **Never show a reading as the authority's words, and never tell anyone what to do from one.** Say that an authority issued a notice of this type, show the reading labeled as Beacon's reading of it, link `row.url` beside it every time, and send people to the document for the instruction. A reading can be wrong, and can leave out the exception that changes its meaning.
  - `data["incomplete"]` names the fields that are not all there: a text or the areas were more than Beacon keeps and were left out whole (never cut), or the streets were cut to the first thirty. When `instruction` or `areas` is in it, say that the notice has more and link it; do not present what is there as the whole.
  - `issuer`, `title`, `areas`, `streets`, `instruction`, and `summary` are empty for a `press_release` and for `other`, by design: those rows say that a document of that type was published, when, and where to read it. Do not put text back into them.
  - A notice row has no `place` and no `point`. Match it to an incident by `data["streets"]`, `hazard`, and time. An order's `areas` and `instruction` can name addresses finer than a block, as the order does; they are shown only as part of the reading, and never become a row's or an incident's `place`.
  - Nothing lifts an order but another row: an `all_clear` is a separate notice, and which order it lifts is the incident engine's to decide.
  - The redaction pass still reads a notice's text before it is shown: Ember is told to leave private people out, which is not a guarantee.
- **A `tap` row** (`source == "app"`) is one press of the button, written by `POST /api/taps` through `beacon.taps.Taps.take`. `row.ref` is the tap's id, `row.occurred_at` the server's time, `row.point` the position rounded to three decimals (about a block), and `row.confidence` is `low`. Its `origin` is `live` in the mock store too: a person's tap is nobody's stand-in, so `row.stand_in` is False, and what makes an answer a stand-in's is the other evidence it is built on and the store's `mode`. `row.data` has `accuracy_m` (metres, or null), `photo` (`{"bytes", "width", "height"}` or null), `clip` (`{"bytes", "seconds"}` or null), and `media_kept_until`. The files themselves are read with `request.app.state.taps.media(tap_id, "photo" | "clip")`, which returns the JPEG Beacon drew from the photo (at most 2048 pixels on a side) or the WAV of the clip, or None once the row's `media_kept_until` has passed, for a tap with no row, and for a kind its row does not say it sent: handle None, do not copy the bytes anywhere that outlives the hour, and keep only what was concluded from them. A call to Ember that sends a tap's photo or clip is `sensitive=True` (it can show or record a person), so it is paid for every time and cannot run in replay-only mode; test it with `EmberClient.fake`. Do not store or log the position a phone sent, and do not add anything to a tap that says who sent it.
- What `data` holds is documented where it is written: `beacon/ingest/nws.py` (`_alert`), `opensky.py` (`_aircraft`), `traffic511.py` (`_event`), for radio `beacon/radio/extract.py` (`Transmission.as_data`), and for a notice `beacon/notice/read.py` (`Notice.as_data`). Units are SI: metres, metres per second, degrees clockwise from north. A 511 row keeps the event's `schedule` as 511 sent it; the poller asks only for events in effect now.
- Anything shown from a `511` row carries the words "data provided by 511.org" with a link to https://511.org beside it: 511's data agreement requires it.
- The mock store and the live store are separate files, and a store is one or the other for life. Code that reads evidence takes an `EvidenceStore` as an argument; only an entry point (the server's startup, a script) calls `open_store`. In the server, the store the pollers write to is `request.app.state.ingest.store`, and `request.app.state.taps.store` is the same file, opened for the taps, which is there with `BEACON_INGEST=0` too. It is closed when the server shuts down: `store.close()` makes every later `add` raise `EvidenceStoreError`, while reading goes on working.
- **In tests**, make a store with `EvidenceStore(tmp_path / "evidence.sqlite3", "mock")` and add rows, or play the scenario: `build({"BEACON_DATA_DIR": str(tmp_path)}, Clock(lambda: moment)).catch_up()` from `beacon.ingest` stores everything the invented fire's sources said up to `moment`, where the story began twenty minutes before the `build`. `server/tests/test_ingest_setup.py` shows the whole story and when each row appears. The story's radio is in `beacon/scenario/radio.py` (`CALLS`): to hear a call of your own in a test, hand `extract_transmission` an `EmberClient.fake` whose responder answers with a filled-in form, as `server/tests/test_radio_extract.py` does.
- **A new source** is a module in `beacon/ingest/` that copies `nws.py`: a class with `name`, `interval_seconds`, a read-only `origin` property that returns its client's (`self._http.origin`), and a `poll(store, now)` that begins with `store.refuse_stand_in(self.origin)` and returns a `PollReport` (with `partial=True` when it did not read the source's answer to its end), a pure `parse_...` function that turns the source's reply into `NewEvidence` and counts what it could not read, the source's answers added to `beacon/scenario/`, and its name added to `SOURCES`, `source_pollers`, and the mock pollers in `setup.py`. Its tests answer through `SourceHttp(httpx2.MockTransport(...))` and write to a mock store. A poller does not choose its rows' origin: it is `http.origin`, which is `mock` for a client built with a transport and `live` only for one that asks the network, so what a stand-in answers cannot be stored as `live`.

## Using the incident engine

```python
from beacon.incident import open_engine

engine = open_engine(os.environ, store)  # in the server: request.app.state.incidents
for incident in engine.incidents():  # the last twelve hours, newest first
    incident.id, incident.title, incident.status, incident.open
    incident.place, incident.point  # no finer than a block; the point can be None
    incident.stand_in  # True when any row under it is a stand-in's
    for entry in incident.timeline:
        entry.text, entry.confidence, entry.sources  # what one source said, and which
    incident.official  # alerts and notices, each with its state
    incident.signals  # what code can say makes it more than an everyday call
engine.get(incident_id)  # one incident, or None
```

- **An incident is derived, never stored.** It is a function of the evidence rows (`build_incidents`), worked out again when the store has a new row. Do not add a table of incidents: a second record can disagree with the first. What a model later writes about an incident is kept under the incident's id and the evidence it was written from, beside the engine and not in it.
- **The engine asks no model.** Grouping, what is in force, circling, and signals are plain code with tests. Change a rule in `calls.py` or `build.py` with a test that fails without the change.
- **Every line names its rows.** An `Entry`, an `Official`, and a `Signal` carry the evidence ids they rest on, and an entry's confidence is its row's. A new kind of line does the same, with `source_of(row)`; a line that rests on several rows is trusted as little as the weakest (`weakest`).
- **Medical stays medical.** A medical call is grouped only with medical calls, its entry is "Medical response", units, and block, and its incident has no alarm, no official list, no signals, and no taps. Nothing downstream adds them back.
- **The engine never shows a notice's words.** `Official.issuer`, `headline`, and `instruction` are None for a notice, and a notice's entry says only that a document of its type was published, with its link. The words are a model's reading and may name a person: they go through the redaction pass, and are shown as a reading beside the link. Of a notice's streets only the incident's own are shown.
- **`published` is not `in_force`.** Beacon has read a document; it does not know that an order still holds. Say that an authority published it and whether a later notice lifts it.
- **`not_listed` is what Beacon saw.** Keep the words "not an official all-clear" with it.
- **Taps are counted.** An incident holds how many taps came from near it and none of their ids, because a tap's id is what its sender reads the answer with. Do not put a tap's id or point into an incident.
- An incident's `id` is a digest of the source and reference of the row that opened it, so it is the same in any store that holds the same story. It changes when that row leaves the engine's twelve hours.
- **In tests**, `server/tests/incident_rows.py` has `Rows`, which stores one row of each kind in a mock store, and `open_engine({}, store, clock=...)` reads it at a moment of your choosing. `server/tests/test_incident_engine.py` plays the whole mock scenario through the engine.

## Conventions

- Match the code that is there: a new route copies the shape of `server/beacon/api/health.py` and its test; a new web API module copies `web/src/api/health.ts` and uses `fetchJson`.
- A route that fails says why in a fixed sentence in `detail`, and never repeats what the client sent (see `_validation_error` in `server/beacon/main.py`): a tap can carry a location, a photo, or a microphone clip. A route that takes something private reads its body itself, as `server/beacon/api/taps.py` does, and its path is added to `PRIVATE_PATHS` in `main.py`, which keeps it out of uvicorn's access log and FastAPI's telemetry. Do not add another logger, tracer, or middleware that records requests without leaving those paths out. Work on what a stranger sent is bounded before it is done: admission and rate limits first, decoding after, and decoding of media only by the confined ffmpeg (`beacon.radio.audio.Ffmpeg`), never by a library in Beacon's own process. `Ffmpeg` raises `ConversionUnavailable` when the host could not run it just then, which says nothing about the file: answer `503` or try again later, and never count the file out for it. The server marks itself not dumpable before it serves a request (`forbid_core_dumps` in the lifespan), and does not start when that fails on a host with a core collector; a program it starts does not inherit the mark, so one that is handed private bytes is started only where `core_dump_problem()` is None. `TapMedia` holds a lock on its directory, so build one per data directory per process, and `close()` it.
- Deterministic code decides and is tested; Ember writes and judges. Keep model calls out of code that groups, measures, or filters.
- Comments say why, in plain sentences. Test names are sentences that state the behavior.
