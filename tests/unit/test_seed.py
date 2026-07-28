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
