def test_companies_come_from_bus_companies_list(seed_result):
    names = {c.name for c in seed_result.companies}
    assert {"Ollex", "Vilneda", "Kautra", "Nordic Alliance",
            "UAB VIP Coach Service", "Nordibuzz", "RunWay"} <= names
    # 'RunWay' appears once — the trip data's 'Runway' resolves to it by casefold.
    assert "Runway" not in names


def test_units_come_from_the_fleets_column(seed_result):
    by_name = {c.name: c for c in seed_result.companies}
    assert by_name["Kautra"].units == (1, 2, 3, 4)
    assert by_name["Ollex"].units == (1, 2)
    assert by_name["Nordic Alliance"].units == (1, 2, 3, 4)
    assert by_name["UAB VIP Coach Service"].units == (1, 2)


def test_fleets_column_is_paired_by_row_not_by_name(seed_result):
    """The regression this column exists to fix.

    'Nordibuzz' was listed in the old charter block as 'Nordi Bus', which no
    defensible name match reaches, so its two vehicles went unregistered and
    every 'Nordibuzz 1'/'Nordibuzz 2' row fell out of the operable set behind
    a warning. A count in the cell beside the name cannot miss.
    """
    by_name = {c.name: c for c in seed_result.companies}
    assert by_name["Nordibuzz"].units == (1, 2)
    assert not any("Nordi Bus" in w for w in seed_result.warnings)
    # Likewise Vilneda, whose charter line read 'Vilneda - no charter' while
    # the trip sheet assigned 'Vilneda 1'.
    assert by_name["Vilneda"].units == (1,)


def test_blank_fleets_cell_means_no_units(seed_result):
    by_name = {c.name: c for c in seed_result.companies}
    assert by_name["RunWay"].units == ()
    assert by_name["RunWay"].fleet_size == 0


def test_fleet_size_is_the_unit_count(seed_result):
    for c in seed_result.companies:
        assert c.fleet_size == len(c.units)


def test_registry_resolution(seeded_registry):
    assert seeded_registry.resolve_company("Runway") == ("runway", None)
    assert seeded_registry.resolve_company("Kautra 3") == ("kautra", 3)
    assert seeded_registry.resolve_company("Kautra 9") is None
    assert seeded_registry.resolve_city("CPH trf") == "CPH"
    assert seeded_registry.resolve_city("ferry Turku-STO") is None


# -- pick-list columns on the Information sheet --------------------------------

def _grid(header_row, *rows):
    return [list(header_row), *(list(r) for r in rows)]


def test_option_lists_are_absent_when_the_sheet_does_not_head_them(seed_result):
    """The reference workbook predates the columns, so it declares nothing —
    which must not be read as 'both lists are empty'."""
    assert seed_result.options == {}


def test_option_lists_read_off_their_own_column():
    from buschain.codecs.seed import sheet_option_lists
    grid = _grid(
        ["Company", None, "Nationality", "OP"],
        ["Ollex", None, " Swedish ", "Andrea"],
        [None, None, "Norwegian", None],
        [None, None, None, "Bryant"],
    )
    lists = sheet_option_lists(grid)
    assert lists["Nationality"].column == "C"
    assert lists["Nationality"].values == ("Swedish", "Norwegian")   # trimmed
    assert lists["Nationality"].last_row == 3
    assert lists["OP"].values == ("Andrea", "Bryant")
    assert lists["OP"].last_row == 4                                 # gaps skipped


def test_a_headed_but_empty_column_is_an_empty_list_not_a_missing_one():
    """The distinction the sync turns on: this is the user clearing the list
    in Excel, which must reach the stored one."""
    from buschain.codecs.seed import sheet_option_lists
    lists = sheet_option_lists(_grid(["Nationality", "OP"], [None, "Andrea"]))
    assert lists["Nationality"].values == ()
    assert "Nationality" in lists


def test_lists_follow_their_header_rather_than_a_fixed_letter():
    """A column inserted to the left moves them; reading the old letter would
    pick up someone else's data and then write the list back over it."""
    from buschain.codecs.seed import sheet_option_lists
    lists = sheet_option_lists(_grid(
        ["Inserted", "Nationality", "OP"], ["x", "Swedish", "Andrea"]))
    assert lists["Nationality"].column == "B" and lists["OP"].column == "C"
    assert lists["Nationality"].values == ("Swedish",)


def test_repeated_values_collapse():
    from buschain.codecs.seed import sheet_option_lists
    lists = sheet_option_lists(_grid(
        ["Nationality"], ["Swedish"], ["Swedish"], ["Norwegian"]))
    assert lists["Nationality"].values == ("Swedish", "Norwegian")


# -- the Cities column ------------------------------------------------------

def test_cities_fall_back_to_the_defaults_when_the_sheet_heads_no_column(seed_result):
    """The reference workbook predates the column, so DEFAULT_CITIES still
    applies and nothing about its import changes."""
    from buschain.codecs.seed import DEFAULT_CITIES
    assert seed_result.city_list is None
    assert seed_result.cities == list(DEFAULT_CITIES)


def test_cities_come_from_the_sheet_when_it_heads_the_column():
    """A workbook that states its own list owns it — the defaults are only a
    fallback for workbooks that predate the column."""
    from buschain.codecs.seed import DEFAULT_CITIES, SeedResult, sheet_city_list
    grid = _grid(["Cities"], ["OSL"], ["Turku"], ["OSL"], [None], ["TEST"])
    lst = sheet_city_list(grid)
    assert lst is not None
    assert lst.column == "A"
    assert lst.values == ("OSL", "Turku", "TEST")     # deduped, order kept
    assert "Turku" not in DEFAULT_CITIES              # i.e. genuinely from the sheet
    result = SeedResult(companies=[], cities=list(DEFAULT_CITIES), city_list=lst)
    assert result.to_registry().cities["turku"] == "Turku"


def test_city_column_follows_its_header_not_a_fixed_letter():
    """Same rule as the pick lists: reading the fixed letter after someone
    inserts a column would pick up other data and write the list back over it."""
    from buschain.codecs.seed import sheet_city_list
    lst = sheet_city_list(_grid(["Inserted", "Cities"], ["x", "OSL"]))
    assert lst is not None and lst.column == "B" and lst.values == ("OSL",)


def test_headed_but_empty_city_column_is_not_the_same_as_absent():
    """An emptied column must be able to clear the list; a sheet that says
    nothing must not. The two cannot be conflated — see sync_cities."""
    from buschain.codecs.seed import sheet_city_list
    empty = sheet_city_list(_grid(["Cities"], [None]))
    assert empty is not None and empty.values == ()
    assert sheet_city_list(_grid(["Something else"], ["x"])) is None


def _info_workbook(rows):
    """A minimal workbook carrying just an Information sheet, for read_seed.

    Built here rather than committing a binary fixture: the cases below are
    about one or two columns, and a real workbook would hide which cell the
    test actually depends on.
    """
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Information"
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_empty_fleets_column_warns_rather_than_registering_nothing_in_silence():
    """A fresh season's workbook arrives with the column headed and blank.

    Silent until this warning: a blank cell is a legitimate 0 for one company,
    so no per-row warning fires, and the 'no Fleets column' warning needs the
    header to be missing rather than the values. The symptom was every company
    present, nothing chainable, and nothing on screen saying why.
    """
    from buschain.codecs.seed import read_seed
    result = read_seed(_info_workbook([
        ["Bus Companies", "Fleets"], ["Ollex", None], ["Kautra", None],
    ]))
    assert result.companies and not any(c.units for c in result.companies)
    assert any("'Fleets' column is empty" in w for w in result.warnings)


def test_a_populated_fleets_column_does_not_warn():
    """The other side of the boundary — one real count is enough to mean the
    column was filled in, so the warning must not fire on a partial list."""
    from buschain.codecs.seed import read_seed
    result = read_seed(_info_workbook([
        ["Bus Companies", "Fleets"], ["Ollex", 2], ["Kautra", None],
    ]))
    assert not any("column is empty" in w for w in result.warnings)
