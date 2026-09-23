"""Step one: pull referrals out of IntakeQ and write them to an Excel file.

This runs entirely on your machine. It talks to IntakeQ over HTTPS and writes a
local .xlsx. No patient data is sent to any AI service, pasted anywhere, or
uploaded. There is no copy-paste step to get wrong.

Run this before building anything else — it proves the API key works and shows you
the real shape of your own data.

    python pull_intakeq.py --inspect            # show field names from one record
    python pull_intakeq.py --days 30            # last 30 days -> referrals.xlsx
    python pull_intakeq.py --days 30 --out q3.xlsx

Requires INTAKEQ_API_KEY in the environment or a .env file beside this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

BASE = "https://intakeq.com/api/v1"

# A standard PracticeQ subscription allows roughly 10 requests/minute and 500/day.
# We stay under both rather than discovering the ceiling mid-run.
PER_MINUTE = 8
PER_DAY = 450


class RateLimiter:
    """Blocks rather than erroring. A slow run beats a half-finished one."""

    def __init__(self, per_minute: int = PER_MINUTE, per_day: int = PER_DAY) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self._minute: deque[float] = deque()
        self._day_count = 0

    def take(self) -> None:
        if self._day_count >= self.per_day:
            raise RuntimeError(
                f"daily IntakeQ budget of {self.per_day} requests reached. "
                "Re-run tomorrow, or narrow --days."
            )
        now = time.monotonic()
        while self._minute and now - self._minute[0] > 60:
            self._minute.popleft()
        if len(self._minute) >= self.per_minute:
            wait = 60 - (now - self._minute[0]) + 0.5
            print(f"  … rate limit, waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            return self.take()
        self._minute.append(time.monotonic())
        self._day_count += 1


class IntakeQ:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise SystemExit(
                "INTAKEQ_API_KEY is not set.\n\n"
                "Get it from IntakeQ: More -> Settings -> Integrations -> Developer API\n"
                "(only the main account owner can see that page), then either export it:\n"
                "    setx INTAKEQ_API_KEY \"your-key\"\n"
                "or put INTAKEQ_API_KEY=your-key in a .env file beside this script."
            )
        self.session = requests.Session()
        self.session.headers.update({"X-Auth-Key": api_key})
        self.limiter = RateLimiter()

    def _get(self, path: str, **params) -> object:
        self.limiter.take()
        resp = self.session.get(f"{BASE}/{path}", params=params, timeout=30)
        if resp.status_code == 401:
            raise SystemExit("IntakeQ rejected the API key (401). Check it was copied whole.")
        if resp.status_code == 429:
            print("  … IntakeQ returned 429, backing off 60s", file=sys.stderr)
            time.sleep(60)
            return self._get(path, **params)
        resp.raise_for_status()
        return resp.json()

    def intake_summaries(self, start: date, end: date) -> list[dict]:
        """Submitted intake forms in a date range, newest first, paged."""
        out: list[dict] = []
        page = 1
        while True:
            batch = self._get(
                "intakes/summary",
                startDate=start.isoformat(),
                endDate=end.isoformat(),
                page=page,
            )
            if not batch:
                break
            out.extend(batch)
            print(f"  page {page}: {len(batch)} intakes", file=sys.stderr)
            if len(batch) < 100:  # IntakeQ pages at 100
                break
            page += 1
        return out

    def intake(self, intake_id: str) -> dict:
        """One completed intake, including every question and answer."""
        return self._get(f"intakes/{intake_id}")


def flatten(intake: dict) -> dict[str, object]:
    """Turn one intake into a flat row.

    IntakeQ nests answers under Questions[], and the question text differs per form,
    so the columns are discovered from the data rather than hardcoded.
    """
    row: dict[str, object] = {
        "IntakeId": intake.get("Id"),
        "ClientId": intake.get("ClientId"),
        "ClientName": intake.get("ClientName"),
        "ClientEmail": intake.get("ClientEmail"),
        "ClientPhone": intake.get("ClientPhone"),
        "DateOfBirth": intake.get("DateOfBirth"),
        "FormName": intake.get("QuestionnaireName"),
        "SubmittedAt": _epoch_to_date(intake.get("DateSubmitted")),
        "Status": intake.get("Status"),
    }
    for q in intake.get("Questions") or []:
        text = (q.get("Text") or "").strip()
        if not text:
            continue
        answer = q.get("Answer")
        if isinstance(answer, list):
            answer = "; ".join(str(a) for a in answer)
        row[text[:120]] = answer
    return row


def _epoch_to_date(value: object) -> str | None:
    """IntakeQ returns millisecond epochs on several date fields."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.utcfromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(value)


def write_excel(rows: list[dict], path: Path) -> None:
    if not rows:
        print("No intakes found in that range — nothing written.", file=sys.stderr)
        return

    # Stable column order: the known fields first, then discovered questions.
    leading = [
        "IntakeId", "ClientId", "ClientName", "ClientEmail", "ClientPhone",
        "DateOfBirth", "FormName", "SubmittedAt", "Status",
    ]
    discovered: list[str] = []
    for row in rows:
        for key in row:
            if key not in leading and key not in discovered:
                discovered.append(key)
    headers = leading + discovered

    wb = Workbook()
    ws = wb.active
    ws.title = "IntakeQ referrals"

    header_fill = PatternFill("solid", fgColor="14494F")
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for r, row in enumerate(rows, start=2):
        for c, name in enumerate(headers, start=1):
            value = row.get(name)
            ws.cell(row=r, column=c, value=value if value is not None else "")

    for col, name in enumerate(headers, start=1):
        width = max(12, min(48, len(str(name)) + 4))
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    wb.save(path)
    print(f"Wrote {len(rows)} referrals x {len(headers)} columns -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="how far back to pull (default 30)")
    ap.add_argument("--out", type=Path, default=Path("referrals.xlsx"))
    ap.add_argument(
        "--inspect",
        action="store_true",
        help="print one full intake as JSON and exit, to map your own field names",
    )
    args = ap.parse_args()

    client = IntakeQ(os.environ.get("INTAKEQ_API_KEY", ""))
    end = date.today()
    start = end - timedelta(days=args.days)

    print(f"Fetching intakes {start} to {end} …", file=sys.stderr)
    summaries = client.intake_summaries(start, end)
    print(f"{len(summaries)} intakes found", file=sys.stderr)

    if not summaries:
        return

    if args.inspect:
        full = client.intake(summaries[0]["Id"])
        print(json.dumps(full, indent=2)[:8000])
        print(
            "\n^ that is the real shape of your data. "
            "Map these field names into flatten() before the full run.",
            file=sys.stderr,
        )
        return

    rows = []
    for i, summary in enumerate(summaries, start=1):
        print(f"  [{i}/{len(summaries)}] intake {summary['Id']}", file=sys.stderr)
        rows.append(flatten(client.intake(summary["Id"])))

    write_excel(rows, args.out)


if __name__ == "__main__":
    main()
