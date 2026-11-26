"""The notices of the mock scenario: invented documents, as an official feed would list them.

Nothing here was ever issued. Each notice is a one-page PDF written here
(`beacon.scenario.pdf`), listed in a feed in the JSON Feed format, and "read" by a
stand-in for Ember that fills in the form the story has for that document
(`stand_in_form`). So the real poller runs on this unchanged: it reads the feed,
downloads each document, and has Ember read it. The Ember it has is the stand-in, and
the rows it stores say so (`model == "fake"`).

The notices tell the fire of `beacon.scenario.fire` from the authorities' side: an
evacuation order for the block, the all-clear that lifts it, and a press release. The
stand-in's form for the press release is untidy in the way a model's can be, with a
name, an age, and a house number in its summary, so that what Beacon drops of a press
release is seen to be dropped. The feed also lists an item with no document and one
whose document is on a host nobody named.

The host is under `.example`, which no one can register.
"""

import base64
import functools
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from beacon.ember import fake_response
from beacon.scenario.fire import INVENTED
from beacon.scenario.pdf import text_pdf

FEED_HOST = "notices.sf-mock.example"
FEED_URL = f"https://{FEED_HOST}/feed.json"


@dataclass(frozen=True)
class MockNotice:
    # Minutes into the story at which the feed first lists it.
    minute: float
    # Names its document: `<slug>.pdf`.
    slug: str
    title: str
    # The text of its one page, a line to a list item.
    lines: tuple[str, ...]
    # The form the stand-in for Ember fills in when it reads the document. The
    # times in it are minutes into the story, and are made moments by `_filled`.
    read: dict[str, Any] = field(hash=False)


def _form(notice_type: str, hazard: str, **fields: Any) -> dict[str, Any]:
    return {
        "notice_type": notice_type,
        "hazard": hazard,
        "issuer": "San Francisco Department of Emergency Management",
        "title": None,
        "issued_at": None,
        "effective_until": None,
        "areas": [],
        "streets": [],
        "instruction": None,
        "summary": None,
    } | fields


NOTICES: tuple[MockNotice, ...] = (
    MockNotice(
        19.0,
        "evacuation-order-hayes-st",
        "Evacuation Order: 400 block of Hayes St",
        (
            "EVACUATION ORDER",
            INVENTED,
            "Issued by the San Francisco Department of Emergency Management",
            "at the request of the San Francisco Fire Department.",
            "",
            "A building fire on the 400 block of Hayes St is spreading.",
            "Everyone in the buildings on both sides of Hayes St between Gough St",
            "and Octavia St must leave now.",
            "",
            "Leave on foot toward Franklin St. Do not use Hayes St.",
            "A reception point is open at Civic Center Plaza.",
            "",
            "This order is in effect until it is lifted.",
        ),
        _form(
            "evacuation_order",
            "fire",
            title="Evacuation Order: 400 block of Hayes St",
            issued_at=19.0,
            areas=["Both sides of Hayes St between Gough St and Octavia St"],
            streets=["Hayes St", "Gough St", "Octavia St", "Franklin St"],
            instruction=(
                "Leave now, on foot, toward Franklin St. Do not use Hayes St. A reception "
                "point is open at Civic Center Plaza."
            ),
            summary=(
                f"{INVENTED} Everyone in the buildings on Hayes St between Gough St and "
                "Octavia St is ordered to leave because of a spreading building fire."
            ),
        ),
    ),
    MockNotice(
        74.0,
        "all-clear-hayes-st",
        "Evacuation Order Lifted: 400 block of Hayes St",
        (
            "EVACUATION ORDER LIFTED",
            INVENTED,
            "Issued by the San Francisco Department of Emergency Management.",
            "",
            "The fire on the 400 block of Hayes St is out.",
            "The evacuation order for Hayes St between Gough St and Octavia St",
            "is lifted. Residents may return, except to 400 through 450 Hayes St,",
            "which stay closed until the Fire Department has inspected them.",
            "",
            "Hayes St remains closed to vehicles between Gough St and Octavia St.",
        ),
        _form(
            "all_clear",
            "fire",
            title="Evacuation Order Lifted: 400 block of Hayes St",
            issued_at=74.0,
            areas=[
                "Hayes St between Gough St and Octavia St",
                "400 through 450 Hayes St stay closed",
            ],
            streets=["Hayes St", "Gough St", "Octavia St"],
            instruction=(
                "Residents may return, except to 400 through 450 Hayes St, which stay "
                "closed until the Fire Department has inspected them."
            ),
            summary=(
                f"{INVENTED} The evacuation order for Hayes St between Gough St and "
                "Octavia St is lifted."
            ),
        ),
    ),
    MockNotice(
        88.0,
        "press-release-hayes-st-fire",
        "Three-alarm fire on Hayes St is out",
        (
            "PRESS RELEASE",
            INVENTED,
            "San Francisco Fire Department",
            "",
            "A three-alarm fire on the 400 block of Hayes St was brought under",
            "control in under an hour by more than one hundred firefighters.",
            "One resident was taken to a hospital. The cause is under investigation.",
        ),
        # As a model might fill it in, against its instructions: a person, an age,
        # and a house number, none of which a press release's row may keep.
        _form(
            "press_release",
            "fire",
            issuer="San Francisco Fire Department",
            title="Three-alarm fire on Hayes St is out; Jane Doe hospitalized",
            issued_at=88.0,
            areas=["432 Hayes St, the home of Jane Doe"],
            streets=["432 Hayes St", "Hayes at Gough"],
            summary="Jane Doe, 67, of 432 Hayes St, was taken to a hospital with burns.",
        ),
    ),
)


# What the stand-in makes of a document that is not the story's.
_NOT_A_NOTICE = _form("other", "none", issuer=None)


def document_url(notice: MockNotice) -> str:
    return f"https://{FEED_HOST}/documents/{notice.slug}.pdf"


def feed(start: datetime, at: datetime) -> dict[str, Any]:
    """What the feed would list at the moment `at`, for a story that began at
    `start`: a JSON Feed with the notices published by now, newest first."""
    items: list[dict[str, Any]] = [
        # A page with no document, which is not a notice Beacon reads.
        {
            "id": f"https://{FEED_HOST}/news/street-cleaning",
            "url": f"https://{FEED_HOST}/news/street-cleaning",
            "title": f"Street cleaning schedule. {INVENTED}",
            "date_published": _stamp(start - timedelta(minutes=30)),
        },
        # A document somewhere nobody named, which Beacon does not fetch.
        {
            "id": f"https://{FEED_HOST}/news/elsewhere",
            "url": f"https://{FEED_HOST}/news/elsewhere",
            "title": f"A document kept elsewhere. {INVENTED}",
            "date_published": _stamp(start - timedelta(minutes=20)),
            "attachments": [
                {"url": "https://files.elsewhere.example/a.pdf", "mime_type": "application/pdf"}
            ],
        },
    ]
    for notice in NOTICES:
        published = start + timedelta(minutes=notice.minute)
        if published > at:
            continue
        items.append(
            {
                "id": f"https://{FEED_HOST}/notices/{notice.slug}",
                "url": f"https://{FEED_HOST}/notices/{notice.slug}",
                "title": notice.title,
                "date_published": _stamp(published),
                "attachments": [{"url": document_url(notice), "mime_type": "application/pdf"}],
            }
        )
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": f"San Francisco emergency notices. {INVENTED}",
        "home_page_url": f"https://{FEED_HOST}/",
        "feed_url": FEED_URL,
        "items": items[::-1],
    }


def document(name: str) -> bytes | None:
    """The PDF whose file is `name`, such as `all-clear-hayes-st.pdf`, or None when
    there is no such document."""
    for notice in NOTICES:
        if name == f"{notice.slug}.pdf":
            return _pdf(notice.slug)
    return None


@functools.cache
def _pdf(slug: str) -> bytes:
    (notice,) = (notice for notice in NOTICES if notice.slug == slug)
    return text_pdf(list(notice.lines))


@functools.cache
def _read_by_digest() -> dict[str, MockNotice]:
    return {hashlib.sha256(_pdf(notice.slug)).hexdigest(): notice for notice in NOTICES}


def stand_in_form(body: Mapping[str, Any], start: datetime | None = None) -> dict[str, Any] | None:
    """The form the story has for the document in a `read_notice` request, or None
    when the request holds no document. A document that is not the story's is
    `other` to it. `start` is when the story began, which is what turns the form's
    minutes into moments; without it the form has no times. It reads nothing, and
    its answers are a stand-in's."""
    for item in body.get("input") or []:
        for part in item.get("content") or [] if isinstance(item, Mapping) else []:
            data = part.get("file_data") if isinstance(part, Mapping) else None
            if not isinstance(data, str):
                continue
            digest = hashlib.sha256(base64.b64decode(data.partition(",")[2])).hexdigest()
            notice = _read_by_digest().get(digest)
            if notice is None:
                return dict(_NOT_A_NOTICE)
            return _filled(notice.read, start)
    return None


def stand_in_reader(start: datetime) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    """A responder for `EmberClient.fake` that answers `read_notice` for the
    documents of a story that began at `start`, with `stand_in_form`."""

    def respond(body: Mapping[str, Any]) -> Mapping[str, Any]:
        form = stand_in_form(body, start)
        return fake_response(text=json.dumps(form if form is not None else _NOT_A_NOTICE))

    return respond


def _filled(form: dict[str, Any], start: datetime | None) -> dict[str, Any]:
    filled = dict(form)
    for name in ("issued_at", "effective_until"):
        minute = filled[name]
        filled[name] = (
            None if minute is None or start is None else _stamp(start + timedelta(minutes=minute))
        )
    return filled


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")
