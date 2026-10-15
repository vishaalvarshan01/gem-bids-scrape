"""Read and insert gem_bids rows."""

from __future__ import annotations

from supabase import Client, create_client

from gem_job.gem_client import source_pdf_url
from gem_job.models import ExtractedFields

TABLE = "gem_bids"
PAGE_SIZE = 1000
COLUMNS = (
    "bid_no",
    "ra_no",
    "bid_end_date",
    "bid_opening_date",
    "bid_offer_validity_days",
    "ministry_state_name",
    "department_name",
    "organisation_name",
    "office_name",
    "hod_email",
    "buyer_email",
    "item_category",
    "contract_period",
    "min_avg_annual_turnover",
    "years_past_experience_required",
    "past_experience_required",
    "msme_relaxation",
    "startup_relaxation",
    "searched_strings",
    "searched_result",
    "emd_amount",
    "source_pdf",
)


def get_client(url: str, service_role_key: str) -> Client:
    return create_client(url, service_role_key)


def existing_source_pdfs(client: Client) -> set[str]:
    """Every source_pdf already stored, paged so large tables are covered."""
    found: set[str] = set()
    start = 0
    while True:
        response = (
            client.table(TABLE)
            .select("source_pdf")
            .order("bid_no")
            .range(start, start + PAGE_SIZE - 1)
            .execute()
        )
        rows = response.data or []
        for row in rows:
            source = (row.get("source_pdf") or "").rstrip("/")
            if source:
                found.add(source)
        if len(rows) < PAGE_SIZE:
            return found
        start += PAGE_SIZE


def to_row(record: dict, tender_id: str) -> dict | None:
    """Return a gem_bids row, or None when bid_no is missing."""
    fields = ExtractedFields.model_validate(record)
    data = fields.model_dump()
    if not data.get("bid_no"):
        return None
    data["source_pdf"] = source_pdf_url(tender_id)
    row = {}
    for column in COLUMNS:
        value = data.get(column)
        row[column] = value.replace("\x00", "") if isinstance(value, str) else value
    return row


def is_duplicate_error(exc: Exception) -> bool:
    payload = exc.args[0] if exc.args else None
    if isinstance(payload, dict):
        code = str(payload.get("code", ""))
        message = str(payload.get("message", "")).lower()
        return code == "23505" or "duplicate" in message
    text = str(exc).lower()
    return "23505" in text or "duplicate key" in text or "already exists" in text


def insert_rows(client: Client, rows: list[dict]) -> tuple[int, int]:
    """Insert rows. Returns (inserted, skipped_duplicates)."""
    if not rows:
        return 0, 0
    try:
        client.table(TABLE).insert(rows).execute()
        return len(rows), 0
    except Exception as exc:
        if not is_duplicate_error(exc) or len(rows) == 1:
            if is_duplicate_error(exc):
                print(f"skip duplicate {rows[0].get('bid_no')}", flush=True)
                return 0, 1
            raise
    inserted = 0
    skipped = 0
    for row in rows:
        added, missed = insert_rows(client, [row])
        inserted += added
        skipped += missed
    return inserted, skipped
