"""`python -m beacon.moments`: when each thing in the invented story happens."""

from datetime import UTC, datetime, timedelta

import pytest

from beacon import moments
from beacon.scenario import notices as scenario_notices
from beacon.scenario import radio as scenario_radio

BEGAN = datetime(2026, 12, 1, 18, 10, tzinfo=UTC)


def test_every_call_and_every_notice_of_the_story_is_a_moment_in_the_storys_order() -> None:
    told = moments.moments()

    assert [minute for minute, _ in told] == sorted(minute for minute, _ in told)
    said = [what for _, what in told]
    for call in scenario_radio.CALLS:
        assert (call.minute, f"Radio: {call.said}") in told or call.said is None
    assert said.count("Radio: nothing is said in this call.") == sum(
        call.said is None for call in scenario_radio.CALLS
    )
    for notice in scenario_notices.NOTICES:
        assert (notice.minute, f"Notice published as a PDF: {notice.title}.") in told
    assert told[-1][0] == 95.0


def test_each_moment_is_given_the_time_it_comes_at_and_whether_it_is_past() -> None:
    told = moments.lines(BEGAN, BEGAN + timedelta(minutes=19, seconds=30))

    assert told[0] == "The invented story is in minute 19 of 95."
    order = next(line for line in told if "Evacuation Order: 400 block of Hayes St" in line)
    assert order.startswith("past  min 19.0  18:29:00 UTC  Notice published as a PDF")
    widened = next(line for line in told if "widened" in line)
    assert widened.startswith("      min 20.0  18:30:00 UTC  511")
    assert moments.lines(BEGAN, BEGAN + timedelta(minutes=96))[0] == "The invented story is over."
    before = moments.lines(BEGAN, BEGAN - timedelta(seconds=1))
    assert before[0] == "The invented story has not begun."
    assert not any(line.startswith("past") for line in before)


def test_the_command_wants_the_moment_the_story_began_with_its_offset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for refused in ([], ["yesterday"], ["2026-12-01T18:10:00"], ["a", "b"]):
        assert moments.main(refused) == 2
        assert "Give the moment the story began" in capsys.readouterr().err
    assert moments.main(["2026-12-01T18:10:00+00:00"]) == 0
    assert "Third alarm." in capsys.readouterr().out
