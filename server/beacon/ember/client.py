"""The single client every Muse Ember call goes through.

`EmberClient.respond` sends one Responses API request to `muse-ember-1.0` and returns
the answer. Around that request it:

- answers from the replay cache when the same request was answered before, which costs $0
- refuses a live call that could take the ledger past the spending stop
- refuses a live call whose input is over the per-request token limit
- sets `max_output_tokens` and a reasoning effort on every request
- writes a ledger row for every call, from which the token trace is exported

A client given a `Pacer` (`beacon.ember.pacing`) also spaces its live requests under a
rate limit and tries a call again after the API answers 429. Without one it does
neither, and no failed call is retried.

A client given a `SpendScope` writes the scope's name on every ledger row it makes,
and refuses a live call that could take the scope's own live spend past the scope's
budget. That is a second limit under the spending stop, never in place of it.

A client runs in one of three modes. Replay-only (`REPLAY=1`, the default) never touches
the network and fails on a request with no recording. Live (`REPLAY=0`) calls the API on
a cache miss and records the answer. Fake answers from a function the caller supplies,
so the agent can run with no key and no recordings.
"""

import datetime
import email.utils
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import httpx2
import openai

from beacon.ember.errors import (
    BudgetExceededError,
    EmberCallError,
    EmberConfigError,
    EmberError,
    EmberRequestError,
    InputTooLargeError,
    ReplayMissError,
)
from beacon.ember.ledger import Call, Ledger, LedgerCall, RowRef, ScopeSpend, Source
from beacon.ember.pacing import Pacer
from beacon.ember.pricing import (
    ACCOUNT_CEILING_NANOUSD,
    MAX_INPUT_TOKENS,
    MODEL,
    SpendingStop,
    Usage,
    price_nanousd,
    to_usd,
)
from beacon.ember.replay import ReplayCache, redact, request_hash

BASE_URL = "https://api.meta.ai/v1"

# Ember reasons at "max" when no effort is sent, and reasoning tokens bill as output,
# so every call names its effort. "none" is not supported by Ember.
Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]

# The API rejects a smaller output limit.
MIN_OUTPUT_TOKENS = 16

REQUEST_TIMEOUT_SECONDS = 120.0

# The status the API answers with when a rate limit is passed.
_RATE_LIMITED = 429

# A fake Ember: takes the request body and returns a Responses API response object.
Responder = Callable[[dict[str, Any]], Mapping[str, Any]]

# The request fields that shape the model's input, which is what the token count measures.
_COUNTED_FIELDS = (
    "model",
    "input",
    "instructions",
    "tools",
    "text",
    "reasoning",
    "parallel_tool_calls",
)

# Failures that happen before any byte of the request leaves this machine. Only these
# release a call's reservation. Meta does not document which error responses are free,
# so a call the API answered with an error stays booked at the most it can cost.
_NEVER_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)

# With `store: false` a reasoning item can only be sent back with its encrypted content,
# so every request asks for it. That makes `EmberResponse.output` safe to resend.
_INCLUDE = ("reasoning.encrypted_content",)

# The kinds of item `input` may hold, besides messages given by their `role`.
_INPUT_ITEM_TYPES = frozenset({"message", "reasoning", "function_call", "function_call_output"})

# The `type` and `code` of an API error are short machine-readable words.
_ERROR_WORD = re.compile(r"[\w.-]{1,64}")


@dataclass(frozen=True)
class Storage:
    """Where the client keeps its files."""

    ledger_db: Path
    trace: Path
    replay_dir: Path

    @classmethod
    def under(cls, data_dir: Path) -> "Storage":
        # The ledger has its own database file, so resetting or reseeding the app's
        # data never erases the record of what was spent.
        ember = data_dir / "ember"
        return cls(
            ledger_db=ember / "ledger.sqlite3",
            trace=ember / "trace.jsonl",
            replay_dir=ember / "replay",
        )


@dataclass(frozen=True)
class SpendScope:
    """A piece of work with a budget of its own, such as one tap's investigation or
    the scale run."""

    # Written on every ledger row the client makes. The scope's spend is the live
    # spend of the rows that carry it.
    label: str
    budget_nanousd: int

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("A scope needs a name.")
        if not 0 <= self.budget_nanousd <= ACCOUNT_CEILING_NANOUSD:
            raise ValueError(
                f"A scope's budget is from $0 to ${to_usd(ACCOUNT_CEILING_NANOUSD):.2f}. The "
                "client's spending stop holds it as well."
            )


def read_scope_spend(storage: Storage, label: str) -> ScopeSpend | None:
    """What the live calls made under a scope came to in the ledger file at `storage`
    now. None when there is no ledger file: reading does not create one."""
    if not storage.ledger_db.exists():
        return None
    return Ledger(storage.ledger_db).scope_spend(label)


def default_data_dir(env: Mapping[str, str]) -> Path:
    """`BEACON_DATA_DIR`, or the repository's Git-ignored `data/` directory."""
    configured = env.get("BEACON_DATA_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[3] / "data"


@dataclass(frozen=True)
class EmberResponse:
    # The response object as the API returned it.
    raw: dict[str, Any]
    usage: Usage
    # For a replay, how long the recorded live call took.
    latency_ms: int
    source: Source
    request_hash: str
    # The call's row in the ledger, and the generation of the ledger file it is in.
    # The id names the call only beside the generation.
    ledger_id: int
    ledger_generation: str

    @property
    def status(self) -> str | None:
        """`completed`, or `incomplete` when the output limit cut the answer short."""
        return self.raw.get("status")

    @property
    def output(self) -> list[dict[str, Any]]:
        """Output items in order: reasoning, function calls, and messages. A tool loop
        sends these back unchanged as input on its next round, followed by a
        `function_call_output` item for each function call."""
        return self.raw.get("output") or []

    @property
    def output_text(self) -> str:
        """The final answer's text. Commentary the model writes before a tool call is
        left out, so a structured output parses as JSON."""
        return "".join(
            part.get("text", "")
            for item in self.output
            if item.get("type") == "message" and item.get("phase") != "commentary"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        )

    @property
    def cost_usd(self) -> float:
        """What these tokens cost at Ember's rates. Only a live call spends it."""
        return to_usd(self.usage.cost_nanousd)


class EmberClient:
    def __init__(
        self,
        storage: Storage,
        *,
        api_key: str | None = None,
        http_client: httpx2.Client | None = None,
        responder: Responder | None = None,
        stop: SpendingStop | None = None,
        pacer: Pacer | None = None,
        scope: SpendScope | None = None,
    ) -> None:
        """Prefer `from_env`, `replay_only`, `live`, or `fake`."""
        if api_key is not None and responder is not None:
            raise ValueError("A client is either live or fake, not both.")
        if http_client is not None and api_key is None:
            raise ValueError("http_client is only used by a live client, which needs api_key.")
        if pacer is not None and api_key is None:
            raise ValueError("pacer is only used by a live client, which needs api_key.")
        self._storage = storage
        self._ledger = Ledger(storage.ledger_db)
        self._cache = ReplayCache(storage.replay_dir)
        # The client builds its own SDK object and takes none from callers, so every
        # live request goes to Meta with retries off.
        self._sdk = (
            None
            if api_key is None
            else openai.OpenAI(
                base_url=BASE_URL,
                api_key=api_key,
                # The SDK would otherwise resend a request that timed out or got an
                # error. Such a request may already have been billed, and the
                # reservation covers one attempt, so a retry is a new call that the
                # caller makes.
                max_retries=0,
                timeout=REQUEST_TIMEOUT_SECONDS,
                http_client=http_client,
            )
        )
        self._responder = responder
        # `SpendingStop` holds itself under the account's ceiling when it is made.
        self._stop = SpendingStop() if stop is None else stop
        self._pacer = pacer
        self._scope = scope

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        storage: Storage | None = None,
        pacer: Pacer | None = None,
        scope: SpendScope | None = None,
    ) -> "EmberClient":
        """Replay-only unless `REPLAY=0`, which needs `MODEL_API_KEY`. A live client
        stops where `EMBER_STOP_USD` and `EMBER_SPENT_BEFORE_USD` say
        (`SpendingStop.from_env`), and is the only kind that uses `pacer`.

        Raises EmberConfigError when a setting cannot be read, in either mode."""
        env = os.environ if env is None else env
        storage = storage or Storage.under(default_data_dir(env))
        # Read in replay mode too, so a mistyped stop is found before the day a key
        # is set.
        stop = SpendingStop.from_env(env)
        replay = env.get("REPLAY", "1")
        if replay == "1":
            return cls.replay_only(storage, scope=scope)
        if replay != "0":
            raise EmberConfigError(f"REPLAY must be 1 or 0, not {replay!r}.")
        api_key = env.get("MODEL_API_KEY")
        if not api_key:
            raise EmberConfigError(
                "REPLAY=0 asks for live Ember calls, but MODEL_API_KEY is not set. "
                "Set the key, or use REPLAY=1 to replay recorded responses."
            )
        return cls.live(storage, api_key=api_key, stop=stop, pacer=pacer, scope=scope)

    @classmethod
    def replay_only(cls, storage: Storage, *, scope: SpendScope | None = None) -> "EmberClient":
        return cls(storage, scope=scope)

    @classmethod
    def live(
        cls,
        storage: Storage,
        *,
        api_key: str,
        http_client: httpx2.Client | None = None,
        stop: SpendingStop | None = None,
        pacer: Pacer | None = None,
        scope: SpendScope | None = None,
    ) -> "EmberClient":
        """`http_client` replaces the HTTP transport, which is how tests stand in for the
        API. `stop` is where live spend stops, by default $3.00 in the ledger
        (`beacon.ember.pricing`). `pacer` spaces the requests and
        has a call tried again after a 429. `scope` holds the client's calls to a
        budget of their own as well."""
        return cls(
            storage,
            api_key=api_key,
            http_client=http_client,
            stop=stop,
            pacer=pacer,
            scope=scope,
        )

    @classmethod
    def fake(
        cls, storage: Storage, responder: Responder, *, scope: SpendScope | None = None
    ) -> "EmberClient":
        """A client that answers from `responder` and never spends. See `beacon.ember.fake`."""
        return cls(storage, responder=responder, scope=scope)

    @property
    def spends(self) -> bool:
        """Whether the client can make a paid call: it is live, with a key."""
        return self._sdk is not None

    @property
    def stop(self) -> SpendingStop:
        """Where this client's live spend stops."""
        return self._stop

    def spent_usd(self) -> float:
        """Live spend so far, counting calls with an unknown outcome at their maximum."""
        return to_usd(self._ledger.spent_nanousd())

    def ledger_generation(self) -> str:
        """The generation of the ledger file now: it changes when the file is deleted
        and begun again, and everything the old file held of spend is then gone."""
        return self._ledger.generation()

    def scope_spend(self, label: str) -> ScopeSpend:
        """What the live calls made under a scope came to, in the ledger file now."""
        return self._ledger.scope_spend(label)

    def export_trace(self) -> tuple[Path, int]:
        """Rewrite the JSONL token trace from the ledger. Returns its path and the
        number of calls in it."""
        return self._storage.trace, self._ledger.export_trace(self._storage.trace)

    def ledger_calls(self, refs: Sequence[tuple[str, int]]) -> list[LedgerCall]:
        """The ledger's rows for these calls, each named by its response's
        `(ledger_generation, ledger_id)`: tokens, cost, and latency, without the
        requests and responses. A call made in a ledger file that has since been
        deleted and begun again has no row, whatever row now has its id."""
        return self._ledger.calls([RowRef(*ref) for ref in refs])

    def respond(
        self,
        *,
        task: str,
        effort: Effort,
        max_output_tokens: int,
        input: str | Sequence[Mapping[str, Any]],
        instructions: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        text: Mapping[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
    ) -> EmberResponse:
        """Send one request to Muse Ember. `task` labels the call in the ledger; the
        other arguments are the Responses API fields of the same names. There is no
        `tool_choice`, because Meta supports only its default, `auto`. Media must be
        inline (`data:` URLs or base64) or an uploaded `file_id`, never a remote URL.

        Raises BudgetExceededError or InputTooLargeError when a live call is refused
        (LedgerReplacedError, a BudgetExceededError, when the ledger file was replaced
        under a running client),
        ReplayMissError when replay-only mode has no recording, and EmberCallError when
        the API fails. A failed call is not retried, except by a client with a pacer
        after the API answers 429: each such attempt is a call of its own in the ledger.
        A client with a pacer raises RateLimitHoldError when the API asks for a wait
        longer than the pacer makes, to the call that was told so and to every call
        after it until the wait has passed.
        """
        if not isinstance(task, str) or not task:
            raise EmberRequestError(
                "task must be a non-empty label, such as 'extract_transmission'."
            )
        body = _build_body(
            effort=effort,
            max_output_tokens=max_output_tokens,
            input=input,
            optional={
                "instructions": instructions,
                "tools": tools,
                "text": text,
                "parallel_tool_calls": parallel_tool_calls,
                "prompt_cache_key": prompt_cache_key,
            },
        )
        call = Call(
            request_hash=request_hash(body),
            task=task,
            effort=effort,
            max_output_tokens=max_output_tokens,
            request=redact(body),
            scope=None if self._scope is None else self._scope.label,
        )

        if self._responder is not None:
            started = time.perf_counter()
            response = _as_json(self._responder(body), "The fake Ember response")
            return self._record_free(call, "fake", response, _elapsed_ms(started))

        recording = self._cache.get(call.request_hash)
        if recording is not None:
            return self._record_free(call, "replay", recording.response, recording.latency_ms)
        if self._sdk is None:
            raise ReplayMissError(
                f"No recorded Ember response for request {call.request_hash} (task {task!r}) "
                f"in {self._cache.path(call.request_hash).parent}. Replay-only mode makes no "
                "API calls: record it with REPLAY=0 and MODEL_API_KEY, or use a fake client."
            )
        with self._cache.lock(call.request_hash):
            # Another thread or process may have asked the same thing and recorded the
            # answer while this one waited for the lock.
            recording = self._cache.get(call.request_hash)
            if recording is not None:
                return self._record_free(call, "replay", recording.response, recording.latency_ms)
            attempt = 0
            while True:
                try:
                    return self._call_live(self._sdk, body, call)
                except EmberCallError as error:
                    # Only a rate limit is waited out. Any other failure may have been
                    # billed and may fail the same way again. A wait too long to sit
                    # through is raised by `back_off` as a RateLimitHoldError.
                    if (
                        self._pacer is None
                        or error.status_code != _RATE_LIMITED
                        or not self._pacer.back_off(attempt, error.retry_after)
                    ):
                        raise
                    attempt += 1

    def _pace(self) -> None:
        if self._pacer is not None:
            self._pacer.wait()

    def _record_free(
        self,
        call: Call,
        source: Literal["replay", "fake"],
        response: dict[str, Any],
        latency_ms: int,
    ) -> EmberResponse:
        try:
            usage = Usage.from_response(response)
        except ValueError as error:
            raise EmberError(f"The {source} response for {call.request_hash}: {error}") from error
        safe, _holds_no_media = _for_disk(response)
        row = self._ledger.record(
            call, source=source, usage=usage, latency_ms=latency_ms, response=safe
        )
        return EmberResponse(
            response, usage, latency_ms, source, call.request_hash, row.id, row.generation
        )

    def _call_live(self, sdk: openai.OpenAI, body: dict[str, Any], call: Call) -> EmberResponse:
        # Before any request at all, as the stop below is. A replaced ledger holds
        # none of what was spent.
        self._ledger.require_opened_file()
        spent = self._ledger.spent_nanousd()
        if spent >= self._stop.ledger_nanousd:
            # Checked again, atomically, when the call is reserved. This early check
            # keeps a client that is over the stop from making any request at all.
            raise BudgetExceededError(
                f"Refused: the Ember ledger holds ${to_usd(spent):.6f} of live spend, which "
                f"reaches the ${to_usd(self._stop.ledger_nanousd):.2f} stop."
            )
        if self._scope is not None:
            scoped = self._ledger.scope_spend(self._scope.label).spent_nanousd
            if scoped >= self._scope.budget_nanousd:
                raise BudgetExceededError(
                    f"Refused: {self._scope.label} holds ${to_usd(scoped):.6f} of live "
                    f"spend in the Ember ledger, which reaches its "
                    f"${to_usd(self._scope.budget_nanousd):.6f} budget."
                )

        # Each of the call's two requests waits for its own place under the rate
        # limit. Those waits are not part of the call's latency.
        self._pace()
        started = time.perf_counter()
        input_tokens = _count_input_tokens(sdk, body)
        if input_tokens > MAX_INPUT_TOKENS:
            raise InputTooLargeError(
                f"Refused: the request has {input_tokens:,} input tokens, over the "
                f"{MAX_INPUT_TOKENS:,} limit."
            )

        # The most this call can cost: no cache hits and every output token used.
        reserved = price_nanousd(
            input_tokens=input_tokens, cached_tokens=0, output_tokens=call.max_output_tokens
        )
        counting = time.perf_counter() - started
        row = self._ledger.reserve(
            call,
            reserved_nanousd=reserved,
            budget_nanousd=self._stop.ledger_nanousd,
            scope_budget_nanousd=None if self._scope is None else self._scope.budget_nanousd,
        )
        try:
            # The last thing before the request that costs money, so a cooldown that
            # began while the tokens were counted or the call was reserved holds it.
            self._pace()
        except BaseException:
            # The request was not sent, so it cost nothing.
            self._ledger.settle(
                row,
                status="error",
                cost_nanousd=0,
                latency_ms=round(counting * 1000),
                error="Not sent: the client stopped while waiting for the rate limit.",
            )
            raise
        # The time the two requests took, without the wait between them.
        started = time.perf_counter() - counting

        try:
            response = _as_json(
                json.loads(sdk.responses.with_raw_response.create(**body).text),
                "The API response",
            )
        except Exception as error:
            # A request that never left this machine cost nothing. After any other
            # failure the model may have run, so the reservation stays.
            never_sent = _was_never_sent(error)
            self._ledger.settle(
                row,
                status="error" if never_sent else "unknown",
                cost_nanousd=0 if never_sent else reserved,
                latency_ms=_elapsed_ms(started),
                error=_summary(error),
            )
            raise EmberCallError(
                _describe(error),
                status_code=getattr(error, "status_code", None),
                retry_after=_retry_after(error),
            ) from error

        latency_ms = _elapsed_ms(started)
        # `safe` is what is written to disk. The caller gets the response as the API sent it.
        safe, holds_no_media = _for_disk(response)
        try:
            usage = Usage.from_response(response)
        except ValueError as error:
            message = "The API answered without usable token counts"
            self._ledger.settle(
                row,
                status="unknown",
                cost_nanousd=reserved,
                latency_ms=latency_ms,
                response=safe,
                error=message,
            )
            raise EmberCallError(f"{message}: {redact(str(error))[:300]}") from error

        status = response.get("status")
        answered = status in ("completed", "incomplete")
        message = None
        if not answered:
            shown = status if isinstance(status, str) and status.isidentifier() else "unknown"
            message = f"The response ended with status {shown}."
        self._ledger.settle(
            row,
            status="ok" if answered else "error",
            cost_nanousd=usage.cost_nanousd,
            latency_ms=latency_ms,
            usage=usage,
            response=safe,
            error=message,
        )
        if message is not None:
            raise EmberCallError(message)
        if holds_no_media:
            self._cache.put(
                call.request_hash, request=call.request, response=response, latency_ms=latency_ms
            )
        # Otherwise the answer itself holds media. A recording must be exact to be
        # replayed and must not hold media, so this answer is not recorded.
        return EmberResponse(
            response, usage, latency_ms, "live", call.request_hash, row.id, row.generation
        )


def _build_body(
    *,
    effort: str,
    max_output_tokens: int,
    input: str | Sequence[Mapping[str, Any]],
    optional: Mapping[str, Any],
) -> dict[str, Any]:
    if effort not in get_args(Effort):
        raise EmberRequestError(f"effort must be one of {get_args(Effort)}, not {effort!r}.")
    if (
        not isinstance(max_output_tokens, int)
        or isinstance(max_output_tokens, bool)
        or max_output_tokens < MIN_OUTPUT_TOKENS
    ):
        raise EmberRequestError(
            f"max_output_tokens must be an integer of at least {MIN_OUTPUT_TOKENS}, "
            f"not {max_output_tokens!r}."
        )
    for tool in optional.get("tools") or ():
        if not isinstance(tool, Mapping) or tool.get("type") != "function":
            # Hosted tools bill outside the token prices (web search is $2.50 per 1,000
            # queries) and run extra model rounds the reservation does not cover.
            raise EmberRequestError(
                f"Only function tools are allowed, not {tool!r}: a hosted tool spends "
                "money the ledger cannot see."
            )
    body = {
        "model": MODEL,
        "input": input,
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": effort},
        # Nothing is kept on Meta's servers. A tool loop resends its history, which also
        # means the request hash describes the whole input.
        "store": False,
        "include": list(_INCLUDE),
        **{key: value for key, value in optional.items() if value is not None},
    }
    try:
        # A plain-JSON copy: detached from the caller's objects, and the same shape
        # whether the caller passed lists or tuples.
        body = json.loads(json.dumps(body))
    except (TypeError, ValueError) as error:
        raise EmberRequestError(f"The request is not JSON: {error}") from error
    if isinstance(body["input"], list):
        for item in body["input"]:
            _check_input_item(item)
    _reject_remote_media(body["input"], None)
    return body


def _check_input_item(item: Any) -> None:
    """Only messages, reasoning, and function calls with their results go in as input.
    Other item types belong to hosted tools: a `tool_search_output` item, for one,
    declares tools of its own, which would get around the function-only rule."""
    kind = item.get("type") if isinstance(item, Mapping) else None
    is_message = isinstance(item, Mapping) and kind is None and "role" in item
    if kind not in _INPUT_ITEM_TYPES and not is_message:
        raise EmberRequestError(
            f"An input item of type {kind!r} is not allowed. Input items are messages or "
            f"one of {sorted(_INPUT_ITEM_TYPES)}."
        )


def _for_disk(response: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """The copy of a response that goes in the ledger, and whether the response holds
    no media. Only a response that holds none can be recorded for replay as it is."""
    safe = redact(response)
    # Redaction replaces every `encrypted_content`, since the name alone proves nothing
    # about what a string holds. The one place it is put back is on the reasoning items
    # of `output` themselves: a replayed answer must carry the reasoning state the live
    # one did. The same key inside model text or tool arguments stays replaced.
    output = response.get("output")
    if isinstance(output, list):
        for original, copy in zip(output, safe["output"], strict=True):
            if _is_reasoning_state(original):
                copy["encrypted_content"] = original["encrypted_content"]
    holds_no_media = safe == response
    error = safe.get("error")
    if isinstance(error, Mapping):
        # A failed response's error message can repeat part of the request.
        code = error.get("code")
        known = isinstance(code, str) and _ERROR_WORD.fullmatch(code) is not None
        safe["error"] = {"code": code if known else None}
    return safe, holds_no_media


def _is_reasoning_state(item: Any) -> bool:
    if not isinstance(item, Mapping) or item.get("type") != "reasoning":
        return False
    state = item.get("encrypted_content")
    # Reasoning state is an opaque blob, never a data URL.
    return isinstance(state, str) and state[:5].lower() != "data:"


def _reject_remote_media(value: Any, key: str | None) -> None:
    """Meta fetches `image_url`, `video_url`, `audio_url` and `file_url` when they are
    web addresses. The request hash would then name a location, not bytes: the picture
    behind it could change under a recording, or grow after the token count."""
    if isinstance(value, str):
        if key is not None and key.endswith("_url") and value[:5].lower() != "data:":
            raise EmberRequestError(
                f"{key} must be an inline data: URL, not a remote URL. Send the bytes "
                "inline or upload the file and pass its file_id, so that the request "
                "fixes exactly what the model sees."
            )
    elif isinstance(value, Mapping):
        for name, item in value.items():
            _reject_remote_media(item, name)
    elif isinstance(value, list):
        for item in value:
            _reject_remote_media(item, key)


def _count_input_tokens(sdk: openai.OpenAI, body: Mapping[str, Any]) -> int:
    """Ask the API how many input tokens the request carries. Counting generates nothing
    and is not billed."""
    try:
        count = sdk.responses.input_tokens.count(
            **{field: body[field] for field in _COUNTED_FIELDS if field in body}
        ).input_tokens
    except openai.OpenAIError as error:
        raise EmberCallError(
            f"Could not count the request's input tokens, so it was not sent: {_describe(error)}",
            status_code=getattr(error, "status_code", None),
            retry_after=_retry_after(error),
        ) from error
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise EmberCallError(
            f"The token count endpoint returned {count!r}; the request was not sent."
        )
    return count


def _retry_after(error: Exception) -> float | None:
    """The seconds an error response's `Retry-After` header asks for: the header is
    a number of seconds or an HTTP date. None when there is no such header, or it
    holds neither."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    try:
        value = headers.get("retry-after")
    except AttributeError:
        return None
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.UTC)
        # A date that has passed asks for no wait.
        seconds = max(0.0, (moment - datetime.datetime.now(datetime.UTC)).total_seconds())
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _was_never_sent(error: Exception) -> bool:
    return isinstance(error, openai.APIConnectionError) and isinstance(error.__cause__, _NEVER_SENT)


def _summary(error: Exception) -> str:
    """What the ledger keeps about a failure: its kind, and none of the text the API
    or a library wrote, which could repeat part of a request."""
    if isinstance(error, openai.APIStatusError):
        words = [
            word
            for word in (getattr(error, "type", None), getattr(error, "code", None))
            if isinstance(word, str) and _ERROR_WORD.fullmatch(word)
        ]
        return " ".join([f"HTTP {error.status_code}", *words])
    cause = error.__cause__
    return type(error).__name__ + (f" ({type(cause).__name__})" if cause is not None else "")


def _describe(error: Exception) -> str:
    """The message for the exception the caller sees, which is not written to disk."""
    detail = error.message if isinstance(error, openai.APIStatusError) else str(error)
    return f"{_summary(error)}: {redact(str(detail))[:300]}"


def _as_json(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} is not a JSON object.")
    return json.loads(json.dumps(value))


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
