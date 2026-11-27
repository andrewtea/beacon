"""A stand-in for Ember that "learns" a glossary, for running with no key.

It is a responder for `EmberClient.fake`. It reads the table `learn_glossary` sends
and answers with the form, filled in by a rule of thumb over the counts: a code
whose incidents are nearly all medical is a medic unit, one that is at medical and
other incidents alike is an engine, one that is never alone and comes with many
units is a chief officer, and any other is a truck; a code named in fewer than
`FEW` incidents is `unknown`. It understands nothing, it knows no fire department's
codes, and the rule is wrong for whole kinds of unit, which the comparison with
DataSF's unit types then shows. What it proposes is a stand-in's: the glossary says
`model == "fake"`.
"""

import json
from collections.abc import Mapping
from typing import Any

from beacon.ember import fake_response

# A code named in fewer incidents than this is not guessed at.
FEW = 5
_MEDICAL = "medical response"


def stand_in_learner(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Answers `learn_glossary` with roles guessed by rule from the request's table."""
    given = body.get("input")
    try:
        document = json.loads(given) if isinstance(given, str) else {}
    except ValueError:
        document = {}
    rows = document.get("codes") if isinstance(document, dict) else None
    entries = [
        {"code": row["code"], "role": _guess(row)}
        for row in rows or []
        if isinstance(row, dict) and isinstance(row.get("code"), str)
    ]
    return fake_response(text=json.dumps({"entries": entries}))


def _guess(row: Mapping[str, Any]) -> str:
    incidents = row.get("incidents")
    by_category = row.get("by_category")
    if not isinstance(incidents, int) or incidents < FEW or not isinstance(by_category, dict):
        return "unknown"
    medical = by_category.get(_MEDICAL, 0) / incidents
    if medical >= 0.9:
        return "medic"
    if medical >= 0.4:
        return "engine"
    alone = row.get("alone", 0) / incidents
    mean_units = row.get("mean_units", 0)
    if alone < 0.1 and mean_units >= 3:
        return "chief"
    return "truck"
