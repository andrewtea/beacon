"""The notice poller: what it fetches, what it pays Ember to read, and what a row holds."""

import base64
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from beacon.ember import (
    BudgetExceededError,
    EmberClient,
    EmberClosedError,
    EmberError,
    LedgerReplacedError,
    Pacer,
    SpendScope,
    Storage,
    fake_response,
)
from beacon.evidence import EvidenceStore, EvidenceStoreError
from beacon.ingest import Clock, Ingest, PaidWork, PollerHeldError, Runner, SourceError
from beacon.ingest import notices as notices_module
from beacon.ingest.http import SourceHttp
from beacon.ingest.notices import (
    MAX_AGE,
    MAX_FETCHES_PER_POLL,
    MAX_ITEMS,
    MAX_READS_PER_POLL,
    NoticeFeed,
    parse_feed,
)
from beacon.ingest.paid import HELD_INTERVAL_SECONDS, UNUSABLE_RUN_LIMIT
from beacon.notice import MAX_PDF_BYTES
from beacon.scenario.pdf import text_pdf
from tests.test_radio_extract import everything_on_disk

NOON = datetime(2026, 11, 27, 20, 0, tzinfo=UTC)
HOST = "notices.example.org"
FEED = f"https://{HOST}/feed.json"


def minutes(count: float) -> datetime:
    return NOON + timedelta(minutes=count)


def pdf(number: int) -> bytes:
    """An invented one-page document no other notice has."""
    return text_pdf([f"NOTICE {number}", "Invented for a test.", "Leave Hayes St now."])


def form(**changes: Any) -> dict[str, Any]:
    filled = {
        "notice_type": "evacuation_order",
        "hazard": "fire",
        "issuer": "San Francisco Department of Emergency Management",
        "title": "Evacuation Order: 400 block of Hayes St",
        "issued_at": "2026-11-27T19:40:00Z",
        "effective_until": None,
        "areas": ["Both sides of Hayes St between Gough St and Octavia St"],
        "streets": ["Hayes St", "Gough St"],
        "instruction": "Leave now, on foot, toward Franklin St.",
        "summary": "Everyone on the block is ordered to leave.",
    }
    return filled | changes


class Site:
    """Stands in for an agency's site and for Ember. `notice` publishes one: the
    feed lists it, its document is served, and Ember "reads" in it the form given."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.documents: dict[str, bytes | int] = {}
        self.read: dict[bytes, Any] = {}
        self.requests: list[httpx2.Request] = []
        # The Ember requests, by the document each carried.
        self.asked: list[bytes] = []
        # What Ember's client refuses a document with before anything is sent.
        self.refused: dict[bytes, Exception] = {}
        # What the feed's address answers with, when it is not the feed.
        self.feed_answer: httpx2.Response | None = None

    def notice(
        self,
        number: int,
        at: datetime | None,
        read: Any = None,
        *,
        document: bytes | int | None = None,
        host: str = HOST,
        **changes: Any,
    ) -> str:
        content = pdf(number) if document is None else document
        url = f"https://{host}/documents/notice-{number}.pdf"
        item = {
            "id": f"https://{HOST}/notices/{number}",
            "url": f"https://{HOST}/notices/{number}",
            "title": f"Notice {number}",
            "attachments": [{"url": url, "mime_type": "application/pdf"}],
        }
        if at is not None:
            item["date_published"] = at.isoformat().replace("+00:00", "Z")
        self.items.append(item | changes)
        self.documents[url] = content
        if isinstance(content, bytes):
            self.read[content] = form() if read is None else read
        return url

    def http(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == FEED:
            if self.feed_answer is not None:
                return self.feed_answer
            feed = {"version": "https://jsonfeed.org/version/1.1", "items": self.items[::-1]}
            return httpx2.Response(200, json=feed)
        content = self.documents.get(url, 404)
        if isinstance(content, int):
            return httpx2.Response(content, text=f"No document for Jane Doe at {url}")
        return httpx2.Response(200, content=content)

    def ember(self, body: dict[str, Any]) -> dict[str, Any]:
        (message,) = body["input"]
        data = message["content"][1]["file_data"].partition(",")[2]
        document = base64.b64decode(data)
        self.asked.append(document)
        read = self.read[document]
        if isinstance(read, Exception):
            raise read
        return fake_response(text=read if isinstance(read, str) else json.dumps(read))

    @property
    def downloads(self) -> list[str]:
        return [
            request.url.path.rsplit("-", 1)[-1].removesuffix(".pdf")
            for request in self.requests
            if str(request.url) != FEED
        ]


@pytest.fixture
def site() -> Site:
    return Site()


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence.sqlite3", "mock")


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage.under(tmp_path)


@pytest.fixture
def wall() -> list[datetime]:
    """The real time, as a poller is told it, which a test moves."""
    return [NOON]


@pytest.fixture(autouse=True)
def refusals(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """Has the Ember client refuse the documents `site.refused` names, with the
    error it names: a fake client has no spending stop and no ledger to lose."""
    read = notices_module.read_notice

    def reading(ember: EmberClient, document: bytes) -> Any:
        if document in site.refused:
            raise site.refused[document]
        return read(ember, document)

    monkeypatch.setattr(notices_module, "read_notice", reading)


def build(site: Site, storage: Storage, responder: Any = None, **options: Any) -> NoticeFeed:
    return NoticeFeed(
        SourceHttp(httpx2.MockTransport(site.http)),
        EmberClient.fake(storage, site.ember if responder is None else responder),
        **({"feed_url": FEED} | options),
    )


@pytest.fixture
def poller(site: Site, storage: Storage) -> NoticeFeed:
    return build(site, storage)


def ref_of(site: Site, number: int) -> str:
    """The name the evidence store knows a notice of the site's by."""
    (ref,) = (
        notice.ref
        for notice in parse_feed({"items": site.items}, {HOST})[0]
        if notice.document.endswith(f"notice-{number}.pdf")
    )
    return ref


def documents(store: EvidenceStore) -> list[str]:
    """The notices that have rows, by the number in each one's address, oldest first."""
    return [(row.url or "").rsplit("-", 1)[-1].removesuffix(".pdf") for row in store.search()]


def test_a_notice_is_fetched_read_by_ember_and_stored_as_one_row(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    url = site.notice(
        1, minutes(-20), form(effective_until="2026-11-28T04:00:00Z"), id="order/2026/11/27-1"
    )

    report = poller.poll(store, NOON)

    feed, document = site.requests
    assert (feed.method, str(feed.url)) == ("GET", FEED)
    assert feed.headers["accept"] == "application/feed+json, application/json"
    assert (document.method, str(document.url)) == ("GET", url)
    assert site.asked == [pdf(1)]
    assert (report.added, report.unreadable, report.partial) == (1, 0, False)
    (row,) = store.search()
    assert (row.kind, row.source, row.origin, row.model) == ("notice", "notices", "mock", "fake")
    # A model's reading of the authority's document, which is not the authority
    # speaking: not `official`, and the document is linked.
    assert (row.confidence, row.stand_in) == ("high", True)
    # When the feed says it was published, and where a person can read it.
    assert (row.occurred_at, row.url) == (minutes(-20), url)
    assert row.until == datetime(2026, 11, 28, 4, 0, tzinfo=UTC)
    # Where it applies is in the authority's words, and not a place on the map.
    assert (row.place, row.point) == (None, None)
    assert row.data == {
        "notice_type": "evacuation_order",
        "hazard": "fire",
        "issuer": "San Francisco Department of Emergency Management",
        "title": "Evacuation Order: 400 block of Hayes St",
        "issued_at": "2026-11-27T19:40:00+00:00",
        "effective_until": "2026-11-28T04:00:00+00:00",
        "areas": ["Both sides of Hayes St between Gough St and Octavia St"],
        "streets": ["Hayes St", "Gough St"],
        "instruction": "Leave now, on foot, toward Franklin St.",
        "summary": "Everyone on the block is ordered to leave.",
        "incomplete": [],
        "page": f"https://{HOST}/notices/1",
    }
    # A name of its own for this document of this item, which is no address.
    assert len(row.ref) == 32
    assert int(row.ref, 16) >= 0


def test_a_notice_is_read_once_however_often_it_is_listed(
    store: EvidenceStore, site: Site, storage: Storage, poller: NoticeFeed
) -> None:
    site.notice(1, minutes(-20))
    poller.poll(store, NOON)

    again = poller.poll(store, minutes(2))
    # Beacon is started again: a new poller, and the same store.
    restarted = build(site, storage).poll(EvidenceStore(store.path, "mock"), minutes(4))

    assert (again.added, restarted.added) == (0, 0)
    # Neither fetched nor paid for a second time.
    assert (site.downloads, site.asked) == (["1"], [pdf(1)])
    assert documents(store) == ["1"]


def test_a_document_is_fetched_only_from_a_host_that_was_named_over_https(
    store: EvidenceStore, site: Site, storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    site.notice(1, minutes(-50), host="files.elsewhere.example")
    site.notice(2, minutes(-40), attachments=[{"url": f"http://{HOST}/documents/notice-2.pdf"}])
    site.notice(3, minutes(-30), attachments=[{"url": f"https://{HOST}:8443/notice-3.pdf"}])
    site.notice(4, minutes(-25), attachments=[{"url": f"https://jane@{HOST}/notice-4.pdf"}])
    # A page with no document, and one whose attachment is a picture: no notices.
    site.items.append({"id": "street-cleaning", "url": f"https://{HOST}/news/street-cleaning"})
    site.notice(5, minutes(-20), attachments=[{"url": "https://x.example/a.jpg"}])
    site.notice(6, minutes(-10))
    poller = build(site, storage)
    runner = Runner([poller], store, Clock(lambda: NOON))

    with caplog.at_level(logging.WARNING):
        report = runner.step()["notices"]
        # The feed lists them again at every poll. They are counted once.
        again = poller.poll(store, minutes(2))

    assert (report.added, report.unreadable, report.partial) == (1, 4, False)
    assert again.unreadable == 0
    assert (site.downloads, documents(store)) == (["6"], ["6"])
    assert "notices sent 4 items Beacon could not read." in caplog.text

    # Told the other host, a poller reads what is there.
    named = build(site, storage, document_hosts=["Files.Elsewhere.Example"])
    assert named.poll(store, minutes(4)).added == 1
    assert documents(store) == ["1", "6"]


PUBLISHED = "2026-11-27T19:40:00Z"


def test_an_attachment_is_a_pdf_by_its_type_or_by_how_its_address_ends() -> None:
    def listed(attachment: Any, **item: Any) -> tuple[int, int]:
        fields = {"id": "a", "date_published": PUBLISHED, "attachments": [attachment]}
        notices, unreadable, _cut = parse_feed({"items": [fields | item]}, {HOST})
        return len(notices), len(unreadable)

    address = f"https://{HOST}/a"
    assert listed({"url": address, "mime_type": "application/pdf"}) == (1, 0)
    assert listed({"url": address, "mime_type": "Application/PDF; qs=1"}) == (1, 0)
    assert listed({"url": address + ".PDF"}) == (1, 0)
    # Said to be something else, it is not taken for a PDF by its name.
    assert listed({"url": address + ".pdf", "mime_type": "text/html"}) == (0, 0)
    assert listed({"url": address}) == (0, 0)
    assert listed("a.pdf") == (0, 0)
    # A notice with no id cannot be told from another, and is not read.
    assert listed({"url": address + ".pdf"}, id=None) == (0, 1)
    assert listed({"url": address + ".pdf"}, id="") == (0, 1)
    assert listed({"url": address + ".pdf"}, id=17) == (1, 0)
    assert listed({"url": "https://[not-an-address/a.pdf", "mime_type": "application/pdf"}) == (
        0,
        1,
    )


def test_a_notice_is_named_by_its_item_and_its_document() -> None:
    def refs(*items: dict[str, Any]) -> list[str]:
        return [notice.ref for notice in parse_feed({"items": list(items)}, {HOST})[0]]

    def item(item_id: str, name: str) -> dict[str, Any]:
        return {
            "id": item_id,
            "date_published": PUBLISHED,
            "attachments": [{"url": f"https://{HOST}/{name}.pdf"}],
        }

    (first,) = refs(item("a", "one"))
    # Listed twice, it is one notice. Another item, or another document of the
    # same item, is another: a revision under a new address is read.
    assert refs(item("a", "one"), item("a", "one")) == [first]
    assert len({first, *refs(item("b", "one")), *refs(item("a", "two"))}) == 3
    # What follows a # is never sent to a site, and names no other document.
    assert refs(item("a", "one.pdf#page=2")) == refs(item("a", "one.pdf#page=3"))


def test_a_document_is_at_a_plain_address_that_is_the_same_at_every_poll(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    signed = f"https://{HOST}/documents/notice-1.pdf?X-Signature=3c9d&X-Expires=600"
    site.notice(1, minutes(-30), attachments=[{"url": signed, "mime_type": "application/pdf"}])
    site.notice(2, minutes(-20), attachments=[{"url": f"https://{HOST}/download.pdf?"}])
    plain = site.notice(3, minutes(-10), url=f"https://{HOST}/notices/3?utm_source=feed#top")
    site.items[-1]["attachments"][0]["url"] = plain + "#page=2"

    report = poller.poll(store, NOON)

    # A link that is signed for a while would be another notice at the next poll,
    # and what signs it would be stored as the notice's source. It is not read.
    assert (report.added, report.unreadable) == (1, 2)
    assert site.downloads == ["3"]
    (row,) = store.search()
    # Stored and asked for without the fragment, and no page whose address has a query.
    assert (row.url, row.data["page"]) == (plain, None)
    assert "Signature" not in everything_on_disk(store.path.parent)


def test_what_is_not_a_feed_is_a_source_that_cannot_be_read(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    for answer in (
        httpx2.Response(200, json=[{"id": "a"}]),
        httpx2.Response(200, json={"items": "Jane Doe"}),
    ):
        site.feed_answer = answer
        with pytest.raises(SourceError, match=r"^notices sent a reply Beacon cannot read\.$"):
            poller.poll(store, NOON)
    site.feed_answer = httpx2.Response(200, text="<html>Jane Doe</html>")
    with pytest.raises(SourceError, match=r"^notices sent a reply that is not JSON\.$"):
        poller.poll(store, NOON)
    site.feed_answer = httpx2.Response(503, text="Down for Jane Doe")
    with pytest.raises(SourceError, match=r"^notices answered HTTP 503\.$"):
        poller.poll(store, NOON)
    assert site.asked == []


def test_only_the_top_of_a_long_feed_is_looked_at_and_the_poll_says_so() -> None:
    def item(number: int) -> dict[str, Any]:
        return {
            "id": str(number),
            "date_published": PUBLISHED,
            "attachments": [{"url": f"https://{HOST}/{number}.pdf"}],
        }

    notices, _unreadable, cut = parse_feed({"items": [item(n) for n in range(MAX_ITEMS)]}, {HOST})
    assert (len(notices), cut) == (MAX_ITEMS, False)

    notices, _unreadable, cut = parse_feed(
        {"items": [item(n) for n in range(MAX_ITEMS + 1)]}, {HOST}
    )
    assert (len(notices), cut) == (MAX_ITEMS, True)
    assert notices[-1].document.endswith(f"/{MAX_ITEMS - 1}.pdf")


def test_a_document_that_cannot_be_had_is_left_for_the_next_poll_and_the_rest_are_read(
    store: EvidenceStore, site: Site, storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    site.notice(1, minutes(-40))
    not_yet = site.notice(2, minutes(-30), document=404)
    down = site.notice(3, minutes(-20), document=503)
    page = site.notice(4, minutes(-10), document=b"<html>Jane Doe: not published yet</html>")
    site.notice(5, minutes(-5))
    poller = build(site, storage)
    runner = Runner([poller], store, Clock(lambda: NOON))

    with caplog.at_level(logging.DEBUG):
        report = runner.step()["notices"]

    # The newest first, and a document that failed kept none of the others back.
    assert site.downloads == ["5", "4", "3", "2", "1"]
    assert (site.asked, documents(store)) == ([pdf(5), pdf(1)], ["1", "5"])
    assert (report.added, report.unreadable, report.partial) == (2, 3, True)
    assert "Jane" not in caplog.text
    # Nothing was claimed for them, and nothing asked of Ember.
    assert poller.poll(store, minutes(2)).unreadable == 0
    assert site.downloads[5:] == ["4", "3", "2"]

    # They are there now, and are read.
    for url, number in ((not_yet, 2), (down, 3), (page, 4)):
        site.documents[url] = pdf(number)
        site.read[pdf(number)] = form()
    report = poller.poll(store, minutes(4))

    assert (report.added, report.partial) == (3, False)
    assert documents(store) == ["1", "2", "3", "4", "5"]


def test_a_document_longer_than_a_notice_is_finished_with_unread(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    site.notice(1, minutes(-20), document=pdf(1) + b"\x00" * MAX_PDF_BYTES)
    site.notice(2, minutes(-10))

    report = poller.poll(store, NOON)
    again = poller.poll(store, minutes(2))

    assert (report.added, report.unreadable, report.partial) == (1, 1, False)
    assert (again.unreadable, again.partial) == (0, False)
    # Not sent to Ember, and not fetched every two minutes for a day.
    assert (site.asked, site.downloads) == ([pdf(2)], ["2", "1"])


def test_only_what_is_recent_is_read_and_a_notice_says_when_it_was_published(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    site.notice(1, NOON - MAX_AGE - timedelta(minutes=1))
    site.notice(2, NOON - MAX_AGE)
    # No date, a date with no zone, which is no moment, and what is no date.
    site.notice(3, None)
    site.notice(4, None, date_published="2026-11-27T12:00:00")
    site.notice(5, None, date_published=20261127)
    # A date still to come.
    site.notice(6, minutes(30))

    report = poller.poll(store, NOON)

    # When Beacon saw an item says nothing of when a notice was issued: an old
    # all-clear seen today is not a new one. Without a date it is not read.
    assert (report.added, report.unreadable, report.partial) == (1, 3, False)
    assert (site.downloads, documents(store)) == (["2"], ["2"])
    assert store.search()[0].occurred_at == NOON - MAX_AGE

    # The one dated later waits until then, and is then a notice of that moment.
    assert poller.poll(store, minutes(29)).added == 0
    assert poller.poll(store, minutes(30)).added == 1
    assert [row.occurred_at for row in store.search()] == [NOON - MAX_AGE, minutes(30)]


def test_a_poll_reads_the_newest_few_and_leaves_the_rest_for_the_next(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    for number in range(1, 7):
        site.notice(number, minutes(-70 + 10 * number))

    first = poller.poll(store, NOON)

    assert MAX_READS_PER_POLL == 4
    assert (first.added, first.partial) == (4, True)
    assert site.asked == [pdf(6), pdf(5), pdf(4), pdf(3)]

    second = poller.poll(store, minutes(2))

    assert (second.added, second.partial) == (2, False)
    assert site.asked[4:] == [pdf(2), pdf(1)]


def test_documents_that_cannot_be_had_do_not_use_up_a_poll_without_end(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    for number in range(1, MAX_FETCHES_PER_POLL + 3):
        site.notice(number, minutes(-60 + number), document=503)

    report = poller.poll(store, NOON)

    assert len(site.downloads) == MAX_FETCHES_PER_POLL
    assert (report.added, report.unreadable, report.partial) == (0, MAX_FETCHES_PER_POLL, True)


def test_until_when_a_notice_applies_is_kept_only_when_it_is_later_than_the_notice(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    site.notice(1, minutes(-20), form(effective_until="2026-11-27T19:00:00Z"))

    poller.poll(store, NOON)

    (row,) = store.search()
    assert row.until is None
    assert row.data["effective_until"] == "2026-11-27T19:00:00+00:00"


def test_of_a_press_release_the_row_keeps_no_text_and_nothing_of_it_is_on_disk(
    store: EvidenceStore, site: Site, poller: NoticeFeed, tmp_path: Path
) -> None:
    site.notice(
        1,
        minutes(-20),
        form(
            notice_type="press_release",
            issuer="Jane Doe, 67",
            title="Three-alarm fire on Hayes St is out; Jane Doe hospitalized",
            areas=["432 Hayes St, the home of Jane Doe"],
            streets=["432 Hayes St", "Four Three Two Hayes St"],
            instruction=None,
            summary="Jane Doe, 67, of 432 Hayes St, was taken to a hospital with burns.",
        ),
        document=text_pdf(["PRESS RELEASE", "Jane Doe, 67, of 432 Hayes St, has burns."]),
    )
    site.notice(2, minutes(-10), form(notice_type="other", hazard="none", issuer=None))

    poller.poll(store, NOON)

    release, other = store.search()
    assert (release.confidence, release.data["notice_type"]) == ("high", "press_release")
    # What it is, what it is about, when, and where to read it.
    assert release.data == {
        "notice_type": "press_release",
        "hazard": "fire",
        "issuer": None,
        "title": None,
        "issued_at": "2026-11-27T19:40:00+00:00",
        "effective_until": None,
        "areas": [],
        "streets": [],
        "instruction": None,
        "summary": None,
        "incomplete": [],
        "page": f"https://{HOST}/notices/1",
    }
    # What Ember did not take for an authority's notice is trusted least.
    assert (other.confidence, other.data["notice_type"]) == ("low", "other")
    on_disk = everything_on_disk(tmp_path)
    for word in ("Jane", "Doe", "burns", "432 Hayes", "Four Three Two", "PRESS RELEASE"):
        assert word not in on_disk


def test_a_notice_whose_row_could_not_be_stored_is_not_paid_for_again(
    store: EvidenceStore, site: Site, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.notice(1, minutes(-20))
    poller = build(site, storage)
    add = EvidenceStore.add

    def no_room(self: EvidenceStore, items: Any, **options: Any) -> Any:
        if list(items):
            raise sqlite3.OperationalError("database or disk is full")
        return add(self, items, **options)

    with monkeypatch.context() as patched:
        patched.setattr(EvidenceStore, "add", no_room)
        with pytest.raises(sqlite3.OperationalError):
            poller.poll(store, NOON)
    assert (documents(store), site.asked) == ([], [pdf(1)])

    # Beacon is started again. The document is claimed, and is not read again.
    report = build(site, storage).poll(EvidenceStore(store.path, "mock"), minutes(2))

    assert (report.added, site.asked, site.downloads) == (0, [pdf(1)], ["1"])


def test_a_notice_another_process_claimed_is_left_to_it(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    site.notice(1, minutes(-20))
    site.notice(2, minutes(-10))
    assert EvidenceStore(store.path, "mock").claim("notices", ref_of(site, 1), now=NOON) is True

    report = poller.poll(store, NOON)

    assert (report.added, documents(store), site.asked) == (1, ["2"], [pdf(2)])
    assert site.downloads == ["2"]


def test_an_answer_that_is_no_form_was_paid_for_and_is_not_asked_for_again(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    site.notice(1, minutes(-20), "The order tells Jane Doe to leave.")
    site.notice(2, minutes(-10))

    report = poller.poll(store, NOON)
    again = poller.poll(store, minutes(2))

    assert (report.added, report.unreadable, again.added) == (1, 1, 0)
    assert site.asked == [pdf(2), pdf(1)]
    assert store.state("notices")["unusable_run"] == 1


def test_answers_that_are_no_form_several_times_running_hold_the_poller(
    store: EvidenceStore, site: Site, storage: Storage, wall: list[datetime]
) -> None:
    for number in range(1, 6):
        site.notice(number, minutes(-60 + number), "A sentence, and no form.")
    poller = build(site, storage, wall_clock=lambda: wall[0])

    with pytest.raises(PollerHeldError) as held:
        poller.poll(store, NOON)

    assert str(held.value) == (
        "The notice poller is leaving Ember alone for a while: its answers about notices "
        "were not filled-in forms, several times in a row."
    )
    assert held.value.retry_after == HELD_INTERVAL_SECONDS
    assert len(site.asked) == UNUSABLE_RUN_LIMIT
    # Held, it asks the feed nothing either.
    with pytest.raises(PollerHeldError):
        poller.poll(store, minutes(2))
    assert len(site.requests) == 1 + UNUSABLE_RUN_LIMIT

    # After the hold, one more answer of the kind holds it again.
    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS)
    with pytest.raises(PollerHeldError):
        poller.poll(store, wall[0])
    assert len(site.asked) == UNUSABLE_RUN_LIMIT + 1


def test_a_spending_stop_ends_the_poll_and_the_notice_is_read_when_it_is_lifted(
    store: EvidenceStore, site: Site, storage: Storage, wall: list[datetime]
) -> None:
    site.notice(1, minutes(-20))
    site.notice(2, minutes(-10))
    site.refused[pdf(1)] = BudgetExceededError("The spending stop was reached.")
    poller = build(site, storage, wall_clock=lambda: wall[0])
    runner = Runner([poller], store, Clock(lambda: wall[0]))

    results = runner.step()

    # The notice before the stop is stored, and the poller leaves Ember alone a while.
    assert isinstance(results["notices"], BudgetExceededError)
    assert documents(store) == ["2"]
    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS - 1)
    held = runner.step()["notices"]
    assert isinstance(held, PollerHeldError)
    assert str(held) == (
        "The notice poller is leaving Ember alone for a while, after Ember refused or failed."
    )
    assert held.retry_after == 1.0

    # The stop is lifted. The notice it refused was never sent, and is read now.
    site.refused.clear()
    wall[0] = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS + 2 * poller.interval_seconds)
    assert runner.step()["notices"].added == 1
    assert (documents(store), site.asked) == (["1", "2"], [pdf(2), pdf(1)])


def test_when_ember_itself_fails_the_notice_is_not_asked_about_again_and_the_hold_is_kept(
    store: EvidenceStore,
    site: Site,
    storage: Storage,
    wall: list[datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    site.notice(1, minutes(-30))
    site.notice(2, minutes(-20), RuntimeError("the API dropped Jane Doe's order"))
    site.notice(3, minutes(-10))
    poller = build(site, storage, wall_clock=lambda: wall[0])
    runner = Runner([poller], store, Clock(lambda: wall[0]))

    with caplog.at_level(logging.DEBUG):
        failure = runner.step()["notices"]

    # The one before it is stored. The one after it is not tried while Ember fails.
    assert isinstance(failure, EmberError)
    assert (failure.__cause__, failure.__context__) == (None, None)
    assert (documents(store), site.asked) == (["3"], [pdf(3), pdf(2)])
    assert "Jane" not in caplog.text
    held_until = NOON + timedelta(seconds=HELD_INTERVAL_SECONDS)
    assert store.state("notices")["held_until"] == held_until.isoformat()

    # Beacon is started again, which does not shorten the hold.
    restarted = build(site, storage, wall_clock=lambda: wall[0])
    wall[0] = held_until - timedelta(seconds=60)
    with pytest.raises(PollerHeldError) as held:
        restarted.poll(store, wall[0])
    assert held.value.retry_after == 60.0

    # After it, the notice that failed is left alone and the next one is read.
    wall[0] = held_until
    assert restarted.poll(store, wall[0]).added == 1
    assert (documents(store), site.asked) == (["1", "3"], [pdf(3), pdf(2), pdf(1)])


def test_a_replaced_ledger_keeps_the_claim_and_stops_the_poller_for_the_process(
    store: EvidenceStore,
    site: Site,
    storage: Storage,
    wall: list[datetime],
    caplog: pytest.LogCaptureFixture,
) -> None:
    site.notice(1, minutes(-20))
    site.refused[pdf(1)] = LedgerReplacedError("The ledger file was replaced.")
    poller = build(site, storage, wall_clock=lambda: wall[0])

    with caplog.at_level(logging.ERROR), pytest.raises(LedgerReplacedError):
        poller.poll(store, NOON)

    assert "The notice poller has stopped: the Ember ledger was replaced" in caplog.text
    # It can be raised after the paid request was sent, so the claim stays.
    site.refused.clear()
    wall[0] = NOON + timedelta(days=30)
    with pytest.raises(PollerHeldError, match="Compare Meta's Usage page with the ledger"):
        poller.poll(store, wall[0])
    assert (site.asked, len(site.requests)) == ([], 2)
    assert store.taken("notices", [ref_of(site, 1)]) == {ref_of(site, 1)}


def test_a_poller_that_is_closed_sends_ember_nothing_and_gives_up_its_claim(
    store: EvidenceStore, site: Site, storage: Storage
) -> None:
    site.notice(1, minutes(-20))
    poller = build(site, storage)
    assert isinstance(poller, PaidWork)
    poller.close()

    with pytest.raises(EmberClosedError):
        poller.poll(store, NOON)

    # Nothing was sent, so the notice is neither paid for nor lost, and the poller
    # is not held: the next run reads it.
    assert site.asked == []
    assert store.state("notices").get("held_until") is None
    assert poller.drain(0) is True
    assert build(site, storage).poll(store, minutes(2)).added == 1


def test_stopping_waits_for_a_reading_that_is_out_and_stores_its_row(
    store: EvidenceStore, site: Site, storage: Storage
) -> None:
    site.notice(1, minutes(-20))
    site.notice(2, minutes(-10))
    began, go_on = threading.Event(), threading.Event()

    def slow(body: dict[str, Any]) -> dict[str, Any]:
        began.set()
        assert go_on.wait(10)
        return site.ember(body)

    http = SourceHttp(httpx2.MockTransport(site.http))
    poller = NoticeFeed(http, EmberClient.fake(storage, slow), feed_url=FEED)
    clock = Clock(lambda: NOON)
    ingest = Ingest(store, Runner([poller], store, clock), clock, http, None, [poller])
    ingest.start()
    assert began.wait(10)
    assert poller.drain(0) is False
    threading.Timer(0.2, go_on.set).start()

    assert ingest.stop() is True

    # The answer that was paid for is stored. The other notice was never sent.
    assert (documents(store), site.asked) == (["2"], [pdf(2)])
    assert store.closed is True


def test_a_real_notice_is_not_read_by_a_stand_in_nor_a_stand_ins_by_a_live_ember(
    site: Site, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = EmberClient.fake(storage, site.ember)
    # What a stand-in for Ember made of a real order would be an invention about it.
    with pytest.raises(ValueError, match="A real notice is read by a live Ember"):
        NoticeFeed(SourceHttp(), fake, feed_url=FEED)
    with pytest.raises(ValueError, match="A real notice is read by a live Ember"):
        NoticeFeed(SourceHttp(), EmberClient.replay_only(storage), feed_url=FEED)

    # And a stand-in's documents are not paid for.
    stand_in = SourceHttp(httpx2.MockTransport(site.http))
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    live = EmberClient.live(storage, api_key="test-key")
    with pytest.raises(ValueError, match="a stand-in's by a fake one"):
        NoticeFeed(stand_in, live, feed_url=FEED)
    # A client that tries a call again after a 429 would send a claimed document twice.
    paced = EmberClient.live(storage, api_key="test-key", pacer=Pacer(3000))
    with pytest.raises(ValueError, match="makes each call once: no pacer"):
        NoticeFeed(SourceHttp(), paced, feed_url=FEED)


@pytest.mark.parametrize(
    "feed_url",
    [
        "",
        "notices.example.org/feed.json",
        "http://notices.example.org/feed.json",
        "https://notices.example.org:8443/feed.json",
        "https://jane:secret@notices.example.org/feed.json",
        "file:///etc/hostname",
        "https://notices.example.org/" + "a" * 500,
    ],
)
def test_the_feed_is_named_by_an_https_address(site: Site, storage: Storage, feed_url: str) -> None:
    with pytest.raises(ValueError, match="named by an https address"):
        build(site, storage, feed_url=feed_url)


def test_the_pollers_origin_is_its_clients_and_a_stand_ins_feed_is_kept_out_of_the_live_store(
    tmp_path: Path, site: Site, poller: NoticeFeed
) -> None:
    site.notice(1, minutes(-20))
    live = EvidenceStore(tmp_path / "live.sqlite3", "live")

    assert poller.origin == "mock"
    with pytest.raises(AttributeError):
        poller.origin = "live"  # type: ignore[misc]
    with pytest.raises(EvidenceStoreError, match="holds nothing a stand-in produced"):
        poller.poll(live, NOON)
    assert (site.requests, site.asked, live.search()) == ([], [], [])


def test_documents_that_keep_failing_do_not_keep_another_notice_waiting_until_it_is_old(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    # An order, and after it more notices than a poll fetches, none of which can be had.
    site.notice(1, minutes(-60))
    for number in range(2, MAX_FETCHES_PER_POLL + 2):
        site.notice(number, minutes(-60 + number), document=404)

    first = poller.poll(store, NOON)

    # The newest first, and the poll's fetches are used up before the order's turn.
    assert site.downloads == [str(number) for number in range(9, 1, -1)]
    assert (first.added, first.partial) == (0, True)

    second = poller.poll(store, minutes(2))

    # What has not been tried comes before what could not be had.
    assert site.downloads[8] == "1"
    assert (second.added, documents(store), site.asked) == (1, ["1"], [pdf(1)])
    # The ones that failed are still asked for, the longest left alone first.
    assert site.downloads[9:] == [str(number) for number in range(9, 2, -1)]
    third = poller.poll(store, minutes(4))
    assert site.downloads[16:18] == ["2", "9"]
    assert (third.added, third.partial) == (0, True)


@pytest.mark.parametrize(
    ("answer", "waits"),
    [
        (httpx2.Response(429, headers={"Retry-After": "900"}), 900.0),
        (httpx2.Response(503, headers={"Retry-After": "300"}), 300.0),
        # Asked to slow down, with no word on for how long.
        (httpx2.Response(429), None),
    ],
)
def test_a_documents_host_that_asks_for_a_wait_ends_the_poll_and_is_left_alone(
    store: EvidenceStore,
    site: Site,
    storage: Storage,
    wall: list[datetime],
    answer: httpx2.Response,
    waits: float | None,
) -> None:
    site.notice(1, minutes(-30))
    busy = site.notice(2, minutes(-20))
    site.notice(3, minutes(-10))
    real_http = site.http

    def http(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == busy:
            site.requests.append(request)
            return answer
        return real_http(request)

    poller = NoticeFeed(
        SourceHttp(httpx2.MockTransport(http)),
        EmberClient.fake(storage, site.ember),
        feed_url=FEED,
    )
    runner = Runner([poller], store, Clock(lambda: wall[0]))

    failure = runner.step()["notices"]

    # The notice before it is read. The next document is not asked for.
    assert isinstance(failure, SourceError)
    assert (failure.status, failure.retry_after) == (answer.status_code, waits)
    assert (site.downloads, documents(store)) == (["3", "2"], ["3"])
    # The runner leaves the feed and its documents alone for as long as was asked.
    wall[0] = NOON + timedelta(seconds=(waits or poller.interval_seconds) - 1)
    assert runner.step() == {}
    assert len(site.requests) == 3


def test_what_could_not_be_read_is_counted_once_whatever_went_wrong_with_it_later(
    store: EvidenceStore, site: Site, poller: NoticeFeed
) -> None:
    late = site.notice(1, minutes(-20), document=404)
    site.read[pdf(1)] = "A sentence, and no form."

    assert poller.poll(store, NOON).unreadable == 1
    assert poller.poll(store, minutes(2)).unreadable == 0
    # It is there now, and Ember's answer about it is no form: the same notice,
    # which was counted when its document could not be had.
    site.documents[late] = pdf(1)
    report = poller.poll(store, minutes(4))

    assert (report.added, report.unreadable, site.asked) == (0, 0, [pdf(1)])


@pytest.mark.parametrize("trouble", ["host", "ember"])
def test_what_a_poll_that_ended_in_an_error_counted_is_in_the_next_report(
    store: EvidenceStore, site: Site, storage: Storage, wall: list[datetime], trouble: str
) -> None:
    # An item that does not say when it was published, a document that is not
    # there, and after them the trouble that ends the poll with no report.
    site.notice(1, None)
    site.notice(2, minutes(-10), document=404)
    last = site.notice(3, minutes(-20), document=429 if trouble == "host" else None)
    if trouble == "ember":
        site.refused[pdf(3)] = EmberError("Ember failed.")
    poller = build(site, storage, wall_clock=lambda: wall[0])

    with pytest.raises(SourceError if trouble == "host" else EmberError):
        poller.poll(store, NOON)
    assert site.downloads == ["2", "3"]

    # The trouble is over, and Ember's hold on the poller with it.
    site.documents[last] = pdf(3)
    site.read[pdf(3)] = form()
    site.refused.clear()
    wall[0] = minutes(15)
    report = poller.poll(store, minutes(15))

    # The undated item and the missing document, each once, and not lost with the
    # poll that found them.
    assert report.unreadable == 2
    assert poller.poll(store, minutes(17)).unreadable == 0


def test_what_was_counted_is_remembered_for_the_last_two_thousand_and_no_more(
    site: Site, poller: NoticeFeed
) -> None:
    assert poller._new(["a", "b", "a"]) == 2
    assert poller._new(["b", "c"]) == 1
    assert poller._new([f"item {number}" for number in range(10 * MAX_ITEMS)]) == 10 * MAX_ITEMS

    # The oldest have been forgotten, and are news again. The newest have not.
    assert poller._new(["a", "b", "c"]) == 3
    assert poller._new([f"item {10 * MAX_ITEMS - 1}", "c"]) == 0


def test_a_poller_that_can_spend_has_a_budget_of_its_own(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBER_SPENT_BEFORE_USD", "0")
    unscoped = EmberClient.live(storage, api_key="test-key")
    scoped = EmberClient.live(
        storage, api_key="test-key", scope=SpendScope("ingest:notices", 500_000_000)
    )

    radio = EmberClient.live(
        storage, api_key="test-key", scope=SpendScope("ingest:openmhz", 500_000_000)
    )

    # Without one, all that holds its spending is the stop on everything.
    with pytest.raises(ValueError, match="needs the spending scope ingest:notices"):
        NoticeFeed(SourceHttp(), unscoped, feed_url=FEED)
    # Under radio's, reading notices would spend what was set aside for radio.
    with pytest.raises(ValueError, match="needs the spending scope ingest:notices"):
        NoticeFeed(SourceHttp(), radio, feed_url=FEED)

    with SourceHttp() as http:
        assert NoticeFeed(http, scoped, feed_url=FEED).origin == "live"
