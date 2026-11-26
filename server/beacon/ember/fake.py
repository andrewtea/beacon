"""A fake Ember for running the agent with no API key and no recordings.

Pass a responder to `EmberClient.fake`. The client still builds and checks the request
the way it does for a live call, and writes a ledger row with source `fake`, so code
that works against the fake sends requests that pass the client's own rules.

    script = Script([
        fake_response(function_calls=[("get_radio", {"channel": "fire", "minutes": 3})]),
        fake_response(text='{"status": "unconfirmed"}'),
    ])
    ember = EmberClient.fake(storage, script)
"""

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from beacon.ember.errors import EmberError
from beacon.ember.pricing import MODEL


def fake_response(
    *,
    text: str | None = None,
    function_calls: Sequence[tuple[str, Mapping[str, Any]]] = (),
    input_tokens: int = 0,
    cached_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    status: str = "completed",
) -> dict[str, Any]:
    """A Responses API response object. `function_calls` are (tool name, arguments)
    pairs, and `text` is the final answer. Token counts default to zero, which prices
    the call at $0 in the ledger; pass counts to rehearse realistic costs. Ids are
    random, as the API's are."""
    output: list[dict[str, Any]] = [
        {
            "type": "function_call",
            "id": f"fc_{_token()}",
            "call_id": f"call_{_token()}",
            "name": name,
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
        for name, arguments in function_calls
    ]
    if text is not None:
        output.append(
            {
                "type": "message",
                "id": f"msg_{_token()}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    return {
        "id": f"resp_{_token()}",
        "object": "response",
        "status": status,
        "model": MODEL,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": input_tokens + output_tokens,
        },
    }


class Script:
    """A responder that answers the nth request with the nth response. `requests` holds
    the request bodies it was sent, in order."""

    def __init__(self, responses: Iterable[Mapping[str, Any]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, body: dict[str, Any]) -> Mapping[str, Any]:
        self.requests.append(body)
        if len(self.requests) > len(self._responses):
            raise EmberError(
                f"The fake Ember script has {len(self._responses)} responses, and this is "
                f"request {len(self.requests)}."
            )
        return self._responses[len(self.requests) - 1]


def _token() -> str:
    return uuid.uuid4().hex[:24]
