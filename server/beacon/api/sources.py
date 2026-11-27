from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


class Source(BaseModel):
    name: str
    # When the source was last asked, and whether it could be read. Both are null
    # for a source that has not been asked yet.
    polled_at: datetime | None
    ok: bool | None
    # Whether that poll read only part of what the source lists, so that items can
    # be missing from the store that the source has.
    partial: bool
    # How many evidence rows the source has in the store.
    rows: int


class Sources(BaseModel):
    # `mock` when the pollers are fed the invented scenario and no real source is
    # asked anything. Everything built on the evidence is then a stand-in's.
    mode: Literal["live", "mock"]
    # On the invented scenario: the moment its story's first minute is counted
    # from, which is before the store was begun by as many minutes as the story
    # was begun at (`BEACON_MOCK_STORY_MINUTE`, twenty when it is not set). It is
    # how somebody who shows the story works out which minute it is in. Null on
    # real sources, which have no story.
    story_began_at: datetime | None
    sources: list[Source]


@router.get("/sources")
def sources(request: Request) -> Sources:
    ingest = getattr(request.app.state, "ingest", None)
    if ingest is None:
        raise HTTPException(503, "The pollers are not running in this server.")
    rows = ingest.store.count_by_source()
    return Sources(
        mode=ingest.store.mode,
        story_began_at=None if ingest.scenario is None else ingest.scenario.start,
        sources=[
            Source(
                name=status.name,
                polled_at=status.polled_at,
                ok=status.ok,
                partial=status.partial,
                rows=rows.get(status.name, 0),
            )
            for status in ingest.runner.status()
        ],
    )
