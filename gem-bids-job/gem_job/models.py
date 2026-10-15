from __future__ import annotations

from datetime import datetime, timezone

from pydantic import AliasChoices, BaseModel, Field, field_validator


class ExtractedFields(BaseModel):
    bid_no: str | None = None
    ra_no: str | None = None
    bid_end_date: str | None = None
    bid_opening_date: str | None = None
    bid_offer_validity_days: int | None = None
    ministry_state_name: str | None = None
    department_name: str | None = None
    organisation_name: str | None = None
    office_name: str | None = None
    hod_email: str | None = None
    buyer_email: str | None = None
    item_category: str | None = None
    contract_period: str | None = None
    min_avg_annual_turnover: str | None = None
    years_past_experience_required: str | None = None
    past_experience_required: str | None = None
    msme_relaxation: str | None = None
    startup_relaxation: str | None = None
    searched_strings: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "searched_strings", "Searched Strings used in GeMARPTS"
        ),
    )
    searched_result: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "searched_result", "Searched Result generated in GeMARPTS"
        ),
    )
    emd_amount: float | None = Field(
        default=None,
        validation_alias=AliasChoices("emd_amount", "EMD Amount"),
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @field_validator(
        "bid_no",
        "ra_no",
        "bid_end_date",
        "bid_opening_date",
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
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {
            "",
            "null",
            "none",
            "n/a",
            "na",
            "-",
            "—",
        }:
            return None
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("emd_amount", "bid_offer_validity_days", mode="before")
    @classmethod
    def coerce_number(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            if not cleaned or cleaned.lower() in {"null", "none", "n/a", "na"}:
                return None
            return cleaned
        return value

    def to_db_row(self, tender_id: str, fallback_bid_no: str | None = None) -> dict:
        data = self.model_dump(by_alias=False)
        data["tender_id"] = tender_id
        data["bid_no"] = data.get("bid_no") or fallback_bid_no
        data["source_pdf"] = f"https://bidplus.gem.gov.in/showbidDocument/{tender_id}"
        data["status"] = "extracted"
        data["error"] = None
        data["extracted_at"] = datetime.now(timezone.utc).isoformat()
        return data
