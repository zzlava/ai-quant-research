#!/usr/bin/env python3
"""Create a verifiable AIQ *anchor-only* compatibility view of Claude's audit pack.

This tool is intentionally conservative.  It never reconstructs a missing
constituent period, never materializes daily membership, and never changes an
input file.  Its output is only a normalized representation of the pack's
already-direct 300-member anchors so that ``verify-universe-source`` can audit
their byte hashes and availability evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_COLUMNS = ("universe_id", "effective_from", "symbol", "available_at", "weight")
EVIDENCE_COLUMNS = (
    "effective_from",
    "available_at",
    "availability_basis",
    "source_published_on",
    "evidence_type",
    "source_url",
    "source_document",
    "source_document_sha256",
)
RAW_MANIFEST = "membership_source_manifest.json"
RAW_SNAPSHOTS = "csi300_snapshots.csv"
RAW_EVIDENCE = "event_evidence.csv"
OUTPUT_SNAPSHOTS = "aiq_anchor_snapshots.csv"
OUTPUT_EVIDENCE = "aiq_anchor_event_evidence.csv"
OUTPUT_MANIFEST = "aiq_anchor_membership_source_manifest.json"
OUTPUT_README = "AIQ_ANCHOR_COMPATIBILITY.md"


class CompatibilityError(ValueError):
    """A source package cannot be converted without weakening its evidence."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_csv(path: Path, expected_columns: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = tuple(reader.fieldnames or ())
            if columns != expected_columns:
                raise CompatibilityError(
                    f"{path.name} columns must be exactly {list(expected_columns)}, got {list(columns)}"
                )
            rows = list(reader)
    except UnicodeDecodeError as exc:
        raise CompatibilityError(f"{path.name} must be UTF-8") from exc
    if not rows:
        raise CompatibilityError(f"{path.name} has no rows")
    return rows


def _parse_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CompatibilityError(f"{field} must be an ISO date, got {value!r}") from exc


def _parse_timestamp(value: str, *, field: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CompatibilityError(f"{field} must be an ISO timestamp, got {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CompatibilityError(f"{field} must include an explicit UTC offset, got {value!r}")
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _format_timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec) + "Z"


def _read_raw_manifest(source_dir: Path) -> dict[str, Any]:
    path = source_dir / RAW_MANIFEST
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompatibilityError(f"{RAW_MANIFEST} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise CompatibilityError(f"{RAW_MANIFEST} must be a JSON object")
    for key in ("universe_id", "index_code", "generated_at_utc"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise CompatibilityError(f"{RAW_MANIFEST} missing non-empty {key!r}")
    return raw


def _read_snapshots(source_dir: Path, universe_id: str) -> tuple[list[dict[str, str]], dict[str, datetime]]:
    rows = _read_csv(source_dir / RAW_SNAPSHOTS, SNAPSHOT_COLUMNS)
    per_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    normalized_stamps: dict[str, datetime] = {}
    for line, row in enumerate(rows, start=2):
        if row["universe_id"] != universe_id:
            raise CompatibilityError(
                f"{RAW_SNAPSHOTS} line {line} universe_id {row['universe_id']!r} is not {universe_id!r}"
            )
        _parse_date(row["effective_from"], field=f"{RAW_SNAPSHOTS} line {line} effective_from")
        if len(row["symbol"]) != 9 or not row["symbol"][:6].isdigit() or row["symbol"][6:] not in {".SH", ".SZ"}:
            raise CompatibilityError(f"{RAW_SNAPSHOTS} line {line} has an invalid A-share symbol")
        key = (row["universe_id"], row["effective_from"], row["symbol"])
        if key in seen:
            raise CompatibilityError(f"{RAW_SNAPSHOTS} has duplicate primary key {key}")
        seen.add(key)
        stamp = _parse_timestamp(row["available_at"], field=f"{RAW_SNAPSHOTS} line {line} available_at")
        prior = normalized_stamps.setdefault(row["effective_from"], stamp)
        if prior != stamp:
            raise CompatibilityError(
                f"{RAW_SNAPSHOTS} effective_from={row['effective_from']} has mixed available_at values"
            )
        per_date[row["effective_from"]].append(row)
    for effective_from, members in per_date.items():
        if len(members) != 300:
            raise CompatibilityError(
                f"{RAW_SNAPSHOTS} effective_from={effective_from} has {len(members)} members, expected 300"
            )
    normalized_rows = [
        {
            **row,
            "available_at": _format_timestamp(normalized_stamps[row["effective_from"]]),
        }
        for row in sorted(rows, key=lambda item: (item["effective_from"], item["symbol"]))
    ]
    return normalized_rows, normalized_stamps


def _availability_basis(raw_type: str) -> str:
    if "wayback_mirror" in raw_type:
        return "observed_source_timestamp"
    if "announcement" in raw_type or "live" in raw_type:
        return "conservative_next_cn_decision_after_notice_date"
    raise CompatibilityError(f"cannot map raw evidence type {raw_type!r} to an availability basis")


def _evidence_type(raw_type: str) -> str:
    if "cons_file" in raw_type or "closeweight_file" in raw_type or "full_list" in raw_type:
        return "official_constituent_list"
    if "announcement" in raw_type:
        return "official_adjustment_notice"
    raise CompatibilityError(f"cannot map raw evidence type {raw_type!r} to an AIQ evidence type")


def _build_anchor_evidence(
    source_dir: Path, snapshot_stamps: dict[str, datetime]
) -> list[dict[str, str]]:
    raw_rows = _read_csv(source_dir / RAW_EVIDENCE, EVIDENCE_COLUMNS)
    matches: dict[str, list[dict[str, str]]] = defaultdict(list)
    for line, row in enumerate(raw_rows, start=2):
        effective_from = row["effective_from"].strip()
        if effective_from not in snapshot_stamps:
            continue
        stamp = _parse_timestamp(row["available_at"], field=f"{RAW_EVIDENCE} line {line} available_at")
        if stamp == snapshot_stamps[effective_from]:
            matches[effective_from].append(row)

    output: list[dict[str, str]] = []
    for effective_from in sorted(snapshot_stamps):
        candidates = matches[effective_from]
        if len(candidates) != 1:
            raise CompatibilityError(
                f"{RAW_EVIDENCE} must have exactly one source row matching anchor {effective_from}; "
                f"found {len(candidates)}"
            )
        row = candidates[0]
        source_document = source_dir / row["source_document"]
        if not source_document.is_file():
            raise CompatibilityError(f"anchor {effective_from} source document is missing: {row['source_document']}")
        if _sha256(source_document) != row["source_document_sha256"].lower():
            raise CompatibilityError(f"anchor {effective_from} source document SHA-256 does not match the raw ledger")
        available = snapshot_stamps[effective_from]
        basis = _availability_basis(row["evidence_type"])
        raw_published = row["source_published_on"].strip()
        if raw_published:
            published = _parse_date(raw_published, field=f"anchor {effective_from} source_published_on")
        elif basis == "observed_source_timestamp":
            # The legacy source records only the preserved Archive observation
            # timestamp, not an original publisher date.  Its UTC calendar day
            # is the auditable source-observation date, never a download time.
            published = available.date()
        else:
            raise CompatibilityError(f"anchor {effective_from} has no source_published_on evidence")
        if published > available.date():
            raise CompatibilityError(f"anchor {effective_from} has available_at before its source publication date")
        output.append(
            {
                "effective_from": effective_from,
                "available_at": _format_timestamp(available),
                "availability_basis": basis,
                "source_published_on": published.isoformat(),
                "evidence_type": _evidence_type(row["evidence_type"]),
                "source_url": row["source_url"],
                "source_document": row["source_document"],
                "source_document_sha256": row["source_document_sha256"].lower(),
            }
        )
    return output


def _csv_bytes(columns: tuple[str, ...], rows: list[dict[str, str]]) -> bytes:
    from io import StringIO

    handle = StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _write_bytes(path: Path, contents: bytes, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise CompatibilityError(f"refusing to overwrite {path.name}; pass --overwrite after reviewing it")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(contents)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def _readme_text() -> str:
    return """# AIQ anchor-only compatibility view

These files are a derived, UTC-normalized view of the direct 300-member anchors
in this directory's original Claude public-audit package.  The original files,
including `csi300_snapshots.csv`, `event_evidence.csv`, `gaps.csv`, and
`membership_source_manifest.json`, have not been altered.

## What this view proves

- Each of the 24 direct anchors has exactly 300 distinct A-share symbols.
- `aiq_anchor_event_evidence.csv` has exactly one audited source document for
  each anchor and records its exact source-byte SHA-256.
- Every `available_at` represents the same instant as the source package:
  source `+08:00` timestamps are converted to their equivalent UTC `Z` value;
  no download/retrieval time is substituted for historical availability.
- For legacy Wayback anchors without an original publisher date, the ledger's
  `source_published_on` is the preserved Archive observation's UTC calendar
  date and the availability basis is `observed_source_timestamp`; it is not a
  claim about the publisher's earlier release time.
- It can be checked with `python -m app.cli verify-universe-source`.

## Strict boundary

This is **not continuous CSI300 historical membership data** and must not be
passed to `build-universe-membership`, `import-market-data`, `preflight-research`,
`score`, or `backtest` for a continuous CSI300 study.  The source package's
`gaps.csv` records unobserved constituent periods, including the 2022--2024
research period.  Forward-filling these anchors through a missing adjustment
would create an unproven membership history.

Use this view only for source audit, anchor-date checks, and as a basis for
obtaining the missing official full constituent snapshots.  It remains a public
reconstruction and is not licensed point-in-time data.
"""


def create(source_dir: Path, *, overwrite: bool) -> list[Path]:
    source_dir = source_dir.resolve()
    raw_manifest = _read_raw_manifest(source_dir)
    if raw_manifest["universe_id"] != "csi300" or raw_manifest["index_code"] != "000300":
        raise CompatibilityError("this adapter only accepts the CSI300 (csi300 / 000300) audit package")
    generated_at = _parse_timestamp(raw_manifest["generated_at_utc"], field="generated_at_utc")
    snapshots, snapshot_stamps = _read_snapshots(source_dir, raw_manifest["universe_id"])
    evidence = _build_anchor_evidence(source_dir, snapshot_stamps)

    snapshot_path = source_dir / OUTPUT_SNAPSHOTS
    evidence_path = source_dir / OUTPUT_EVIDENCE
    manifest_path = source_dir / OUTPUT_MANIFEST
    readme_path = source_dir / OUTPUT_README
    snapshot_bytes = _csv_bytes(SNAPSHOT_COLUMNS, snapshots)
    evidence_bytes = _csv_bytes(EVIDENCE_COLUMNS, evidence)
    manifest = {
        "schema_version": "2",
        "universe_id": "csi300",
        "source_name": "public-reconstruction-anchor-only-not-licensed-pit",
        "snapshots_file_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "file_obtained_at": _format_timestamp(generated_at),
        "effective_from_coverage": {
            "start": min(snapshot_stamps),
            "end": max(snapshot_stamps),
        },
        "available_at_definition": (
            "Exact source timestamp for each direct 300-member anchor, normalized to UTC Z. "
            "A +08:00 source timestamp is converted to the same UTC instant; retrieval time is never used."
        ),
        "available_at_evidence": (
            "aiq_anchor_event_evidence.csv maps every direct anchor once to its preserved source document "
            "and exact SHA-256. For legacy Wayback anchors, source_published_on is the Archive observation UTC "
            "date because no original publisher date is present. The original event ledger remains event_evidence.csv."
        ),
        "expected_constituents": 300,
        "source_url": "https://www.csindex.com.cn/",
        "source_note": (
            "Anchor-only public reconstruction derived without filling gaps; it is not continuous CSI300 "
            "historical membership and not licensed point-in-time data. See AIQ_ANCHOR_COMPATIBILITY.md."
        ),
        "event_evidence_ledger": {
            "path": OUTPUT_EVIDENCE,
            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        },
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    outputs = [snapshot_path, evidence_path, manifest_path, readme_path]
    existing = [path.name for path in outputs if path.exists()]
    if existing and not overwrite:
        raise CompatibilityError(f"refusing to overwrite existing derived files: {', '.join(existing)}")
    _write_bytes(snapshot_path, snapshot_bytes, overwrite=overwrite)
    _write_bytes(evidence_path, evidence_bytes, overwrite=overwrite)
    _write_bytes(manifest_path, manifest_bytes, overwrite=overwrite)
    _write_bytes(readme_path, _readme_text().encode("utf-8"), overwrite=overwrite)
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="Claude CSI300 audit package directory")
    parser.add_argument("--overwrite", action="store_true", help="replace existing aiq_anchor_* derived files")
    args = parser.parse_args()
    try:
        outputs = create(args.source_dir, overwrite=args.overwrite)
    except CompatibilityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
