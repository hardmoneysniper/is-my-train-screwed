"""backend/app/day_type.py

Shared day_type classification (weekday vs. weekend/holiday), used by both
`backend/scripts/ingest_subwaydata.py` (Task 3, subway) and
`backend/scripts/derive_bus_arrival_events.py` (Task 4, bus) -- factored out
here per task-4-brief.md step 6 so the hardcoded holiday table isn't
independently duplicated (and able to silently drift) in two places.
"""
from datetime import date

# Major US federal holidays, 2025-2026 (this project's active window).
# Plain hardcoded set, not a recurring-holiday calculation library --
# per task-3-brief.md step 3i.
HOLIDAYS = {
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents Day
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 10, 13), # Columbus Day
    date(2025, 11, 11), # Veterans Day
    date(2025, 11, 27), # Thanksgiving Day
    date(2025, 12, 25), # Christmas Day
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 4),   # Independence Day
    date(2026, 9, 7),   # Labor Day
    date(2026, 10, 12), # Columbus Day
    date(2026, 11, 11), # Veterans Day
    date(2026, 11, 26), # Thanksgiving Day
    date(2026, 12, 25), # Christmas Day
}


def day_type_for(service_date: date) -> str:
    if service_date.weekday() >= 5 or service_date in HOLIDAYS:
        return "weekend"
    return "weekday"
