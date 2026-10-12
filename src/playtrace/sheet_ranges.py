"""Bounded A1 coordinates and lossless literal-cell comparison for Sheets."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SheetRange:
    sheet: str | None
    row: int
    column: int
    end_row: int
    end_column: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.end_row - self.row + 1, self.end_column - self.column + 1


def parse_sheet_range(value: str, *, require_sheet: bool = False) -> SheetRange:
    match = re.fullmatch(
        r"(?:(?P<sheet>'(?:[^']|'')+'|[^'!]+)!)?"
        r"\$?(?P<col>[A-Za-z]{1,3})\$?(?P<row>[1-9][0-9]*)"
        r"(?::\$?(?P<endcol>[A-Za-z]{1,3})\$?(?P<endrow>[1-9][0-9]*))?",
        value,
    )
    if not match:
        raise ValueError("Sheets requires a bounded A1 cell range")
    sheet = match["sheet"]
    if require_sheet and not sheet:
        raise ValueError("live sheet patches require an explicit tab name")
    if sheet and sheet.startswith("'"):
        sheet = sheet[1:-1].replace("''", "'")

    def column(label: str) -> int:
        result = 0
        for letter in label.upper():
            result = result * 26 + ord(letter) - ord("A") + 1
        return result

    result = SheetRange(
        sheet, int(match["row"]), column(match["col"]),
        int(match["endrow"] or match["row"]), column(match["endcol"] or match["col"]),
    )
    rows, columns = result.shape
    if rows < 1 or columns < 1 or rows * columns > 10_000:
        raise ValueError("sheet ranges must contain between 1 and 10,000 cells")
    return result


def literal_matrix(values: Any, target: SheetRange) -> tuple[tuple[Any, ...], ...]:
    """Pad omitted trailing blanks, rejecting skips, formulas and lossy values."""
    rows, columns = target.shape
    if not isinstance(values, (list, tuple)) or len(values) > rows:
        raise ValueError("sheet values exceed the declared range")
    result = []
    for index in range(rows):
        row = values[index] if index < len(values) else ()
        if not isinstance(row, (list, tuple)) or len(row) > columns:
            raise ValueError("sheet values exceed the declared range")
        for value in row:
            if type(value) not in (str, int, float, bool):
                raise ValueError("sheet patches require literal strings, numbers or booleans; use an empty string to clear")
            if isinstance(value, str) and value.startswith("="):
                raise ValueError("formula cells and formula-like strings are not supported by literal sheet patches")
            if type(value) in (int, float) and (
                abs(value) > 2**53 - 1 or not math.isfinite(value)
            ):
                raise ValueError("sheet numbers must be finite and within the supported numeric range")
        result.append(tuple(row) + ("",) * (columns - len(row)))
    return tuple(result)


def same_cells(left: Any, right: Any) -> bool:
    # JSON distinguishes booleans from numbers; Python's True == 1 must not
    # make a changed cell look unchanged. Int/float numeric equality is valid.
    return len(left) == len(right) and all(
        len(a) == len(b) and all(
            x == y and isinstance(x, bool) == isinstance(y, bool)
            for x, y in zip(a, b)
        ) for a, b in zip(left, right)
    )
