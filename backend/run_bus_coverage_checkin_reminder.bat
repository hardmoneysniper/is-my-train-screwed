@echo off
cd /d "F:\Cornell Tech\is-my-train-screwed\backend"
python scripts\bus_coverage_checkin_reminder.py >> data\bus_coverage_checkin_reminder.log 2>&1
