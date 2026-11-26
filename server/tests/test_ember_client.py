"""The Ember client, driven through the real OpenAI SDK with the HTTP layer replaced.

Nothing here reaches the network: `MetaApi` stands in for api.meta.ai as an httpx2
transport, so the tests cover the URLs, headers and bodies the SDK really sends.
"""

import base64
import datetime
import email.utils
import json
import os
import shutil
import sqlite3
import textwrap
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import (
    BudgetExceededError,
    EmberCallError,
    EmberClient,
    EmberClosedError,
    EmberConfigError,
    EmberError,
    EmberRequestError,
    InputTooLargeError,
    LedgerReplacedError,
    Pacer,
    RateLimitHoldError,
    ReplayMissError,
    Script,
    SpendingStop,
    SpendScope,
    Storage,
    fake_response,
    read_scope_spend,
)
from beacon.ember.__main__ import main
from beacon.ember.client import default_data_dir
from beacon.ember.ledger import Call, Ledger, RowRef

# The usage example from https://dev.meta.ai/docs/prompt-caching.
USAGE = {
    "input_tokens": 1847,
    "input_tokens_details": {"cached_tokens": 1792},
    "output_tokens": 98,
    "output_tokens_details": {"reasoning_tokens": 40},
    "total_tokens": 1945,
}
# 55 uncached input tokens at $0.10/M, 1,792 cached at $0.004/M, 98 output at $0.50/M.
USAGE_COST_NANOUSD = 55 * 100 + 1792 * 4 + 98 * 500

# What a call with 1,200 input tokens and a 1,024-token output limit can cost at most.
INPUT_TOKENS = 1200
MAX_OUTPUT_TOKENS = 1024
RESERVED_NANOUSD = 1200 * 100 + 1024 * 500

# What a sensitive call is asked, kept out of the lines a traceback quotes.
SAID = "Jane Doe, left heel"


def completed(
    text: str = "Engine 3 and Medic 52, 16th and Mission.", **overrides: Any
) -> dict[str, Any]:
    return {
        "id": "resp_abc123",
        "object": "response",
        "status": "completed",
        "model": "muse-ember-1.0",
        "output": [
            {"id": "rs_abc123", "type": "reasoning", "summary": []},
            {
                "id": "msg_abc123",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
        "usage": USAGE,
        **overrides,
    }


class MetaApi:
    """An httpx2 transport handler that answers like the Meta Model API."""

    def __init__(
        self,
        *,
        input_tokens: int = INPUT_TOKENS,
        create: Callable[[httpx2.Request], httpx2.Response] | None = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.create = create or (lambda _request: httpx2.Response(200, json=completed()))
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/v1/responses/input_tokens":
            return httpx2.Response(
                200, json={"object": "response.input_tokens", "input_tokens": self.input_tokens}
            )
        if request.url.path == "/v1/responses":
            return self.create(request)
        return httpx2.Response(404)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def http_client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))

    def client(self, storage: Storage, **options: Any) -> EmberClient:
        return EmberClient.live(
            storage, api_key="test-key", http_client=self.http_client(), **options
        )


@pytest.fixture(autouse=True)
def account_that_had_spent_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process's setting that a client needs before it can spend. A test about a
    different earlier spend, or none, sets or removes it."""
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


def ask(ember: EmberClient, **overrides: Any):
    arguments = {
        "task": "extract_transmission",
        "effort": "low",
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "sensitive": False,
        "input": "Summarize the transmission.",
    }
    return ember.respond(**(arguments | overrides))


def ledger_rows(storage: Storage) -> list[dict[str, Any]]:
    with sqlite3.connect(storage.ledger_db) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM ember_calls ORDER BY id")]


def trace_lines(storage: Storage) -> list[dict[str, Any]]:
    path, _calls = EmberClient.replay_only(storage).export_trace()
    return [json.loads(line) for line in path.read_text().splitlines()]


def persisted(storage: Storage) -> str:
    """Everything the client writes: ledger rows, the exported trace, and recordings."""
    trace_lines(storage)
    recordings = "".join(path.read_text() for path in storage.replay_dir.glob("*.json"))
    return json.dumps(ledger_rows(storage)) + storage.trace.read_text() + recordings


def encoded(content: bytes) -> str:
    return base64.b64encode(content).decode()


def test_a_live_call_is_sent_to_ember_with_an_output_limit_and_logged(storage: Storage) -> None:
    api = MetaApi()

    answer = ask(api.client(storage), instructions="You log dispatch radio.")

    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]
    # The count covers everything that becomes model input, instructions included.
    assert json.loads(api.requests[0].content) == {
        "model": "muse-ember-1.0",
        "input": "Summarize the transmission.",
        "instructions": "You log dispatch radio.",
        "reasoning": {"effort": "low"},
    }
    sent = api.requests[1]
    assert sent.url.host == "api.meta.ai"
    assert sent.headers["authorization"] == "Bearer test-key"
    assert json.loads(sent.content) == {
        "model": "muse-ember-1.0",
        "input": "Summarize the transmission.",
        "instructions": "You log dispatch radio.",
        "max_output_tokens": 1024,
        "reasoning": {"effort": "low"},
        "store": False,
        # Without stored responses, reasoning items can only be resent encrypted.
        "include": ["reasoning.encrypted_content"],
    }
    assert answer.output_text == "Engine 3 and Medic 52, 16th and Mission."
    assert answer.source == "live"
    assert answer.usage.cost_nanousd == USAGE_COST_NANOUSD

    (row,) = ledger_rows(storage)
    assert row["latency_ms"] >= 0
    assert json.loads(row.pop("request"))["input"] == "Summarize the transmission."
    assert json.loads(row.pop("response"))["id"] == "resp_abc123"
    assert len(row.pop("token")) == 16
    assert {key: row[key] for key in row if key not in ("id", "started_at", "latency_ms")} == {
        "source": "live",
        "status": "ok",
        "task": "extract_transmission",
        "model": "muse-ember-1.0",
        "effort": "low",
        "max_output_tokens": 1024,
        "request_hash": answer.request_hash,
        "input_tokens": 1847,
        "cached_tokens": 1792,
        "output_tokens": 98,
        "reasoning_tokens": 40,
        "cost_nanousd": USAGE_COST_NANOUSD,
        "response_id": "resp_abc123",
        "error": None,
        "scope": None,
    }
    (line,) = trace_lines(storage)
    assert line["cost_usd"] == pytest.approx(0.000061668)
    assert line["request"]["input"] == "Summarize the transmission."
    assert line["response"]["id"] == "resp_abc123"
    assert (line["task"], line["effort"], line["input_tokens"]) == (
        "extract_transmission",
        "low",
        1847,
    )


def test_a_repeated_request_is_replayed_without_calling_the_api_or_spending(
    storage: Storage,
) -> None:
    api = MetaApi()
    first = ask(api.client(storage))
    calls_after_first = len(api.requests)

    # Replay-only mode holds no SDK client at all, so it cannot reach the API.
    again = ask(EmberClient.replay_only(storage))
    live_again = ask(api.client(storage))

    assert len(api.requests) == calls_after_first
    assert again.output_text == live_again.output_text == first.output_text
    assert again.latency_ms == first.latency_ms
    assert [row["source"] for row in ledger_rows(storage)] == ["live", "replay", "replay"]
    assert EmberClient.replay_only(storage).spent_usd() == pytest.approx(0.000061668)


def test_each_response_names_its_ledger_row_and_the_rows_read_back_by_id(
    storage: Storage,
) -> None:
    api = MetaApi()
    live = ask(api.client(storage))
    replayed = ask(EmberClient.replay_only(storage))
    fake = ask(EmberClient.fake(storage, lambda _body: fake_response(text="ok", input_tokens=10)))

    assert [live.ledger_id, replayed.ledger_id, fake.ledger_id] == [
        row["id"] for row in ledger_rows(storage)
    ]
    # Asked for out of order, with an id that has no row: the rows come back in order.
    reader = EmberClient.replay_only(storage)
    generation = live.ledger_generation
    assert replayed.ledger_generation == fake.ledger_generation == generation
    assert fake.ledger_ref == RowRef(generation, fake.ledger_id, fake.ledger_token)
    first, second = reader.ledger_calls(
        [fake.ledger_ref, RowRef(generation, 999, fake.ledger_token), live.ledger_ref]
    )
    # A row is named by its token as well as its id, so one call's id beside
    # another's token names no row.
    assert reader.ledger_calls([RowRef(generation, live.ledger_id, fake.ledger_token)]) == []
    assert (first.id, first.source, first.status, first.task) == (
        live.ledger_id,
        "live",
        "ok",
        "extract_transmission",
    )
    assert (first.input_tokens, first.cached_tokens, first.output_tokens) == (1847, 1792, 98)
    assert first.cost_usd == pytest.approx(USAGE_COST_NANOUSD / 1e9)
    assert first.latency_ms == live.latency_ms
    assert (first.model, first.effort, first.request_hash) == (
        "muse-ember-1.0",
        "low",
        live.request_hash,
    )
    assert (second.id, second.source, second.input_tokens) == (fake.ledger_id, "fake", 10)
    assert reader.ledger_calls([]) == []
    # A row is shown without its request and response.
    assert not hasattr(first, "request")
    assert not hasattr(first, "response")


def test_a_client_that_outlives_its_ledger_file_never_reads_the_next_files_rows(
    storage: Storage,
) -> None:
    def answer(_body: Any) -> dict[str, Any]:
        return fake_response(text="ok", input_tokens=10)

    survivor = EmberClient.fake(storage, answer)
    old = ask(survivor)
    assert [call.id for call in survivor.ledger_calls([old.ledger_ref])] == [old.ledger_id]

    # The ledger file is deleted while the first client lives, and another client
    # begins the next one with the same request: the same row id and the same hash.
    storage.ledger_db.unlink()
    new = ask(EmberClient.fake(storage, answer))

    assert (new.ledger_id, new.request_hash) == (old.ledger_id, old.request_hash)
    assert new.ledger_generation != old.ledger_generation
    assert survivor.ledger_calls([old.ledger_ref]) == []
    # What the surviving client writes from now on belongs to the file it is written in.
    later = ask(survivor)
    assert later.ledger_generation == new.ledger_generation
    assert [call.id for call in survivor.ledger_calls([later.ledger_ref])] == [later.ledger_id]


def test_a_client_whose_ledger_file_is_deleted_begins_a_new_one(storage: Storage) -> None:
    ember = EmberClient.fake(storage, lambda _body: fake_response(text="ok", input_tokens=10))
    old = ask(ember)
    storage.ledger_db.unlink()

    assert ember.ledger_calls([old.ledger_ref]) == []
    assert ember.spent_usd() == 0
    new = ask(ember)

    assert new.ledger_generation != old.ledger_generation
    assert [row["id"] for row in ledger_rows(storage)] == [new.ledger_id]


def test_replay_only_mode_refuses_a_request_it_has_no_recording_for(storage: Storage) -> None:
    ask(MetaApi().client(storage))

    with pytest.raises(ReplayMissError, match="No recorded Ember response"):
        ask(EmberClient.replay_only(storage), input="Summarize the alert.")
    with pytest.raises(ReplayMissError):
        ask(EmberClient.replay_only(storage), effort="medium")


def test_media_stays_out_of_everything_written_to_disk_but_still_keys_the_cache(
    storage: Storage,
) -> None:
    media = {
        "photo": b"first tap photo",
        "second photo": b"a photo sent with an upper-case scheme",
        "voice note": b"a voice note, sent as bare base64",
        "scan": b"a scanned notice, sent as bare base64",
        "overlay": b"an overlay image inside a tool result",
        "tool file": b"a short file inside a tool result",
    }
    # Base64 wrapped over lines, as mail and PEM tools write it.
    wrapped = "\n".join(textwrap.wrap(encoded(bytes(range(256)) * 2), 76))

    def request(photo: bytes) -> list[dict[str, Any]]:
        overlay = {
            "overlay": "data:image/png;base64," + encoded(media["overlay"]),
            "file_data": encoded(media["tool file"]),
        }
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Before: data:image/png;base64,{wrapped}\nEnd.",
                    },
                    {"type": "input_text", "text": f"The same bytes, bare:\n{wrapped}"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/jpeg;base64," + encoded(photo),
                    },
                    {
                        "type": "input_image",
                        "image_url": "DATA:image/jpeg;BASE64," + encoded(media["second photo"]),
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": encoded(media["voice note"]), "format": "wav"},
                    },
                    {
                        "type": "input_file",
                        "filename": "c.pdf",
                        "file_data": encoded(media["scan"]),
                    },
                ],
            },
            {"type": "function_call", "call_id": "call_1", "name": "get_radio", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": json.dumps(overlay)},
        ]

    api = MetaApi()
    first = ask(api.client(storage), input=request(media["photo"]))

    written = persisted(storage)
    for name, content in media.items():
        assert encoded(content).encode() in api.requests[1].content, name
        assert encoded(content) not in written, name
    for line in wrapped.splitlines():
        assert line.encode() in api.requests[1].content
        assert line not in written
    assert "data:image/jpeg;base64,<redacted 20 chars, sha256 " in written

    replay = EmberClient.replay_only(storage)
    assert ask(replay, input=request(media["photo"])).raw == first.raw
    with pytest.raises(ReplayMissError):
        ask(replay, input=request(b"other tap photo"))


PICTURE = "data:image/png;base64," + encoded(b"a photo the model wrote back out")


def _with_extra_output(item: dict[str, Any]) -> dict[str, Any]:
    response = completed()
    response["output"].append(item)
    return response


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(completed(f"See {PICTURE}"), id="in-prose"),
        # The name of the field that holds reasoning state does not make a string safe:
        # not in JSON the model wrote, not in tool arguments, and not on other items.
        pytest.param(
            completed(json.dumps({"encrypted_content": PICTURE})), id="in-json-the-model-wrote"
        ),
        pytest.param(
            _with_extra_output(
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "get_radio",
                    "arguments": json.dumps({"encrypted_content": PICTURE}),
                }
            ),
            id="in-tool-arguments",
        ),
        pytest.param(
            _with_extra_output(
                {"type": "message", "role": "assistant", "encrypted_content": PICTURE}
            ),
            id="on-an-item-that-is-not-reasoning",
        ),
        pytest.param(
            _with_extra_output({"type": "reasoning", "summary": [], "encrypted_content": PICTURE}),
            id="as-a-data-url-on-a-reasoning-item",
        ),
    ],
)
def test_an_answer_that_holds_media_is_returned_but_not_written_to_disk(
    storage: Storage, response: dict[str, Any]
) -> None:
    api = MetaApi(create=lambda _request: httpx2.Response(200, json=response))

    answer = ask(api.client(storage))

    # The caller gets the answer as it came. Nothing on disk holds the picture, and
    # since a redacted answer would not be the real one, there is no recording.
    assert answer.raw == response
    assert encoded(b"a photo the model wrote back out") not in persisted(storage)
    with pytest.raises(ReplayMissError):
        ask(EmberClient.replay_only(storage))


def test_reasoning_state_is_recorded_so_a_replayed_answer_can_be_sent_back(
    storage: Storage,
) -> None:
    # Encrypted reasoning is long and opaque, and a tool loop has to send it back.
    reasoned = completed()
    reasoned["output"][0]["encrypted_content"] = encoded(bytes(range(256)) * 3)
    api = MetaApi(create=lambda _request: httpx2.Response(200, json=reasoned))
    live = ask(api.client(storage), input="Another question.")

    assert (
        ask(EmberClient.replay_only(storage), input="Another question.").raw == live.raw == reasoned
    )


def test_identical_requests_made_at_once_are_paid_for_once(storage: Storage) -> None:
    def slow(_request: httpx2.Request) -> httpx2.Response:
        time.sleep(0.2)
        return httpx2.Response(200, json=completed())

    api = MetaApi(create=slow)

    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda _: ask(api.client(storage)), range(4)))

    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]
    assert sorted(answer.source for answer in answers) == ["live", "replay", "replay", "replay"]
    assert all(answer.raw == completed() for answer in answers)


def test_a_call_cut_off_mid_flight_keeps_its_reservation_and_shows_in_the_trace(
    storage: Storage,
) -> None:
    api = MetaApi(create=_raise(KeyboardInterrupt()))
    ember = api.client(storage)

    with pytest.raises(KeyboardInterrupt):
        ask(ember)

    assert ember.spent_usd() == pytest.approx(RESERVED_NANOUSD / 1e9)
    (line,) = trace_lines(storage)
    assert (line["status"], line["response"]) == ("pending", None)
    assert line["request"]["input"] == "Summarize the transmission."
    # The trace is rebuilt from the ledger, so exporting again changes nothing.
    exported = storage.trace.read_text()
    assert ember.export_trace() == (storage.trace, 1)
    assert storage.trace.read_text() == exported


def test_the_module_command_exports_the_trace_and_prints_the_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BEACON_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EMBER_STOP_USD", "2.50")
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "1.20")
    storage = Storage.under(tmp_path)
    ask(MetaApi().client(storage))

    main()

    assert capsys.readouterr().out.splitlines() == [
        f"Trace: {storage.trace}",
        "Calls: 1",
        "Live spend: $0.000062",
        "Stop: $2.50 in this ledger, on top of $1.20 spent before it began",
    ]
    assert json.loads(storage.trace.read_text())["response"]["id"] == "resp_abc123"

    # It needs neither a key nor the earlier spend, and says what a paid call needs.
    monkeypatch.delenv("EMBER_STOP_USD")
    monkeypatch.delenv("EMBER_SPENT_BEFORE_USD")
    main()
    assert capsys.readouterr().out.splitlines()[-1] == (
        "Stop: $3.00 in this ledger, EMBER_SPENT_BEFORE_USD is not set, so no paid call can be made"
    )
    # A setting it cannot read stops it, with the setting's name.
    monkeypatch.setenv("EMBER_STOP_USD", "three")
    with pytest.raises(SystemExit, match="EMBER_STOP_USD"):
        main()


def test_a_request_over_150k_input_tokens_is_refused_before_it_is_sent(storage: Storage) -> None:
    at_limit = MetaApi(input_tokens=150_000)
    ask(at_limit.client(storage))
    assert at_limit.paths() == ["/v1/responses/input_tokens", "/v1/responses"]

    over = MetaApi(input_tokens=150_001)
    with pytest.raises(InputTooLargeError, match="150,001 input tokens"):
        ask(over.client(storage), input="A different, longer request.")
    assert over.paths() == ["/v1/responses/input_tokens"]
    assert len(ledger_rows(storage)) == 1


def test_a_call_that_could_pass_the_stop_is_refused_before_it_is_sent(storage: Storage) -> None:
    api = MetaApi()

    with pytest.raises(BudgetExceededError, match="would pass"):
        ask(api.client(storage, stop_nanousd=RESERVED_NANOUSD - 1))
    assert api.paths() == ["/v1/responses/input_tokens"]
    assert ledger_rows(storage) == []

    ask(api.client(storage, stop_nanousd=RESERVED_NANOUSD))
    assert api.paths()[-1] == "/v1/responses"


def test_a_live_client_whose_ledger_is_replaced_makes_no_paid_call(storage: Storage) -> None:
    api = MetaApi()
    ember = api.client(storage)
    ask(ember)
    made = len(api.requests)
    assert ember.spent_usd() > 0

    # The ledger, the only record of what was spent, is deleted under the client.
    storage.ledger_db.unlink()

    # Its spend would be counted from zero, so no paid call is made, and no request.
    with pytest.raises(LedgerReplacedError, match="deleted or replaced"):
        ask(ember, input="Another request.")
    assert len(api.requests) == made
    assert ledger_rows(storage) == []
    # A refusal for spend's sake, to code that already stops on one.
    assert issubclass(LedgerReplacedError, BudgetExceededError)
    # What spends nothing still works, and is logged in the file that is there now.
    assert ask(ember).source == "replay"
    assert [row["source"] for row in ledger_rows(storage)] == ["replay"]
    # It stays refused, until the client is started again on the ledger as it stands.
    with pytest.raises(LedgerReplacedError):
        ask(ember, input="Another request.")
    restarted = api.client(storage)
    assert ask(restarted, input="Another request.").source == "live"


def test_the_reservation_itself_refuses_a_ledger_that_was_replaced(storage: Storage) -> None:
    # 90 of a budget of 100 is reserved, and a further 20 is refused.
    ledger = Ledger(storage.ledger_db)
    call = Call(request_hash="h", task="scale", effort="low", max_output_tokens=16, request={})
    settled = ledger.reserve(call, reserved_nanousd=45, budget_nanousd=100)
    lost = ledger.reserve(call, reserved_nanousd=45, budget_nanousd=100)
    with pytest.raises(BudgetExceededError, match="would pass"):
        ledger.reserve(call, reserved_nanousd=20, budget_nanousd=100)
    assert ledger.settle(settled, status="ok", cost_nanousd=45, latency_ms=5) is True

    # Another process begins the ledger again. The 20 would fit an empty ledger.
    storage.ledger_db.unlink()
    replacement = Ledger(storage.ledger_db)

    with pytest.raises(LedgerReplacedError):
        ledger.reserve(call, reserved_nanousd=20, budget_nanousd=100)
    # A reservation made in the file that is gone has no row to settle, and says so.
    assert ledger.settle(lost, status="ok", cost_nanousd=45, latency_ms=5) is False
    assert ledger_rows(storage) == []
    assert ledger.spent_nanousd() == 0
    # The ledger opened on the new file reserves in it.
    replacement.reserve(call, reserved_nanousd=20, budget_nanousd=100)
    assert replacement.spent_nanousd() == 20


def test_once_the_ledger_reaches_the_stop_no_request_is_made_at_all(storage: Storage) -> None:
    ask(MetaApi().client(storage))

    api = MetaApi()
    with pytest.raises(BudgetExceededError, match="reaches"):
        ask(api.client(storage, stop_nanousd=USAGE_COST_NANOUSD), input="Another request.")
    assert api.requests == []

    # Replays are free, so they still work past the stop.
    assert ask(api.client(storage, stop_nanousd=USAGE_COST_NANOUSD)).source == "replay"


def test_a_client_given_no_stop_stops_at_three_dollars_in_the_ledger(storage: Storage) -> None:
    # Live spend that leaves room under $3.00 for exactly one call at the most it can cost.
    earlier = Call(request_hash="h", task="scale", effort="low", max_output_tokens=16, request={})
    Ledger(storage.ledger_db).reserve(
        earlier, reserved_nanousd=3_000_000_000 - RESERVED_NANOUSD, budget_nanousd=10**10
    )
    api = MetaApi()
    ember = api.client(storage)
    assert ember.stop == SpendingStop(ledger_nanousd=3_000_000_000, spent_before_nanousd=0)

    assert ask(ember).source == "live"
    # It was answered for less than it could have cost, which is not room for another.
    with pytest.raises(BudgetExceededError, match=r"would pass the \$3\.00 stop"):
        ask(ember, input="Another request.")
    assert api.paths().count("/v1/responses") == 1


@pytest.mark.parametrize("build", [EmberClient.live, EmberClient], ids=["live", "constructor"])
def test_every_way_of_building_a_live_client_keeps_the_stop_the_environment_sets(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, build: Callable[..., EmberClient]
) -> None:
    # An operator stops all spending, and code builds a client without `from_env`.
    monkeypatch.setenv("EMBER_STOP_USD", "0")
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    api = MetaApi()

    ember = build(storage, api_key="test-key", http_client=api.http_client())

    assert ember.stop == SpendingStop(ledger_nanousd=0, spent_before_nanousd=0)
    with pytest.raises(BudgetExceededError, match=r"reaches the \$0\.00 stop"):
        ask(ember)
    assert api.requests == []
    # A client that cannot spend has no stop, and neither kind takes one.
    assert EmberClient.replay_only(storage).stop is None
    with pytest.raises(ValueError, match="stop_nanousd is only used by a live client"):
        EmberClient(storage, stop_nanousd=0)


def test_a_client_can_lower_the_environments_stop_and_cannot_raise_it(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MetaApi()
    monkeypatch.setenv("EMBER_STOP_USD", "0.50")

    lowered = api.client(storage, stop_nanousd=1000)
    assert lowered.stop == SpendingStop(ledger_nanousd=1000, spent_before_nanousd=0)
    assert api.client(storage, stop_nanousd=500_000_000).stop.ledger_nanousd == 500_000_000

    with pytest.raises(ValueError, match=r"cannot raise it above the \$0\.50"):
        api.client(storage, stop_nanousd=500_000_001)
    for not_an_amount in (-1, True, 1.5, "3"):
        with pytest.raises(ValueError, match="nano-dollars"):
            api.client(storage, stop_nanousd=not_an_amount)
    # A stop the environment sets wrongly is not one a client can talk its way past.
    monkeypatch.setenv("EMBER_STOP_USD", "three")
    with pytest.raises(EmberConfigError, match="EMBER_STOP_USD"):
        api.client(storage, stop_nanousd=0)
    assert api.requests == []


def test_a_client_that_can_spend_needs_what_the_account_had_spent_before(
    storage: Storage, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = MetaApi()
    live = {"REPLAY": "0", "MODEL_API_KEY": "test-key"}

    for unset in (None, "", " "):
        if unset is None:
            monkeypatch.delenv("EMBER_SPENT_BEFORE_USD")
        else:
            monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", unset)
        with pytest.raises(EmberConfigError, match="EMBER_SPENT_BEFORE_USD is not set"):
            EmberClient.live(storage, api_key="test-key", http_client=api.http_client())
        with pytest.raises(EmberConfigError, match="EMBER_SPENT_BEFORE_USD is not set"):
            EmberClient(storage, api_key="test-key", http_client=api.http_client())
        with pytest.raises(EmberConfigError, match="Usage page"):
            EmberClient.from_env(live, storage=storage)
        # A client that is refused writes nothing.
        assert not storage.ledger_db.parent.exists()
        # Replaying needs no answer: nothing is spent.
        with pytest.raises(ReplayMissError):
            ask(EmberClient.from_env({}, storage=Storage.under(tmp_path_factory.mktemp("r"))))

    # An account that had spent $8.00 has $1.00 left under the ceiling.
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "8")
    with pytest.raises(EmberConfigError, match=r"\$3\.00 on top of \$8\.00"):
        api.client(storage)
    monkeypatch.setenv("EMBER_STOP_USD", "1")
    assert api.client(storage).stop == SpendingStop(
        ledger_nanousd=1_000_000_000, spent_before_nanousd=8_000_000_000
    )
    # Zero is an answer when it is given.
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    assert ask(api.client(storage)).source == "live"
    assert ask(EmberClient.replay_only(storage)).source == "replay"


def test_the_spending_settings_are_the_processs_and_no_mapping_replaces_them(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator has stopped all spending, on an account that had spent $8.00.
    monkeypatch.setenv("EMBER_STOP_USD", "0")
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "8")
    api = MetaApi()
    monkeypatch.setattr(
        EmberClient,
        "live",
        classmethod(
            lambda cls, storage, **options: cls(storage, http_client=api.http_client(), **options)
        ),
    )
    # Code builds a client from a mapping of its own that says otherwise.
    generous = {
        "REPLAY": "0",
        "MODEL_API_KEY": "test-key",
        "EMBER_STOP_USD": "3",
        "EMBER_SPENT_BEFORE_USD": "0",
    }

    ember = EmberClient.from_env(generous, storage=storage)

    assert ember.stop == SpendingStop(ledger_nanousd=0, spent_before_nanousd=8_000_000_000)
    with pytest.raises(BudgetExceededError, match=r"reaches the \$0\.00 stop"):
        ask(ember)
    assert api.requests == []
    # Nor does a mapping answer for an earlier spend the process was never given,
    monkeypatch.delenv("EMBER_SPENT_BEFORE_USD")
    with pytest.raises(EmberConfigError, match="EMBER_SPENT_BEFORE_USD is not set"):
        EmberClient.from_env(generous, storage=storage)
    # or hide a stop the process sets wrongly, in replay mode either.
    monkeypatch.setenv("EMBER_STOP_USD", "three")
    with pytest.raises(EmberConfigError, match="EMBER_STOP_USD"):
        EmberClient.from_env({"EMBER_STOP_USD": "1"}, storage=storage)
    # No way of building a client takes the settings as an argument.
    monkeypatch.undo()
    for build in (EmberClient.live, EmberClient):
        with pytest.raises(TypeError, match="env"):
            build(storage, api_key="test-key", env=generous)


def test_a_ledger_put_back_from_an_older_copy_stops_every_paid_call(
    storage: Storage, tmp_path: Path
) -> None:
    api = MetaApi()
    ember = api.client(storage)
    ask(ember)
    generation = ember.ledger_generation()
    backup = tmp_path / "backup.sqlite3"
    shutil.copyfile(storage.ledger_db, backup)
    ask(ember, input="A second request.")
    spent, made = ember.spent_usd(), len(api.requests)

    # The copy is put back by renaming it over the ledger. It is the same ledger by
    # its generation, and it holds half of what was spent.
    os.replace(backup, storage.ledger_db)
    assert ember.ledger_generation() == generation
    assert ember.spent_usd() == pytest.approx(spent / 2)

    with pytest.raises(LedgerReplacedError, match="deleted or replaced"):
        ask(ember, input="A third request.")
    assert len(api.requests) == made
    # What spends nothing still works.
    assert ask(ember).source == "replay"


def test_a_reservation_is_not_settled_into_an_older_copys_row_of_the_same_id(
    storage: Storage, tmp_path: Path
) -> None:
    ledger = Ledger(storage.ledger_db)
    call = Call(request_hash="h", task="scale", effort="low", max_output_tokens=16, request={})
    ledger.reserve(call, reserved_nanousd=10, budget_nanousd=100)
    backup = tmp_path / "backup.sqlite3"
    shutil.copyfile(storage.ledger_db, backup)
    lost = ledger.reserve(call, reserved_nanousd=20, budget_nanousd=100)

    # The copy from before the second reservation is put back, and another process
    # reserves in it: the same generation, and the same row id as the one that is gone.
    os.replace(backup, storage.ledger_db)
    restored = Ledger(storage.ledger_db)
    theirs = restored.reserve(call, reserved_nanousd=30, budget_nanousd=100)
    assert (theirs.generation, theirs.id) == (lost.generation, lost.id)

    # The call that is gone is not looked up as the one now at its id, by a ledger
    # that outlived the file or by one opened since,
    assert ledger.calls([lost]) == restored.calls([lost]) == []
    assert [shown.id for shown in restored.calls([lost, theirs])] == [theirs.id]
    # nor settled into it.
    assert ledger.settle(lost, status="ok", cost_nanousd=1, latency_ms=5) is False
    assert restored.settle(lost, status="ok", cost_nanousd=1, latency_ms=5) is False
    # The other call's reservation is as it was.
    assert restored.spent_nanousd() == 10 + 30
    with pytest.raises(LedgerReplacedError):
        ledger.reserve(call, reserved_nanousd=1, budget_nanousd=100)
    assert restored.settle(theirs, status="ok", cost_nanousd=3, latency_ms=5) is True
    assert restored.spent_nanousd() == 10 + 3


def test_a_ledger_deleted_while_a_reserved_call_waits_its_turn_stops_the_paid_request(
    storage: Storage,
) -> None:
    api = MetaApi()
    clock = Clock()

    def sleep(seconds: float) -> None:
        # The only wait is the paid request's, for its place under the rate limit,
        # after the call is reserved. The ledger goes during it.
        storage.ledger_db.unlink()
        clock.sleep(seconds)

    ember = api.client(storage, pacer=Pacer(3000, clock=clock, sleep=sleep))

    with pytest.raises(LedgerReplacedError, match="deleted or replaced"):
        ask(ember)

    # The request that costs money was not sent: it would be in no ledger.
    assert api.paths() == ["/v1/responses/input_tokens"]
    assert clock.sleeps == pytest.approx([0.02])
    assert ledger_rows(storage) == []


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, completed()), (200, {"status": "completed"}), (500, {"error": {"message": "x"}})],
    ids=["answered", "answered-without-usage", "failed"],
)
def test_a_ledger_deleted_while_a_paid_request_is_out_is_reported_and_not_passed_over(
    storage: Storage, status: int, body: dict[str, Any]
) -> None:
    def create(_request: httpx2.Request) -> httpx2.Response:
        storage.ledger_db.unlink()
        return httpx2.Response(status, json=body)

    api = MetaApi(create=create)
    ember = api.client(storage)

    # The request was sent and may have been billed, and its reservation went with the
    # file. That is not an answer to hand back, nor a failed call to try again.
    with pytest.raises(LedgerReplacedError, match="may have been billed") as raised:
        ask(ember)

    assert not isinstance(raised.value, EmberCallError)
    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]
    assert ledger_rows(storage) == []
    assert not list(storage.replay_dir.glob("*.json"))
    # No more paid calls from this client.
    with pytest.raises(LedgerReplacedError):
        ask(ember, input="Another request.")
    assert len(api.requests) == 2


def test_a_sensitive_call_leaves_nothing_of_what_was_said_on_disk(storage: Storage) -> None:
    said = "Medic 52 on scene, 67 year old male, chest pain, 1458 Hayes Street"
    prompt = "Extract the incident. The caller is Jane Doe."
    heard = '{"address": "1458 Hayes Street", "patient": "67M chest pain"}'
    tool = {"type": "function", "name": "lookup_hayes", "parameters": {"type": "object"}}
    # Everything the API writes besides the answer can hold a word of it too.
    response = completed(
        heard,
        id="resp_Hayes",
        model="Hayes-1.0",
        metadata={"note": "Hayes"},
        usage=USAGE | {"Hayes": 1, "input_tokens_details": {"cached_tokens": 1792, "Hayes": 2}},
    )
    api = MetaApi(create=lambda _request: httpx2.Response(200, json=response))
    ember = api.client(storage)
    words = {
        "sensitive": True,
        "input": said,
        "instructions": prompt,
        "tools": [tool],
        "text": {"format": {"type": "json_schema", "name": "hayes", "schema": {}}},
        "parallel_tool_calls": False,
        "prompt_cache_key": "hayes-street",
    }

    answer = ask(ember, **words)

    # The caller gets the answer as the API sent it.
    assert (answer.source, answer.output_text, answer.raw) == ("live", heard, response)
    # Nothing that was said, in the request or the answer, is in the ledger, the trace,
    # or a recording.
    # (Words, not numbers: a number can turn up in a hash or a timestamp by chance.)
    stored = persisted(storage).lower()
    for text in ("hayes", "street", "chest", "male", "jane", "caller", "medic", "lookup"):
        assert text not in stored
    assert not list(storage.replay_dir.glob("*.json"))
    # The accounting is whole.
    (row,) = ledger_rows(storage)
    assert (row["source"], row["status"], row["task"], row["request_hash"]) == (
        "live",
        "ok",
        "extract_transmission",
        answer.request_hash,
    )
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (1847, 1792, 98)
    assert (row["cost_nanousd"], row["model"], row["response_id"]) == (
        USAGE_COST_NANOUSD,
        "muse-ember-1.0",
        None,
    )
    # Of the request, the settings the client wrote and the size of everything else.
    sent = json.loads(api.requests[-1].content)
    kept = json.loads(row["request"])
    assert set(kept) == set(sent) | {"withheld"}
    assert {name: kept[name] for name in kept if not str(kept[name]).startswith("<")} == {
        "withheld": "sensitive",
        "model": "muse-ember-1.0",
        "max_output_tokens": 1024,
        "reasoning": {"effort": "low"},
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "parallel_tool_calls": False,
    }
    assert kept["input"] == f"<withheld: {len(json.dumps(said))} chars>"
    # Of the answer, a status the client knows, the counts it reads, and a size.
    output_size = len(json.dumps(response["output"], sort_keys=True, separators=(",", ":")))
    assert json.loads(row["response"]) == {
        "withheld": "sensitive",
        "status": "completed",
        "usage": USAGE,
        "output": f"<withheld: {output_size} chars>",
    }

    # Nothing was recorded, so the same call is paid for again, and replay-only mode
    # has nothing to give.
    assert ask(ember, **words).source == "live"
    assert api.paths().count("/v1/responses") == 2
    with pytest.raises(ReplayMissError, match=r"sensitive call .* is never recorded"):
        ask(EmberClient.replay_only(storage), **words)
    # A recording of the same request made as an ordinary call is not replayed into a
    # sensitive one's place.
    ordinary = ask(ember, **(words | {"sensitive": False}))
    assert (ordinary.source, ask(ember, **words).source) == ("live", "live")


def _says_the_name(http_status: int, /, **body: Any) -> httpx2.Response:
    """An answer that repeats who the request was about wherever it can."""
    error = {"message": "Bad input: 'Jane Doe'", "type": "JaneDoe", "code": "Jane.Doe"}
    return httpx2.Response(http_status, json={"error": error} if http_status >= 400 else body)


@pytest.mark.parametrize(
    ("count", "create", "status_code", "booked"),
    [
        (None, _says_the_name(200, **completed("Jane Doe", status="failed")), None, "error"),
        (None, _says_the_name(200, **completed("x", status="JaneDoe")), None, "error"),
        (None, _says_the_name(200, status="completed", note="Jane Doe"), None, "unknown"),
        (
            None,
            _says_the_name(200, status="completed", usage={"input_tokens": "Jane Doe"}),
            None,
            "unknown",
        ),
        (None, _says_the_name(400), 400, "unknown"),
        (None, httpx2.Response(200, content=b"Jane Doe is not JSON"), None, "unknown"),
        (_says_the_name(400), None, 400, None),
        (httpx2.Response(200, json={"input_tokens": "Jane Doe"}), None, None, None),
        # The SDK's decoder raises an error of its own, which holds the whole reply.
        (
            httpx2.Response(
                200,
                content=b'{"input_tokens": Jane Doe',
                headers={"content-type": "application/json"},
            ),
            None,
            None,
            None,
        ),
    ],
    ids=[
        "failed",
        "status-not-known",
        "no-usage",
        "usage-of-words",
        "rejected",
        "not-json",
        "count-rejected",
        "count-of-words",
        "count-not-json",
    ],
)
def test_a_sensitive_call_that_fails_says_nothing_of_what_was_said_anywhere(
    storage: Storage,
    count: httpx2.Response | None,
    create: httpx2.Response | None,
    status_code: int | None,
    booked: str | None,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            return count or httpx2.Response(200, json={"input_tokens": 1200})
        return create

    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )

    with pytest.raises(EmberCallError) as raised:
        ask(ember, sensitive=True, input=SAID)

    # Not on disk,
    assert [row["status"] for row in ledger_rows(storage)] == ([booked] if booked else [])
    stored = persisted(storage).lower()
    assert "jane" not in stored
    assert "heel" not in stored
    if status_code is not None and booked:
        assert ledger_rows(storage)[0]["error"] == f"HTTP {status_code}"
    # and not in the error, which a caller may print or log, or under it.
    error = raised.value
    assert type(error) is EmberCallError
    assert error.status_code == status_code
    assert (error.__cause__, error.__context__) == (None, None)
    assert "jane" not in repr(vars(error)).lower()
    shown = "".join(traceback.format_exception(error)).lower()
    assert "jane" not in shown
    assert "heel" not in shown

    # The same failure of an ordinary call is a failed call too, and not an error
    # of the SDK's or a decoder's.
    with pytest.raises(EmberCallError):
        ask(ember, input="An ordinary request.")


@pytest.mark.parametrize(
    "overrides",
    [
        {"tools": [{"type": "web_search", "description": SAID}]},
        {"input": [{"type": SAID}]},
        {"input": [SAID]},
        {"effort": SAID},
        {"max_output_tokens": SAID},
        {"parallel_tool_calls": SAID},
        {"task": ""},
        {"input": [{"role": "user", "content": [{"image_url": "https://example.com/" + SAID}]}]},
        {"input": [{"role": "user", "content": [{SAID + "_url": "https://example.com/a"}]}]},
        {"input": [{"role": "user", "content": {SAID: b"bytes"}}]},
        {"input": [{"role": "user", "content": {SAID: float("nan")}, "more": {1, 2}}]},
    ],
    ids=[
        "tool",
        "item-type",
        "bare-string",
        "effort",
        "output-limit",
        "parallel",
        "task",
        "remote-media",
        "remote-media-key",
        "bytes",
        "set",
    ],
)
def test_a_sensitive_request_that_is_refused_is_not_repeated_in_the_refusal(
    storage: Storage, overrides: dict[str, Any]
) -> None:
    api = MetaApi()
    for ember in (api.client(storage), EmberClient.fake(storage, Script([fake_response()]))):
        with pytest.raises(EmberRequestError) as raised:
            ask(ember, **({"sensitive": True, "input": SAID} | overrides))

        error = raised.value
        assert (error.__cause__, error.__context__) == (None, None)
        shown = "".join(traceback.format_exception(error)).lower()
        assert "jane" not in shown
        assert "heel" not in shown
    assert api.requests == []
    assert ledger_rows(storage) == []


def test_a_stand_in_that_fails_on_a_sensitive_call_is_not_quoted(storage: Storage) -> None:
    def fails(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"cannot answer {body['input']}")

    def refuses(body: dict[str, Any]) -> dict[str, Any]:
        raise EmberCallError(f"refused: {body['input']}")

    for responder in (fails, refuses, lambda body: [body["input"]]):
        with pytest.raises(EmberError) as raised:
            ask(EmberClient.fake(storage, responder), sensitive=True, input=SAID)

        error = raised.value
        assert type(error) is EmberError
        assert "The fake responder failed on a sensitive call" in str(error)
        assert (error.__cause__, error.__context__) == (None, None)
        assert "jane" not in "".join(traceback.format_exception(error)).lower()
    assert ledger_rows(storage) == []

    # An ordinary call's stand-in fails as itself, which is what a test wants to see.
    with pytest.raises(RuntimeError, match="cannot answer Summarize"):
        ask(EmberClient.fake(storage, fails))


def test_an_error_that_is_not_the_clients_own_is_not_passed_on_from_a_sensitive_call(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    ember = EmberClient.fake(storage, Script([fake_response(), fake_response()]))

    def breaks(*_arguments: Any, **_options: Any) -> None:
        raise sqlite3.OperationalError(f"near {SAID!r}: syntax error")

    monkeypatch.setattr(Ledger, "record", breaks)

    with pytest.raises(EmberError) as raised:
        ask(ember, sensitive=True, input=SAID)
    assert str(raised.value).startswith("A sensitive call failed (OperationalError).")
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)
    assert "jane" not in "".join(traceback.format_exception(raised.value)).lower()
    # An ordinary call's is.
    with pytest.raises(sqlite3.OperationalError):
        ask(ember)


def test_a_sensitive_failure_has_nothing_under_it_when_the_caller_is_handling_an_error(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fails(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("the stand-in failed")

    def breaks(*_arguments: Any, **_options: Any) -> None:
        raise sqlite3.OperationalError("the ledger failed")

    rejecting = MetaApi(create=lambda _request: _says_the_name(400)).client(storage)
    answering = EmberClient.fake(storage, Script([fake_response()]))
    cases: list[tuple[EmberClient, dict[str, Any], type[EmberError]]] = [
        # The client's own errors: a refused request and a failed call.
        (answering, {"effort": "none"}, EmberRequestError),
        (rejecting, {}, EmberCallError),
        # Errors that are not: a stand-in's, and one from under the client.
        (EmberClient.fake(storage, fails), {}, EmberError),
        (answering, {}, EmberError),
    ]
    for number, (ember, overrides, kind) in enumerate(cases):
        if number == len(cases) - 1:
            monkeypatch.setattr(Ledger, "record", breaks)
        # The caller is recovering from an extraction that failed, and what it is
        # handling holds what was said.
        try:
            raise ValueError(SAID)
        except ValueError:
            with pytest.raises(EmberError) as raised:
                ask(ember, sensitive=True, input="Try again.", **overrides)

        error = raised.value
        assert type(error) is kind
        assert (error.__cause__, error.__context__) == (None, None)
        shown = "".join(traceback.format_exception(error)).lower()
        assert "jane" not in shown
        assert "heel" not in shown


def test_a_sensitive_call_is_waited_out_after_a_rate_limit_like_any_other(
    storage: Storage,
) -> None:
    refusals = [_rate_limited("3")]
    api = MetaApi(
        create=lambda _request: (
            refusals.pop(0) if refusals else httpx2.Response(200, json=completed())
        )
    )
    clock = Clock()
    pacer = Pacer(600, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    ember = api.client(storage, pacer=pacer)

    assert ask(ember, sensitive=True, input=SAID).source == "live"

    # The wait the API asked for is kept, though none of its words are.
    assert clock.sleeps == pytest.approx([0.1, 3.0, 0.1])
    assert [(row["status"], row["error"]) for row in ledger_rows(storage)] == [
        ("unknown", "HTTP 429"),
        ("ok", None),
    ]

    # A wait too long to sit through stops the call, with nothing under the error.
    refusals.append(_rate_limited("3600"))
    with pytest.raises(RateLimitHoldError, match="3600 s") as raised:
        ask(ember, sensitive=True, input=SAID)
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)


def test_a_fake_client_keeps_nothing_of_a_sensitive_call_either(storage: Storage) -> None:
    script = Script([fake_response(text="1458 Hayes Street", input_tokens=900, output_tokens=40)])

    answer = ask(EmberClient.fake(storage, script), sensitive=True, input="Medic 52, Hayes")

    assert answer.output_text == "1458 Hayes Street"
    assert "Hayes" not in persisted(storage)
    (row,) = ledger_rows(storage)
    assert (row["source"], row["input_tokens"], row["output_tokens"]) == ("fake", 900, 40)

    # A stand-in's answer that cannot be priced is refused without repeating it.
    unpriced = fake_response(text="ok") | {"usage": {"input_tokens": "Hayes"}}
    with pytest.raises(EmberError, match="has no usable token counts") as raised:
        ask(EmberClient.fake(storage, Script([unpriced])), sensitive=True, input="Medic 52")
    assert "Hayes" not in "".join(traceback.format_exception(raised.value))


def test_parallel_calls_cannot_pass_the_stop_together(storage: Storage) -> None:
    ledger = Ledger(storage.ledger_db)
    call = Call(request_hash="h", task="scale", effort="low", max_output_tokens=16, request={})

    def reserve(_: int) -> bool:
        try:
            ledger.reserve(call, reserved_nanousd=100, budget_nanousd=550)
        except BudgetExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        admitted = list(pool.map(reserve, range(16)))

    assert admitted.count(True) == 5
    assert ledger.spent_nanousd() == 500


def test_a_scope_is_held_to_its_own_budget_whatever_else_the_ledger_holds(
    storage: Storage,
) -> None:
    ledger = Ledger(storage.ledger_db)
    other = Call(
        request_hash="o",
        task="extract_transmission",
        effort="low",
        max_output_tokens=16,
        request={},
    )
    ours = Call(
        request_hash="h", task="scale", effort="low", max_output_tokens=16, request={}, scope="run"
    )
    # Another client's call is in flight, booked at the most it can cost.
    pending = ledger.reserve(other, reserved_nanousd=1000, budget_nanousd=10_000)

    ledger.reserve(ours, reserved_nanousd=60, budget_nanousd=10_000, scope_budget_nanousd=100)
    # It is answered for far less. The ledger's spend falls, and the scope's does not
    # move: the room that opens under the stop is not the scope's to spend.
    ledger.settle(pending, status="ok", cost_nanousd=1, latency_ms=5)
    with pytest.raises(BudgetExceededError, match="run holds"):
        ledger.reserve(ours, reserved_nanousd=60, budget_nanousd=10_000, scope_budget_nanousd=100)
    ledger.reserve(ours, reserved_nanousd=40, budget_nanousd=10_000, scope_budget_nanousd=100)

    spend = ledger.scope_spend("run")
    assert (spend.calls, spend.unanswered, spend.spent_nanousd) == (2, 2, 100)
    assert spend.generation == ledger.generation()
    assert ledger.scope_spend("another run").spent_nanousd == 0
    assert ledger.spent_nanousd() == 101

    # The stop holds under a scope with room left.
    with pytest.raises(BudgetExceededError, match="stop"):
        ledger.reserve(ours, reserved_nanousd=1, budget_nanousd=101, scope_budget_nanousd=1000)
    with pytest.raises(ValueError, match="names its scope"):
        ledger.reserve(other, reserved_nanousd=1, budget_nanousd=10_000, scope_budget_nanousd=10)


def test_calls_of_one_scope_made_at_once_cannot_pass_its_budget_together(
    storage: Storage,
) -> None:
    ledger = Ledger(storage.ledger_db)
    call = Call(
        request_hash="h", task="scale", effort="low", max_output_tokens=16, request={}, scope="run"
    )

    def reserve(_: int) -> bool:
        try:
            ledger.reserve(
                call, reserved_nanousd=100, budget_nanousd=10_000, scope_budget_nanousd=550
            )
        except BudgetExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        admitted = list(pool.map(reserve, range(16)))

    assert admitted.count(True) == 5
    assert ledger.scope_spend("run").spent_nanousd == 500


def test_a_scoped_client_books_every_call_under_its_scope_and_stops_at_its_budget(
    storage: Storage,
) -> None:
    api = MetaApi()
    # Room for one call at the most it can cost, and after it is answered for less,
    # not for another.
    scope = SpendScope("scale-run:test", RESERVED_NANOUSD)
    ember = api.client(storage, scope=scope)
    # Spend that is not the scope's.
    ask(api.client(storage), input="Something else.")

    assert ask(ember).source == "live"
    with pytest.raises(BudgetExceededError, match="scale-run:test holds"):
        ask(ember, input="Another question.")
    # A replay is free and is booked under the scope too.
    assert ask(ember).source == "replay"

    assert [(row["scope"], row["source"]) for row in ledger_rows(storage)] == [
        (None, "live"),
        ("scale-run:test", "live"),
        ("scale-run:test", "replay"),
    ]
    spend = read_scope_spend(storage, "scale-run:test")
    assert (spend.calls, spend.unanswered, spend.spent_nanousd) == (1, 0, USAGE_COST_NANOUSD)
    assert spend.generation == ember.ledger_generation()

    # A scope that has reached its budget makes no request at all.
    api = MetaApi()
    with pytest.raises(BudgetExceededError, match="reaches its"):
        ask(
            api.client(storage, scope=SpendScope("scale-run:test", USAGE_COST_NANOUSD)),
            input="A third question.",
        )
    assert api.requests == []

    with pytest.raises(ValueError, match=r"\$9\.00"):
        SpendScope("too much", 9_000_000_001)
    with pytest.raises(ValueError, match="name"):
        SpendScope("", 1)


def test_reading_a_scope_creates_no_ledger(storage: Storage) -> None:
    assert read_scope_spend(storage, "run") is None
    assert not storage.ledger_db.exists()

    assert ask(MetaApi().client(storage, scope=SpendScope("run", 10**9))).source == "live"
    assert read_scope_spend(storage, "run").calls == 1


def _raise(error: BaseException) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    return handler


def _error(status: int, kind: str) -> Callable[[httpx2.Request], httpx2.Response]:
    # The API's message can repeat part of the request, here a patient's name.
    error = {"message": "Bad input: 'Jane Doe, left heel'", "type": kind, "param": None}
    return lambda _request: httpx2.Response(status, json={"error": error | {"code": None}})


@pytest.mark.parametrize(
    ("create", "status", "cost_nanousd", "status_code", "error"),
    [
        # Never left this machine: nothing can have been billed.
        pytest.param(
            _raise(httpx2.ConnectError("refused")),
            "error",
            0,
            None,
            "APIConnectionError (ConnectError)",
            id="never-sent",
        ),
        # The API answered with an error, or not at all. Meta does not say which of
        # these are free, so the call stays booked at the most it can cost.
        pytest.param(
            _error(400, "invalid_request_error"),
            "unknown",
            RESERVED_NANOUSD,
            400,
            "HTTP 400 invalid_request_error",
            id="invalid-request",
        ),
        pytest.param(
            _error(429, "rate_limit_error"),
            "unknown",
            RESERVED_NANOUSD,
            429,
            "HTTP 429 rate_limit_error",
            id="rate-limited",
        ),
        pytest.param(
            _error(500, "server_error"),
            "unknown",
            RESERVED_NANOUSD,
            500,
            "HTTP 500 server_error",
            id="server-error",
        ),
        pytest.param(
            _error(504, "server_error"),
            "unknown",
            RESERVED_NANOUSD,
            504,
            "HTTP 504 server_error",
            id="gateway-timeout",
        ),
        pytest.param(
            _raise(httpx2.ReadTimeout("slow")),
            "unknown",
            RESERVED_NANOUSD,
            None,
            "APITimeoutError (ReadTimeout)",
            id="timed-out",
        ),
        pytest.param(
            lambda _request: httpx2.Response(200, json=completed(usage=None)),
            "unknown",
            RESERVED_NANOUSD,
            None,
            "The API answered without usable token counts",
            id="no-usage",
        ),
        # A failed response that reports its usage is booked at that cost.
        pytest.param(
            lambda _request: httpx2.Response(
                200,
                json=completed(
                    status="failed",
                    error={"code": "server_error", "message": "Bad input: 'Jane Doe, left heel'"},
                ),
            ),
            "error",
            USAGE_COST_NANOUSD,
            None,
            "The response ended with status failed.",
            id="failed-with-usage",
        ),
    ],
)
def test_a_failed_call_is_booked_by_whether_it_may_have_been_billed(
    storage: Storage,
    create: Callable[[httpx2.Request], httpx2.Response],
    status: str,
    cost_nanousd: int,
    status_code: int | None,
    error: str,
) -> None:
    api = MetaApi(create=create)
    ember = api.client(storage)

    with pytest.raises(EmberCallError) as raised:
        ask(ember)

    assert raised.value.status_code == status_code
    # One count and one attempt: the client does not retry.
    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]
    (row,) = ledger_rows(storage)
    assert (row["status"], row["cost_nanousd"]) == (status, cost_nanousd)
    # The ledger keeps the kind of failure, not the text that came with it.
    assert row["error"] == error
    assert "Jane Doe" not in persisted(storage)
    assert trace_lines(storage)[0]["status"] == status

    # A failure is not recorded for replay, so the next attempt calls the API again.
    with pytest.raises(EmberCallError):
        ask(ember)
    assert len(api.requests) == 4


class Clock:
    """A clock that only moves when the pacer sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _rate_limited(retry_after: str | None = None) -> httpx2.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    error = {"message": "Slow down.", "type": "rate_limit_error", "param": None, "code": None}
    return httpx2.Response(429, json={"error": error}, headers=headers)


def test_a_paced_client_waits_out_a_rate_limit_and_books_every_attempt(storage: Storage) -> None:
    refusals = [_rate_limited("3"), _rate_limited()]
    api = MetaApi(
        create=lambda _request: (
            refusals.pop(0) if refusals else httpx2.Response(200, json=completed())
        )
    )
    clock = Clock()
    # 600 requests a minute is one every 0.1 s, and a call is two requests: the token
    # count, and 0.1 s behind it the call itself.
    pacer = Pacer(600, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    ember = api.client(storage, pacer=pacer)

    answer = ask(ember)

    assert answer.source == "live"
    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"] * 3
    # The header's 3 s after the first refusal, then the second attempt's own 1 s,
    # each waited by the next attempt's token count.
    assert clock.sleeps == pytest.approx([0.1, 3.0, 0.1, 1.0, 0.1])
    assert pacer.rate_limited == 2
    # Each refused attempt is a call of its own, booked at the most it could have
    # cost, because Meta does not say that a refusal is free.
    assert [(row["status"], row["cost_nanousd"]) for row in ledger_rows(storage)] == [
        ("unknown", RESERVED_NANOUSD),
        ("unknown", RESERVED_NANOUSD),
        ("ok", USAGE_COST_NANOUSD),
    ]

    # A replayed call sends nothing, so it takes no place under the rate limit. The
    # next live call's two requests are each spaced behind the one before.
    assert ask(ember).source == "replay"
    assert len(clock.sleeps) == 5
    ask(ember, input="Another question.")
    assert clock.sleeps[5:] == pytest.approx([0.1, 0.1])


def test_a_paced_client_gives_a_call_up_after_its_retries(storage: Storage) -> None:
    api = MetaApi(create=lambda _request: _rate_limited())
    clock = Clock()
    pacer = Pacer(3000, max_retries=2, clock=clock, sleep=clock.sleep)
    ember = api.client(storage, pacer=pacer)

    with pytest.raises(EmberCallError) as raised:
        ask(ember)

    assert raised.value.status_code == 429
    assert api.paths().count("/v1/responses") == 3
    # The stop still holds through the retries: the three attempts are all booked.
    assert Ledger(storage.ledger_db).spent_nanousd() == 3 * RESERVED_NANOUSD
    # The call is given up, and the API is still saying to slow down: the run's other
    # threads share the pacer, and their next request waits out the last refusal.
    assert pacer.wait() >= 2.0


def test_a_paced_client_retries_only_a_rate_limit(storage: Storage) -> None:
    api = MetaApi(create=_error(500, "server_error"))
    clock = Clock()
    ember = api.client(storage, pacer=Pacer(3000, clock=clock, sleep=clock.sleep))

    with pytest.raises(EmberCallError) as raised:
        ask(ember)

    assert (raised.value.status_code, raised.value.retry_after) == (500, None)
    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]


def test_a_rate_limited_token_count_is_retried_and_costs_nothing(storage: Storage) -> None:
    counts = iter([_rate_limited("0.5"), httpx2.Response(200, json={"input_tokens": 1200})])

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            return next(counts)
        return httpx2.Response(200, json=completed())

    clock = Clock()
    pacer = Pacer(3000, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        pacer=pacer,
    )

    assert ask(ember).source == "live"
    # The call was never reserved for the count that was refused, so only the one
    # that was answered is in the ledger.
    assert [row["status"] for row in ledger_rows(storage)] == ["ok"]
    # The header's 0.5 s before the count is asked again, then the call's own place.
    assert clock.sleeps == pytest.approx([0.5, 0.02])


def test_a_rate_limit_that_starts_while_the_tokens_are_counted_holds_the_paid_request(
    storage: Storage,
) -> None:
    clock = Clock()
    pacer = Pacer(3000, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)
    sent: list[tuple[str, float]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append((request.url.path, clock.now))
        if request.url.path == "/v1/responses/input_tokens":
            # Another thread of the run is answered 429 while this count is out.
            pacer.back_off(0, retry_after=2.0)
            return httpx2.Response(200, json={"input_tokens": 1200})
        return httpx2.Response(200, json=completed())

    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        pacer=pacer,
    )

    answer = ask(ember)

    # The request that costs money is not sent into the cooldown.
    assert sent == [("/v1/responses/input_tokens", 0.0), ("/v1/responses", pytest.approx(2.0))]
    # And the wait is not counted as the call's latency.
    assert answer.latency_ms < 1000


def test_a_wait_too_long_to_sit_through_stops_the_call_and_books_nothing(
    storage: Storage,
) -> None:
    clock = Clock()
    pacer = Pacer(3000, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            # Another thread's call is told to wait an hour while this count is out.
            with pytest.raises(RateLimitHoldError):
                pacer.back_off(0, retry_after=3600.0)
            return httpx2.Response(200, json={"input_tokens": 1200})
        raise AssertionError("the paid request was sent into an hour's cooldown")

    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        pacer=pacer,
    )

    with pytest.raises(RateLimitHoldError):
        ask(ember)
    # The call was reserved and never sent, so its reservation is given back.
    assert [(row["status"], row["cost_nanousd"]) for row in ledger_rows(storage)] == [("error", 0)]
    assert clock.sleeps == []
    # Nor is the next call's token count sent.
    with pytest.raises(RateLimitHoldError):
        ask(ember, input="Another question.")


def test_the_call_that_is_told_to_wait_too_long_is_stopped_and_not_failed(
    storage: Storage,
) -> None:
    api = MetaApi(create=lambda _request: _rate_limited("3600"))
    clock = Clock()
    ember = api.client(storage, pacer=Pacer(3000, clock=clock, sleep=clock.sleep))

    # Not the EmberCallError of a call that failed, which a run would take for the
    # image's failure and try again at once.
    with pytest.raises(RateLimitHoldError, match="asked for a wait of 3600 s") as raised:
        ask(ember)

    assert isinstance(raised.value.__context__, EmberCallError)
    assert not isinstance(raised.value, EmberCallError)
    # One attempt, booked at the most it could have cost, and no retry.
    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]
    assert [(row["status"], row["cost_nanousd"]) for row in ledger_rows(storage)] == [
        ("unknown", RESERVED_NANOUSD)
    ]
    # Nothing is sent after it, by this call or another, until the hour has passed.
    with pytest.raises(RateLimitHoldError, match="still to run"):
        ask(ember, input="Another question.")
    assert len(api.requests) == 2
    # The paid request's own place under the rate limit, and no sleep through the hour.
    assert clock.sleeps == pytest.approx([0.02])

    # A token count that is told the same stops the call too, with nothing booked.
    def refuse_the_count(request: httpx2.Request) -> httpx2.Response:
        return _rate_limited("7200")

    counted = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(refuse_the_count)),
        pacer=Pacer(3000, clock=clock, sleep=clock.sleep),
    )
    with pytest.raises(RateLimitHoldError, match="7200 s"):
        ask(counted, input="A third question.")
    assert len(ledger_rows(storage)) == 1


def test_retry_after_is_read_as_seconds_or_as_a_date(storage: Storage) -> None:
    def refused_with(header: str | None) -> float | None:
        api = MetaApi(create=lambda _request: _rate_limited(header))
        with pytest.raises(EmberCallError) as raised:
            ask(api.client(storage))
        return raised.value.retry_after

    now = datetime.datetime.now(datetime.UTC)
    in_a_minute = email.utils.format_datetime(now + datetime.timedelta(seconds=60), usegmt=True)
    an_hour_ago = email.utils.format_datetime(now - datetime.timedelta(hours=1), usegmt=True)

    assert refused_with("7") == 7.0
    assert refused_with("1.5") == 1.5
    assert 55 < refused_with(in_a_minute) <= 60
    # A date that has passed asks for no wait.
    assert refused_with(an_hour_ago) == 0.0
    for unusable in (None, "soon", "-3", "nan", "inf"):
        assert refused_with(unusable) is None


def test_a_date_in_retry_after_is_waited_out(storage: Storage) -> None:
    later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=20)
    refusals = [_rate_limited(email.utils.format_datetime(later, usegmt=True))]
    api = MetaApi(
        create=lambda _request: (
            refusals.pop(0) if refusals else httpx2.Response(200, json=completed())
        )
    )
    clock = Clock()
    pacer = Pacer(3000, clock=clock, sleep=clock.sleep, jitter=lambda: 0.0)

    assert ask(api.client(storage, pacer=pacer)).source == "live"

    # The call's own place, then the date's 20 s, less what the test itself took.
    assert clock.sleeps[0] == pytest.approx(0.02)
    assert 15 < clock.sleeps[1] <= 20


def test_the_wait_for_the_rate_limit_is_not_part_of_a_calls_latency(storage: Storage) -> None:
    api = MetaApi()
    # One request every 0.15 s, so the second call waits 0.3 s behind the first.
    ember = api.client(storage, pacer=Pacer(400))

    ask(ember)
    started = time.perf_counter()
    second = ask(ember, input="Another question.")

    assert time.perf_counter() - started >= 0.25
    assert second.latency_ms < 150


def test_a_pacer_belongs_to_a_live_client(storage: Storage) -> None:
    with pytest.raises(ValueError, match="pacer"):
        EmberClient(storage, pacer=Pacer())


def test_an_answer_cut_short_by_the_output_limit_is_returned_as_incomplete(
    storage: Storage,
) -> None:
    cut_short = completed(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    api = MetaApi(create=lambda _request: httpx2.Response(200, json=cut_short))

    answer = ask(api.client(storage))

    assert answer.status == "incomplete"
    assert ledger_rows(storage)[0]["cost_nanousd"] == USAGE_COST_NANOUSD


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_output_tokens": 15}, "from 16 to 524,288"),
        ({"max_output_tokens": None}, "from 16 to 524,288"),
        # One over Ember's largest output, which the API would answer with an error.
        ({"max_output_tokens": 524_289}, "from 16 to 524,288"),
        ({"sensitive": None}, "sensitive must be True or False"),
        ({"sensitive": 1}, "sensitive must be True or False"),
        # A setting the ledger keeps of a sensitive call, so not a string of any kind.
        ({"parallel_tool_calls": "ExampleName"}, "parallel_tool_calls must be True or False"),
        ({"parallel_tool_calls": 1}, "parallel_tool_calls must be True or False"),
        ({"effort": "none"}, "effort must be one of"),
        ({"task": ""}, "task"),
        ({"tools": [{"type": "web_search"}]}, r"tools\[0\] is not a function tool"),
        # Items that belong to hosted tools, one of which carries tool definitions.
        (
            {
                "input": [
                    {
                        "type": "tool_search_output",
                        "execution": "client",
                        "call_id": "call_1",
                        "status": "completed",
                        "tools": [{"type": "web_search"}],
                    }
                ]
            },
            r"input\[0\] is not an allowed kind of item",
        ),
        (
            {"input": [{"role": "user", "content": "hi"}, {"type": "web_search_call"}]},
            r"input\[1\] is not an allowed kind of item",
        ),
        ({"input": ["a bare string"]}, r"input\[0\] is not an allowed kind of item"),
        ({"input": [{"role": "user", "content": b"bytes"}]}, "not JSON"),
        # The bytes behind a web address can change after they are counted or recorded.
        (
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "https://example.com/smoke.jpg"}
                        ],
                    }
                ]
            },
            "image_url must be an inline data: URL",
        ),
        (
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_file", "file_url": "https://example.com/c.pdf"}
                        ],
                    }
                ]
            },
            "file_url must be an inline data: URL",
        ),
    ],
)
def test_a_request_that_breaks_a_client_rule_is_refused_in_every_mode(
    storage: Storage, overrides: dict[str, Any], message: str
) -> None:
    api = MetaApi()
    for ember in (api.client(storage), EmberClient.fake(storage, Script([fake_response()]))):
        with pytest.raises(EmberRequestError, match=message):
            ask(ember, **overrides)
    assert api.requests == []
    assert ledger_rows(storage) == []


def test_the_output_limit_reaches_embers_largest_output_and_no_further(storage: Storage) -> None:
    api = MetaApi()

    assert ask(api.client(storage), max_output_tokens=524_288).source == "live"

    assert json.loads(api.requests[-1].content)["max_output_tokens"] == 524_288
    # Reserved at the most it could cost, which is most of what the call can spend.
    with pytest.raises(BudgetExceededError, match="would pass"):
        ask(
            api.client(storage, stop_nanousd=524_288 * 500),
            max_output_tokens=524_288,
            input="Another request.",
        )


@pytest.mark.parametrize(
    "output",
    [
        None,
        "Jane Doe",
        [None, "Jane Doe", 7],
        [{"type": "message", "content": None}],
        [{"type": "message", "content": "Jane Doe"}],
        [{"type": "message", "content": [None]}],
        [{"type": "message", "content": [{"type": "output_text", "text": None}]}],
        [{"type": "message", "content": [{"type": "output_text", "text": ["Jane Doe"]}]}],
    ],
)
def test_an_answer_that_is_not_shaped_as_a_message_has_no_text_and_raises_nothing(
    storage: Storage, output: Any
) -> None:
    response = completed() | {"output": output}
    ember = EmberClient.fake(storage, lambda _body: response)

    assert ask(ember, sensitive=True).output_text == ""


def test_the_text_of_an_answer_is_read_past_a_part_that_is_not_text(storage: Storage) -> None:
    content = [None, {"type": "output_text", "text": "Two units."}, {"type": "refusal"}]
    response = completed() | {"output": [{"type": "message", "content": content}]}

    assert ask(EmberClient.fake(storage, lambda _body: response)).output_text == "Two units."


def test_output_text_is_the_final_answer_without_tool_call_commentary(storage: Storage) -> None:
    response = fake_response(text='{"units": 2}')
    response["output"].insert(
        0,
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Let me check the dispatch channel."}],
        },
    )

    answer = ask(EmberClient.fake(storage, Script([response])))

    assert json.loads(answer.output_text) == {"units": 2}


def test_a_fake_client_runs_a_tool_loop_with_no_key_and_spends_nothing(storage: Storage) -> None:
    tool = {"type": "function", "name": "get_radio", "parameters": {"type": "object"}}
    script = Script(
        [
            fake_response(function_calls=[("get_radio", {"channel": "fire", "minutes": 3})]),
            fake_response(text="Two units dispatched.", input_tokens=900, output_tokens=40),
        ]
    )
    ember = EmberClient.fake(storage, script)

    first = ask(ember, tools=[tool])
    (call,) = first.output
    result = {
        "type": "function_call_output",
        "call_id": call["call_id"],
        "output": json.dumps({"transmissions": 2}),
    }
    second = ask(ember, tools=[tool], input=[*first.output, result])

    assert (call["type"], call["name"]) == ("function_call", "get_radio")
    assert json.loads(call["arguments"]) == {"channel": "fire", "minutes": 3}
    assert second.output_text == "Two units dispatched."
    assert script.requests[0]["tools"] == [tool]
    assert script.requests[1]["input"][0]["call_id"] == call["call_id"]
    assert [(row["source"], row["cost_nanousd"]) for row in ledger_rows(storage)] == [
        ("fake", 0),
        ("fake", 900 * 100 + 40 * 500),
    ]
    assert ember.spent_usd() == 0
    assert not storage.replay_dir.exists()

    with pytest.raises(EmberError, match="has 2 responses"):
        ask(ember)


def test_the_environment_selects_replay_by_default_and_live_only_with_a_key(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env in ({}, {"REPLAY": "1", "MODEL_API_KEY": "test-key"}):
        with pytest.raises(ReplayMissError):
            ask(EmberClient.from_env(env, storage=storage))

    with pytest.raises(EmberConfigError, match="MODEL_API_KEY is not set"):
        EmberClient.from_env({"REPLAY": "0"}, storage=storage)
    with pytest.raises(EmberConfigError, match="REPLAY must be 1 or 0"):
        EmberClient.from_env({"REPLAY": "false", "MODEL_API_KEY": "test-key"}, storage=storage)

    live = {"REPLAY": "0", "MODEL_API_KEY": "test-key"}
    assert EmberClient.from_env(live, storage=storage).stop == SpendingStop(
        ledger_nanousd=3_000_000_000, spent_before_nanousd=0
    )
    monkeypatch.setenv("EMBER_STOP_USD", "0.50")
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "1.25")
    assert EmberClient.from_env(live, storage=storage).stop == SpendingStop(
        ledger_nanousd=500_000_000, spent_before_nanousd=1_250_000_000
    )

    # Building the live client is stubbed out, to see what it is handed.
    built: list[tuple[Storage, dict[str, Any]]] = []
    monkeypatch.setattr(
        EmberClient,
        "live",
        classmethod(lambda cls, storage, **options: built.append((storage, options))),
    )
    scope = SpendScope("tap:1", 1000)
    pacer = Pacer()
    EmberClient.from_env(live, storage=storage, scope=scope, pacer=pacer)
    assert built == [(storage, {"api_key": "test-key", "pacer": pacer, "scope": scope})]


def test_a_stop_that_cannot_be_read_is_refused_in_replay_mode_too(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in (("EMBER_STOP_USD", "three"), ("EMBER_SPENT_BEFORE_USD", "-1")):
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            with pytest.raises(EmberConfigError, match=name):
                EmberClient.from_env({"REPLAY": "1"}, storage=storage)


def test_a_replay_from_the_environment_is_booked_under_the_scope_it_is_given(
    storage: Storage,
) -> None:
    ask(MetaApi().client(storage))

    replayed = ask(EmberClient.from_env({}, storage=storage, scope=SpendScope("tap:1", 1000)))

    assert replayed.source == "replay"
    assert [row["scope"] for row in ledger_rows(storage)] == [None, "tap:1"]


def test_data_lives_under_beacon_data_dir(tmp_path: Path) -> None:
    ember = EmberClient.from_env({"BEACON_DATA_DIR": str(tmp_path)})

    with pytest.raises(ReplayMissError):
        ask(ember)
    assert (tmp_path / "ember" / "ledger.sqlite3").exists()


def test_a_relative_data_directory_is_refused_so_every_process_shares_one_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unset or empty, it is the repository's data/ whatever the working directory.
    repository = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)
    assert default_data_dir({}) == default_data_dir({"BEACON_DATA_DIR": ""}) == repository / "data"

    # A relative one would be a different directory, and ledger, from here.
    for relative in ("data", "./data", "../data"):
        with pytest.raises(EmberConfigError, match="BEACON_DATA_DIR must be an absolute path"):
            EmberClient.from_env({"BEACON_DATA_DIR": relative})
    assert not (tmp_path / "data").exists()
    monkeypatch.setenv("BEACON_DATA_DIR", "data")
    with pytest.raises(SystemExit, match="absolute path"):
        main()
    # Nor does a client that can spend take a relative ledger path it is handed.
    with pytest.raises(EmberConfigError, match="needs an absolute ledger path"):
        MetaApi().client(Storage.under(Path("data")))
    assert not (tmp_path / "data").exists()


def test_a_client_says_where_its_answers_can_come_from(storage: Storage) -> None:
    assert EmberClient.fake(storage, Script([])).mode == "fake"
    assert EmberClient.replay_only(storage).mode == "replay"
    assert MetaApi().client(storage).mode == "live"


@pytest.mark.parametrize("sensitive", [False, True])
def test_a_closed_client_makes_no_call_and_says_nothing_was_sent(
    storage: Storage, sensitive: bool
) -> None:
    api = MetaApi()
    live = api.client(storage)
    asked: list[Any] = []
    fake = EmberClient.fake(storage, lambda body: asked.append(body) or fake_response(text="ok"))
    live.close()
    fake.close()

    for ember in (live, fake):
        with pytest.raises(
            EmberClosedError, match=r"is closed .* Nothing that costs money was sent\.$"
        ) as closed:
            ask(ember, sensitive=sensitive)
        assert (closed.value.__cause__, closed.value.__context__) == (None, None)

    assert (api.paths(), asked, ledger_rows(storage)) == ([], [], [])
    assert (live.drain(0), fake.drain(0)) == (True, True)


@pytest.mark.parametrize("sensitive", [False, True])
def test_a_client_closed_while_a_calls_tokens_are_counted_does_not_send_the_paid_request(
    storage: Storage, sensitive: bool
) -> None:
    api = MetaApi()
    built: list[EmberClient] = []

    def close_during_the_count(request: httpx2.Request) -> httpx2.Response:
        # The process begins to shut down while the free request is out.
        built[0].close()
        return api(request)

    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(close_during_the_count)),
    )
    built.append(ember)

    with pytest.raises(EmberClosedError):
        ask(ember, sensitive=sensitive)

    # The request that costs money never left, and the ledger says so.
    assert api.paths() == ["/v1/responses/input_tokens"]
    (row,) = ledger_rows(storage)
    assert (row["status"], row["cost_nanousd"]) == ("error", 0)
    assert row["error"] == "Not sent: the client stopped before the paid request."
    assert ember.spent_usd() == 0
    assert ember.drain(0) is True


def test_a_paid_request_that_is_out_when_the_client_is_closed_is_answered_and_waited_for(
    storage: Storage,
) -> None:
    out, answer = threading.Event(), threading.Event()

    def create(_request: httpx2.Request) -> httpx2.Response:
        out.set()
        assert answer.wait(10)
        return httpx2.Response(200, json=completed())

    api = MetaApi(create=create)
    ember = api.client(storage)
    with ThreadPoolExecutor(max_workers=1) as pool:
        call = pool.submit(ask, ember)
        assert out.wait(10)

        # Closing does not call the request off: it may be paid for either way.
        ember.close()
        assert ember.drain(0.05) is False
        assert not call.done()
        answer.set()

        assert call.result(10).source == "live"
        assert ember.drain(10) is True
    (row,) = ledger_rows(storage)
    assert row["status"] == "ok"
    # And nothing new is sent.
    with pytest.raises(EmberClosedError):
        ask(ember, input="Another request.")
    assert api.paths() == ["/v1/responses/input_tokens", "/v1/responses"]


def test_a_refusal_of_a_second_attempt_does_not_say_that_nothing_was_sent(
    storage: Storage,
) -> None:
    api = MetaApi(create=lambda _request: _rate_limited())
    clock = Clock()
    # The scope has room for one attempt at the most it can cost, and not for two.
    scope = SpendScope("ingest:openmhz", RESERVED_NANOUSD + 1)
    ember = api.client(storage, scope=scope, pacer=Pacer(3000, clock=clock, sleep=clock.sleep))

    with pytest.raises(EmberCallError) as raised:
        ask(ember)

    # The first attempt was sent and answered 429, and booked as it may have cost.
    # The second was refused by the budget before it was sent. What the caller is
    # told is the first: a refusal alone would say that the call cost nothing.
    assert not isinstance(raised.value, BudgetExceededError)
    assert raised.value.status_code == 429
    assert api.paths().count("/v1/responses") == 1
    assert Ledger(storage.ledger_db).spent_nanousd() == RESERVED_NANOUSD
    # A call that has sent nothing yet is told of the budget as before.
    with pytest.raises(BudgetExceededError):
        ask(ember, input="Another question.")


def test_a_second_attempt_that_counts_too_many_tokens_reports_the_attempt_that_was_sent(
    storage: Storage,
) -> None:
    api = MetaApi(input_tokens=150_000, create=lambda _request: _rate_limited())

    def grow(request: httpx2.Request) -> httpx2.Response:
        # The count is asked for again on the second attempt, and comes back one
        # token over the limit.
        if api.paths().count("/v1/responses") == 1:
            api.input_tokens = 150_001
        return api(request)

    clock = Clock()
    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(grow)),
        pacer=Pacer(3000, clock=clock, sleep=clock.sleep),
    )

    with pytest.raises(EmberCallError) as raised:
        ask(ember)

    # "Too large" alone would say that nothing of the call was sent. One paid
    # request was, and what it may have cost stays booked.
    assert not isinstance(raised.value, InputTooLargeError)
    assert raised.value.status_code == 429
    assert api.paths().count("/v1/responses") == 1
    assert Ledger(storage.ledger_db).spent_nanousd() > 0
    # A call of which nothing was sent is still told that it is too large.
    with pytest.raises(InputTooLargeError):
        ask(ember, input="Another question.")
    assert api.paths().count("/v1/responses") == 1


def test_a_client_closed_between_attempts_reports_the_attempt_that_was_sent(
    storage: Storage,
) -> None:
    built: list[EmberClient] = []

    def refuse_and_close(_request: httpx2.Request) -> httpx2.Response:
        built[0].close()
        return _rate_limited()

    api = MetaApi(create=refuse_and_close)
    clock = Clock()
    built.append(api.client(storage, pacer=Pacer(3000, clock=clock, sleep=clock.sleep)))

    with pytest.raises(EmberCallError) as raised:
        ask(built[0])

    assert not isinstance(raised.value, EmberClosedError)
    assert raised.value.status_code == 429
    assert api.paths().count("/v1/responses") == 1


def test_a_client_says_whether_it_makes_a_call_again(storage: Storage) -> None:
    api = MetaApi()
    assert api.client(storage).retries is False
    assert api.client(storage, pacer=Pacer(3000)).retries is True
    assert EmberClient.fake(storage, Script([])).retries is False


@pytest.mark.parametrize("status", [301, 302, 307, 308])
@pytest.mark.parametrize("redirected", ["/v1/responses/input_tokens", "/v1/responses"])
def test_a_redirect_from_the_api_is_not_followed_with_the_request(
    storage: Storage, status: int, redirected: str
) -> None:
    requests: list[httpx2.Request] = []
    api = MetaApi()

    def redirecting(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path == redirected and request.url.host != "elsewhere.example":
            return httpx2.Response(
                status, headers={"location": f"https://elsewhere.example{request.url.path}"}
            )
        # Wherever a followed redirect would take the request, it would be answered.
        return api(request)

    # A client as a caller could hand it over, set to follow redirects.
    http_client = httpx2.Client(transport=httpx2.MockTransport(redirecting), follow_redirects=True)
    ember = EmberClient.live(storage, api_key="test-key", http_client=http_client)
    audio = encoded(b"RIFF generated tone")

    with pytest.raises(EmberError):
        ask(
            ember,
            sensitive=True,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}
                    ],
                }
            ],
        )

    # Nothing was sent anywhere but to Meta, and the redirected request once.
    assert {request.url.host for request in requests} == {"api.meta.ai"}
    assert [request.url.path for request in requests].count(redirected) == 1


def test_the_client_built_without_an_http_client_follows_no_redirect(storage: Storage) -> None:
    ember = EmberClient.live(storage, api_key="test-key")

    assert ember._sdk is not None
    assert ember._sdk._client.follow_redirects is False


def test_a_rate_limited_token_count_is_not_taken_for_an_attempt_that_was_sent(
    storage: Storage,
) -> None:
    counts = [_rate_limited(), None]
    api = MetaApi()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            refused = counts.pop(0) if counts else None
            if refused is not None:
                api.requests.append(request)
                return refused
        return api(request)

    clock = Clock()
    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        # No room for even one attempt at the most it can cost.
        scope=SpendScope("ingest:openmhz", RESERVED_NANOUSD - 1),
        pacer=Pacer(3000, clock=clock, sleep=clock.sleep),
    )

    # The first count is answered 429 and the second succeeds, and then the budget
    # refuses the call. No paid request was ever sent, and the caller is told so.
    with pytest.raises(BudgetExceededError):
        ask(ember)

    assert api.paths() == ["/v1/responses/input_tokens"] * 2
    assert Ledger(storage.ledger_db).spent_nanousd() == 0


def test_a_sent_attempt_is_still_reported_when_a_later_attempts_count_fails(
    storage: Storage,
) -> None:
    counts = [None, httpx2.Response(500, json={"error": {"message": "x"}})]
    api = MetaApi(create=lambda _request: _rate_limited())

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/responses/input_tokens":
            failed = counts.pop(0) if counts else None
            if failed is not None:
                api.requests.append(request)
                return failed
        return api(request)

    clock = Clock()
    ember = EmberClient.live(
        storage,
        api_key="test-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        pacer=Pacer(3000, clock=clock, sleep=clock.sleep),
    )

    with pytest.raises(EmberCallError) as raised:
        ask(ember)

    # The paid request of the first attempt was sent and answered 429. The second
    # attempt's count failed, which says nothing was sent only of that attempt.
    assert raised.value.status_code == 429
    assert "was not sent" not in str(raised.value)
    assert api.paths().count("/v1/responses") == 1
    assert Ledger(storage.ledger_db).spent_nanousd() == RESERVED_NANOUSD
