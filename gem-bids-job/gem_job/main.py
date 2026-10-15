"""List ongoing GeM bids, extract PDF text, and insert new rows into gem_bids.

Credentials come from SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
PDFs stay in memory. Nothing is written to disk.

Usage (from this folder):
    python -m gem_job.main
    LIMIT=20 python -m gem_job.main
"""

from __future__ import annotations

import multiprocessing
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from dotenv import load_dotenv

from gem_job.gem_client import collect_tender_ids, init_worker, process_tender, source_pdf_url
from gem_job.supabase_store import existing_source_pdfs, get_client, insert_rows, to_row


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name}")
    return value


def run() -> None:
    load_dotenv()
    url = _require("SUPABASE_URL")
    key = _require("SUPABASE_SERVICE_ROLE_KEY")
    download_workers = _env_int("DOWNLOAD_WORKERS", 4)
    listing_workers = _env_int("LISTING_WORKERS", 16)
    max_pages = _env_int("MAX_PDF_PAGES", 3)
    batch_size = _env_int("BATCH_SIZE", 200)
    limit = _env_int("LIMIT", 0)
    print(
        f"workers={download_workers} listing_workers={listing_workers} "
        f"pdf_pages={max_pages} batch_size={batch_size} limit={limit or 'all'}",
        flush=True,
    )

    client = get_client(url, key)
    known = existing_source_pdfs(client)
    print(f"already stored: {len(known)} source pdfs", flush=True)

    tenders = collect_tender_ids(listing_workers)
    pending = [
        (tender_id, bid_no)
        for tender_id, bid_no in tenders
        if source_pdf_url(tender_id) not in known
    ]
    skipped_existing = len(tenders) - len(pending)
    if limit > 0:
        pending = pending[:limit]
    print(
        f"listed {len(tenders)} skipped_existing={skipped_existing} to_fetch={len(pending)}",
        flush=True,
    )
    if not pending:
        print(
            f"done: listed={len(tenders)} skipped_existing={skipped_existing} "
            "inserted=0 failed=0",
            flush=True,
        )
        return

    inserted = 0
    skipped_duplicate = 0
    skipped_no_bid = 0
    failed = 0
    buffer: list[dict] = []
    seen_bid_nos: set[str] = set()
    seen_pdfs: set[str] = set()
    started = time.perf_counter()
    total = len(pending)
    context = multiprocessing.get_context("spawn")

    def flush() -> None:
        nonlocal inserted, skipped_duplicate, buffer
        if not buffer:
            return
        added, missed = insert_rows(client, buffer)
        inserted += added
        skipped_duplicate += missed
        buffer = []
        print(
            f"uploaded={inserted} skipped_duplicate={skipped_duplicate} failed={failed}",
            flush=True,
        )

    with ProcessPoolExecutor(
        max_workers=download_workers,
        mp_context=context,
        initializer=init_worker,
    ) as pool:
        futures = [
            pool.submit(process_tender, tender_id, bid_no, max_pages)
            for tender_id, bid_no in pending
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result.get("error"):
                failed += 1
                print(f"failed {result['tender_id']}: {result['error']}", flush=True)
            else:
                row = to_row(result["fields"], result["tender_id"])
                if row is None:
                    skipped_no_bid += 1
                elif row["bid_no"] in seen_bid_nos or row["source_pdf"] in seen_pdfs:
                    skipped_duplicate += 1
                    print(f"skip duplicate {row['bid_no']}", flush=True)
                else:
                    seen_bid_nos.add(row["bid_no"])
                    seen_pdfs.add(row["source_pdf"])
                    buffer.append(row)
                    if len(buffer) >= batch_size:
                        flush()
            if index % 25 == 0 or index == total:
                elapsed = time.perf_counter() - started
                print(
                    f"[{index}/{total}] failed={failed} elapsed={elapsed:.0f}s",
                    flush=True,
                )
    flush()
    elapsed = time.perf_counter() - started
    print(
        f"done: listed={len(tenders)} skipped_existing={skipped_existing} "
        f"inserted={inserted} skipped_duplicate={skipped_duplicate} "
        f"skipped_no_bid={skipped_no_bid} failed={failed} elapsed={elapsed:.0f}s",
        flush=True,
    )


def main() -> None:
    try:
        run()
    except Exception as exc:
        traceback.print_exc()
        print(f"job failed: {exc}", flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
