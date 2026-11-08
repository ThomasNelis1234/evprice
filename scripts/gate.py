"""Cheap pre-flight gate for the GitHub Actions workflow — standard library only.

Decides in well under a second whether this tick can do anything at all, so the workflow can skip
installing dependencies and running the tests on ticks that would exit immediately anyway
(before the earliest send time, or once today's email has gone out). The real gates in
evprice.main stay in place; this only saves runner minutes.

Prints ``skip=true`` or ``skip=false`` (plus a reason) and appends ``skip=...`` to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _yaml_scalar(text: str, key: str, default: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*['\"]?([^'\"#\n]+)", text, re.MULTILINE)
    return m.group(1).strip() if m else default


def main() -> int:
    cfg = Path(os.environ.get("EVPRICE_CONFIG", "config.yaml")).read_text(encoding="utf-8")
    tz = ZoneInfo(_yaml_scalar(cfg, "timezone", "Europe/Brussels"))
    earliest = _yaml_scalar(cfg, "earliest_send_local", "13:00")
    state_file = Path(_yaml_scalar(cfg, "state_file", "state/runs.json"))

    now_local = datetime.now(timezone.utc).astimezone(tz)
    today = now_local.date().isoformat()
    eh, em = (int(x) for x in earliest.split(":"))

    skip, reason = False, "run"
    if (now_local.hour, now_local.minute) < (eh, em):
        skip, reason = True, f"before earliest send time {earliest} ({now_local:%H:%M} local)"
    elif state_file.exists():
        try:
            runs = json.loads(state_file.read_text(encoding="utf-8") or "{}").get("runs", [])
            if any(r.get("run_date") == today and r.get("status") == "sent" for r in runs):
                skip, reason = True, "already sent today"
        except (OSError, ValueError) as exc:  # unreadable state: let the real run decide
            reason = f"state unreadable ({exc}); running"

    print(f"skip={'true' if skip else 'false'}  {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"skip={'true' if skip else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
