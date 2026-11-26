"""Official notices for San Francisco: documents an authority published, from a feed.

A notice is an evacuation order, a shelter-in-place order, a road closure, an
all-clear, a press release: a PDF on an agency's site. The poller reads a feed that
lists them, downloads each document it has not read, has Ember read it
(`beacon.notice`), and stores one row for it.

**No real feed has been read.** The poller reads a feed in the JSON Feed format
(https://www.jsonfeed.org/version/1.1/), in which a notice is an item with a PDF
among its `attachments`. Beacon knows of no San Francisco agency that publishes its
notices that way today, so going live needs such a feed first: an agency's own, or
one an operator writes for the agency's pages. `beacon.ingest.setup` builds this
poller for a real feed only when `BEACON_NOTICES_FEED_URL` names one.

What follows from what a notice is:

- **A document is fetched only from a host that was named**, over HTTPS: the feed's
  own host and the ones in `document_hosts`. A feed that lists a document anywhere
  else is not followed there.
- **Nothing in Beacon's process parses a PDF.** The poller looks at how the file
  begins and how long it is, and hands it to Ember. The document is held in memory
  and is not stored; the row links it (`url`).
- **A notice is paid for once**, by the rules radio keeps to (`beacon.ingest.paid`).
  A document is claimed in the evidence store before Ember is asked about it, under
  a name made of the feed item's id and the document's address, and a document
  that is claimed is never read again: not after a crash, not after an answer that
  was no form. So **a document that is changed where it lies is not read again**;
  an authority that revises a notice lists it again, or under a new address.
- **Only what is recent is paid for.** A notice the feed dates more than `MAX_AGE`
  ago is passed over, at most `MAX_READS_PER_POLL` documents are read in a poll,
  the newest first, and all of it under the budget `setup` gives the poller's
  Ember client. A notice the feed gives no date is taken to be new, because not
  reading an order is the worse mistake; a feed that dates nothing has its whole
  listing read, four documents a poll, until the budget is spent.
- **A document that cannot be had is left for the next poll**, whatever the reason:
  it is not there yet, its host is down, it is not a PDF. The poll is reported as
  partial, and the other notices are read all the same. Only a document longer
  than a notice is (`beacon.notice.MAX_PDF_BYTES`) is finished with unread, because
  it is not fetched every two minutes for a day.
- **What could not be read is counted once.** A feed lists the same items at every
  poll, so an item that cannot be read, or a document that cannot be had, is
  counted as `unreadable` the first time this process sees it and not again.
- **Trouble with Ember holds the poller**, as it holds radio.

What a row says is Ember's reading of the document, and `beacon.notice.read` says
what is kept of it and why. The row's confidence is `official` because of where the
document came from: a feed and a host an operator named as an authority's. That it
was read by a model is said by `model`, and whoever shows the row links the
document, which outranks the reading.
"""

import contextlib
import hashlib
import json
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import urlsplit

from beacon.ember import EmberClient, EmberClosedError, EmberError, InputTooLargeError
from beacon.errors import raise_bare
from beacon.evidence import EvidenceStore, EvidenceStoreError, NewEvidence, Origin
from beacon.ingest.http import SourceError, SourceHttp, fetch, read_json
from beacon.ingest.paid import Hold, PaidDesk, read_hold
from beacon.ingest.runner import PollReport
from beacon.notice import MAX_PDF_BYTES, Notice, NoticeError, is_pdf, read_notice

SOURCE = "notices"

INTERVAL_SECONDS = 120.0
# A notice the feed dates longer ago than this is not read: the first poll of a
# feed does not buy its archive.
MAX_AGE = timedelta(hours=24)
# The most documents Ember is asked about in one poll, and the most that are
# fetched in one, which is more: a document that could not be had costs a fetch.
MAX_READS_PER_POLL = 4
MAX_FETCHES_PER_POLL = 8
# How many items of a feed are looked at, from its top.
MAX_ITEMS = 200

_MAX_FEED_BYTES = 2_000_000
_MAX_ADDRESS = 500
_MAX_ID = 1000


@dataclass(frozen=True)
class ListedNotice:
    """One notice in a feed: an item with a PDF Beacon may fetch."""

    # Names this document of this item, for the evidence store.
    ref: str
    # Where the document is.
    document: str
    # The page the feed gives for the item, when it is an HTTPS address.
    page: str | None
    # When the feed says the item was published, when it says so readably.
    published: datetime | None


class NoticeFeed:
    name = SOURCE
    interval_seconds = INTERVAL_SECONDS

    def __init__(
        self,
        http: SourceHttp,
        ember: EmberClient,
        *,
        feed_url: str,
        document_hosts: Collection[str] = (),
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`feed_url` is the address of a JSON Feed, over HTTPS. A document is
        fetched from the feed's own host or one of `document_hosts`, and from
        nowhere else. `wall_clock` is the time a hold is counted by, which is the
        real time even while the mock scenario is played on a clock of its own.

        `ember` is closed with the poller (`close`), so it is a client of the
        poller's own."""
        host = _host_of(feed_url)
        if host is None:
            raise ValueError("The notices feed is named by an https address, with no port.")
        if ember.mode != ("live" if http.origin == "live" else "fake"):
            # A real notice is not handed to a stand-in for Ember, whose answer
            # would be an invention about a real order, and a real Ember is not
            # paid to read a stand-in's document. A client that only replays can
            # read neither: the call is a sensitive one, and is never recorded.
            raise ValueError(
                "A real notice is read by a live Ember, and a stand-in's by a fake one."
            )
        if ember.retries:
            # Each try is a paid request, and a document is claimed for one.
            raise ValueError("The notice poller's Ember client makes each call once: no pacer.")
        self._http = http
        self._ember = ember
        self._feed_url = feed_url
        self._hosts = frozenset({host, *(named.lower() for named in document_hosts)})
        # What every poller that pays Ember keeps to: `beacon.ingest.paid`.
        self._paid = PaidDesk(ember, poller="notice", items="notices", wall_clock=wall_clock)
        # The items and documents already counted as unreadable by this process.
        self._counted: set[str] = set()

    @property
    def origin(self) -> Origin:
        """Where this poller's rows come from, which is where its client's answers
        come from and cannot be set."""
        return self._http.origin

    def close(self) -> None:
        """Admit no more paid work. A document the Ember client had already
        admitted still sends its paid request; `drain` waits for it."""
        self._paid.close()

    def drain(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the document that is being read under a
        claim to be finished with, and say whether it has been: Ember has answered,
        and the row is stored."""
        return self._paid.drain(timeout)

    def poll(self, store: EvidenceStore, now: datetime) -> PollReport:
        store.refuse_stand_in(self.origin)
        held_until, unusable_run = read_hold(store.state(self.name))
        hold = Hold(held_until, unusable_run)
        self._paid.check(hold)
        body = fetch(
            self._http,
            "GET",
            self._feed_url,
            source=SOURCE,
            max_bytes=_MAX_FEED_BYTES,
            headers={"Accept": "application/feed+json, application/json"},
        )
        listed, unread, cut = parse_feed(read_json(body, SOURCE), self._hosts)
        unreadable = self._new(unread)
        recent = [
            notice
            for notice in listed
            if notice.published is None or now - notice.published <= MAX_AGE
        ]
        taken = store.taken(SOURCE, [notice.ref for notice in recent])
        # The newest first: the all-clear before the order it lifts, and a notice
        # of today before one that has waited since yesterday. One with no date
        # is taken to be new.
        waiting = sorted(
            (notice for notice in recent if notice.ref not in taken),
            key=lambda notice: (notice.published or now, notice.ref),
            reverse=True,
        )
        added = fetched = read = 0
        left = False
        missed: list[str] = []
        for notice in waiting:
            if fetched >= MAX_FETCHES_PER_POLL or read >= MAX_READS_PER_POLL:
                left = True
                break
            fetched += 1
            # Free, and done before the claim: a document that cannot be had just
            # now is not claimed, and waits for the next poll.
            try:
                pdf = self._document(notice)
            except SourceError:
                if self._http.closed:
                    # Beacon is stopping, which is nothing about the document.
                    raise
                missed.append(notice.ref)
                continue
            if not store.claim(SOURCE, notice.ref, now=now):
                # Another process took it between the listing and now.
                continue
            if pdf is None:
                # Longer than a notice is. It stays claimed, and is not fetched again.
                unreadable += self._new([notice.ref])
                continue
            read += 1
            # From here to the row, a shutdown waits (`drain`).
            with self._paid.working():
                reading = self._read(store, notice, pdf, hold, now)
                if reading is None:
                    unreadable += 1
                    continue
                # The row and what the poller remembers are saved together.
                state = (self.name, hold.hold_state())
                added += len(store.add([self._row(notice, reading, now)], state=state, now=now))
        store.add([], state=(self.name, hold.hold_state()), now=now)
        return PollReport(
            added=added,
            unreadable=unreadable + self._new(missed),
            partial=left or cut or bool(missed),
        )

    def _new(self, keys: list[str]) -> int:
        """How many of these have not been counted as unreadable before, by this
        process. They have been now."""
        if len(self._counted) > 10 * MAX_ITEMS:
            # Long enough ago that saying it again is news.
            self._counted.clear()
        new = set(keys) - self._counted
        self._counted |= new
        return len(new)

    def _document(self, notice: ListedNotice) -> bytes | None:
        """A notice's document, or None for one that is longer than a notice is,
        which is finished with for good.

        Raises SourceError when it cannot be had as a PDF file just now: it could
        not be fetched, or what was fetched is not one."""
        try:
            body = fetch(self._http, "GET", notice.document, source=SOURCE, max_bytes=MAX_PDF_BYTES)
        except SourceError as error:
            if not error.too_large:
                raise
            return None
        if not is_pdf(body):
            # A page that says the document is not there yet looks like this.
            raise_bare(SourceError(SOURCE, "sent a document that is not a PDF file"))
        return body

    def _read(
        self, store: EvidenceStore, listed: ListedNotice, pdf: bytes, hold: Hold, now: datetime
    ) -> Notice | None:
        """What Ember read in a document that is claimed, or None when its answer
        came to nothing: it was no form, or the document is more than Ember takes.
        The document is finished with either way, and what the poller remembers is
        saved.

        Raises what Ember raised when it refused the document or failed on it, with
        the poller held, and PollerHeldError when answers that are no form have
        run on."""
        reading: Notice | None = None
        unusable = False
        failure: EmberError | None = None
        try:
            reading = read_notice(self._ember, pdf)
        except NoticeError:
            # Ember answered, and was paid, and the answer is no form. `poll` hands
            # over only what `read_notice` asks Ember about: a PDF of a notice's size.
            unusable = True
        except InputTooLargeError:
            # Refused before anything was paid, and it would be refused again.
            pass
        except EmberError as error:
            failure = error
        if failure is not None:
            self._failed(store, listed, hold, failure, now)
        held = None
        if unusable or reading is not None:
            held = self._paid.count_answer(hold, usable=reading is not None)
        if reading is None:
            store.add([], state=(self.name, hold.hold_state()), now=now)
        if held is not None:
            raise_bare(held)
        return reading

    def _failed(
        self,
        store: EvidenceStore,
        listed: ListedNotice,
        hold: Hold,
        failure: EmberError,
        now: datetime,
    ) -> NoReturn:
        """End the poll after Ember refused a document or failed on it. The claim
        on the document is given up only when the paid request is known not to
        have been sent; otherwise it may have been paid for, and is finished with."""
        if self._paid.not_sent(failure):
            # When the claim cannot be given up it stays, which loses the notice
            # and pays for nothing.
            with contextlib.suppress(EvidenceStoreError):
                store.release(SOURCE, listed.ref)
        if isinstance(failure, EmberClosedError):
            # Beacon is shutting down. That is no news about Ember.
            raise_bare(failure)
        self._paid.hold_after(failure, hold)
        with contextlib.suppress(EvidenceStoreError):
            store.add([], state=(self.name, hold.hold_state()), now=now)
        # Nothing under it: not the error this was caught as, nor one the caller
        # of `poll` may be handling.
        raise_bare(failure)

    def _row(self, listed: ListedNotice, reading: Notice, now: datetime) -> NewEvidence:
        # When the feed says it was published, which nobody had to read out of the
        # document, and when Beacon first saw it for a feed that does not say. A
        # date that is still to come is a mistake of the feed's.
        occurred_at = min(listed.published or now, now)
        until = reading.effective_until
        return NewEvidence(
            kind="notice",
            source=SOURCE,
            ref=listed.ref,
            url=listed.document,
            occurred_at=occurred_at,
            until=until if until is not None and until > occurred_at else None,
            # A notice has no place in words that is known to be no finer than a
            # block, and nothing here knows where on the map its streets are.
            # Where it applies is in `data`, in the authority's words.
            confidence="official" if reading.notice_type != "other" else "low",
            origin=self.origin,
            model=reading.read_by,
            data=reading.as_data()
            | {
                "page": listed.page,
                "published": None if listed.published is None else listed.published.isoformat(),
            },
        )


def parse_feed(payload: Any, hosts: Collection[str]) -> tuple[list[ListedNotice], list[str], bool]:
    """The notices a JSON Feed lists among its first `MAX_ITEMS` items, a name for
    each of its items that could not be read (the same each time it is listed), and
    whether it has more items than that.

    An item is a notice when it has a PDF among its attachments. It is unreadable
    when it has no id, or when none of its PDFs is at an HTTPS address on one of
    `hosts`: a document is not fetched from wherever a feed points. Any other item
    is neither, and a feed is full of them.

    Raises SourceError when the answer is not a feed."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise_bare(SourceError(SOURCE, "sent a reply Beacon cannot read"))
    notices: dict[str, ListedNotice] = {}
    unreadable: list[str] = []
    for item in items[:MAX_ITEMS]:
        fields = item if isinstance(item, dict) else {}
        attachments = fields.get("attachments")
        documents = [
            attachment.get("url")
            for attachment in (attachments if isinstance(attachments, list) else [])
            if isinstance(attachment, dict) and _says_pdf(attachment)
        ]
        if not documents:
            continue
        item_id = _item_id(fields.get("id"))
        allowed = [url for url in documents if _host_of(url) in hosts]
        if item_id is None or not allowed:
            unreadable.append(_digest(item))
            continue
        document = allowed[0]
        # Of the two as a pair, so that no id and address run together into another's.
        ref = hashlib.sha256(json.dumps([item_id, document]).encode()).hexdigest()[:32]
        page = fields.get("url")
        notices.setdefault(
            ref,
            ListedNotice(
                ref=ref,
                document=document,
                page=page if _host_of(page) is not None else None,
                published=_moment(fields.get("date_published")),
            ),
        )
    return list(notices.values()), unreadable, len(items) > MAX_ITEMS


def _digest(item: Any) -> str:
    """A name for an item that cannot be read, the same each time it is listed."""
    try:
        text = json.dumps(item, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(item)
    return "#" + hashlib.sha256(text.encode()).hexdigest()[:32]


def _says_pdf(attachment: dict[str, Any]) -> bool:
    """Whether an attachment is said to be a PDF: by its type, or, when it gives
    none, by how its address ends."""
    kind, url = attachment.get("mime_type"), attachment.get("url")
    if isinstance(kind, str) and kind.strip():
        return kind.partition(";")[0].strip().lower() == "application/pdf"
    try:
        return isinstance(url, str) and urlsplit(url).path.lower().endswith(".pdf")
    except ValueError:
        return False


def _item_id(value: Any) -> str | None:
    """A feed item's id. JSON Feed asks for a string, and says to take a number."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_ID else None


def _host_of(url: Any) -> str | None:
    """The host of an address Beacon may ask or link: HTTPS, on the usual port,
    with nobody's name or password in it. None for anything else."""
    if not isinstance(url, str) or len(url) > _MAX_ADDRESS:
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        has_port = parts.port is not None
    except ValueError:
        return None
    if parts.scheme != "https" or has_port or parts.username is not None or not host:
        return None
    return host


def _moment(value: Any) -> datetime | None:
    """When an item says it was published, or None when it does not say so readably."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.utcoffset() is None else moment
