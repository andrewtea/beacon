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
- **A document's address is a plain one**: no query. The address names the document
  in the evidence store and is stored and shown as its source, so it has to be the
  same at every poll and hold nothing but where the document is. A link that is
  signed for a while is neither, and an item that gives only such a link is not
  read. What follows a `#` is never sent to a site, and is dropped.
- **Nothing in Beacon's process parses a PDF.** The poller looks at how the file
  begins and how long it is, and hands it to Ember. The document is held in memory
  and is not stored; the row links it (`url`).
- **A notice is paid for once**, by the rules radio keeps to (`beacon.ingest.paid`).
  A document is claimed in the evidence store before Ember is asked about it, under
  a name made of the feed item's id and the document's address, and a document
  that is claimed is never read again: not after a crash, not after an answer that
  was no form. So **a document that is changed where it lies is not read again**;
  an authority that revises a notice lists it again, or under a new address.
- **A notice has to say when it was published.** A row's time is the feed's
  `date_published`, and never the moment Beacon happened to see the item: an old
  all-clear seen today is not a new one. An item with no date, or one that cannot
  be read as a moment, is not read. One dated later than now waits until then.
- **Only what is recent is paid for.** A notice dated more than `MAX_AGE` ago is
  passed over, at most `MAX_READS_PER_POLL` documents are read in a poll, the
  newest first, and all of it under the budget `setup` gives the poller's Ember
  client.
- **A document that cannot be had is left for the next poll**, whatever the reason:
  it is not there yet, its host is down, it is not a PDF. The poll is reported as
  partial, and the other notices are read all the same. At the next poll the
  documents that have not been tried come first, so that ones that keep failing
  do not use up every poll's fetches while another notice grows old. Only a
  document longer than a notice is (`beacon.notice.MAX_PDF_BYTES`) is finished
  with unread, because it is not fetched every two minutes for a day.
- **A host that asks for a wait gets it.** When a document's host answers 429, or
  with a `Retry-After`, the poll ends there with that error, and the runner leaves
  the feed and its documents alone for as long as was asked.
- **What could not be read is counted once.** A feed lists the same items at every
  poll, so an item that cannot be read, or a document that cannot be had, is
  counted as `unreadable` once and not at every poll, for as long as it is among
  the last `_COUNTED_ONCE` things this process counted. A poll that ends in an
  error makes no report, so what it counted is in the next report that is made.
- **Trouble with Ember holds the poller**, as it holds radio.

What a row says is Ember's reading of the document, and `beacon.notice.read` says
what is kept of it and why. The document is the authority speaking and the reading
is a model's, so the row's confidence is `high` and not `official`. `model` says
which Ember read it, and `url` is the document: whoever shows the row says it is a
reading, links the document, and sends people there for what to do.
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
# The label of the spending scope every live reading of a notice is paid under. A
# live Ember client that pays under any other is refused (`beacon.ingest.paid`).
SCOPE = "ingest:notices"

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
# How many of the things it could not read a poller remembers having counted.
_COUNTED_ONCE = 10 * MAX_ITEMS


@dataclass(frozen=True)
class ListedNotice:
    """One notice in a feed: an item with a PDF Beacon may fetch."""

    # Names this document of this item, for the evidence store.
    ref: str
    # Where the document is: a plain HTTPS address.
    document: str
    # The page the feed gives for the item, when it is a plain HTTPS address.
    page: str | None
    # When the feed says the item was published.
    published: datetime


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
        if not all(isinstance(named, str) and named for named in document_hosts):
            raise ValueError("A host a document may be on is named by its name.")
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
        self._paid = PaidDesk(
            ember, poller="notice", items="notices", scope=SCOPE, wall_clock=wall_clock
        )
        # The items and documents this process has counted as unreadable, the
        # last `_COUNTED_ONCE` of them, oldest first.
        self._counted: dict[str, None] = {}
        # How many of them no report has said yet: a poll that ends in an error
        # makes none, and what it counted is not counted again.
        self._unreported = 0
        # How many polls this process has made, and for a document that could not
        # be had, the poll in which that was last so.
        self._polls = 0
        self._missed_in: dict[str, int] = {}

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
        self._new(unread)
        # Published by now, and not long ago. One dated later than now waits.
        recent = [notice for notice in listed if timedelta(0) <= now - notice.published <= MAX_AGE]
        taken = store.taken(SOURCE, [notice.ref for notice in recent])
        self._polls += 1
        self._missed_in = {
            ref: poll
            for ref, poll in self._missed_in.items()
            if ref not in taken and any(notice.ref == ref for notice in recent)
        }
        # The documents that have not been tried before the ones that could not be
        # had, and those the longest left alone first. Within that the newest
        # first: the all-clear before the order it lifts, and a notice of today
        # before one that has waited since yesterday.
        waiting = sorted(
            (notice for notice in recent if notice.ref not in taken),
            key=lambda notice: (
                self._missed_in.get(notice.ref, 0),
                -notice.published.timestamp(),
                notice.ref,
            ),
        )
        added = fetched = read = 0
        left = missed = False
        for notice in waiting:
            if fetched >= MAX_FETCHES_PER_POLL or read >= MAX_READS_PER_POLL:
                left = True
                break
            fetched += 1
            # Free, and done before the claim: a document that cannot be had just
            # now is not claimed, and waits for the next poll.
            try:
                pdf = self._document(notice)
            except SourceError as error:
                self._missed_in[notice.ref] = self._polls
                if self._http.closed or error.retry_after is not None or error.status == 429:
                    # Beacon is stopping, which is nothing about the document, or
                    # the host asked to be left alone, and is: the runner waits
                    # as long as it asked before the next poll.
                    raise
                missed = True
                self._new([notice.ref])
                continue
            self._missed_in.pop(notice.ref, None)
            if not store.claim(SOURCE, notice.ref, now=now):
                # Another process took it between the listing and now.
                continue
            if pdf is None:
                # Longer than a notice is. It stays claimed, and is not fetched again.
                self._new([notice.ref])
                continue
            read += 1
            # From here to the row, a shutdown waits (`drain`).
            with self._paid.working():
                reading = self._read(store, notice, pdf, hold, now)
                if reading is None:
                    self._new([notice.ref])
                    continue
                # The row and what the poller remembers are saved together.
                state = (self.name, hold.hold_state())
                added += len(store.add([self._row(notice, reading)], state=state, now=now))
        store.add([], state=(self.name, hold.hold_state()), now=now)
        # Taken only now that there is a report to say it in.
        unreadable, self._unreported = self._unreported, 0
        return PollReport(added=added, unreadable=unreadable, partial=left or cut or missed)

    def _new(self, keys: list[str]) -> int:
        """How many of these are not among what this process last counted as
        unreadable. They are now, and the next report says so."""
        new = [key for key in dict.fromkeys(keys) if key not in self._counted]
        self._counted.update(dict.fromkeys(new))
        while len(self._counted) > _COUNTED_ONCE:
            # The oldest: long enough ago that saying it again is news.
            del self._counted[next(iter(self._counted))]
        self._unreported += len(new)
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

    def _row(self, listed: ListedNotice, reading: Notice) -> NewEvidence:
        until = reading.effective_until
        return NewEvidence(
            kind="notice",
            source=SOURCE,
            ref=listed.ref,
            # The authority's own document, which the reading is of.
            url=listed.document,
            # When the feed says it was published, which nobody had to read out
            # of the document.
            occurred_at=listed.published,
            until=until if until is not None and until > listed.published else None,
            # A notice has no place in words that is known to be no finer than a
            # block, and nothing here knows where on the map its streets are.
            # Where it applies is in `data`, as Ember read it.
            #
            # A model's reading of an official document is not the authority
            # speaking for itself, which is what `official` says.
            confidence="high" if reading.notice_type != "other" else "low",
            origin=self.origin,
            model=reading.read_by,
            data=reading.as_data() | {"page": listed.page},
        )


def parse_feed(payload: Any, hosts: Collection[str]) -> tuple[list[ListedNotice], list[str], bool]:
    """The notices a JSON Feed lists among its first `MAX_ITEMS` items, a name for
    each of its items that could not be read (the same each time it is listed), and
    whether it has more items than that.

    An item is a notice when it has a PDF among its attachments. It is unreadable
    when it has no id, when it does not say when it was published, or when none of
    its PDFs is at a plain HTTPS address on one of `hosts`: a document is not
    fetched from wherever a feed points, and is named by an address that stays
    the same. Any other item is neither, and a feed is full of them.

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
        published = _moment(fields.get("date_published"))
        allowed = [
            address
            for url in documents
            if (address := _plain(url)) is not None and _host_of(address) in hosts
        ]
        if item_id is None or published is None or not allowed:
            unreadable.append(_digest(item))
            continue
        document = allowed[0]
        # Of the two as a pair, so that no id and address run together into another's.
        ref = hashlib.sha256(json.dumps([item_id, document]).encode()).hexdigest()[:32]
        notices.setdefault(
            ref,
            ListedNotice(
                ref=ref, document=document, page=_plain(fields.get("url")), published=published
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


def _plain(url: Any) -> str | None:
    """An address Beacon may ask, store, and show, without what follows a `#`, or
    None for one it may not: one that is not HTTPS on the usual port, that has
    somebody's name or password in it, or that has a query, which is where a link
    carries a signature that runs out or a token that is somebody's."""
    if _host_of(url) is None:
        return None
    parts = urlsplit(url)
    if parts.query or "?" in url.partition("#")[0]:
        return None
    return url.partition("#")[0]


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
