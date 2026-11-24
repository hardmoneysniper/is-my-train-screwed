from app.gtfs_paths import ALL_ZIP_NAMES, BUS_ZIP_NAMES, TRAM_ZIP_NAMES


def test_bus_zip_names_is_exactly_the_bus_subset_of_all_zip_names():
    # BUS_ZIP_NAMES is kept as its own literal list (order matters for
    # bus_schedule_periods.py's first-zip-with-real-data-wins walk, see
    # gtfs_paths.py's module docstring) rather than derived from
    # ALL_ZIP_NAMES -- this guards against the two silently drifting
    # apart (e.g. a new bus borough zip added to one list and not the
    # other) without constraining BUS_ZIP_NAMES's order in any way.
    non_bus = {"subway.zip", "ferry.zip", "airtrain.zip", "roosevelt_island_tram.zip"}
    assert set(BUS_ZIP_NAMES) == set(ALL_ZIP_NAMES) - non_bus


def test_tram_zip_names_is_exactly_the_tram_subset_of_all_zip_names():
    # Same drift guard as BUS_ZIP_NAMES above, for app/tram_schedule.py's
    # own scan list.
    assert set(TRAM_ZIP_NAMES) == {"airtrain.zip", "roosevelt_island_tram.zip"}
    assert set(TRAM_ZIP_NAMES) <= set(ALL_ZIP_NAMES)
