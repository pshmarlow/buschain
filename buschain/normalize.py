"""The four allowed formatting normalizations — and nothing else.

The brief permits exactly: trim, case-fold, whitespace collapse, and
'X trf' / 'X(trf)' / 'X TRF' -> X. Content interpretation (deriving
constraints from notes, repairing dates, parsing free-text arrival strings)
is forbidden; a value that doesn't resolve after these transforms is a
non-operable reason, never a repair. Keep this module closed: adding a
"helpful" transform here silently widens the data contract.
"""
from __future__ import annotations

import re

# Optional space or parenthesised 'trf' suffix, any case: 'CPH trf',
# 'BGO TRF', 'OSL(trf)', 'OSL (trf)'. Anchored at the end so free text like
# 'Trondheim trf at 09:00' does NOT match (that row stays non-operable), and
# the bare form requires preceding whitespace so a code that merely ENDS in
# these letters ('TRF' itself, 'MTRF') is not eaten — this is a suffix
# transform, never allowed to consume the whole value.
_TRF_SUFFIX = re.compile(r"(?:\s*\(\s*trf\s*\)|\s+trf)\s*$", re.IGNORECASE)


def trim(value: str) -> str:
    return value.strip()


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value)


def casefold_key(value: str) -> str:
    return collapse_ws(trim(value)).casefold()


def strip_trf(value: str) -> str:
    return _TRF_SUFFIX.sub("", value)
