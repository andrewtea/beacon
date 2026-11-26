"""The evidence store: what every source said, with its time, place, source, and confidence.

    from beacon.evidence import EvidenceStore, Point

    store = EvidenceStore.from_env(os.environ)
    rows = store.search(kinds=["radio", "alert"], start=since, near=Point(37.7763, -122.4260),
                        radius_m=500, include_unlocated=True)

See `store` for what a row holds and why there is a mock store and a live one.
"""

from beacon.evidence.store import (
    LIVE_SOURCES_VARIABLE,
    Confidence,
    Evidence,
    EvidenceStore,
    EvidenceStoreError,
    Kind,
    Mode,
    ModelSource,
    NewEvidence,
    Origin,
    Point,
)

__all__ = [
    "LIVE_SOURCES_VARIABLE",
    "Confidence",
    "Evidence",
    "EvidenceStore",
    "EvidenceStoreError",
    "Kind",
    "Mode",
    "ModelSource",
    "NewEvidence",
    "Origin",
    "Point",
]
