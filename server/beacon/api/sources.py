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
    # How many evidence rows the source has in the store.
    rows: int


class Sources(BaseModel):
    # `mock` when the pollers are fed the invented scenario and no real source is
    # asked anything. Everything built on the evidence is then a stand-in's.
    mode: Literal["live", "mock"]
    sources: list[Source]


@router.get("/sources")
def sources(request: Request) -> Sources:
    ingest = getattr(request.app.state, "ingest", None)
    if ingest is None:
        raise HTTPException(503, "The pollers are not running in this server.")
    rows = ingest.store.count_by_source()
    return Sources(
        mode=ingest.store.mode,
        sources=[
            Source(
                name=status.name,
                polled_at=status.polled_at,
                ok=status.ok,
                rows=rows.get(status.name, 0),
            )
            for status in ingest.runner.status()
        ],
    )
