# Telegram Attendance Tracker - container image
#
# The bot has ZERO third-party dependencies, so there is no pip install step.
# A slim Python base image is all that is needed.
FROM python:3.12-slim

# Predictable, unbuffered logging (so `fly logs` shows output immediately).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy only what the runtime needs.
COPY attendance_bot ./attendance_bot
COPY main.py ./

# The SQLite database lives on a mounted volume at /data (see fly.toml).
# DB_PATH is provided via environment; this is a safe default.
ENV DB_PATH=/data/attendance.db

# Long-polling worker: no ports are exposed because there is no HTTP server.
CMD ["python", "-m", "attendance_bot"]
