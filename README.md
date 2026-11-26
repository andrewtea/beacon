# Beacon

Beacon is a "What's that?" button for your city. You hear a siren or see a helicopter, tap once, and Beacon says what is going on, which crews are on it, roughly where, and when it cleared, with a source on every claim. Behind the button, Muse Ember listens to San Francisco's public fire and EMS dispatch radio, reads aircraft positions, alerts, and notices, and stitches them into one incident record per event.

Beacon is being built for the Meta Muse Ember Hackathon (Nov 23 to Dec 2, 2026). The rules, the $10 budget, and the submission checklist are in [HACKATHON.md](HACKATHON.md).

**Status:** scaffold, Ember client, evidence store, and four pollers. The web app is an installable shell that shows whether the server is reachable. The server stores evidence rows and runs pollers for National Weather Service alerts, OpenSky aircraft positions, 511 road closures, and OpenMHz dispatch radio, each call of which Ember hears and turns into a row. **By default the pollers are fed an invented scenario, not real sources:** a made-up three-alarm fire in Hayes Valley, told in each source's own format, and every row from it is stored as `mock`. Its radio calls are generated tones, and what was "said" in them is filled in by a stand-in for Ember, so its radio rows are a stand-in's twice over. An early version of the NWS poller was run once against the real API; nothing else has been. The 511 poller has run only against mocked HTTP, because there is no token. The OpenSky poller has too, and **may not be pointed at the real API yet:** OpenSky's terms require a written license for a running service, which nobody has asked for. **OpenMHz has never been asked anything:** using its audio needs its permission, which Beacon does not have, and the shapes of its API are read from its server's source and unverified. **No real Ember call has been made:** there is no API key, so the client has run only against a mock of the Meta Model API in tests, and its request shapes come from Meta's documentation, not from an answer of Ember's. Nobody knows yet how well Ember hears a radio call. The audio token rate the budget rests on is [still a guess](#measure-the-audio-token-rate). No incident exists.

## Layout

| Path | What it holds |
| --- | --- |
| [`web/`](web) | React + Vite + TypeScript PWA, installable on a phone and usable on a desktop. |
| [`server/`](server) | Python 3.12 + FastAPI server. Every route is under `/api`. |
| [`scripts/`](scripts) | Repository scripts: `check` runs what CI runs, `measure_audio_rate.py` measures what Ember bills for audio, and `generate-icons.sh` redraws the PWA icons from `web/public/favicon.svg`. |
| `data/` | Local runtime data. Ignored by Git, never committed. |

Inside `server/beacon/`, each package has one concern so pull requests stay apart:

| Package | What it holds |
| --- | --- |
| `api/` | The HTTP routes. |
| `ember/` | The Muse Ember client, the only code that calls the Meta Model API. |
| `evidence/` | The evidence store: one SQLite row for each thing a source said. |
| `ingest/` | One poller per source, the loop that runs them, and how they are built from the environment. |
| `radio/` | What is kept of a dispatch radio call, the Ember call that hears one (`extract_transmission`), and the conversion of its audio. |
| `scenario/` | The mock scenario: an invented San Francisco fire, served to the pollers in each source's format. |

## Run locally

You need [uv](https://docs.astral.sh/uv/), Node.js 26, and pnpm 12. uv installs Python 3.12 itself. [ffmpeg](https://ffmpeg.org) and [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) are needed only to hear real radio, on Linux, and for the tests of that conversion, which are skipped without them (CI installs both).

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

Tests never call the Meta Model API or a live data source: the pollers' tests answer from hand-written replies and the mock scenario, and the radio tests hand generated tones to a fake Ember. Two entry points enforce the environment for that: `scripts/check`, which CI also runs, sets `REPLAY=1` and removes `MODEL_API_KEY` before it checks either side, and server pytest does the same however it is started, because `server/tests/conftest.py` does it. CI holds no key. A web test run directly with `pnpm test` keeps whatever environment the shell has; nothing in `web/` reads the key. As a backstop for the server tests, `server/tests/conftest.py` patches Python's `socket.connect` and `socket.connect_ex` in the test process to raise `NetworkBlockedError` for any address that is not this machine. It is a backstop and not a sandbox: a test that catches that error still passes, and the patch does not reach DNS lookups, subprocesses, or native code that opens sockets without Python's `socket` module.

## Configuration

Settings come from the environment. The server does not load the repository-root `.env` file: export a variable in your shell or put it in front of the command. (Vite loads `web/.env` for the web app, as it does by default, and exposes only names that start with `VITE_` to the browser. Beacon has no such file and no such setting.)

[`.env.example`](.env.example) lists the settings. The Ember client reads the first five (`EmberClient.from_env`), and the server reads the rest when it starts. The two `EMBER_` settings are read from the process's environment and nowhere else: a mapping handed to `from_env` can choose the mode, the key, and the data directory, and cannot supply or change the spending settings.

| Variable | What it does | When unset |
| --- | --- | --- |
| `REPLAY` | `1` answers from recorded Ember responses and never calls the API. `0` calls the API for a request with no recording. | `1` |
| `MODEL_API_KEY` | The Meta Model API key. Read only when `REPLAY=0`, which is refused without it. | No live calls. |
| `EMBER_STOP_USD` | The most the Ember ledger may hold in live spend, in dollars. | `3.00` |
| `EMBER_SPENT_BEFORE_USD` | The total spend against the $10 credit, for every model, that Meta's Usage page showed before Beacon's first call, in dollars. `0` is an answer; it has no default. | No live calls: a client that can spend is refused. |
| `BEACON_DATA_DIR` | Where runtime data goes. An absolute path; a relative one is refused. | The repository's `data/`. |
| `BEACON_LIVE_SOURCES` | The sources to poll for real, as a list: `nws`, `opensky`, `511`, `openmhz`. Naming any source turns the mock scenario off for all of them. | Every poller runs on the mock scenario, and nothing asks a real source. |
| `BEACON_511_API_KEY` | The [511 SF Bay token](https://511.org/open-data/token). Needed when `BEACON_LIVE_SOURCES` names `511`. | 511 cannot be named. |
| `BEACON_OPENSKY_PERMITTED` | `1` says OpenSky has given Beacon the written license its terms require. Needed when `BEACON_LIVE_SOURCES` names `opensky`. | OpenSky cannot be named. |
| `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` | An OpenSky API client, for 4,000 requests a day in place of 400. Set both or neither. | OpenSky is asked without an account, every four minutes. |
| `BEACON_OPENMHZ_PERMITTED` | `1` says OpenMHz has given Beacon permission to use its audio. Needed when `BEACON_LIVE_SOURCES` names `openmhz`, with the talkgroups and the budget below, `REPLAY=0`, `MODEL_API_KEY`, `EMBER_SPENT_BEFORE_USD`, and ffmpeg and bubblewrap on Linux. | OpenMHz cannot be named. |
| `BEACON_OPENMHZ_TALKGROUPS` | The talkgroups to hear, as a list of OpenMHz's numbers. Every call on them is paid for. | OpenMHz cannot be named. |
| `BEACON_OPENMHZ_EMS_TALKGROUPS` | The ones among them that carry EMS traffic only. Every call on one is kept as a medical response. | No talkgroup is treated as EMS-only; a call is medical when Ember hears it as one. |
| `BEACON_OPENMHZ_BUDGET_USD` | The most, in dollars, that hearing radio may cost over every run. It has no default. | OpenMHz cannot be named. |
| `BEACON_OPENMHZ_MEDIA_HOSTS` | Hosts a call's audio may be fetched from besides `openmhz.com` and its subdomains, as a list. | Audio anywhere else is not fetched. |
| `BEACON_RADIO_STREETS_FILE` | A text file of the street names a real radio row may name, one to a line (`Hayes St`); lines that begin with `#` are skipped. | A real radio row names no street, and so has no place. |
| `BEACON_RADIO_UNITS_FILE` | A text file of the unit ids a real radio row may name, one to a line (`E36`). | A real radio row names no unit. |
| `BEACON_INGEST` | `0` starts the server without its pollers. | `1`: the server runs the pollers. |

The server's routes do not build an Ember client yet, so today the Ember settings are read by the two commands under [Ember client](#ember-client) and nothing else. The server reads the source settings when it starts, and `BEACON_DATA_DIR` with them. Three places set any of them. `scripts/check`, and so CI, sets `REPLAY=1` and removes `MODEL_API_KEY`. Server pytest does the same, removes the spending settings and every source setting, and points `BEACON_DATA_DIR` at a temporary directory for each test. The orb's API service in [`.amp/services.yaml`](.amp/services.yaml) sets `REPLAY=1`.

## Evidence and ingest

Each source has a poller in [`server/beacon/ingest/`](server/beacon/ingest) that writes **evidence** rows to SQLite through [`server/beacon/evidence/`](server/beacon/evidence). A row is one thing one source said: a weather alert, one position of an aircraft, one version of a road closure, one radio call. Later phases read the rows back by kind, time, and place to build incidents and answer taps.

| Column | What it holds |
| --- | --- |
| `kind` | `radio`, `aircraft`, `alert`, `traffic`, `notice`, or `tap`. `notice` and `tap` are not written yet. |
| `source`, `ref` | The feed (`nws`, `opensky`, `511`, `openmhz`) and the source's own name for the item. Together they name one row, so an item a source sends twice is stored once. |
| `occurred_at`, `until` | When it happened according to the source, and when it stops applying, for an alert. |
| `point`, `place` | A latitude and longitude, and the place in words. Either can be missing: a city-wide alert has no point. |
| `confidence` | `official` for an authority speaking for itself, else `high`, `medium`, or `low`. |
| `origin` | `mock` for the invented scenario, `live` for a real source. |
| `model` | Which kind of Ember client wrote the row's data, when a model did: `live`, `replay`, or `fake`. |
| `data` | What the source said, as JSON in a shape of the row's kind. Each poller's module documents its own. |
| `url` | Where a person can read the original, when it is public. |

Rows are never changed. A source that revises what it said gets a new row: an NWS update is a new message, and each version of a 511 event is stored under the event's id and its update time.

**An item that goes missing from a source's answers has not been officially ended.** NWS and 511 answer with what is active now, and mostly an alert or closure that is over is simply absent from the next answer. NWS can also cancel an alert outright, with a `Cancel` message: that is a row like any other, and it is the authority speaking, so it stays `official`. Absence is different. When an item has been missing from two answers in a row that were read in full, and for an alert before the time it said it would end, the poller stores a row whose `data` says `"change": "not_listed"` and names the row it is about. That row is what Beacon saw, not an all-clear from an authority, so its confidence is `medium` and not `official`. When the same item is listed again, as night road work is every night, a `"change": "listed_again"` row says so.

| Source | What is asked | Kept | Terms |
| --- | --- | --- | --- |
| [NWS](https://www.weather.gov/documentation/services-web-api) | Active alerts for San Francisco's forecast zone and county, every 2 minutes. No key. | Every `Actual` alert, including civil messages NWS relays, such as a shelter-in-place warning. | Open data, free to use for any purpose. |
| [OpenSky](https://openskynetwork.github.io/opensky-api/rest.html) | Aircraft in a box around the city, every 4 minutes without an account or every 30 seconds with one. | Aircraft that are airborne below 3,000 m: what a person on the street could notice. | **Needs a written license first.** [OpenSky's terms](https://opensky-network.org/about/terms-of-use) allow the REST API for non-profit research and education, and require a written license for use "in any operational capacity", whoever asks. Beacon refuses to poll it until `BEACON_OPENSKY_PERMITTED=1` says the license was given. |
| [511 SF Bay](https://511.org/open-data/traffic) | The traffic events in effect now, for the whole Bay Area: one request every 2 minutes, and a poll that needs several pages waits that long for each. Needs a token. | Events inside the city, each with its schedule. | [511's data agreement](https://511.org/sites/default/files/2026-04/511_Data_Agreement_Final_2026.pdf) licenses storing, displaying, and deriving from the data. Wherever the app shows it, it must say "data provided by 511.org" with a link to 511.org. |
| [OpenMHz](https://openmhz.com) | The calls on the fire and EMS talkgroups of San Francisco's P25 system, every 30 seconds, and each call's audio. | One row for each call Ember heard, in the closed vocabulary described under [Radio](#radio). Never the audio or a transcript. | **Needs OpenMHz's permission first**, which has not been given; Beacon refuses to poll it until `BEACON_OPENMHZ_PERMITTED=1`. Beacon publishes summaries, never the audio. |

### Radio

A radio call is the one kind of evidence a model makes: the poller downloads a call's audio and Ember hears it (`extract_transmission` in [`server/beacon/radio/`](server/beacon/radio)). Dispatch radio names people, describes patients, and gives house numbers, so this is what ingest guarantees about a call, before any later redaction:

- **Nothing a call said is stored as text.** There is no transcript. Ember fills in a form, and each field is checked against a closed vocabulary before anything is kept: a category, a status, and detail tags from fixed lists, and the hundred block of an address (`1458` is kept as `1400`). A unit id or a street name is kept only when it is **on a list**: `BEACON_RADIO_UNITS_FILE` and `BEACON_RADIO_STREETS_FILE` for real radio, and the story's own units and streets in the mock scenario. A shape is not enough, because `BP 180` has the shape of a unit and `One Two Three Hayes St` the shape of a street. With no list, a row names no unit and no street. What does not fit is dropped, whatever Ember wrote.
- **A medical call is "medical response", its crews, and its block.** Its status, details, and alarm level are not kept. That holds for a call Ember hears as medical and for every call on a talkgroup named in `BEACON_OPENMHZ_EMS_TALKGROUPS`, whatever Ember made of it.
- **The audio is never written to a file**, not even while ffmpeg decodes it: it is handed over as a file that exists only in memory. ffmpeg runs confined by bubblewrap, with no network, no environment, and no directory to read but the ones a system keeps its programs and their libraries in (`/usr/bin`, `/usr/lib`, and the like, not `/usr/local` or `/usr/share`), with Beacon's data directory covered over wherever it is, and under limits on its memory, time, and output. Two things are left to the host. **Swap:** run real radio with swap off or encrypted; nothing checks it. **Core dumps:** Beacon and ffmpeg both run with a core size limit of nothing, which Linux ignores when cores are piped to a collector such as systemd-coredump or apport, so Beacon refuses to hear real radio on a host whose `kernel.core_pattern` begins with `|`. Set it to a file name, such as `core`.
- **Ember is sent a WAV file Beacon made itself**, whatever the call arrived as, so Beacon knows how long the audio really plays before it pays, and nothing else a file carried is sent.
- **The Ember call is a sensitive one**, so the Ember ledger and trace hold its tokens and cost and none of its audio or its answer, and it is never recorded for replay.
- A radio row has a place in words and no point: nothing in ingest knows where on the map a block is.

The limits of this: a name that is on a list is kept whatever Ember meant by it, so `M72` is kept as Medic 72 even if what was said was a man of 72, and the hundred block is a number Ember gave. And the form is only as good as Ember's hearing, which has not been measured: a row heard `clear` is `medium` confidence, and anything less is `low`. No list of San Francisco's streets or of its fire department's units is in the repository, and nobody has put one together yet: whoever turns real radio on supplies both and names the files. The city's open-data portal, [DataSF](https://datasf.org/opendata/), is the place to look first.

Every call is paid for, once. The poller hears only the talkgroups it is given, passes over a call shorter than a second or longer than two minutes (by its listing, and then by its decoded audio), hears at most 20 calls every 30 seconds, and makes its calls under a budget of its own (`BEACON_OPENMHZ_BUDGET_USD`, the spending scope `ingest:openmhz`) inside the client's spending stop. Before Ember is asked about a call, the call is **claimed** in the evidence store, and a claimed call is never asked about again, by this process or another: not after a crash, a row that could not be stored, or an answer that was no form. The claim is given up only when the paid request is known not to have been sent. So a process that dies between Ember's answer and the row loses that call, and does not pay for it twice.

When the spending stop or the budget refuses a call, or Ember fails on one, the poller asks nothing for ten minutes, counted from the failure and kept in the store, so a restart does not shorten it; the same after three answers in a row that were no form. Calls that grew older than fifteen minutes meanwhile are passed over. If the Ember ledger is replaced while it runs, the poller stops until Beacon is started again. Each poll asks again for the five minutes before the newest call it has finished with, because OpenMHz lists a call only when its upload arrives; calls it has already taken are told apart by their ids.

Shutting the server down admits nothing new: a download, a conversion, or a token count is called off unpaid, and a call the Ember client had already admitted is waited for, for up to about two minutes, so that its answer is stored. If that wait runs out, the server logs it and exits: the request can still be sent and paid for, it is settled in the Ember ledger, its row is lost, and the call is not asked about again.

### Mock and live

**With `BEACON_LIVE_SOURCES` unset, which is the default, no real source is asked anything.** The pollers are the real ones, and where the network would be stands the mock scenario in [`server/beacon/scenario/`](server/beacon/scenario): an invented three-alarm fire on the 400 block of Hayes St, told as what NWS, OpenSky, 511, and OpenMHz would have answered minute by minute. Twenty radio calls dispatch the first alarm, call a second and a third, order an evacuation, send a medic to a block nearby, and clear the units; a helicopter circles, 511 closes Hayes St and widens the closure, a shelter-in-place warning is issued, narrowed, and dropped, and the closure stops being listed. Nothing in it happened; its texts say "Invented for Beacon's mock scenario", and its rows have the origin `mock`. The radio calls are tones, and a stand-in for Ember (`beacon.scenario.stand_in_ember`) fills in what was "said" in each, so a radio row also has the model `fake`. The story begins twenty minutes before a mock store is first opened and is played in real time for 105 minutes, after which nothing more is stored. A restart goes on from where the story had been played to, and a step in which a poller failed is played again ten seconds later, not passed over. To play it again, stop the server and delete `data/evidence/mock.sqlite3` with its `-wal` and `-shm` files.

There are two store files, `data/evidence/mock.sqlite3` and `data/evidence/live.sqlite3`, and **the live store refuses any row a stand-in had a part in**: one from the mock scenario, or one a fake Ember wrote. So a source that is not named in `BEACON_LIVE_SOURCES` does not run beside the live ones; it is off. Anything read from the mock store is a stand-in's, and has to be labeled as one wherever it is shown.

To poll a source for real, name it and set what it needs. No code changes:

```sh
BEACON_LIVE_SOURCES=nws,511 BEACON_511_API_KEY=... \
  uv run uvicorn beacon.main:app
```

`opensky` can be added to that list only with `BEACON_OPENSKY_PERMITTED=1`, and only once OpenSky has given a written license (write to contact@opensky-network.org). Having an account or an API client is not that license.

`openmhz` can be added only once OpenMHz has given permission to use its audio, and it is the one source that spends the Ember credit:

```sh
BEACON_LIVE_SOURCES=nws,openmhz BEACON_OPENMHZ_PERMITTED=1 \
  BEACON_OPENMHZ_TALKGROUPS=... BEACON_OPENMHZ_EMS_TALKGROUPS=... BEACON_OPENMHZ_BUDGET_USD=0.25 \
  BEACON_RADIO_STREETS_FILE=/path/to/streets.txt BEACON_RADIO_UNITS_FILE=/path/to/units.txt \
  REPLAY=0 MODEL_API_KEY=... EMBER_SPENT_BEFORE_USD=... \
  uv run uvicorn beacon.main:app
```

The talkgroup numbers are OpenMHz's for the San Francisco fire and EMS channels; nobody has looked them up yet, because that means asking OpenMHz. If a call's audio turns out to be served from a host that is not under `openmhz.com`, the poller counts the call as unreadable until the host is named in `BEACON_OPENMHZ_MEDIA_HOSTS`. Run [the audio-rate measurement](#measure-the-audio-token-rate) before the first real poll: what a call costs is still a guess.

The server runs the pollers on a thread of its own, so run one server process: two would ask every source twice as often. `GET /api/sources` says which mode the server is in and how each poller's last poll went, including whether it was `partial`: a 511 answer that ran past four pages, or whose pages did not say plainly where they stood, is stored as far as it was read, and events on the pages that were not read are missing until a later poll reads them. To poll without the server:

```sh
cd server
uv run python -m beacon.ingest            # poll once, print what the store holds
uv run python -m beacon.ingest --follow   # keep polling
```

A request to a source is given 60 seconds from the lookup of its name to the last byte of its reply, however slowly the source sends, and a reply that is cut short is refused whole. A source that asks for a wait gets it, counted from when it answered and up to a week: `Retry-After` as a number of seconds or as a date, and OpenSky's own header of seconds. The one thing a deadline cannot call off is the lookup of a source's name, which goes on until the system's resolver gives up; Beacon keeps at most two such lookups waiting however many requests time out. When the server shuts down, a request that is out is cancelled and a write that is waiting gives up, so nothing is asked or stored after shutdown; a poll is stored whole or not at all, and is made again at the next start.

The store files are SQLite in write-ahead mode, so each has a `-wal` and a `-shm` file beside it while it is in use, and the newest rows can be in the `-wal` file alone. Do not copy the files of a store that is in use: copies made one after another can each be from a different moment. To copy one while Beacon runs, let SQLite do it, `sqlite3 data/evidence/live.sqlite3 ".backup copy.sqlite3"`; otherwise stop every process that uses the store first. Stop them as well before deleting the mock store to play its story again, and delete its `-wal` and `-shm` files with it.

A poller's failure is logged as one fixed sentence, such as `511 answered HTTP 401.`, and never with the address that was asked, which for 511 holds the token, or with anything the source replied. The HTTP libraries' own request and header logs are switched off for the same reason, at every level.

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
| `ledger.sqlite3` | One `ember_calls` row per call: task, tokens, cached tokens, cost, latency, model, effort, scope, whether the call was `live`, `replay`, or `fake`, and its request and response, or for a sensitive call the sizes of them. Spend is the sum of the `live` rows. A row is named from outside by its response's `ledger_ref`: the file's generation, the row id, and a random token of the row's own. |
| `trace.jsonl` | The submission's token trace: every ledger row as one JSON line. It is written from the ledger on request, by the command below. |
| `replay/<request hash>.json` | Recorded responses, keyed by a SHA-256 of the request. A sensitive call has none. |

```sh
cd server
uv run python -m beacon.ember   # writes trace.jsonl and prints the live spend and the stop
```

### What is stored

Every call says whether it is `sensitive`: whether its request or its answer can hold what Beacon must not store, such as a patient's details, a name, or a house number in EMS radio traffic.

- **An ordinary call** (`sensitive=False`) is stored in full. The ledger, the trace, and the recordings hold the request's text and Ember's response as it wrote it, including the arguments of its tool calls, which can be free text. Audio, images, and files are replaced by a length and a digest in all three, so no radio audio is stored, but the words of a transcription or an extraction are.
- **A sensitive call** (`sensitive=True`) leaves its accounting and nothing of what was said. Its ledger row and trace line hold the task, scope, tokens, cost, latency, model, effort, the response's status, the SHA-256 of the whole request, and the size of each field in place of its content. Everything kept is a number, a value the client wrote itself, or one of a fixed set of words: no string the API or the caller chose is copied, so the response's id is not kept, and a failure is stored as `HTTP 400` without the API's error type. The answer is not recorded for replay and is never replayed, so the same call is paid for every time, and replay-only mode refuses it. The caller still gets the full answer, and what the caller stores from it is the caller's to strip. When a sensitive call fails, whether at the API, in a stand-in, or in the client's own check of the request, the caller gets one of the client's errors with a fixed text: the kind of failure, and the HTTP status as a number when there is one. It does not carry the API's message, the value that was refused, or another error chained under it, including one the caller was handling when it made the call, so its message and its printed traceback repeat nothing that was sent. A traceback captured with local variables still holds the request, as the caller's own frames do for any call: do not log locals.

**Any call whose input is EMS radio traffic, or anything else that can name a person or a home, must be made with `sensitive=True`.** The request's SHA-256 is still stored, and someone who holds the exact request can confirm it was made. The `task` label and a `SpendScope`'s label are stored as given for every call, so they must name the kind of work (`extract_transmission`, `tap:1842`) and never what it is about.

An API error is stored without its message: as its HTTP status and error type for an ordinary call, and as its HTTP status alone for a sensitive one. Read the trace before sharing it. The client uses POSIX file locks, so it runs on Linux and macOS.

### The spending stop

The plan's hard stop is $3 above what Meta's Usage page showed on Nov 26, 2026, the day the ledger began. The ledger counts only Beacon's own calls, so that is **$3.00 of live spend in the ledger** (`EMBER_STOP_USD`), whatever the page showed. The client reserves each call's most before sending it, in one transaction, so calls made at once cannot pass the stop together.

The ledger cannot see what the account had spent before it began, and the credit can: the stop and the earlier spend together may not pass $9.00 of the $10.00 credit. So `EMBER_SPENT_BEFORE_USD` has no default. It is the total for every model with no model filter, because every model is paid for from the one credit; the submission's screenshot, filtered to Muse Ember, can show less. **Nobody has read that total off the Usage page yet**, and until someone sets the variable, to `0` if that is what the page shows, every client that can spend is refused when it is built. Spend made outside Beacon after the ledger began is as invisible to it: add it to the variable. A client reads both settings once, when it is built, and keeps that stop, so **after changing either one, restart every process that makes paid calls**. A guess of $0.00 would let an account that had already spent $8.00 spend $3.00 more. Replay-only and fake clients need neither the key nor this setting.

The stop is the process environment's for every live client. `EmberClient.from_env`, `EmberClient.live`, and the constructor all read both settings from `os.environ`, none takes them as an argument, and `stop_nanousd` can lower the stop for one client and cannot raise it. So `EMBER_STOP_USD=0` stops all paid calls whichever way a client was built and whatever mapping it was built from.

**Every process that can spend must use the same ledger file, and the file must outlive the process.** The stop is counted in one ledger: a second ledger is a second $3.00. That is why a relative `BEACON_DATA_DIR` is refused, as is a live client handed a relative ledger path (either would name a different file from each working directory), and why a deployment must put the data directory on storage that survives a restart.

A live call that was sent and did not come back with a usable answer, such as a timeout or any error response, may still have been billed, so the ledger keeps it at the most it could have cost. Only a call that never left the machine is booked at $0. That makes the ledger an upper bound on Beacon's spend; compare it with Meta's [Usage page](https://dev.meta.ai/usage) every evening.

The ledger is the only record of what was spent. When its file is deleted or replaced under a client that is already built, that client logs calls that spend nothing in the new file and refuses every paid call with `LedgerReplacedError`, because the stop would otherwise be counted from zero. It checks before a call's first request, when it reserves the call, and again immediately before the request that costs money. A file that goes while that request is out cannot be caught in time: the call may have been billed and is in no ledger, the client raises `LedgerReplacedError` saying so instead of returning the answer, and **Meta's Usage page has to be compared with the ledger by hand before any more paid calls are made**. A client built after the file was deleted counts from zero: deleting the ledger forgets its spend, so do not delete it during the hackathon.

A running client tells its ledger file by the generation written in it and by which file is at the path, so it also refuses paid calls after an older copy is put back by renaming it over the ledger. **It cannot tell a ledger rolled back in place**, as a filesystem snapshot or a copy written over the same file does: the file is the same one and holds less than was spent. Restoring a ledger by any means needs every process that spends stopped first, and the ledger compared with the Usage page before they start again.

### Measure the audio token rate

**Not done yet: it needs a key.** The budget rests on a guess of 25 to 30 input tokens per second of audio. This command makes the plan's first ten real calls to check it, one per clip of a known length, and fits the rate from what each was billed:

```sh
REPLAY=0 MODEL_API_KEY=... EMBER_SPENT_BEFORE_USD=... \
  uv run --project server python scripts/measure_audio_rate.py
```

Its calls are booked under one scope in the ledger, `measure-audio-rate`, which is the same on every run. `--budget` (default $0.05) is therefore the most the measurement may spend **over all its runs** in one ledger, not new money for each run: once the scope holds that much, raise `--budget` to measure again.

The clips are generated tones, which tell the rate only if Ember bills audio by its length. Their answers are recorded, so a rerun with the same clips replays them at no cost; a clip with no recording is a new paid call, and without `REPLAY=0` the command stops and says so. `--clips DIR` sends a folder of WAV files instead: use recordings of the kind Beacon will send, and only ones we have the rights to send. Because those can be radio traffic, their calls are sensitive ones: nothing of them is recorded, and every run pays for them again, under the same budget. The command prints such clips as `clip-1`, `clip-2`, numbered in the order of their file names, and never prints a file's or the folder's name. A WAV file that is cut short of the length its header declares is refused.

## Rules Beacon is built under

- Every model call goes through Muse Ember on the Meta Model API, from one client that tracks tokens and spend and stops before the budget is passed.
- The answer to a tap is the product. The map is secondary.
- A photo only adds evidence to an incident. Beacon is not an image analyzer.
- EMS traffic is described as "medical response" with crews and a block-level location, never the patient.
- Beacon relays official orders and never tells anyone to stay or go on its own.
- Beacon publishes summaries, never raw radio audio, and uses only data it has the rights to use.

[AGENTS.md](AGENTS.md) holds the conventions for contributors and coding agents.
