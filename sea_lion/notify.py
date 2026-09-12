"""Health events + external notification (design §19.2, §17).

A silent safe mode is a failed control: every actionable condition is persisted as a health
event and, for warning/critical severities, emailed to the configured owner address. Delivery
attempts are audited. A fake transport exists so tests verify the wiring without sending mail."""
from __future__ import annotations

import hashlib
import logging
import smtplib
import subprocess
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from .config import NotifyCfg
from .store import Store

log = logging.getLogger(__name__)

SENT: List[Dict[str, Any]] = []   # fake transport sink (tests)


class Notifier:
    def __init__(self, cfg: NotifyCfg, store: Store, mode: str):
        self.cfg = cfg
        self.store = store
        self.mode = mode

    def event(self, severity: str, component: str, reason: str, run_id: Optional[str] = None,
              remediation: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> int:
        """Persist a health event; email if severity qualifies. Never raises."""
        hid = self.store.add_health_event(severity, component, reason, run_id, remediation)
        log.log(logging.ERROR if severity == "critical" else logging.WARNING if severity == "warning" else logging.INFO,
                "health[%s] %s: %s", severity, component, reason)
        if self.cfg.enabled and severity in self.cfg.email_severities and self.cfg.transport != "none":
            self._email(hid, severity, component, reason, run_id, remediation, details or {})
        return hid

    def _email(self, hid: int, severity: str, component: str, reason: str, run_id: Optional[str],
               remediation: Optional[str], details: Dict[str, Any]) -> None:
        subject = f"[sea-lion {self.mode}] {severity.upper()} {component}: {reason[:80]}"
        body = (f"Sea Lion ({self.mode}) health event #{hid}\nseverity: {severity}\ncomponent: {component}\nrun: {run_id}\n\n"
                f"{reason}\n\nremediation: {remediation or 'see runtime/reports/' + self.mode + '/latest.html'}\n")
        if details:
            body += "\ndetails:\n" + "\n".join(f"  {k}: {v}" for k, v in details.items())
        for to in self.cfg.email_to:
            dest_hash = hashlib.sha256(to.encode()).hexdigest()[:12]
            err = None
            for attempt in range(1, self.cfg.max_retries + 2):
                try:
                    self._send(to, subject, body)
                    self.store.add_notification_attempt(hid, self.cfg.transport, dest_hash, "sent", attempt, None)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:300]
                    self.store.add_notification_attempt(hid, self.cfg.transport, dest_hash, "failed", attempt, err)
            if err:
                log.error("notification delivery failed for event %s: %s", hid, err)

    def _send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.cfg.email_from, to, subject
        msg.set_content(body)
        if self.cfg.transport == "fake":
            SENT.append({"to": to, "subject": subject, "body": body})
        elif self.cfg.transport == "sendmail":
            p = subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"], input=msg.as_bytes(), capture_output=True, timeout=30)
            if p.returncode != 0:
                raise RuntimeError(f"sendmail rc={p.returncode}: {p.stderr.decode()[:200]}")
        elif self.cfg.transport == "smtp":
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as s:
                if self.cfg.smtp_starttls:
                    s.starttls()
                s.send_message(msg)
        else:
            raise RuntimeError(f"unknown transport {self.cfg.transport}")


def summary_line(d: Dict[str, Any]) -> str:
    """One machine+human readable line for the scheduler log (design §19.2 layer 1)."""
    keys = ["run_date", "decision_as_of", "run_outcome", "status", "safe_mode", "n_orders", "ai_available",
            "reconciliation_status", "attention"]
    return "SEA_LION_SUMMARY " + " ".join(f"{k}={d.get(k)}" for k in keys)
