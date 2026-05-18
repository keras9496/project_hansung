from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    # 프로토타입은 KST 로컬 시간 기준. date_input/time_input 위젯이 naive 로컬을 반환하므로 통일.
    return datetime.now()


class Classroom(Base):
    __tablename__ = "classrooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    room_type: Mapped[str] = mapped_column(String(64), index=True)
    capacity: Mapped[int] = mapped_column(Integer, default=0)
    building: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    managing_dept: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)


class Semester(Base):
    __tablename__ = "semesters"
    __table_args__ = (UniqueConstraint("year", "term", name="uq_semester_year_term"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    term: Mapped[int] = mapped_column(Integer)  # 1 또는 2
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    application_open_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    application_deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    assignment_publish_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # draft | open | closed | assigning | published | active | archived
    status: Mapped[str] = mapped_column(String(16), default="draft")

    applications: Mapped[list["Application"]] = relationship(back_populates="semester")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    semester_id: Mapped[int] = mapped_column(ForeignKey("semesters.id"), index=True)
    applicant_name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(String(128), index=True)
    affiliation: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    course_name: Mapped[str] = mapped_column(String(128))
    requested_type: Mapped[str] = mapped_column(String(64), index=True)
    capacity_needed: Mapped[int] = mapped_column(Integer)
    # 강의실 구분 (희망 건물). 예: "상상관", "공학관", ...
    building: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # 교과구분 1. "교양" 또는 "전공-공학" / "전공-무용" / "전공-회화" / "전공-패션+디자인"
    course_category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # 교과구분 2. "이론" / "이론+실기" / "실기"
    class_format: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    days: Mapped[list] = mapped_column(JSON, default=list)         # ["월","수"]
    time_slot: Mapped[str] = mapped_column(String(32))             # "09:00-10:50" 등
    weeks: Mapped[list] = mapped_column(JSON, default=list)        # [1..15]
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # pending | assigned | deadlock | withdrawn
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)

    semester: Mapped[Semester] = relationship(back_populates="applications")
    assignment: Mapped[Optional["Assignment"]] = relationship(back_populates="application", uselist=False)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), unique=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    semester_id: Mapped[int] = mapped_column(ForeignKey("semesters.id"), index=True)
    days: Mapped[list] = mapped_column(JSON, default=list)
    time_slot: Mapped[str] = mapped_column(String(32))
    weeks: Mapped[list] = mapped_column(JSON, default=list)
    method: Mapped[str] = mapped_column(String(16), default="auto")  # auto | random | manual
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    application: Mapped[Application] = relationship(back_populates="assignment")
    classroom: Mapped[Classroom] = relationship()


class TempReservation(Base):
    __tablename__ = "temp_reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    requester_email: Mapped[str] = mapped_column(String(128), index=True)
    purpose: Mapped[str] = mapped_column(String(256))
    capacity: Mapped[int] = mapped_column(Integer)
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | expired | cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    classroom: Mapped[Classroom] = relationship()


class MailLog(Base):
    """프로토타입 메일 시뮬레이션 로그.

    이벤트가 발생하면 pending 으로 적재되고, 어드민이 발송 버튼을 누르면 sent 로 전환된다.
    실제 SMTP 발송은 하지 않는다.
    """

    __tablename__ = "mail_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # confirmation | assigned | deadlock | temp_confirmed | temp_expired
    event_kind: Mapped[str] = mapped_column(String(32), index=True)
    related_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | sent
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    triggered_by_admin: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
