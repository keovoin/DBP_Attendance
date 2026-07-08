#!/usr/bin/env python3
"""Convenience launcher for the Telegram Attendance Tracker.

Equivalent to ``python -m attendance_bot``.
"""

from attendance_bot.app import run

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
