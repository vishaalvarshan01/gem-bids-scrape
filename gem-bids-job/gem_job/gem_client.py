"""List ongoing GeM bids and download each PDF into memory."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from gem_job.pdf_extract import extract_fields_from_pdf

BASE_URL = "https://bidplus.gem.gov.in"
ALL_BIDS_PAGE = f"{BASE_URL}/all-bids"
ALL_BIDS_DATA = f"{BASE_URL}/all-bids-data"
DOCUMENT_URL = f"{BASE_URL}/showbidDocument"

DOWNLOAD_ATTEMPTS = 6
RETRY_STATUSES = {403, 429, 500, 502, 503}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_session: requests.Session | None = None
_shared_csrf = ""
_thread_state = threading.local()


def source_pdf_url(tender_id: str) -> str:
    return f"{DOCUMENT_URL}/{tender_id}"


def _csrf_token(session: requests.Session) -> str:
    values = [cookie.value for cookie in session.cookies if cookie.name == "csrf_gem_cookie" and cookie.value]
    return values[-1] if values else ""


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-IN,en;q=0.9",
    }


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_headers())
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            home = session.get(ALL_BIDS_PAGE, timeout=30)
            if home.status_code in {403, 429, 500, 502, 503}:
                time.sleep(attempt * 2)
                continue
            home.raise_for_status()
            if _csrf_token(session):
                return session
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(attempt * 2)
    raise RuntimeError("Could not open the GeM bids page") from last_error


def init_worker() -> None:
    global _session
    _session = _new_session()


def _listing_bid(doc: dict) -> tuple[str, str | None] | None:
    bid_id = doc.get("id")
    if bid_id is None:
        raw = doc.get("b_id")
        bid_id = raw[0] if isinstance(raw, list) and raw else raw
    if bid_id is None:
        return None
    bid_no_raw = doc.get("b_bid_number")
    if isinstance(bid_no_raw, list):
        bid_no = str(bid_no_raw[0]) if bid_no_raw else None
    elif bid_no_raw:
        bid_no = str(bid_no_raw)
    else:
        bid_no = None
    return str(bid_id), bid_no


def _session_refresh(session: requests.Session) -> None:
    try:
        session.get(ALL_BIDS_PAGE, timeout=30)
    except requests.RequestException:
        return


def listing_page(session: requests.Session, page: int) -> tuple[list[tuple[str, str | None]], int, int]:
    payload = {
        "page": str(page),
        "param": {"searchBid": "", "searchType": "fullText"},
        "filter": {
            "sort": "Bid-End-Date-Latest",
            "bidStatusType": "ongoing_bids",
            "byType": "all_type",
        },
    }
    last_error: Exception | None = None
    for attempt in range(1, 5):
        try:
            response = session.post(
                ALL_BIDS_DATA,
                data={
                    "payload": json.dumps(payload, separators=(",", ":")),
                    "csrf_bd_gem_nk": _csrf_token(session),
                },
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": ALL_BIDS_PAGE,
                    "Origin": BASE_URL,
                },
                timeout=30,
            )
            if response.status_code in {403, 429, 500, 502, 503}:
                time.sleep(attempt * 2)
                _session_refresh(session)
                continue
            response.raise_for_status()
            body = response.json().get("response", {}).get("response", {}) or {}
            docs = body.get("docs") or []
            ids = [bid for doc in docs if (bid := _listing_bid(doc)) is not None]
            num_found = int(body.get("numFound") or 0)
            start = int(body.get("start") or 0)
            return ids, num_found, start + len(docs)
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(attempt)
            _session_refresh(session)
    raise RuntimeError(f"listing page {page} failed") from last_error


def _thread_session() -> requests.Session:
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(_headers())
        if _shared_csrf:
            session.cookies.set("csrf_gem_cookie", _shared_csrf, domain="bidplus.gem.gov.in", path="/")
        _thread_state.session = session
    return session


def _fetch_listing_page(page: int) -> tuple[int, list[tuple[str, str | None]], int]:
    page_ids, num_found, _covered = listing_page(_thread_session(), page)
    return page, page_ids, num_found


def collect_tender_ids(listing_workers: int) -> list[tuple[str, str | None]]:
    global _shared_csrf
    session = _new_session()
    try:
        _shared_csrf = _csrf_token(session)
        first_ids, num_found, _covered = listing_page(session, 1)
        page_size = max(len(first_ids), 1)
        total_pages = max(1, (num_found + page_size - 1) // page_size) if num_found else 1
        print(
            f"listing 1/{total_pages}: {len(first_ids)} ids, {num_found} ongoing bids",
            flush=True,
        )
        pages: dict[int, list[tuple[str, str | None]]] = {1: first_ids}
        if total_pages > 1:
            workers = min(listing_workers, total_pages - 1)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_fetch_listing_page, page) for page in range(2, total_pages + 1)]
                done = 1
                for future in as_completed(futures):
                    page, page_ids, _found = future.result()
                    pages[page] = page_ids
                    done += 1
                    if done % 100 == 0 or done == total_pages:
                        print(f"listing {done}/{total_pages} pages fetched", flush=True)

        ids: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for page in range(1, total_pages + 1):
            for tender_id, bid_no in pages.get(page, []):
                if tender_id in seen:
                    continue
                seen.add(tender_id)
                ids.append((tender_id, bid_no))
        return ids
    finally:
        session.close()


def _backoff(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 30))


def _download_pdf(session: requests.Session, url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            response = session.get(
                url,
                headers={"Accept": "application/pdf,*/*", "Referer": ALL_BIDS_PAGE},
                timeout=60,
            )
            if response.status_code in RETRY_STATUSES:
                response.close()
                last_error = RuntimeError(f"{response.status_code} Server Error for url: {url}")
                _backoff(attempt)
                _session_refresh(session)
                continue
            response.raise_for_status()
            pdf_bytes = response.content
            response.close()
            if pdf_bytes.startswith(b"%PDF"):
                return pdf_bytes
            last_error = RuntimeError("response is not a PDF" if pdf_bytes else "response is empty")
            _backoff(attempt)
            _session_refresh(session)
        except requests.RequestException as exc:
            last_error = exc
            _backoff(attempt)
            _session_refresh(session)
    raise RuntimeError(f"download failed for {url}") from last_error


def process_tender(tender_id: str, bid_no: str | None, max_pages: int) -> dict:
    """Download one PDF into memory, extract fields, then drop the bytes."""
    global _session
    if _session is None:
        _session = _new_session()
    pdf_bytes: bytes | None = None
    try:
        pdf_bytes = _download_pdf(_session, source_pdf_url(tender_id))
        fields = extract_fields_from_pdf(
            pdf_bytes, max_pages=max_pages, fallback_bid_no=bid_no
        )
        return {"tender_id": tender_id, "error": None, "fields": fields.model_dump()}
    except Exception as exc:
        return {"tender_id": tender_id, "error": str(exc), "fields": None}
    finally:
        pdf_bytes = None
