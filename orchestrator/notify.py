"""notify.py — Email notifications. Silently skips if not configured."""
 
import smtplib
from email.mime.text import MIMEText
 
from .config import (
    NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO,
    NOTIFY_SMTP_HOST, NOTIFY_SMTP_PORT,
    NOTIFY_SMTP_USER, NOTIFY_SMTP_PASS,
    log,
)
 
 
def send(subject: str, body: str) -> None:
    if not all([NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO, NOTIFY_SMTP_USER, NOTIFY_SMTP_PASS]):
        log.info("Notifications not configured — skipping.")
        return
    try:
        msg             = MIMEText(body)
        msg["Subject"]  = subject
        msg["From"]     = NOTIFY_EMAIL_FROM
        msg["To"]       = NOTIFY_EMAIL_TO
        with smtplib.SMTP(NOTIFY_SMTP_HOST, NOTIFY_SMTP_PORT) as s:
            s.starttls()
            s.login(NOTIFY_SMTP_USER, NOTIFY_SMTP_PASS)
            s.sendmail(NOTIFY_EMAIL_FROM, [NOTIFY_EMAIL_TO], msg.as_string())
        log.info(f"Notification sent: {subject}")
    except Exception as e:
        log.warning(f"Notification failed: {e}")