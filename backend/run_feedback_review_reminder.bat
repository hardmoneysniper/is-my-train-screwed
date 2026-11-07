@echo off
cd /d "F:\Cornell Tech\is-my-train-screwed\backend"
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    set "%%A=%%B"
)
python scripts\feedback_review_reminder.py >> data\feedback_review_reminder.log 2>&1
