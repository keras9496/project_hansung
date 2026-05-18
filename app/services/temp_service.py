"""임시 예약 가용성·확정 서비스.

프로토타입 단순화: 임시 예약 간 시간 충돌만 검사한다.
정규 배정과의 충돌(요일/교시 매트릭스)은 향후 확장 항목으로 둔다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Classroom, TempReservation
from app.services.mailer import queue_mail


def find_available_rooms(
    start_at: datetime,
    end_at: datetime,
    capacity_needed: int,
    room_type: Optional[str] = None,
) -> list[Classroom]:
    if start_at >= end_at:
        return []
    with SessionLocal() as s:
        q = select(Classroom).where(Classroom.capacity >= capacity_needed)
        if room_type:
            q = q.where(Classroom.room_type == room_type)
        candidates = list(s.scalars(q.order_by(Classroom.capacity, Classroom.code)).all())

        conflicting = s.scalars(
            select(TempReservation).where(
                TempReservation.status == "active",
                TempReservation.start_at < end_at,
                TempReservation.end_at > start_at,
            )
        ).all()
        blocked = {t.classroom_id for t in conflicting}
        return [c for c in candidates if c.id not in blocked]


def create_temp_reservation(
    classroom_id: int,
    requester_email: str,
    purpose: str,
    capacity: int,
    start_at: datetime,
    end_at: datetime,
) -> TempReservation:
    with SessionLocal() as s:
        reservation = TempReservation(
            classroom_id=classroom_id,
            requester_email=requester_email,
            purpose=purpose,
            capacity=capacity,
            start_at=start_at,
            end_at=end_at,
            status="active",
        )
        s.add(reservation)
        s.flush()

        classroom = s.get(Classroom, classroom_id)
        queue_mail(
            event_kind="temp_confirmed",
            related_id=reservation.id,
            recipients=[requester_email],
            subject=f"[강의실 예약] 임시 사용 확정 (예약 ID #{reservation.id})",
            body=(
                f"임시 사용 신청이 확정되었습니다.\n\n"
                f"- 장소: {classroom.code} {classroom.name}\n"
                f"- 사용 목적: {purpose}\n"
                f"- 사용 시간: {start_at:%Y-%m-%d %H:%M} ~ {end_at:%Y-%m-%d %H:%M}\n"
            ),
            session=s,
        )

        s.commit()
        s.refresh(reservation)
        return reservation


def expire_due_reservations(now: Optional[datetime] = None) -> int:
    """end_at 이 지난 active 예약을 expired 로 전환. 처리 건수 반환."""
    if now is None:
        now = datetime.now()
    count = 0
    with SessionLocal() as s:
        due = s.scalars(
            select(TempReservation).where(
                TempReservation.status == "active",
                TempReservation.end_at <= now,
            )
        ).all()
        for r in due:
            r.status = "expired"
            count += 1
            queue_mail(
                event_kind="temp_expired",
                related_id=r.id,
                recipients=[r.requester_email],
                subject=f"[강의실 예약] 임시 사용 종료 (예약 ID #{r.id})",
                body="임시 사용 기간이 종료되어 강의실이 다시 가용 풀로 복귀했습니다.",
                session=s,
            )
        s.commit()
    return count
