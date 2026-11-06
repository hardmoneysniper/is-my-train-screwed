from app.gtfs_paths import ALL_ZIP_NAMES, BUS_ZIP_NAMES


def test_bus_zip_names_is_exactly_the_bus_subset_of_all_zip_names():
    # BUS_ZIP_NAMES is kept as its own literal list (order matters for
    # bus_schedule_periods.py's first-zip-with-real-data-wins walk, see
    # gtfs_paths.py's module docstring) rather than derived from
    # ALL_ZIP_NAMES -- this guards against the two silently drifting
    # apart (e.g. a new bus borough zip added to one list and not the
    # other) without constraining BUS_ZIP_NAMES's order in any way.
    non_bus = {"subway.zip", "ferry.zip"}
    assert set(BUS_ZIP_NAMES) == set(ALL_ZIP_NAMES) - non_bus
