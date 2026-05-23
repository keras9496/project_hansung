"""학기 라이프사이클 도우미."""
from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Semester


def ensure_demo_semester() -> Semester:
    """기본 학기가 없으면 2026-1학기 를 만들고 반환한다."""
    with SessionLocal() as s:
        existing = s.scalars(select(Semester).order_by(Semester.id)).first()
        if existing is not None:
            return existing
        sem = Semester(year=2026, term=1, status="open")
        s.add(sem)
        s.commit()
        s.refresh(sem)
        return sem


def create_next_semester() -> Semester:
    """현재 가장 최신 학기의 다음 학기를 만들고 반환한다.

    Why: '다음학기 배정하기' 버튼이 누적 시드(150건)에 영향을 주지 않고
    완전히 새 학기(신청 0건)에서 배정을 시작할 수 있게 하기 위함.
    1학기 → 2학기, 2학기 → 다음 해 1학기 로 자연스럽게 롤오버한다.
    """
    with SessionLocal() as s:
        latest = s.scalars(
            select(Semester).order_by(Semester.year.desc(), Semester.term.desc())
        ).first()
        if latest is None:
            new_year, new_term = 2026, 1
        elif latest.term == 1:
            new_year, new_term = latest.year, 2
        else:
            new_year, new_term = latest.year + 1, 1
        sem = Semester(year=new_year, term=new_term, status="open")
        s.add(sem)
        s.commit()
        s.refresh(sem)
        return sem


def list_semesters() -> list[Semester]:
    with SessionLocal() as s:
        return list(
            s.scalars(
                select(Semester).order_by(Semester.year.desc(), Semester.term.desc())
            ).all()
        )


def get_semester(semester_id: int) -> Semester | None:
    with SessionLocal() as s:
        return s.get(Semester, semester_id)


def update_semester_dates(
    semester_id: int,
    application_open_at=None,
    application_deadline_at=None,
    assignment_publish_at=None,
) -> Semester:
    with SessionLocal() as s:
        sem = s.get(Semester, semester_id)
        if sem is None:
            raise ValueError("semester not found")
        if application_open_at is not None:
            sem.application_open_at = application_open_at
        if application_deadline_at is not None:
            sem.application_deadline_at = application_deadline_at
        if assignment_publish_at is not None:
            sem.assignment_publish_at = assignment_publish_at
        s.commit()
        s.refresh(sem)
        return sem
