"""Read already-rendered Senate results. Never contact eFD or operate its consent form."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

EFD = "https://efdsearch.senate.gov"
REPORT = re.compile(r"/search/(?:view/)?(?P<type>ptr|paper)/[a-zA-Z0-9-]+/?")
PTR_LABEL = re.compile(r"periodic transaction report|\bPTR\b", re.IGNORECASE)


class TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, list[str], bool]] = []
        self.parts: list[str] | None = None
        self.links: list[str] = []
        self.header = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.parts, self.links, self.header = [], [], False
        elif self.parts is not None:
            if tag == "th":
                self.header = True
            if tag == "a":
                href = dict(attrs).get("href")
                if href:
                    self.links.append(href)

    def handle_data(self, data: str) -> None:
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self.parts is not None:
            self.rows.append((" ".join(" ".join(self.parts).split()), self.links, self.header))
            self.parts = None


def read_senate_table(html: str) -> dict[str, dict[str, str]]:
    table = TableRows()
    table.feed(html)
    table.close()
    records = {}
    header = empty = False
    for text, links, is_header in table.rows:
        if is_header:
            header |= "report" in text.casefold()
            continue
        if text.rstrip(".").casefold() in {"no matching records found", "no data available in table"}:
            empty = True
            continue
        urls = set()
        for link in links:
            url = urlsplit(urljoin(EFD + "/search/", link))
            match = REPORT.fullmatch(url.path)
            if url.scheme != "https" or url.netloc.lower() != "efdsearch.senate.gov" or not match:
                continue
            if match["type"] == "ptr" or PTR_LABEL.search(text):
                urls.add(EFD + url.path.rstrip("/") + "/")
        if not urls:
            if text:
                raise ValueError("Expected an official PTR link in each Senate row; export only PTR results")
            continue
        if len(urls) != 1:
            raise ValueError("Ambiguous Senate result: more than one PTR link in a row")
        url = urls.pop()
        record = {"listing": text}
        if url in records and records[url] != record:
            raise ValueError(f"Conflicting Senate rows for {url}")
        records[url] = record
    if not records and not (header and empty):
        raise ValueError("No rendered Senate PTR table found (a consent page or page source is not a result)")
    return records
