"""Enable ``python -m attendance_bot`` to launch the bot."""

from .app import run

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
