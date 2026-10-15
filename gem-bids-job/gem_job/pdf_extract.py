"""Extract GeM bid fields from the PDF text layer.

Digital bid PDFs store the Bid Details table as text. PyMuPDF reads those
rows directly, so this does not render pages or call a model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from gem_job.models import ExtractedFields

DEFAULT_MAX_PAGES = 4

BID_NO_RE = re.compile(r"GEM/\d{4}/[A-Z]/\d+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
HOD_EMAIL_RE = re.compile(r"hod email id\s*:?\s*(" + EMAIL_RE.pattern + ")", re.IGNORECASE)
BUYER_EMAIL_RE = re.compile(
    r"buyer email id\s*:?\s*(" + EMAIL_RE.pattern + ")", re.IGNORECASE
)
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
PAGE_NO_RE = re.compile(r"^\d+\s*/\s*\d+$")
DATE_RE = re.compile(r"\d{2}-\d{2}-\d{4}")

# Longer phrases first so "MSE Exemption for Years of Experience" does not
# fill the years-of-experience field.
LABEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("bid end date", "bid_end_date"),
    ("bid opening date", "bid_opening_date"),
    ("bid offer validity", "bid_offer_validity_days"),
    ("ministry/state name", "ministry_state_name"),
    ("department name", "department_name"),
    ("organisation name", "organisation_name"),
    ("organization name", "organisation_name"),
    ("office name", "office_name"),
    ("item category", "item_category"),
    ("contract period", "contract_period"),
    ("minimum average annual turnover", "min_avg_annual_turnover"),
    ("years of past experience", "years_past_experience_required"),
    ("past experience of similar", "past_experience_required"),
    ("mse relaxation", "msme_relaxation"),
    ("mse exemption", "msme_relaxation"),
    ("startup relaxation", "startup_relaxation"),
    ("startup exemption", "startup_relaxation"),
    ("searched strings used in gemarpts", "searched_strings"),
    ("searched result generated in gemarpts", "searched_result"),
    ("emd amount", "emd_amount"),
)

YES_NO_FIELDS = {
    "msme_relaxation",
    "startup_relaxation",
    "past_experience_required",
}

LABEL_HINTS: tuple[str, ...] = tuple(
    sorted(
        {
            needle
            for needle, _field in LABEL_FIELDS
            for needle in (
                needle,
                " ".join(needle.split()[:2]),
                " ".join(needle.split()[:3]),
            )
            if needle
        }
        | {
            "contact details",
            "grievance redressal",
            "buyer email",
            "document required",
            "hod email",
        },
        key=len,
        reverse=True,
    )
)


def _open(source: str | Path | bytes) -> pymupdf.Document:
    if isinstance(source, bytes):
        return pymupdf.open(stream=source, filetype="pdf")
    return pymupdf.open(source)


def _clean(value: str | None) -> str:
    text = (value or "").replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


def _label(value: str) -> str:
    return _clean(value).lower()


def _yes_no(value: str) -> str:
    tokens = re.findall(r"\b(yes|no)\b", _label(value))
    if tokens:
        return tokens[-1].capitalize()
    return value.strip()


def _field_for_label(label: str) -> str | None:
    for needle, field in LABEL_FIELDS:
        if needle in label:
            return field
    return None


def _apply_contact(raw: dict[str, str], value: str) -> None:
    hod = HOD_EMAIL_RE.search(value)
    buyer = BUYER_EMAIL_RE.search(value)
    if hod and "hod_email" not in raw:
        raw["hod_email"] = hod.group(1)
    if buyer and "buyer_email" not in raw:
        raw["buyer_email"] = buyer.group(1)
    if hod or buyer:
        return
    emails = EMAIL_RE.findall(value)
    if len(emails) == 1 and "buyer_email" not in raw:
        raw["buyer_email"] = emails[0]


def _page_count(document: pymupdf.Document, max_pages: int) -> int:
    if max_pages > 0:
        return min(len(document), max_pages)
    return len(document)


def _looks_like_label(line: str) -> bool:
    if PAGE_NO_RE.match(line) or EMAIL_RE.search(line):
        return False
    low = _label(line)
    if low in {"bid document", "bid details"}:
        return False
    if "/" in line or DEVANAGARI_RE.search(line):
        return True
    return any(hint in low for hint in LABEL_HINTS)


def _looks_like_value(line: str) -> bool:
    low = _label(line)
    if DATE_RE.match(low) or low.startswith(("yes", "no")) or EMAIL_RE.search(line):
        return True
    if re.match(r"\d", low):
        return True
    return len(low) >= 8 and not _looks_like_label(line)


def _continues_label(label_lines: list[str], line: str) -> bool:
    if _looks_like_label(line):
        return True
    low = _label(line)
    if DATE_RE.match(low) or low.startswith(("yes", "no")) or re.match(r"\d", low) or EMAIL_RE.search(line):
        return False
    previous = _label(" ".join(label_lines))
    joined = _label(f"{previous} {low}")
    if _field_for_label(joined) and not _field_for_label(previous):
        return True
    if any(hint in joined and hint not in previous for hint in LABEL_HINTS):
        return True
    return not _looks_like_value(line)


def _text_rows(document: pymupdf.Document, max_pages: int) -> list[tuple[str, str]]:
    """Pair wrapped labels with the following value lines.

    Some GeM PDFs are borderless, so find_tables() returns nothing even though
    the text is a label line followed by its value.
    """
    lines: list[str] = []
    for index in range(_page_count(document, max_pages)):
        for raw_line in document[index].get_text().splitlines():
            line = _clean(raw_line)
            if line and not PAGE_NO_RE.match(line):
                lines.append(line)

    rows: list[tuple[str, str]] = []
    label_lines: list[str] = []
    value_lines: list[str] = []
    state = "seek"

    def emit() -> None:
        label = _clean(" ".join(label_lines))
        value = _clean(" ".join(value_lines))
        if label and value:
            rows.append((label, value))

    for line in lines:
        if _looks_like_label(line):
            if state == "value" and value_lines:
                emit()
                label_lines = [line]
                value_lines = []
                state = "label"
            else:
                label_lines.append(line)
                state = "label"
            continue
        if state == "label" and not value_lines and _continues_label(label_lines, line):
            label_lines.append(line)
            continue
        if state == "label":
            state = "value"
        if state == "value":
            value_lines.append(line)
    if state == "value" and value_lines:
        emit()
    return rows


def _table_rows(document: pymupdf.Document, max_pages: int) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for index in range(_page_count(document, max_pages)):
        for table in document[index].find_tables().tables:
            for row in table.extract():
                if not row or len(row) < 2:
                    continue
                label = _clean(row[0])
                value = _clean(row[1])
                if label and value:
                    rows.append((label, value))
    return rows


def _apply_bid_no(raw: dict[str, str], header: str, fallback_bid_no: str | None) -> None:
    match = BID_NO_RE.search(header)
    found = match.group(0).upper() if match else None
    fallback = fallback_bid_no.strip().upper() if fallback_bid_no else None
    if fallback and (not found or fallback.startswith(found) and fallback != found):
        raw["bid_no"] = fallback
    elif found:
        raw["bid_no"] = found


def _consume_rows(raw: dict[str, str], rows: list[tuple[str, str]], schedule_emd: list[float]) -> None:
    for label_text, value in rows:
        label = _label(label_text)
        value = re.sub(r"^(?:in gemarpts)\s+", "", value, flags=re.IGNORECASE)
        if "buyer email" in label and "buyer_email" not in raw:
            email = EMAIL_RE.search(value)
            raw["buyer_email"] = email.group(0) if email else value
            continue
        if "contact details" in label or "grievance redressal" in label:
            _apply_contact(raw, value)
            continue
        if "document required from seller" in label:
            if "experience criteria" in value.lower() and "past_experience_required" not in raw:
                raw["past_experience_required"] = "Yes"
            continue

        field = _field_for_label(label)
        if field is None or not value:
            continue
        if field == "emd_amount":
            number = NUMBER_RE.search(value.replace(",", ""))
            if number is None:
                continue
            amount = float(number.group(0))
            if "schedule" in label:
                schedule_emd.append(amount)
            elif "emd_amount" not in raw:
                raw["emd_amount"] = number.group(0)
            continue
        if field in raw and field != "past_experience_required":
            continue
        if field == "bid_offer_validity_days":
            number = NUMBER_RE.search(value.replace(",", ""))
            if number is None:
                continue
            value = number.group(0)
        elif field in YES_NO_FIELDS:
            value = _yes_no(value)
        raw[field] = value


def extract_fields_from_pdf(
    source: str | Path | bytes,
    max_pages: int = DEFAULT_MAX_PAGES,
    fallback_bid_no: str | None = None,
) -> ExtractedFields:
    document = _open(source)
    try:
        header_pages = min(len(document), 2)
        header = "\n".join(document[i].get_text() for i in range(header_pages))
        raw: dict[str, str] = {}
        _apply_bid_no(raw, header, fallback_bid_no)

        schedule_emd: list[float] = []
        _consume_rows(raw, _table_rows(document, max_pages), schedule_emd)
        _consume_rows(raw, _text_rows(document, max_pages), schedule_emd)

        if "emd_amount" not in raw and schedule_emd:
            raw["emd_amount"] = str(sum(schedule_emd))
        return ExtractedFields.model_validate(raw)
    finally:
        document.close()
