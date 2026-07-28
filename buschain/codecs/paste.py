"""Clipboard TSV (columns A-O, copied out of Excel) -> manual trip rows.

A codec rather than form handling, and deliberately so: what the clipboard
carries is a *rendering* of the sheet, so turning '1-Apr-26' back into
2026-04-01 is decoding — the same job `xlsx_reader` does when it hands the
contract a real date instead of day serial 46113. It is not the date repair
`normalize` forbids: a rendering that means more than one day is passed through
verbatim, so the contract fails the row and quotes the value back, rather than
being guessed at here.

The other thing the clipboard renders away is *which* columns you copied.
Excel leaves hidden columns out of the text it writes, so a sheet with
Nationality/OP/Quoted rate hidden puts 12 fields on the clipboard where the
layout has 15 — and read by position, every value from 'Nationality' on lands
three columns early, silently. So a paste that carries its header row is mapped
by title instead, and position is only the fallback for a copy that has no
header to go on.

Output is raw values keyed by column letter — the same shape `Repo` stores for
a hand-entered trip. Nothing is validated here; every pasted row goes through
`contract.validate_row` exactly like an imported one, so a bad city code lands
in the non-operable list with its reason instead of being refused at the door.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from .. import normalize
from .types import ALL_COLUMNS, COLUMN_LETTERS, COLUMN_TITLES, END_DATE, START_DATE

_DATE_COLUMNS = (START_DATE, END_DATE)

# Renderings of a date that can only mean one day. The reference sheet formats
# its date columns as 'd-mmm-yy', so '1-Apr-26' is what a paste actually
# carries; ISO is here for text people type themselves.
#
# Slash forms are absent on purpose: '04/05/2026' is 4 May to the planner who
# copied it and 5 April to Excel in another locale, and there is nothing in the
# clipboard to say which. A wrong date that validates is worse than one that
# fails loudly, so those fall through to the contract.
_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%d %b %y", "%d %b %Y")

# How many cells of line 1 must name a contract column before it is read as a
# header rather than as data. Three, because two is reachable by accident (a
# trip whose Departure cell literally reads 'Arrival' is nonsense, but a header
# fragment from a partial copy is not) and requiring the full set would refuse
# exactly the hidden-column pastes this exists for.
_HEADER_MATCHES = 3

_TITLE_BY_KEY = {normalize.casefold_key(t): c for t, c in COLUMN_LETTERS.items()}


class PasteError(ValueError):
    """The pasted text holds no rows — there is nothing to add."""


@dataclass(frozen=True)
class ParsedPaste:
    rows: tuple[dict[str, str], ...]        # raw values keyed by column letter
    # Which contract columns the paste actually carried, in the order it
    # carried them. Reported back rather than kept private: 'these three
    # columns were not in what you pasted' is the only warning a planner gets
    # that a hidden column left a value behind.
    columns: tuple[str, ...]                # column titles
    by_header: bool
    # Rows carrying more fields than a declared subset has columns. Only
    # meaningful when the caller narrowed the layout: against the full layout,
    # overflow is the legacy date grid and expected; against a subset it means
    # the sheet was NOT hiding what the picker says it hides, and values are
    # being dropped off the right-hand edge.
    overflow_rows: int = 0


def parse(text: str, columns: Sequence[str] | None = None) -> ParsedPaste:
    """`columns`: the contract-column titles the paste carries, in paste order.

    This exists because the clipboard cannot say what Excel skipped: a copy
    from a sheet hiding three columns is three fields short per row, and read
    against the full layout every value after the first gap lands early —
    plausibly enough to validate when the hidden columns sit after 'Arrival'.
    Only the planner knows what their sheet hides, so it is declared, never
    guessed. None means the full layout.
    """
    explicit = _explicit_mapping(columns)
    reader = _reader(text)
    first = _next_row(reader)
    if first is None:
        raise PasteError(
            "Nothing to add — paste rows copied from columns A-O of the sheet.")

    # A pasted header still wins over the declared subset: it is per-paste
    # evidence of what this copy holds, where the picker is a standing setting
    # that can be stale. It also makes the header line skippable at all.
    header = _header_mapping(first)
    rows: list[dict[str, str]] = []
    overflow = 0
    if header is None:
        # No header to go on: fields are the declared columns in order (the
        # full layout when nothing was declared), short rows padding and
        # anything past the last column ignored — the same tolerance the csv
        # codec gives a source row. Line 1 is data in this branch.
        mapping: list[str | None] = explicit if explicit is not None else list(ALL_COLUMNS)
        covered = mapping[:len(first)]
        first_values = _row_values(first, mapping)
        if first_values is not None:
            rows.append(first_values)
            overflow += _overflows(first, mapping, explicit)
    else:
        mapping = header
        covered = [c for c in mapping if c is not None]

    while (fields := _next_row(reader)) is not None:
        values = _row_values(fields, mapping)
        if values is not None:
            rows.append(values)
            overflow += _overflows(fields, mapping, explicit if header is None else None)

    if not rows:
        raise PasteError("The paste has a header row but no trips under it."
                         if header is not None else
                         "Nothing to add — paste rows copied from columns A-O "
                         "of the sheet.")
    return ParsedPaste(
        rows=tuple(rows),
        columns=tuple(COLUMN_TITLES[c] for c in covered if c is not None),
        by_header=header is not None,
        overflow_rows=overflow,
    )


def _explicit_mapping(columns: Sequence[str] | None) -> list[str | None] | None:
    """Declared titles -> column letters, validated loudly.

    A bad title here is a client bug, not bad user data — but it still must
    not become a 500, and must never fall back to the full layout: silently
    ignoring the declaration would resurrect exactly the shifted-columns bug
    the declaration exists to prevent.
    """
    if columns is None:
        return None
    letters: list[str | None] = []
    for title in columns:
        letter = COLUMN_LETTERS.get(title)
        if letter is None:
            raise PasteError(f"Unknown column {title!r} in the paste layout")
        if letter in letters:
            raise PasteError(f"Column {title!r} appears twice in the paste layout")
        letters.append(letter)
    if not letters:
        raise PasteError("The paste layout selects no columns")
    return letters


def _overflows(fields: list[str], mapping: list[str | None],
               explicit: list[str | None] | None) -> int:
    if explicit is None or len(mapping) >= len(ALL_COLUMNS):
        return 0
    return 1 if any(f.strip() for f in fields[len(mapping):]) else 0


def _reader(text: str):
    # csv.reader, not text.split('\t'): Excel quotes a cell containing tabs or
    # newlines, and 'P Notes' is exactly where a multi-line cell shows up. A
    # naive split would tear one such note into several broken rows.
    return csv.reader(io.StringIO(text), delimiter="\t")


def _next_row(reader) -> list[str] | None:
    """The next line holding anything, or None at the end."""
    while True:
        # The reader itself can reject the text (an unterminated quote, a cell
        # past csv's field limit). This is arbitrary pasted input, so that has
        # to read as 'bad paste' and not as a 500 from the endpoint behind it.
        try:
            fields = next(reader)
        except StopIteration:
            return None
        except csv.Error as e:
            raise PasteError(f"Could not read the pasted text: {e}") from e
        if any(f.strip() for f in fields):
            return fields


def _header_mapping(fields: list[str]) -> list[str | None] | None:
    """Field index -> column letter, from a pasted header row.

    None when the line is not a header (so it is data, read by position).
    A field naming no contract column maps to None and is dropped: that is the
    sheet's legacy date grid, dragged along by a copy that ran past column O.
    """
    mapping = [_TITLE_BY_KEY.get(normalize.casefold_key(f)) for f in fields]
    if sum(c is not None for c in mapping) < _HEADER_MATCHES:
        return None
    # A title appearing twice takes its first column; a second one would
    # otherwise quietly overwrite the first row's value with a later cell.
    seen: set[str] = set()
    for i, col in enumerate(mapping):
        if col is None:
            continue
        if col in seen:
            mapping[i] = None
        seen.add(col)
    return mapping


def _row_values(fields: list[str], mapping: list[str | None]) -> dict[str, str] | None:
    values = {c: "" for c in ALL_COLUMNS}
    for i, col in enumerate(mapping):
        if col is not None and i < len(fields):
            values[col] = fields[i].strip()
    if not any(values.values()):
        return None
    for col in _DATE_COLUMNS:
        values[col] = _as_iso(values[col])
    return values


def _as_iso(text: str) -> str:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text        # unreadable or ambiguous: let the contract report it
