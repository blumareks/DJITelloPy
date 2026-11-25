"""Parser for the fleet step language. Pure Python: no network, no Streamlit.

Language (one instruction per row, case-insensitive, extra whitespace allowed)::

    takeoff
    land
    up|down|left|right|forward|back <cm 20-500>
    turn left|right <degrees 1-360>
    record on|off
    sleep <seconds, > 0 and <= 600>

Blank rows and ``#`` comments (whole row or trailing) are ignored.
"""
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Union

MOVE_DIRECTIONS = ("up", "down", "left", "right", "forward", "back")
TURN_DIRECTIONS = ("left", "right")
MOVE_MIN_CM, MOVE_MAX_CM = 20, 500
TURN_MIN_DEG, TURN_MAX_DEG = 1, 360
SLEEP_MAX_S = 600

_INT = re.compile(r"[0-9]{1,9}")
_DECIMAL = re.compile(r"[0-9]{1,6}(\.[0-9]{1,3})?")

SYNTAX_HELP = ("takeoff | land | up|down|left|right|forward|back <20-500 cm> | turn left|right <1-360 deg> | "
               "record on|off | sleep <seconds>")


@dataclass(frozen=True)
class Step:
    row: int                       # 1-based row in the script table
    kind: str                      # takeoff | land | move | turn | record | sleep
    direction: Optional[str] = None
    value: Union[int, float, bool, None] = None

    @property
    def text(self) -> str:
        if self.kind == "move":
            return "{} {}".format(self.direction, self.value)
        if self.kind == "turn":
            return "turn {} {}".format(self.direction, self.value)
        if self.kind == "record":
            return "record {}".format("on" if self.value else "off")
        if self.kind == "sleep":
            return "sleep {:g}".format(self.value)
        return self.kind


@dataclass(frozen=True)
class StepError:
    row: int
    message: str


@dataclass(frozen=True)
class ParseResult:
    steps: List[Step]
    errors: List[StepError]


def _int_in_range(token: str, low: int, high: int, what: str):
    """Return (value, None) or (None, error message)."""
    if not _INT.fullmatch(token):
        return None, "{} must be a whole number, got '{}'".format(what, token)
    value = int(token)
    if not low <= value <= high:
        return None, "{} must be between {} and {}, got {}".format(what, low, high, value)
    return value, None


def parse_row(row: int, line: str) -> Union[Step, StepError, None]:
    """Parse one row. Returns None for blank/comment rows."""
    tokens = line.split("#", 1)[0].lower().split()
    if not tokens:
        return None
    command, args = tokens[0], tokens[1:]

    def error(message: str) -> StepError:
        return StepError(row, message)

    if command in ("takeoff", "land"):
        if args:
            return error("'{}' takes no arguments".format(command))
        return Step(row, command)

    if command in MOVE_DIRECTIONS:
        if len(args) != 1:
            return error("expected '{} <cm>'".format(command))
        value, problem = _int_in_range(args[0], MOVE_MIN_CM, MOVE_MAX_CM, "distance (cm)")
        return error(problem) if problem else Step(row, "move", command, value)

    if command == "turn":
        if len(args) != 2 or args[0] not in TURN_DIRECTIONS:
            return error("expected 'turn left|right <degrees>'")
        value, problem = _int_in_range(args[1], TURN_MIN_DEG, TURN_MAX_DEG, "angle (degrees)")
        return error(problem) if problem else Step(row, "turn", args[0], value)

    if command == "record":
        if len(args) != 1 or args[0] not in ("on", "off"):
            return error("expected 'record on' or 'record off'")
        return Step(row, "record", value=args[0] == "on")

    if command == "sleep":
        if len(args) != 1:
            return error("expected 'sleep <seconds>'")
        if not _DECIMAL.fullmatch(args[0]):
            return error("sleep time must be a positive number, got '{}'".format(args[0]))
        seconds = float(args[0])
        if not 0 < seconds <= SLEEP_MAX_S:
            return error("sleep time must be greater than 0 and at most {} s, got {:g}".format(
                SLEEP_MAX_S, seconds))
        return Step(row, "sleep", value=seconds)

    return error("unknown instruction '{}' (use: {})".format(command, SYNTAX_HELP))


def parse_rows(rows: Iterable[str]) -> ParseResult:
    """Parse all rows (row numbers start at 1). Every row is checked so all errors are reported."""
    steps: List[Step] = []
    errors: List[StepError] = []
    for number, line in enumerate(rows, start=1):
        parsed = parse_row(number, line or "")
        if isinstance(parsed, Step):
            steps.append(parsed)
        elif isinstance(parsed, StepError):
            errors.append(parsed)
    return ParseResult(steps, errors)


def parse_script(text: str) -> ParseResult:
    return parse_rows(text.splitlines())
