"""메일 시뮬레이션. 프로토타입은 실제 SMTP 발송을 하지 않는다.

흐름:
    1. 시스템 이벤트(접수/배정/데드락 등) → queue_mail()로 mail_logs 에 pending 적재
    2. 어드민 페이지에서 행을 골라 mark_sent() 호출 → sent 로 전환되고 sent_at 기록
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import MailLog


def queue_mail(
    event_kind: str,
    related_id: Optional[int],
    recipients: Iterable[str],
    subject: str,
    body: str,
    session: Optional[Session] = None,
) -> MailLog:
    """메일 이벤트를 발송 대기열에 추가한다. session 이 주어지면 그 트랜잭션을 사용."""
    log = MailLog(
        event_kind=event_kind,
        related_id=related_id,
        recipients=list(recipients),
        subject=subject,
        body=body,
        simulated=True,
        status="pending",
    )
    if session is not None:
        session.add(log)
        session.flush()
        return log

    with SessionLocal() as s:
        s.add(log)
        s.commit()
        s.refresh(log)
        return log


def mark_sent(mail_log_id: int, triggered_by: str = "admin") -> MailLog:
    with SessionLocal() as s:
        log = s.get(MailLog, mail_log_id)
        if log is None:
            raise ValueError(f"MailLog {mail_log_id} not found")
        log.status = "sent"
        log.sent_at = datetime.now()
        log.triggered_by_admin = triggered_by
        s.commit()
        s.refresh(log)
        return log


def mark_all_pending_sent(triggered_by: str = "admin") -> int:
    """대기 중인 모든 메일을 일괄 시뮬레이션 발송. 발송 건수 반환."""
    count = 0
    with SessionLocal() as s:
        pending = s.query(MailLog).filter(MailLog.status == "pending").all()
        now = datetime.now()
        for log in pending:
            log.status = "sent"
            log.sent_at = now
            log.triggered_by_admin = triggered_by
            count += 1
        s.commit()
    return count
