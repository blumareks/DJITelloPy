import pytest

from fleet_ui.steps import Step, parse_row, parse_rows, parse_script


def ok(text):
    result = parse_script(text)
    assert result.errors == []
    return result.steps


def errors(text):
    result = parse_script(text)
    assert result.errors, "expected errors"
    return result.errors


def test_every_valid_instruction():
    steps = ok("takeoff\nland\nup 20\ndown 500\nleft 50\nright 100\n"
               "turn left 1\nturn right 360\nrecord on\nrecord off\nsleep 2.5\nforward 20\nback 500")
    assert [s.kind for s in steps] == ["takeoff", "land", "move", "move", "move", "move",
                                       "turn", "turn", "record", "record", "sleep", "move", "move"]
    assert [(s.direction, s.value) for s in steps[-2:]] == [("forward", 20), ("back", 500)]
    assert steps[2] == Step(1 + 2, "move", "up", 20)
    assert steps[7] == Step(8, "turn", "right", 360)
    assert steps[8].value is True and steps[9].value is False
    assert steps[10].value == 2.5


def test_step_text():
    assert [s.text for s in ok("takeoff\nup 30\nturn left 90\nrecord on\nsleep 2")] == [
        "takeoff", "up 30", "turn left 90", "record on", "sleep 2"]


def test_case_whitespace_comments_and_blank_rows():
    steps = ok("  TakeOff  \n\n   \n# comment\n  UP    50   # trailing\nTurn  LEFT   45")
    assert [(s.row, s.text) for s in steps] == [(1, "takeoff"), (5, "up 50"), (6, "turn left 45")]


def test_empty_script():
    result = parse_script("")
    assert result.steps == [] and result.errors == []
    assert parse_rows([None, "", "# x"]).steps == []


def test_row_numbers_count_blank_rows_and_all_errors_are_reported():
    result = parse_script("takeoff\n\nfly 10\nup 5\nland now")
    assert [(e.row, e.message.split()[0]) for e in result.errors] == [(3, "unknown"), (4, "distance"),
                                                                       (5, "'land'")]
    assert [s.row for s in result.steps] == [1]


@pytest.mark.parametrize("line", [
    "takeoff now", "land 1", "up", "up 19", "up 501", "up 0", "up -20", "up 20.5", "up abc", "up 10 20",
    "down 10", "left 1000", "right",
    "turn", "turn up 10", "turn left", "turn left 0", "turn right 361", "turn left x", "turn left 5 6",
    "record", "record maybe", "record on off",
    "sleep", "sleep 0", "sleep -1", "sleep 601", "sleep abc", "sleep 1 2", "sleep 1e3", "sleep 1.2345",
    "hover", "emergency",
])
def test_invalid_instructions(line):
    assert isinstance(parse_row(7, line), type(errors(line)[0]))
    assert errors(line)[0].row == 1


def test_forward_and_back():
    assert [s.text for s in ok("FORWARD 100\n  Back   30  # trailing")] == ["forward 100", "back 30"]
    for line in ("forward", "back", "forward 19", "back 501", "forward x", "back 10 20", "back -30",
                 "backward 50", "forwards 50"):
        assert errors(line)[0].row == 1


def test_sleep_limits():
    assert ok("sleep 0.001")[0].value == 0.001
    assert ok("sleep 600")[0].value == 600


def test_unicode_digits_are_rejected():
    errors("up \u0665\u0660")
    errors("sleep \u0661")
