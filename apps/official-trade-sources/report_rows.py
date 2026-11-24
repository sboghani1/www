"""Extract investment rows, keeping the original report/page and disclosed value bands."""

from __future__ import annotations

import bisect
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import pdfplumber
from pdfplumber.page import Page


def clean(text: str) -> str:
    return " ".join(text.replace("\x00", "").split())


def iso_date(text: str) -> str:
    return datetime.strptime(text, "%m/%d/%Y").date().isoformat()


@dataclass
class Entry:
    kind: str
    asset: str
    owner: str
    amount: str
    date: str
    page: int
    action: str = ""
    description: str = ""
    status: str = "new"
    reference: str = ""
    notified: str = ""


@dataclass
class Parsed:
    entries: list[Entry] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    period_year: int | None = None
    note: str = ""


def public_asset(asset: str) -> str:
    # Property locations aren't needed to identify a security or compare value bands.
    if re.search(r"^\d+\s|\[RP\]|\b(primary|personal) residence\b|\bhome & vineyard\b", asset, re.I):
        return "Real property (location omitted)"
    return asset


def page_bands(page: Page):
    """Use table cell geometry: the Clerk draws borders but does not fill every other row."""
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3)
    bold_words = [w for w in page.extract_words(x_tolerance=1.5, y_tolerance=3, extra_attrs=["fontname"])
                  if "bold" in w["fontname"].lower()]
    groups = defaultdict(list)
    for r in page.rects:
        if r["width"] > 5 and r["height"] > 6:
            groups[round(r["top"], 1), round(r["bottom"], 1)].append(r["x0"])

    def read_cells(lefts, top, bottom, source):
        cells = [[] for _ in lefts]
        text = []
        for w in source:
            if top <= (w["top"] + w["bottom"]) / 2 < bottom:
                word = w["text"]
                col = bisect.bisect_right(lefts, w["x0"] + 0.5) - 1
                if col >= 0:
                    cells[col].append(word)
                    text.append(word)
        return [clean(" ".join(c)) for c in cells], clean(" ".join(text))

    last_header_end = -1
    for (top, bottom), xs in sorted(groups.items()):
        if top < last_header_end:
            continue
        lefts = sorted(set(xs))
        if len(lefts) < 5:
            continue
        cells, _ = read_cells(lefts, top, bottom, bold_words)
        labels = [c.casefold() for c in cells]
        if labels[:3] == ["asset", "owner", "value of asset"]:
            kind = "holding"
        elif labels[:3] == ["asset", "owner", "date"] and {"tx. type", "amount"} <= set(labels):
            kind = "annual trade"
        elif labels[:3] == ["id", "owner", "asset"] and {"date", "amount"} <= set(labels):
            kind = "trade"
        else:
            continue
        last_header_end = bottom
        yield kind, page.page_number, None, ""
        # The thin left edge spans one physical row on both shaded and white rows.
        bands = sorted({(round(r["top"], 1), round(r["bottom"], 1)) for r in page.rects
                        if r["width"] < 1.6 and r["height"] > 2 and abs(r["x0"] - lefts[0]) < 0.5
                        and r["bottom"] >= bottom - 2}, reverse=True)
        # Repeated headers sometimes overpaint an extra partial row border. The actual
        # body is the contiguous chain of borders, not the edge nearest header.bottom.
        chains = {}
        for a, b in bands:
            chain = [(a, b), *chains.get(b, [])]
            if a not in chains or chain[-1][1] > chains[a][-1][1]:
                chains[a] = chain
        starts = [c for a, c in chains.items() if a <= bottom + 5]
        for y0, y1 in max(starts, key=lambda c: (c[-1][1], len(c)), default=[]):
            values, text = read_cells(lefts, max(y0, bottom - 2), y1, words)
            yield kind, page.page_number, dict(zip(labels, values)), text


NOTE = re.compile(r"(?:^|\s)(filing status|f s|subholding of|s o|description|d|comments?|c|location|l)\s*:\s*", re.I)


def annual_counts(page: Page) -> dict[str, int]:
    """Independent text-column check; does not use the borders used to reconstruct physical rows."""
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3, extra_attrs=["fontname"])
    heads = [w for w in words if "bold" in w["fontname"].lower()]
    counts = {}
    for h in heads:
        name = h["text"].lower()
        if name not in {"value", "date"} or not any(w["text"].lower() == "asset" and w["x0"] < h["x0"]
                                                    and abs(w["top"] - h["top"]) < 1 for w in heads):
            continue
        end = min([w["top"] for w in words if w["top"] > h["top"] + 10 and w["x0"] < 40
                   and (w["text"] == "*" or (w["text"].casefold() == "asset" and w in heads))] + [page.height])
        pattern = r"\$(?:1|[\d,]+001)|none|undetermined" if name == "value" else r"\d{1,2}/\d{1,2}/\d{4}"
        counts["holding" if name == "value" else "trade"] = sum(
            1 for w in words if h["top"] < w["top"] < end and abs(w["x0"] - h["x0"]) < 2
            and "bold" not in w["fontname"].lower() and re.fullmatch(pattern, w["text"], re.I))
    return counts

def house_report(data: bytes, form: str, doc_id: str) -> Parsed:
    result = Parsed()
    rows: list[tuple[str, int, dict[str, str], dict[str, str]]] = []
    saw_table = False
    status_labels = 0
    anchors = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        first = clean(pdf.pages[0].extract_text() or "")
        if not first:
            return Parsed(issues=["Image-only filing: review the original before using its rows"])
        identity = re.search(r"Filing ID\s*#\s*(\d+)", first, re.I)
        if not identity or identity[1] != doc_id:
            result.issues.append("PDF filing ID missing or different from index")
            return result
        year = re.search(r"filing year:\s*(\d{4})\b", first, re.I)
        if year:
            result.period_year = int(year[1])
        for page in pdf.pages:
            if form != "PTR":
                anchors[page.page_number] = annual_counts(page)
            plain = clean(page.extract_text() or "").casefold()
            if not plain:
                result.issues.append(f"Page {page.page_number}: no readable text")
            status_labels += len(re.findall(r"\b(?:filing status|f s):\s*(?:new|amended|deleted)\b", plain))
            headers = set()
            first_band = True
            for kind, number, fields, text in page_bands(page):
                if fields is None:
                    headers.add(kind)
                    first_band = True
                    continue
                if (form == "PTR") != (kind == "trade"):
                    continue
                saw_table = True
                if not text:
                    continue
                if text.startswith("*") or "complete list of asset type" in text.casefold():
                    continue
                notes = list(NOTE.finditer(text))
                if notes and notes[0].start() == 0:
                    if rows:
                        for i, note in enumerate(notes):
                            key = note[1].casefold()
                            value = text[note.end():notes[i + 1].start() if i + 1 < len(notes) else None].strip()
                            if key in {"filing status", "f s"}:
                                rows[-1][3]["status"] = value.casefold()
                            if key in {"description", "d"}:
                                rows[-1][3]["description"] = value
                            if key in {"subholding of", "s o", "comments", "comment", "c"}:
                                rows[-1][3]["context"] = clean(rows[-1][3].get("context", "") + " " + value)
                    continue
                continuing = (first_band and rows and rows[-1][0] == kind and rows[-1][1] < number
                              and (not fields["asset"] or (kind != "holding" and not fields["date"])
                                   or (kind == "holding" and not fields["value of asset"] and not fields["owner"])))
                if continuing:
                    if rows[-1][3].get("description") and not fields.get("value of asset") and not fields.get("amount"):
                        rows[-1][3]["description"] += " " + text
                        first_band = False
                        continue
                    for key, value in fields.items():
                        if value:
                            rows[-1][2][key] = clean(rows[-1][2].get(key, "") + " " + value)
                elif fields["asset"]:
                    rows.append((kind, number, fields, {}))
                else:
                    result.issues.append(f"Page {number}: unrecognized row; inspect original")
                first_band = False
            for header, expected in (("asset owner value of asset", "holding"),
                                     ("asset owner date tx.", "annual trade"),
                                     ("id owner asset transaction", "trade")):
                if header in plain and expected not in headers:
                    result.issues.append(f"Page {page.page_number}: unsupported {expected} table layout")
            page.close()

    for kind, number, fields, notes in rows:
        asset = public_asset(fields["asset"])
        owner = fields["owner"].upper() or "unmarked"
        details = clean(notes.get("description", "") + " " + notes.get("context", "")) if asset == fields["asset"] else ""
        if kind == "holding":
            if result.period_year is None:
                result.issues.append(f"Page {number}: annual reporting period not identified")
                continue
            result.entries.append(Entry(kind, asset, owner, fields["value of asset"] or "not specified",
                                        f"{result.period_year}-12-31", number, description=details))
        else:
            try:
                traded = iso_date(fields["date"])
                notified = iso_date(fields["notification date"]) if fields.get("notification date") else ""
            except ValueError:
                result.issues.append(f"Page {number}: ambiguous transaction date; inspect original")
                continue
            action, amount = fields.get("tx. type", fields.get("transaction type", "")), fields["amount"]
            status = "annual" if kind == "annual trade" else notes.get("status", "")
            if not action or not amount or status not in {"new", "amended", "deleted", "annual"}:
                result.issues.append(f"Page {number}: missing transaction type, amount or filing status")
                continue
            result.entries.append(Entry("trade", asset, owner, amount, traded, number, action[:1].upper() + action[1:], details,
                                        status, fields.get("id", ""), notified))
    if form == "PTR" and status_labels != len(result.entries):
        result.issues.append(f"PTR has {status_labels} status labels but {len(result.entries)} parsed trades")
    for page_no, counts in anchors.items():
        for kind, expected in counts.items():
            actual = sum(e.page == page_no and e.kind == kind for e in result.entries)
            if actual != expected:
                result.issues.append(f"Page {page_no}: {expected} {kind} column anchors but {actual} rows; inspect original")
    if not saw_table or not result.entries:
        result.issues.append("No supported investment table with entries; inspect original")
    return result
