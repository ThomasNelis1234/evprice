"""Diagnose SMTP connectivity from the current host. Writes a redacted transcript to the path given."""

from __future__ import annotations

import io
import os
import smtplib
import socket
import ssl
import sys
import traceback
from contextlib import redirect_stderr

HOST = "smtp.gmail.com"
out_path = sys.argv[1] if len(sys.argv) > 1 else "diag/smtp.txt"
os.makedirs(os.path.dirname(out_path), exist_ok=True)
user = os.environ.get("GMAIL_USER")
pw = os.environ.get("GMAIL_APP_PASSWORD")
lines = [f"secrets present: user={bool(user)} password={bool(pw)} pw_len={len(pw) if pw else 0}"]

try:
    lines.append(f"resolve: {socket.gethostbyname_ex(HOST)}")
except Exception as exc:  # noqa: BLE001
    lines.append(f"resolve failed: {exc}")

for port in (465, 587):
    try:
        s = socket.create_connection((HOST, port), timeout=15)
        s.close()
        lines.append(f"tcp {port}: open")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"tcp {port}: {exc}")

for mode, port in (("ssl", 465), ("starttls", 587)):
    buf = io.StringIO()
    lines.append(f"--- {mode} {port}")
    try:
        with redirect_stderr(buf):
            if mode == "ssl":
                srv = smtplib.SMTP_SSL(HOST, port, timeout=20, context=ssl.create_default_context())
            else:
                srv = smtplib.SMTP(HOST, port, timeout=20)
            srv.set_debuglevel(1)
            srv.ehlo("evprice.github.actions")
            if mode == "starttls":
                srv.starttls(context=ssl.create_default_context())
                srv.ehlo("evprice.github.actions")
            if user and pw:
                srv.login(user, pw)
                lines.append("login: OK")
            srv.quit()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"error: {type(exc).__name__}: {exc}")
        lines.append(traceback.format_exc().splitlines()[-1])
    transcript = buf.getvalue()
    if pw:
        transcript = transcript.replace(pw, "***")
    # redact base64 auth lines
    transcript = "\n".join(l if "AUTH" not in l and "auth" not in l.lower() else l[:40] + " [redacted]"
                           for l in transcript.splitlines())
    lines.append(transcript)

open(out_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
