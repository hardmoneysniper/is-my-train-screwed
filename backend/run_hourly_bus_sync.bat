@echo off
cd /d "F:\Cornell Tech\is-my-train-screwed\backend"
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    set "%%A=%%B"
)
python scripts\hourly_bus_sync.py >> data\hourly_bus_sync.log 2>&1
