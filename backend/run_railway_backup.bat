@echo off
cd /d "F:\Cornell Tech\is-my-train-screwed\backend"
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    set "%%A=%%B"
)
python scripts\backup_from_railway.py >> data\backup.log 2>&1
