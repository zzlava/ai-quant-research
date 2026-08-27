"""A-share securities transaction stamp-tax factual schedule (E10f-0).

Offline sealed cost contract for the declared 2022-01-01..2024-12-31 research
window. Does not wire production/backtest engines and does not modify the
tranche-evaluation protocol cash-occupancy / stamp-tax blocker flags.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["1"] = "1"
CONTRACT_VERSION: Literal["a-share-stamp-tax-schedule-v1"] = "a-share-stamp-tax-schedule-v1"
DEFAULT_A_SHARE_STAMP_TAX_SCHEDULE_PATH = Path("config/research/a-share-stamp-tax-schedule-v1.json")
BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH: Literal["config/research/a-share-stamp-tax-schedule-v1.json"] = (
    "config/research/a-share-stamp-tax-schedule-v1.json"
)

EXPECTED_CURRENT_CONTRACT_ID = "a5bfd11fec6543b58c761996781ac584e69e4263bc9688281bf4e1f0187735f0"

CONFIRMATION_AS_OF = date(2026, 8, 26)
VERIFIED_THROUGH = date(2026, 8, 26)
# Offline evidence-review timestamp recorded during factual verification (not live crawl).
EVIDENCE_ACCESSED_AT = datetime(2026, 8, 26, 6, 54, 52, tzinfo=UTC)
EVIDENCE_ACCESSED_AT_DEADLINE = datetime(2026, 8, 27, 0, 0, 0, tzinfo=UTC)
DECLARED_WINDOW_START = date(2022, 1, 1)
DECLARED_WINDOW_END = date(2024, 12, 31)
SCHEDULE_COVERAGE_START = date(2008, 9, 19)
BAND_ONE_END = date(2023, 8, 27)
BAND_TWO_START = date(2023, 8, 28)
RATE_BAND_ONE_SELLER = 0.001
RATE_BAND_TWO_SELLER = 0.0005
BUYER_RATE = 0.0

BOUND_EVIDENCE_SOURCE_COUNT = 4
BOUND_EVIDENCE_RECORDS: tuple[tuple[str, str, str, EvidenceRole, date], ...] = (
    (
        "mof_2008_unilateral_levy",
        "http://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/200809/t20080919_76432.htm",
        "MOF-2008-09-19-unilateral-stamp-tax",
        "establishes_seller_only_levy",
        date(2008, 9, 19),
    ),
    (
        "sta_stamp_tax_law_2022",
        "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html",
        "PRC-Stamp-Tax-Law-effective-2022-07-01",
        "reaffirms_seller_only_and_statutory_rate",
        date(2022, 7, 1),
    ),
    (
        "mof_sta_announcement_2023_39",
        "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html",
        "MOF-STA-Announcement-2023-No.39",
        "halves_seller_rate",
        date(2023, 8, 28),
    ),
    (
        "shanghai_tax_2023_39_validity_mirror",
        "https://shanghai.chinatax.gov.cn/zcfw/zcfgk/yhs/202308/t468451.html",
        "Shanghai-Tax-Bureau-2023-No.39-validity-mirror",
        "validity_mirror",
        date(2023, 8, 28),
    ),
)

TradeSide = Literal["buy", "sell"]
EvidenceRole = Literal[
    "establishes_seller_only_levy",
    "reaffirms_seller_only_and_statutory_rate",
    "halves_seller_rate",
    "validity_mirror",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RATE_ABS_TOL = 1e-15


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_literal_true(value: object, *, field_name: str) -> Literal[True]:
    if value is not True:
        raise ValueError(f"{field_name} must be the boolean True")
    return True


def _require_literal_false(value: object, *, field_name: str) -> Literal[False]:
    if value is not False:
        raise ValueError(f"{field_name} must be the boolean False")
    return False


def _require_strict_bool(value: object, *, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _require_real_number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = 0.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a real number (bool rejected)")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return number


def _require_date(value: object, *, field_name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and value.strip():
        return date.fromisoformat(value.strip())
    raise ValueError(f"{field_name} must be a datetime.date")


def _require_aware_datetime(value: object, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    else:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


def _rates_equal(left: float, right: float) -> bool:
    return abs(left - right) <= _RATE_ABS_TOL


class DateWindow(_StrictModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_date(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _order(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError("window end must be on or after start")
        return self


class StampTaxScheduleBand(_StrictModel):
    effective_from: date
    effective_to: date | None
    seller_rate: float
    buyer_rate: float
    open_ended: bool

    @field_validator("effective_from", mode="before")
    @classmethod
    def _from(cls, value: object) -> date:
        return _require_date(value, field_name="effective_from")

    @field_validator("effective_to", mode="before")
    @classmethod
    def _to(cls, value: object) -> date | None:
        if value is None:
            return None
        return _require_date(value, field_name="effective_to")

    @field_validator("seller_rate", "buyer_rate", mode="before")
    @classmethod
    def _rates(cls, value: object, info: Any) -> float:
        return _require_real_number(value, field_name=str(info.field_name), minimum=0.0)

    @field_validator("open_ended", mode="before")
    @classmethod
    def _open(cls, value: object) -> bool:
        return _require_strict_bool(value, field_name="open_ended")

    @model_validator(mode="after")
    def _gate(self) -> StampTaxScheduleBand:
        if self.open_ended:
            if self.effective_to is not None:
                raise ValueError("open-ended band must keep effective_to null")
        else:
            if self.effective_to is None:
                raise ValueError("closed band requires effective_to")
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to must be on or after effective_from")
        if not _rates_equal(self.buyer_rate, BUYER_RATE):
            raise ValueError("buyer_rate must be exactly 0.0 in every band")
        return self

    def contains(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        if self.open_ended or self.effective_to is None:
            return True
        return day <= self.effective_to


class StampTaxEvidenceSource(_StrictModel):
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    document_identifier: str = Field(min_length=1)
    evidence_role: EvidenceRole
    published_or_effective_date: date
    accessed_at: datetime
    notes: str = Field(min_length=1)

    @field_validator("published_or_effective_date", mode="before")
    @classmethod
    def _pub(cls, value: object) -> date:
        return _require_date(value, field_name="published_or_effective_date")

    @field_validator("accessed_at", mode="before")
    @classmethod
    def _accessed(cls, value: object) -> datetime:
        return _require_aware_datetime(value, field_name="accessed_at")


class StampTaxReaffirmationMilestone(_StrictModel):
    milestone_id: str = Field(min_length=1)
    milestone_date: date
    does_not_create_new_rate_band: Literal[True] = True
    evidence_source_id: str = Field(min_length=1)
    notes: str = Field(min_length=1)

    @field_validator("milestone_date", mode="before")
    @classmethod
    def _day(cls, value: object) -> date:
        return _require_date(value, field_name="milestone_date")

    @field_validator("does_not_create_new_rate_band", mode="before")
    @classmethod
    def _true(cls, value: object) -> object:
        return _require_literal_true(value, field_name="does_not_create_new_rate_band")


class AShareStampTaxScheduleContract(_StrictModel):
    schema_version: Literal["1"] = SCHEMA_VERSION
    contract_version: Literal["a-share-stamp-tax-schedule-v1"] = CONTRACT_VERSION
    contract_id: str | None = Field(default=None, pattern=_HEX64.pattern)
    confirmation_as_of: date
    verified_through: date
    declared_window: DateWindow
    schedule_coverage_start: date
    schedule_bands: list[StampTaxScheduleBand]
    evidence_sources: list[StampTaxEvidenceSource]
    reaffirmation_milestones: list[StampTaxReaffirmationMilestone]
    seller_only: Literal[True] = True
    taxable_basis: Literal["transaction_amount"] = "transaction_amount"
    buyer_rate_always_zero_within_coverage: Literal[True] = True
    no_claims_before_schedule_coverage_start: Literal[True] = True
    no_guessing_beyond_evidence: Literal[True] = True
    legal_open_ended_band_does_not_authorize_extrapolation_past_verified_through: Literal[True] = True
    evidence_accessed_at_is_offline_review_timestamp: Literal[True] = True
    factual_cost_contract_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    ready_for_orders: Literal[False] = False
    auto_apply: Literal[False] = False
    existing_tranche_protocol_blocker_not_modified: Literal[True] = True

    @field_validator("confirmation_as_of", "verified_through", "schedule_coverage_start", mode="before")
    @classmethod
    def _dates(cls, value: object, info: Any) -> date:
        return _require_date(value, field_name=str(info.field_name))

    @field_validator(
        "seller_only",
        "buyer_rate_always_zero_within_coverage",
        "no_claims_before_schedule_coverage_start",
        "no_guessing_beyond_evidence",
        "legal_open_ended_band_does_not_authorize_extrapolation_past_verified_through",
        "evidence_accessed_at_is_offline_review_timestamp",
        "factual_cost_contract_only",
        "existing_tranche_protocol_blocker_not_modified",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_trading",
        "ready_for_orders",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _gate(self) -> AShareStampTaxScheduleContract:
        if self.confirmation_as_of != CONFIRMATION_AS_OF:
            raise ValueError("confirmation_as_of must equal 2026-08-26")
        if self.verified_through != VERIFIED_THROUGH:
            raise ValueError("verified_through must equal sealed 2026-08-26")
        if self.declared_window.start != DECLARED_WINDOW_START or self.declared_window.end != DECLARED_WINDOW_END:
            raise ValueError("declared_window must be 2022-01-01..2024-12-31")
        if self.schedule_coverage_start != SCHEDULE_COVERAGE_START:
            raise ValueError("schedule_coverage_start must be 2008-09-19")
        if self.declared_window.start < self.schedule_coverage_start:
            raise ValueError("declared_window start precedes schedule_coverage_start")
        if self.declared_window.end > self.verified_through:
            raise ValueError("declared_window must not extend past verified_through")
        if len(self.schedule_bands) < 1:
            raise ValueError("schedule_bands must be non-empty")
        _assert_contiguous_bands(self.schedule_bands)
        if not _window_fully_covered(self.declared_window, self.schedule_bands):
            raise ValueError("declared_window must be fully covered by schedule bands")
        _assert_sealed_evidence_sources(self.evidence_sources)
        source_ids = [source.source_id for source in self.evidence_sources]
        if len(self.reaffirmation_milestones) != 1:
            raise ValueError("exactly one reaffirmation milestone required (2022-07-01 law)")
        milestone = self.reaffirmation_milestones[0]
        if milestone.milestone_date != date(2022, 7, 1):
            raise ValueError("reaffirmation milestone must be 2022-07-01")
        if milestone.evidence_source_id not in source_ids:
            raise ValueError("reaffirmation milestone must reference an evidence source_id")
        return self


class AShareStampTaxScheduleVerificationResult(_StrictModel):
    contract_id: str
    structural_ok: bool
    disk_binding_ok: bool = False
    ready_for_exit_diagnostic: bool = False
    factual_cost_contract_only: Literal[True] = True
    ready_for_scoring: Literal[False] = False
    ready_for_backtest: Literal[False] = False
    ready_for_trading: Literal[False] = False
    ready_for_orders: Literal[False] = False
    auto_apply: Literal[False] = False
    existing_tranche_protocol_blocker_not_modified: Literal[True] = True

    @field_validator("structural_ok", "disk_binding_ok", "ready_for_exit_diagnostic", mode="before")
    @classmethod
    def _plain(cls, value: object, info: Any) -> bool:
        return _require_strict_bool(value, field_name=str(info.field_name))

    @field_validator(
        "factual_cost_contract_only",
        "existing_tranche_protocol_blocker_not_modified",
        mode="before",
    )
    @classmethod
    def _true(cls, value: object, info: Any) -> object:
        return _require_literal_true(value, field_name=str(info.field_name))

    @field_validator(
        "ready_for_scoring",
        "ready_for_backtest",
        "ready_for_trading",
        "ready_for_orders",
        "auto_apply",
        mode="before",
    )
    @classmethod
    def _false(cls, value: object, info: Any) -> object:
        return _require_literal_false(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def _binding(self) -> AShareStampTaxScheduleVerificationResult:
        if self.ready_for_exit_diagnostic and not (self.structural_ok and self.disk_binding_ok):
            raise ValueError("ready_for_exit_diagnostic requires structural_ok and disk_binding_ok")
        if self.disk_binding_ok and not self.structural_ok:
            raise ValueError("disk_binding_ok requires structural_ok")
        return self


def _assert_contiguous_bands(bands: list[StampTaxScheduleBand]) -> None:
    if not bands:
        raise ValueError("schedule_bands must be non-empty")
    from_dates = [band.effective_from for band in bands]
    if from_dates != sorted(from_dates) or len(from_dates) != len(set(from_dates)):
        raise ValueError("schedule_bands must be strictly increasing by unique effective_from")
    open_ended_count = sum(1 for band in bands if band.open_ended)
    if open_ended_count != 1 or not bands[-1].open_ended:
        raise ValueError("exactly one open-ended band is required and it must be last")
    for index in range(1, len(bands)):
        previous = bands[index - 1]
        current = bands[index]
        if previous.open_ended or previous.effective_to is None:
            raise ValueError("only the final band may be open-ended")
        expected_next = previous.effective_to.toordinal() + 1
        if current.effective_from.toordinal() != expected_next:
            raise ValueError("schedule bands must be contiguous without gap or overlap")


def _assert_sealed_evidence_sources(sources: list[StampTaxEvidenceSource]) -> None:
    if len(sources) != BOUND_EVIDENCE_SOURCE_COUNT:
        raise ValueError(f"evidence_sources must contain exactly {BOUND_EVIDENCE_SOURCE_COUNT} sealed records")
    accessed_values = [source.accessed_at.astimezone(UTC) for source in sources]
    if len(set(accessed_values)) != 1:
        raise ValueError("all evidence accessed_at timestamps must be identical")
    accessed = accessed_values[0]
    if accessed != EVIDENCE_ACCESSED_AT:
        raise ValueError("evidence accessed_at must equal sealed offline review timestamp 2026-08-26T06:54:52Z")
    if accessed > EVIDENCE_ACCESSED_AT_DEADLINE:
        raise ValueError("evidence accessed_at must not be later than confirmation_as_of next-day UTC midnight")
    for source, bound in zip(sources, BOUND_EVIDENCE_RECORDS, strict=True):
        source_id, url, document_identifier, evidence_role, published = bound
        if source.source_id != source_id:
            raise ValueError(f"sealed evidence source_id mismatch for {source_id}")
        if source.url != url:
            raise ValueError(f"sealed evidence url mismatch for {source_id}")
        if source.document_identifier != document_identifier:
            raise ValueError(f"sealed evidence document_identifier mismatch for {source_id}")
        if source.evidence_role != evidence_role:
            raise ValueError(f"sealed evidence_role mismatch for {source_id}")
        if source.published_or_effective_date != published:
            raise ValueError(f"sealed published_or_effective_date mismatch for {source_id}")


def _window_fully_covered(window: DateWindow, bands: list[StampTaxScheduleBand]) -> bool:
    cursor = window.start
    while cursor <= window.end:
        if not any(band.contains(cursor) for band in bands):
            return False
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return True


def _band_for(day: date, bands: list[StampTaxScheduleBand]) -> StampTaxScheduleBand:
    matches = [band for band in bands if band.contains(day)]
    if len(matches) != 1:
        raise ValueError("trade_date is outside schedule coverage or ambiguous")
    return matches[0]


def stamp_tax_rate_for(
    trade_date: date,
    side: TradeSide,
    *,
    contract: AShareStampTaxScheduleContract | None = None,
) -> float:
    """Return the applicable stamp-tax rate; fail closed outside coverage.

    Any supplied (or default factory) contract must pass full structural/factory
    verification before rates are read. Self-hash alone is insufficient: seller_rate
    is not model-sealed and can otherwise be resealed under a forged payload.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    day = _require_date(trade_date, field_name="trade_date")
    use = contract if contract is not None else build_a_share_stamp_tax_schedule_v1()
    # Full canonical/factory verify — not assert_contract_self_hash alone.
    # verify_a_share_stamp_tax_schedule must never call this helper (no recursion).
    verify_a_share_stamp_tax_schedule(use)
    if day < use.schedule_coverage_start:
        raise ValueError("trade_date precedes schedule coverage start (no claims before 2008-09-19)")
    if day > use.verified_through:
        raise ValueError(
            "trade_date exceeds verified_through "
            "(legal open-ended band does not authorize extrapolation past evidence review)"
        )
    band = _band_for(day, use.schedule_bands)
    return float(band.buyer_rate if side == "buy" else band.seller_rate)


def stamp_tax_amount(
    *,
    transaction_amount: float,
    trade_date: date,
    side: TradeSide,
    contract: AShareStampTaxScheduleContract | None = None,
) -> float:
    """Compute stamp tax from finite nonnegative notional and covered trade date."""
    amount = _require_real_number(transaction_amount, field_name="transaction_amount", minimum=0.0)
    rate = stamp_tax_rate_for(trade_date, side, contract=contract)
    tax = amount * rate
    if side == "buy":
        if not _rates_equal(tax, 0.0):
            raise ValueError("buy-side stamp tax must be exactly 0 within coverage")
        return 0.0
    return tax


def canonical_contract_payload(contract: AShareStampTaxScheduleContract) -> dict[str, Any]:
    return contract.model_dump(mode="json", exclude={"contract_id"})


def canonical_contract_bytes(contract: AShareStampTaxScheduleContract) -> bytes:
    return json.dumps(
        canonical_contract_payload(contract),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_contract_id(contract: AShareStampTaxScheduleContract) -> str:
    return hashlib.sha256(canonical_contract_bytes(contract)).hexdigest()


def seal_a_share_stamp_tax_schedule(
    contract: AShareStampTaxScheduleContract,
) -> AShareStampTaxScheduleContract:
    return contract.model_copy(update={"contract_id": compute_contract_id(contract)})


def assert_contract_self_hash(contract: AShareStampTaxScheduleContract) -> None:
    if contract.contract_id is None:
        raise ValueError("stamp tax schedule contract_id is missing")
    if contract.contract_id != compute_contract_id(contract):
        raise ValueError("stamp tax schedule contract_id does not match canonical content hash")


def build_a_share_stamp_tax_schedule_v1() -> AShareStampTaxScheduleContract:
    accessed = EVIDENCE_ACCESSED_AT
    contract = AShareStampTaxScheduleContract(
        confirmation_as_of=CONFIRMATION_AS_OF,
        verified_through=VERIFIED_THROUGH,
        declared_window=DateWindow(start=DECLARED_WINDOW_START, end=DECLARED_WINDOW_END),
        schedule_coverage_start=SCHEDULE_COVERAGE_START,
        schedule_bands=[
            StampTaxScheduleBand(
                effective_from=SCHEDULE_COVERAGE_START,
                effective_to=BAND_ONE_END,
                seller_rate=RATE_BAND_ONE_SELLER,
                buyer_rate=BUYER_RATE,
                open_ended=False,
            ),
            StampTaxScheduleBand(
                effective_from=BAND_TWO_START,
                effective_to=None,
                seller_rate=RATE_BAND_TWO_SELLER,
                buyer_rate=BUYER_RATE,
                open_ended=True,
            ),
        ],
        evidence_sources=[
            StampTaxEvidenceSource(
                source_id="mof_2008_unilateral_levy",
                title="9月19日起证券交易印花税改为单边征收",
                url="http://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/200809/t20080919_76432.htm",
                document_identifier="MOF-2008-09-19-unilateral-stamp-tax",
                evidence_role="establishes_seller_only_levy",
                published_or_effective_date=date(2008, 9, 19),
                accessed_at=accessed,
                notes=(
                    "PRC Ministry of Finance official notice: from 2008-09-19 A/B share "
                    "transfer seller taxed at 0.001; buyer no longer taxed. "
                    "accessed_at is the offline evidence-review timestamp, not a live crawl."
                ),
            ),
            StampTaxEvidenceSource(
                source_id="sta_stamp_tax_law_2022",
                title="中华人民共和国印花税法",
                url="https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193058/content.html",
                document_identifier="PRC-Stamp-Tax-Law-effective-2022-07-01",
                evidence_role="reaffirms_seller_only_and_statutory_rate",
                published_or_effective_date=date(2022, 7, 1),
                accessed_at=accessed,
                notes=(
                    "Passed 2021-06-10, effective 2022-07-01. Securities transaction stamp "
                    "tax levied on transferor not transferee; taxable basis transaction "
                    "amount; statutory table rate 0.001. Reaffirms rather than changes the "
                    "first-band applicable rate. accessed_at is offline evidence-review time."
                ),
            ),
            StampTaxEvidenceSource(
                source_id="mof_sta_announcement_2023_39",
                title="财政部 税务总局公告2023年第39号",
                url="https://fgk.chinatax.gov.cn/zcfgk/c102416/c5211343/content.html",
                document_identifier="MOF-STA-Announcement-2023-No.39",
                evidence_role="halves_seller_rate",
                published_or_effective_date=date(2023, 8, 28),
                accessed_at=accessed,
                notes=(
                    "Dated 2023-08-27, effective 2023-08-28: securities transaction stamp "
                    "tax reduced by half (seller_rate 0.0005). "
                    "accessed_at is offline evidence-review time."
                ),
            ),
            StampTaxEvidenceSource(
                source_id="shanghai_tax_2023_39_validity_mirror",
                title="财政部 税务总局公告2023年第39号（上海税务局转载·全文有效）",
                url="https://shanghai.chinatax.gov.cn/zcfw/zcfgk/yhs/202308/t468451.html",
                document_identifier="Shanghai-Tax-Bureau-2023-No.39-validity-mirror",
                evidence_role="validity_mirror",
                published_or_effective_date=date(2023, 8, 28),
                accessed_at=accessed,
                notes=(
                    "Optional official validity mirror marking Announcement 2023 No.39 全文有效. "
                    "accessed_at is offline evidence-review time."
                ),
            ),
        ],
        reaffirmation_milestones=[
            StampTaxReaffirmationMilestone(
                milestone_id="stamp_tax_law_effective_2022_07_01",
                milestone_date=date(2022, 7, 1),
                evidence_source_id="sta_stamp_tax_law_2022",
                notes=(
                    "Stamp Tax Law effective date is reaffirmation evidence only; it must "
                    "not invent a new rate band inside 2008-09-19..2023-08-27."
                ),
            )
        ],
    )
    return seal_a_share_stamp_tax_schedule(contract)


def verify_a_share_stamp_tax_schedule(
    contract: AShareStampTaxScheduleContract,
) -> AShareStampTaxScheduleVerificationResult:
    """Structural verifier: self-hash + sealed evidence/window identities; no disk binding."""
    assert_contract_self_hash(contract)
    if contract.verified_through != VERIFIED_THROUGH:
        raise ValueError("verified_through must equal sealed operational cutoff 2026-08-26")
    if (
        contract.declared_window.start < contract.schedule_coverage_start
        or contract.declared_window.end > contract.verified_through
    ):
        raise ValueError("declared_window must lie within schedule_coverage_start..verified_through")
    _assert_sealed_evidence_sources(contract.evidence_sources)
    expected = build_a_share_stamp_tax_schedule_v1()
    if canonical_contract_payload(contract) != canonical_contract_payload(expected):
        raise ValueError("stamp tax schedule canonical payload does not match factory contract")
    if contract.contract_id != expected.contract_id:
        raise ValueError("stamp tax schedule contract_id does not match factory seal")
    # Ready flags are constructed here only; never accept caller-supplied ready booleans.
    return AShareStampTaxScheduleVerificationResult(
        contract_id=contract.contract_id or compute_contract_id(contract),
        structural_ok=True,
        disk_binding_ok=False,
        ready_for_exit_diagnostic=False,
    )


def verify_a_share_stamp_tax_schedule_file(
    *,
    repo_root: Path,
    contract_path: Path | None = None,
) -> tuple[AShareStampTaxScheduleContract, AShareStampTaxScheduleVerificationResult]:
    """File verifier: structural path + fixed default repo path + expected contract_id."""
    root = Path(repo_root).resolve()
    relative = BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH
    if str(DEFAULT_A_SHARE_STAMP_TAX_SCHEDULE_PATH) != relative:
        raise ValueError("default stamp tax schedule path drifted from bound path")
    path = Path(contract_path) if contract_path is not None else root / relative
    if not path.is_file():
        raise ValueError("a-share stamp tax schedule file missing")
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("stamp tax schedule path must be inside repo_root") from exc
    if path.resolve() != (root / relative).resolve():
        raise ValueError("stamp tax schedule file verifier requires the fixed default repo path")
    contract = load_a_share_stamp_tax_schedule(path)
    structural = verify_a_share_stamp_tax_schedule(contract)
    if structural.structural_ok is not True:
        raise ValueError("structural verifier must succeed before file binding")
    if structural.disk_binding_ok is not False or structural.ready_for_exit_diagnostic is not False:
        raise ValueError("structural verifier must not claim disk binding or exit readiness")
    if contract.contract_id != EXPECTED_CURRENT_CONTRACT_ID:
        raise ValueError("on-disk contract_id does not match EXPECTED_CURRENT_CONTRACT_ID")
    if contract.contract_id != build_a_share_stamp_tax_schedule_v1().contract_id:
        raise ValueError("on-disk contract_id does not match sealed factory contract")
    # Construct ready result only after complete file verification; do not trust caller flags.
    return contract, AShareStampTaxScheduleVerificationResult(
        contract_id=contract.contract_id or compute_contract_id(contract),
        structural_ok=True,
        disk_binding_ok=True,
        ready_for_exit_diagnostic=True,
    )


def load_a_share_stamp_tax_schedule(path: Path) -> AShareStampTaxScheduleContract:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("a-share stamp tax schedule is missing or invalid") from exc
    try:
        return AShareStampTaxScheduleContract.model_validate(payload)
    except Exception as exc:
        raise ValueError("a-share stamp tax schedule is missing or invalid") from exc


def write_a_share_stamp_tax_schedule(
    path: Path,
    contract: AShareStampTaxScheduleContract,
) -> AShareStampTaxScheduleContract:
    sealed = seal_a_share_stamp_tax_schedule(contract)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sealed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return sealed


_FACTORY_CONTRACT_ID = build_a_share_stamp_tax_schedule_v1().contract_id
if _FACTORY_CONTRACT_ID != EXPECTED_CURRENT_CONTRACT_ID:
    raise RuntimeError("EXPECTED_CURRENT_CONTRACT_ID drifted from factory seal")


__all__ = [
    "BOUND_A_SHARE_STAMP_TAX_SCHEDULE_PATH",
    "BOUND_EVIDENCE_RECORDS",
    "BOUND_EVIDENCE_SOURCE_COUNT",
    "CONFIRMATION_AS_OF",
    "CONTRACT_VERSION",
    "DECLARED_WINDOW_END",
    "DECLARED_WINDOW_START",
    "DEFAULT_A_SHARE_STAMP_TAX_SCHEDULE_PATH",
    "EVIDENCE_ACCESSED_AT",
    "EXPECTED_CURRENT_CONTRACT_ID",
    "SCHEMA_VERSION",
    "VERIFIED_THROUGH",
    "AShareStampTaxScheduleContract",
    "AShareStampTaxScheduleVerificationResult",
    "DateWindow",
    "StampTaxEvidenceSource",
    "StampTaxReaffirmationMilestone",
    "StampTaxScheduleBand",
    "assert_contract_self_hash",
    "build_a_share_stamp_tax_schedule_v1",
    "canonical_contract_bytes",
    "canonical_contract_payload",
    "compute_contract_id",
    "load_a_share_stamp_tax_schedule",
    "seal_a_share_stamp_tax_schedule",
    "stamp_tax_amount",
    "stamp_tax_rate_for",
    "verify_a_share_stamp_tax_schedule",
    "verify_a_share_stamp_tax_schedule_file",
    "write_a_share_stamp_tax_schedule",
]
