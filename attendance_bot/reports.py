"""Attendance report rendering.

Produces two representations of a list of Attendance_Entry records:

* :func:`render_text` - a compact human-readable summary for chat messages
  (used by the /view command).
* :func:`build_csv` - a downloadable CSV document (used by the /export
  command), including every field required by the specification.
"""

from __future__ import annotations

import csv
import io
from typing import Iterable

from .db import AttendanceEntry

CSV_HEADER = [
    "Member",
    "Date",
    "Clock In",
    "Clock Out",
    "Type",
    "Late Remark",
]


def _dash(value: object) -> str:
    """Render None / empty values as an em dash for readability."""
    if value is None or value == "":
        return "-"
    return str(value)


def build_csv(entries: Iterable[AttendanceEntry]) -> bytes:
    """Return a UTF-8 (BOM) encoded CSV report as bytes.

    A BOM is prepended so the file opens cleanly in Excel with correct
    encoding of any non-ASCII names or remarks.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    for e in entries:
        writer.writerow(
            [
                e.member_name,
                e.date,
                _dash(e.clock_in_time),
                _dash(e.clock_out_time),
                _dash(e.clock_in_type),
                _dash(e.late_remark),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def render_text(
    entries: list[AttendanceEntry],
    *,
    include_member: bool = False,
    max_entries: int = 30,
) -> str:
    """Render entries as a readable text block for a chat message.

    ``include_member`` adds the member name to each line (used for Admin
    views spanning multiple members). Output is truncated to ``max_entries``
    with a note pointing the user at /export for the full data set.
    """
    if not entries:
        return "No attendance records found."

    lines: list[str] = []
    shown = entries[:max_entries]
    for e in shown:
        header = f"\U0001F4C5 {e.date}"
        if include_member:
            header += f" - {e.member_name}"
        lines.append(header)
        lines.append(
            f"    In: {_dash(e.clock_in_time)}    Out: {_dash(e.clock_out_time)}"
        )
        lines.append(f"    Type: {_dash(e.clock_in_type)}")
        if e.late_remark:
            lines.append(f"    Late remark: {e.late_remark}")
        lines.append("")

    text = "\n".join(lines).rstrip()
    if len(entries) > max_entries:
        remaining = len(entries) - max_entries
        text += (
            f"\n\n... and {remaining} more record(s). "
            "Use /export to download the full report."
        )
    return text
