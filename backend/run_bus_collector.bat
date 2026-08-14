@echo off
cd /d "F:\Cornell Tech\is-my-train-screwed\backend"
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    set "%%A=%%B"
)
python collectors\bus_collector.py >> data\collector.log 2>&1
