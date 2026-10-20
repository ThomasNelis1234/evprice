"""Run state: a small JSON file committed back to the repository by the workflow.

It is the duplicate-prevention mechanism and a lightweight audit log."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

KEEP_RUNS = 60


@dataclass
class RunRecord:
    run_date: str                 # local date, YYYY-MM-DD
    started_at: str               # UTC ISO
    status: str                   # sent | skipped | failed | dry-run
    reason: str = ""
    sent_at: str | None = None
    warnings: list[str] = field(default_factory=list)
    actual_slots: int = 0
    forecast_slots: int = 0
    tomorrow_published: bool | None = None


class State:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.runs: list[RunRecord] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.runs = [RunRecord(**r) for r in data.get("runs", [])]

    def already_sent(self, run_date: date) -> bool:
        key = run_date.isoformat()
        return any(r.run_date == key and r.status == "sent" for r in self.runs)

    def record(self, rec: RunRecord) -> None:
        self.runs.append(rec)
        # keep bounded: always keep every 'sent' record of the last KEEP_RUNS days,
        # and the most recent KEEP_RUNS records overall
        self.runs = self.runs[-KEEP_RUNS:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "runs": [asdict(r) for r in self.runs]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
