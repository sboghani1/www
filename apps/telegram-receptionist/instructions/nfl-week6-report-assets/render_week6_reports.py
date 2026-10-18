from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
WIDTH = 1800
MARGIN = 72
CONTENT_WIDTH = WIDTH - MARGIN * 2
TITLE_SIZE = 39
BODY_SIZE = 24
TABLE_SIZE = 19
SMALL_SIZE = 19
LINE_GAP = 8
COLUMN_WIDTHS = [215, 300, 235, 300, 606]
TABLE_HEADERS = [
    "Person",
    "Week 6 position",
    "Prior graded record",
    "Confidence / calibration",
    "Interpretation",
]
REQUIRED_REPORT_FIELDS = {
    "slug",
    "title",
    "intro",
    "rows",
    "favorite_records",
    "notable",
    "expectation",
    "footnote",
}
REQUIRED_ROW_FIELDS = {
    "speaker",
    "position",
    "overall_text",
    "confidence_text",
    "interpretation",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render 2026 NFL Week 6 picker-history reports."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT / "base-reports.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "generated",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Render only this report slug; may be repeated.",
    )
    return parser.parse_args()


def font_path(*names: str) -> str:
    roots = (
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
        Path("/Library/Fonts"),
    )
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return str(candidate)
    raise RuntimeError(f"Font not found: {', '.join(names)}")


REGULAR_PATH = font_path(
    "DejaVuSans.ttf",
    "Arial.ttf",
    "Helvetica.ttc",
)
BOLD_PATH = font_path(
    "DejaVuSans-Bold.ttf",
    "Arial Bold.ttf",
    "Arial Bold Italic.ttf",
    "Helvetica.ttc",
)


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(BOLD_PATH if bold else REGULAR_PATH, size)


TITLE = load_font(TITLE_SIZE, bold=True)
BODY = load_font(BODY_SIZE)
BODY_BOLD = load_font(BODY_SIZE, bold=True)
TABLE = load_font(TABLE_SIZE)
TABLE_BOLD = load_font(TABLE_SIZE, bold=True)
SMALL = load_font(SMALL_SIZE)


def line_height(font: ImageFont.FreeTypeFont) -> int:
    box = font.getbbox("Ag")
    return box[3] - box[1] + LINE_GAP


def wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    width: int,
) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    width: int,
    fill: str = "#1f2328",
) -> int:
    x, y = xy
    height = line_height(font)
    for line in wrap(draw, text, font, width):
        draw.text((x, y), line, font=font, fill=fill)
        y += height
    return y


def text_height(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    width: int,
) -> int:
    return len(wrap(draw, text, font, width)) * line_height(font)


def validate_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object.")
    if payload.get("schema_version") != "nfl_week6_picker_reports_v1":
        raise ValueError("Unsupported or missing schema_version.")
    reports = payload.get("reports")
    if not isinstance(reports, list) or not reports:
        raise ValueError("reports must be a non-empty array.")
    seen: set[str] = set()
    for index, report in enumerate(reports):
        if not isinstance(report, dict):
            raise ValueError(f"reports[{index}] must be an object.")
        missing = REQUIRED_REPORT_FIELDS - report.keys()
        if missing:
            raise ValueError(
                f"reports[{index}] missing: {', '.join(sorted(missing))}"
            )
        slug = str(report["slug"])
        if slug in seen:
            raise ValueError(f"Duplicate report slug: {slug}")
        seen.add(slug)
        rows = report["rows"]
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{slug}: rows must be a non-empty array.")
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{slug}: rows[{row_index}] must be an object.")
            row_missing = REQUIRED_ROW_FIELDS - row.keys()
            if row_missing:
                raise ValueError(
                    f"{slug}: rows[{row_index}] missing: "
                    f"{', '.join(sorted(row_missing))}"
                )
        calibration = report.get("ak_calibration", [])
        if not isinstance(calibration, list) or not all(
            isinstance(item, str) for item in calibration
        ):
            raise ValueError(f"{slug}: ak_calibration must be an array of strings.")
    return reports


def table_rows(report: dict[str, Any]) -> list[list[str]]:
    return [
        [
            str(row["speaker"]),
            str(row["position"]),
            str(row["overall_text"]),
            str(row["confidence_text"]),
            str(row["interpretation"]),
        ]
        for row in report["rows"]
    ]


def measure_table(
    draw: ImageDraw.ImageDraw,
    rows: list[list[str]],
) -> tuple[int, list[int]]:
    heights = []
    for index, row in enumerate([TABLE_HEADERS, *rows]):
        font = TABLE_BOLD if index == 0 else TABLE
        row_height = 0
        for text, width in zip(row, COLUMN_WIDTHS, strict=True):
            row_height = max(
                row_height,
                len(wrap(draw, text, font, width - 28))
                * line_height(font)
                + 28,
            )
        heights.append(row_height)
    return sum(heights), heights


def render(report: dict[str, Any], output_dir: Path) -> Path:
    probe = Image.new("RGB", (WIDTH, 100), "white")
    probe_draw = ImageDraw.Draw(probe)
    rows = table_rows(report)
    table_total, row_heights = measure_table(probe_draw, rows)
    title = str(report["title"])
    intro = str(report["intro"])
    favorite_records = str(report["favorite_records"])
    expectation = str(report["expectation"])
    footnote = str(report["footnote"])
    notable = [str(item) for item in report["notable"]]
    calibration = [str(item) for item in report.get("ak_calibration", [])]

    title_height = text_height(probe_draw, title, TITLE, CONTENT_WIDTH)
    intro_height = text_height(probe_draw, intro, BODY, CONTENT_WIDTH)
    favorite_height = text_height(
        probe_draw, favorite_records, BODY, CONTENT_WIDTH
    )
    calibration_height = 0
    if calibration:
        calibration_height = line_height(BODY_BOLD) + 18
        calibration_height += sum(
            text_height(probe_draw, item, BODY, CONTENT_WIDTH - 54) + 12
            for item in calibration
        )
    notable_height = line_height(BODY_BOLD) + 18
    notable_height += sum(
        text_height(probe_draw, item, BODY, CONTENT_WIDTH - 54) + 12
        for item in notable
    )
    expectation_height = text_height(
        probe_draw, expectation, BODY, CONTENT_WIDTH
    )
    footnote_height = text_height(probe_draw, footnote, SMALL, CONTENT_WIDTH)
    height = (
        MARGIN
        + title_height
        + 24
        + intro_height
        + 36
        + table_total
        + 36
        + favorite_height
        + (32 + calibration_height if calibration else 0)
        + 32
        + notable_height
        + 22
        + expectation_height
        + 34
        + 2
        + 22
        + footnote_height
        + MARGIN
    )

    image = Image.new("RGB", (WIDTH, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    y = MARGIN
    y = draw_wrapped(
        draw, (MARGIN, y), title, font=TITLE, width=CONTENT_WIDTH
    )
    y += 24
    y = draw_wrapped(
        draw, (MARGIN, y), intro, font=BODY, width=CONTENT_WIDTH
    )
    y += 36

    for row_index, (row, row_height) in enumerate(
        zip([TABLE_HEADERS, *rows], row_heights, strict=True)
    ):
        x = MARGIN
        background = "#f6f8fa" if row_index == 0 else "#ffffff"
        font = TABLE_BOLD if row_index == 0 else TABLE
        for column_index, (text, width) in enumerate(
            zip(row, COLUMN_WIDTHS, strict=True)
        ):
            draw.rectangle(
                (x, y, x + width, y + row_height),
                fill=background,
                outline="#d0d7de",
                width=2,
            )
            cell_font = (
                TABLE_BOLD
                if row_index == 0 or (row_index > 0 and column_index == 0)
                else font
            )
            draw_wrapped(
                draw,
                (x + 14, y + 14),
                text,
                font=cell_font,
                width=width - 28,
            )
            x += width
        y += row_height

    y += 36
    y = draw_wrapped(
        draw,
        (MARGIN, y),
        favorite_records,
        font=BODY,
        width=CONTENT_WIDTH,
    )

    if calibration:
        y += 32
        draw.text(
            (MARGIN, y),
            "AK score calibration",
            font=BODY_BOLD,
            fill="#1f2328",
        )
        y += line_height(BODY_BOLD) + 18
        for item in calibration:
            draw.ellipse(
                (MARGIN + 8, y + 12, MARGIN + 20, y + 24),
                fill="#1f2328",
            )
            y = draw_wrapped(
                draw,
                (MARGIN + 42, y),
                item,
                font=BODY,
                width=CONTENT_WIDTH - 54,
            )
            y += 12

    y += 32
    draw.text(
        (MARGIN, y),
        "What is most notable",
        font=BODY_BOLD,
        fill="#1f2328",
    )
    y += line_height(BODY_BOLD) + 18
    for item in notable:
        draw.ellipse(
            (MARGIN + 8, y + 12, MARGIN + 20, y + 24),
            fill="#1f2328",
        )
        y = draw_wrapped(
            draw,
            (MARGIN + 42, y),
            item,
            font=BODY,
            width=CONTENT_WIDTH - 54,
        )
        y += 12

    y += 10
    y = draw_wrapped(
        draw,
        (MARGIN, y),
        expectation,
        font=BODY,
        width=CONTENT_WIDTH,
    )
    y += 34
    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill="#d0d7de", width=2)
    y += 22
    draw_wrapped(
        draw,
        (MARGIN, y),
        footnote,
        font=SMALL,
        width=CONTENT_WIDTH,
        fill="#57606a",
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{report['slug']}-week6-analysis.png"
    image.save(output, optimize=True)
    return output


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    reports = validate_payload(payload)
    selected = set(args.only)
    known = {str(report["slug"]) for report in reports}
    unknown = selected - known
    if unknown:
        raise ValueError(f"Unknown report slug(s): {', '.join(sorted(unknown))}")
    rendered = 0
    for report in reports:
        if selected and report["slug"] not in selected:
            continue
        print(render(report, args.output_dir))
        rendered += 1
    if rendered == 0:
        raise ValueError("No reports selected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
