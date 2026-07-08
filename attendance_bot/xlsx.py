"""A minimal, dependency-free ``.xlsx`` (Excel) writer.

Generates a single-sheet workbook from a header row and data rows using only
the standard library (``zipfile`` + a little XML). Values that are ``int`` or
``float`` are written as numbers; everything else is written as inline text.

This is intentionally tiny - just enough for the attendance report export.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from typing import Iterable, Sequence

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _col_letter(index: int) -> str:
    """0-based column index -> Excel column letters (0->A, 26->AA)."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _col_to_index(letters: str) -> int:
    """Excel column letters -> 0-based index (A->0, AA->26)."""
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - 64)
    return idx - 1


def _cell(col: int, row: int, value) -> str:
    ref = f"{_col_letter(col)}{row}"
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = "" if value is None else str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_escape(text)}</t></is></c>'


def _workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_escape(sheet_name)[:31]}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )


def build_xlsx(
    header: Sequence[str],
    rows: Iterable[Sequence[object]],
    sheet_name: str = "Report",
) -> bytes:
    """Return the bytes of a single-sheet .xlsx workbook."""
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
    ]
    r = 1
    parts.append(f'<row r="{r}">')
    for c, value in enumerate(header):
        parts.append(_cell(c, r, value))
    parts.append("</row>")
    for row in rows:
        r += 1
        parts.append(f'<row r="{r}">')
        for c, value in enumerate(row):
            parts.append(_cell(c, r, value))
        parts.append("</row>")
    parts.append("</sheetData></worksheet>")
    sheet_xml = "".join(parts)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        zf.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()



def read_xlsx(data: bytes) -> list[list[str]]:
    """Read the first worksheet of an .xlsx file into a list of string rows.

    Handles shared strings, inline strings, and plain numeric/text values.
    Missing cells are preserved as empty strings using each cell's column
    reference, so columns stay aligned. Never raises on a malformed file - it
    returns whatever rows it could parse (or an empty list).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return []

    with zf:
        names = zf.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.findall(f"{_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
            except ET.ParseError:
                shared = []

        sheet = next(
            (n for n in names
             if n.startswith("xl/worksheets/") and n.endswith(".xml")),
            None,
        )
        if sheet is None:
            return []
        try:
            root = ET.fromstring(zf.read(sheet))
        except ET.ParseError:
            return []

        rows: list[list[str]] = []
        for row_el in root.iter(f"{_NS}row"):
            cells: list[str] = []
            for c in row_el.findall(f"{_NS}c"):
                ref = c.get("r") or ""
                letters = "".join(ch for ch in ref if ch.isalpha())
                target = _col_to_index(letters) if letters else len(cells)
                while len(cells) <= target:
                    cells.append("")
                cells[target] = _read_cell(c, shared)
            rows.append(cells)
        return rows


def _read_cell(c, shared: list[str]) -> str:
    cell_type = c.get("t")
    v = c.find(f"{_NS}v")
    if cell_type == "s" and v is not None and v.text is not None:
        try:
            idx = int(v.text)
        except ValueError:
            return ""
        return shared[idx] if 0 <= idx < len(shared) else ""
    if cell_type == "inlineStr":
        is_el = c.find(f"{_NS}is")
        if is_el is not None:
            return "".join(t.text or "" for t in is_el.iter(f"{_NS}t"))
    if v is not None and v.text is not None:
        return v.text
    return ""
