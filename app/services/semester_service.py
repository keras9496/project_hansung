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
