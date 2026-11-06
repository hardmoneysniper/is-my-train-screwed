"""scripts/railway_ids.py

Single source of truth for this project's Railway project/environment/
service ids, used by every local script that shells out via `railway
ssh`. Pulled out after a real bug (2026-11-06): backup_from_railway.py
independently hardcoded its own SERVICE_ID, which kept pointing at the
standalone bus-collector service after it was deleted on 2026-09-22 (see
CLAUDE.md's cost-incident section) -- every scheduled backup since then
silently failed, for 45 days, with nobody noticing because the ids were
never cross-checked against each other or against what's actually
still deployed.
"""

PROJECT_ID = "77096939-d30b-46a4-b439-c545aff3fe25"
ENVIRONMENT_ID = "95c28f31-9ee7-465f-9046-034215422795"
BACKEND_SERVICE_ID = "f66ebb7b-778d-4041-bde9-d66ce5c17223"
OTP_SERVICE_ID = "7f4331b7-88ed-4688-8c76-b4c957017e1c"
