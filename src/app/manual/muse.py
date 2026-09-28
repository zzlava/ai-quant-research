"""No-root deployment helpers (cron instead of systemd), e.g. for a Muse Secure VM.

Schedules are defined in Asia/Shanghai market time and rendered into the machine's local
time zone, because plain cron has no per-line time zone support on every distribution.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHANGHAI = ZoneInfo("Asia/Shanghai")
BLOCK_BEGIN = "# BEGIN ai-quant-etf (managed by deploy/muse/install.sh)"
BLOCK_END = "# END ai-quant-etf"

# (job, Shanghai weekdays 0=Mon, hour, minute)
JOBS: tuple[tuple[str, tuple[int, ...], int, int], ...] = (
    ("repo", (0, 1, 2, 3, 4), 14, 40),
    ("daily", (0, 1, 2, 3, 4), 15, 20),
    ("weekly", (4,), 15, 40),
)


def _local_tz() -> ZoneInfo | None:
    """The machine's named time zone from $TZ or /etc/localtime, if it can be determined."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    timezone_file = Path("/etc/timezone")
    if timezone_file.exists():
        candidates.append(timezone_file.read_text(encoding="utf-8").strip())
    for key in candidates:
        if not key:
            continue
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def crontab_block(
    repo_dir: Path, *, local_tz: ZoneInfo | None = None, reference: date | None = None
) -> tuple[str, list[str]]:
    """Return (crontab block, warnings) with each Shanghai schedule converted to local cron time."""
    tz = local_tz or _local_tz()
    warnings: list[str] = []
    if tz is None:
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        tz_label = f"fixed offset {offset}"
        convert = lambda dt: (dt.astimezone(SHANGHAI).replace(tzinfo=None) - timedelta(hours=8) + offset)  # noqa: E731
    else:
        tz_label = str(tz)
        convert = lambda dt: dt.astimezone(tz).replace(tzinfo=None)  # noqa: E731
    ref = reference or datetime.now(SHANGHAI).date()
    monday = ref - timedelta(days=ref.weekday())
    lines = [BLOCK_BEGIN, f"# machine time zone: {tz_label}; schedules are Asia/Shanghai market times"]
    runner = Path(repo_dir) / "deploy" / "muse" / "run.sh"
    for job, weekdays, hour, minute in JOBS:
        local_days: set[int] = set()
        local_hm: set[tuple[int, int]] = set()
        for weekday in weekdays:
            market = datetime(monday.year, monday.month, monday.day, hour, minute, tzinfo=SHANGHAI) + timedelta(
                days=weekday
            )
            local = convert(market)
            local_days.add((local.weekday() + 1) % 7)  # cron: 0=Sun … 6=Sat
            local_hm.add((local.hour, local.minute))
        if len(local_hm) != 1:
            raise ValueError(f"cannot express {job} as one cron line in {tz_label}")
        (lh, lm), = local_hm
        days = ",".join(str(d) for d in sorted(local_days))
        lines.append(f"{lm} {lh} * * {days} {runner} {job}  # {hour:02d}:{minute:02d} Asia/Shanghai")
    if tz is not None:
        january = datetime(ref.year, 1, 15, 12, tzinfo=tz).utcoffset()
        july = datetime(ref.year, 7, 15, 12, tzinfo=tz).utcoffset()
        if january != july:
            warnings.append(
                f"warning: {tz_label} observes daylight saving time; cron times will drift by an hour twice a year. "
                "Set the VM time zone to UTC or Asia/Shanghai, or rerun install.sh after each change."
            )
    lines.append(BLOCK_END)
    return "\n".join(lines), warnings
