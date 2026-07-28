"""The clipboard codec: what a paste out of Excel decodes to, and what it
deliberately refuses to decode."""
import pytest

from buschain.codecs import paste
from buschain.codecs.types import (
    ARRIVAL, COLUMN_ORDER, DEPARTURE, END_DATE, PROJECT, START_DATE, STATUS,
)

P_NOTES = "D"
C_NOTES = "O"

ROW = "Confirmed\t22198\tKautra\t-Vilneda\t1-Apr-26\t6-Apr-26\tSTO\tCPH"

# The reference sheet's row 2, whole, as Excel renders it onto the clipboard.
FULL_ROW = ("Confirmed\t22198\tKautra\t\t1-Apr-26\t6-Apr-26\tSTO\tSTO\t"
            "Chinese\tMia\t650EUR\t49\tN/A\tOllex\t（580*6=3480EUR）")
HEADER = "\t".join(COLUMN_ORDER)


def _rows(text):
    return paste.parse(text).rows


def test_row_decodes_to_column_letters():
    (row,) = _rows(ROW)
    assert row[STATUS] == "Confirmed"
    assert row[PROJECT] == "22198"
    assert row[P_NOTES] == "-Vilneda"
    assert row[DEPARTURE] == "STO"
    assert row[ARRIVAL] == "CPH"
    # Short rows pad rather than fail: this paste stopped at 'Arrival'.
    assert row[C_NOTES] == ""
    assert set(row) == {chr(ord("A") + i) for i in range(len(COLUMN_ORDER))}


def test_sheet_rendered_dates_become_iso():
    """The sheet formats its date columns 'd-mmm-yy', so this is what a paste
    actually carries — and the contract only reads ISO."""
    (row,) = _rows(ROW)
    assert row[START_DATE] == "2026-04-01"
    assert row[END_DATE] == "2026-04-06"


@pytest.mark.parametrize("text,iso", [
    ("2026-04-01", "2026-04-01"),
    ("1-Apr-26", "2026-04-01"),
    ("01-Apr-2026", "2026-04-01"),
    ("1 Apr 2026", "2026-04-01"),
])
def test_unambiguous_renderings(text, iso):
    (row,) = _rows(f"Confirmed\t\t\t\t{text}\t{text}\tSTO\tCPH")
    assert row[START_DATE] == iso


@pytest.mark.parametrize("text", ["04/05/2026", "4.5.2026", "next Tuesday", ""])
def test_ambiguous_or_unreadable_dates_pass_through_verbatim(text):
    """Never guessed at here. '04/05/2026' is two different days depending on
    who exported the sheet, so it goes to the contract, which fails the row and
    quotes the value back — a wrong date that validates is the worse outcome."""
    (row,) = _rows(f"Confirmed\t\t\t\t{text}\t{text}\tSTO\tCPH")
    assert row[START_DATE] == text


def test_quoted_cell_holding_a_newline_stays_one_row():
    """Excel quotes a multi-line cell, and notes are exactly where those turn
    up. Splitting on tabs would tear this into two broken rows."""
    text = 'Confirmed\t22198\t\t"first line\nsecond line"\t1-Apr-26\t6-Apr-26\tSTO\tCPH'
    (row,) = _rows(text)
    assert row[P_NOTES] == "first line\nsecond line"
    assert row[START_DATE] == "2026-04-01"


def test_full_header_maps_one_to_one_and_is_not_a_trip():
    parsed = paste.parse(f"{HEADER}\n{FULL_ROW}")
    assert parsed.by_header
    assert parsed.columns == COLUMN_ORDER
    (row,) = parsed.rows
    assert row[STATUS] == "Confirmed"
    assert row["I"] == "Chinese"          # Nationality
    assert row[C_NOTES] == "（580*6=3480EUR）"


def test_hidden_columns_are_mapped_by_title_not_position():
    """The bug this exists for. Excel leaves a hidden column out of the text it
    copies, so a sheet hiding Nationality/OP/Quoted rate puts 12 fields on the
    clipboard where the layout has 15 — and read by position, every value from
    'Nationality' on lands three columns early, silently and plausibly."""
    hidden = {"Nationality", "OP", "Quoted rate"}
    keep = [i for i, t in enumerate(COLUMN_ORDER) if t not in hidden]
    header = "\t".join(COLUMN_ORDER[i] for i in keep)
    fields = FULL_ROW.split("\t")
    body = "\t".join(fields[i] for i in keep)

    parsed = paste.parse(f"{header}\n{body}")
    (row,) = parsed.rows
    assert row["L"] == "49"                    # Pax, not shifted into Nationality
    assert row["M"] == "N/A"                   # ER
    assert row["N"] == "Ollex"                 # Company name
    assert row[C_NOTES] == "（580*6=3480EUR）"
    # The columns the paste could not carry are empty, and are reported as
    # absent rather than quietly filled with the next value along.
    assert (row["I"], row["J"], row["K"]) == ("", "", "")
    assert hidden.isdisjoint(parsed.columns)


def test_header_maps_reordered_columns():
    parsed = paste.parse("Arrival\tDeparture\tStatus\tStart date\tEnd date\n"
                         "CPH\tOSL\tPending\t1-Apr-26\t2-Apr-26")
    (row,) = parsed.rows
    assert (row[DEPARTURE], row[ARRIVAL]) == ("OSL", "CPH")
    assert row[START_DATE] == "2026-04-01"
    assert parsed.columns == ("Arrival", "Departure", "Status",
                              "Start date", "End date")


def test_header_columns_that_name_nothing_are_dropped():
    """A copy that ran into the sheet's legacy date grid brings its date
    headers along; they name no contract column, so they go nowhere."""
    parsed = paste.parse(f"{HEADER}\t2026-04-01\t2026-04-02\n{FULL_ROW}\tSTO\tOSL")
    (row,) = parsed.rows
    assert parsed.columns == COLUMN_ORDER
    assert row[C_NOTES] == "（580*6=3480EUR）"


def test_declared_columns_map_a_hidden_column_paste_without_a_header():
    """The planner's workflow: data rows only, no header — the picker above
    the paste box declares what the sheet hides. Same shifted-paste scenario
    as the header test, resolved by declaration instead."""
    hidden = {"Nationality", "OP", "Quoted rate"}
    keep = [t for t in COLUMN_ORDER if t not in hidden]
    fields = FULL_ROW.split("\t")
    body = "\t".join(f for f, t in zip(fields, COLUMN_ORDER) if t not in hidden)

    parsed = paste.parse(body, columns=keep)
    (row,) = parsed.rows
    assert row["L"] == "49"                    # Pax, not shifted into Nationality
    assert row["N"] == "Ollex"                 # Company name
    assert (row["I"], row["J"], row["K"]) == ("", "", "")
    assert parsed.columns == tuple(keep)
    assert not parsed.by_header
    assert parsed.overflow_rows == 0


def test_declared_subset_counts_rows_that_overflow_it():
    """A subset declared but full-width rows pasted means the sheet was not
    hiding what the picker says — values are falling off the right edge, and
    silence here would be the same bug in the other direction."""
    parsed = paste.parse(FULL_ROW, columns=list(COLUMN_ORDER[:6]))
    assert parsed.overflow_rows == 1


def test_full_layout_overflow_is_not_counted():
    """Against the full layout, extra fields are the legacy date grid —
    expected, not a mis-declaration."""
    full = ROW + "\t" * 7
    parsed = paste.parse(full + "\t46113\t46118", columns=list(COLUMN_ORDER))
    assert parsed.overflow_rows == 0


def test_unknown_or_duplicate_declared_column_is_refused():
    with pytest.raises(paste.PasteError):
        paste.parse(ROW, columns=["Status", "Bogus"])
    with pytest.raises(paste.PasteError):
        paste.parse(ROW, columns=["Status", "Status"])
    with pytest.raises(paste.PasteError):
        paste.parse(ROW, columns=[])


def test_a_pasted_header_overrides_the_declared_columns():
    """The header is per-paste evidence; the picker is a standing setting that
    can be stale. Evidence wins."""
    parsed = paste.parse(f"{HEADER}\n{FULL_ROW}", columns=list(COLUMN_ORDER[:4]))
    assert parsed.by_header
    assert parsed.columns == COLUMN_ORDER
    assert parsed.rows[0]["N"] == "Ollex"


def test_a_data_row_is_never_read_as_a_header():
    """Only line 1 can be a header, and only if it names contract columns. A
    trip is not one, and a 'Status' further down is data someone typed."""
    assert len(_rows(f"{ROW}\n{HEADER}")) == 2
    assert not paste.parse(f"{ROW}\n{ROW}").by_header


def test_blank_lines_are_ignored():
    assert len(_rows(f"{ROW}\n\n{ROW}\n")) == 2


def test_columns_past_c_notes_are_ignored():
    """A paste that ran on into the sheet's legacy date grid is still a paste
    of A-O, the same tolerance the csv codec gives a source row."""
    full = ROW + "\t" * 7                  # A-O, with I-O empty
    (row,) = _rows(full + "\t46113\t46118")
    assert row[C_NOTES] == ""


def test_empty_paste_is_refused():
    with pytest.raises(paste.PasteError):
        paste.parse("   \n\n")


def test_text_the_reader_cannot_parse_is_a_paste_error():
    """Arbitrary pasted input, so a reader failure — here a quoted cell past
    csv's field limit — has to read as 'bad paste' and not as a 500 from the
    endpoint behind it."""
    with pytest.raises(paste.PasteError):
        paste.parse('Confirmed\t"' + "x" * 200_000 + '"')
