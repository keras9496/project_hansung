"""어드민 데모.

좌측: 학기 설정(마감/공개일), 학기 현황 요약.
우측: 메일 발송함 — 대기 중 메일 목록 + 본문 미리보기 + '시뮬레이션 발송' 버튼.
"""
from __future__ import annotations

from datetime import datetime, date, time, timedelta

import streamlit as st
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Application, Assignment, MailLog, Semester, TempReservation
from app.services.mailer import mark_all_pending_sent, mark_sent
from app.services.semester_service import update_semester_dates


EVENT_LABELS = {
    "confirmation": "📥 접수 확인",
    "assigned": "✅ 배정 완료",
    "deadlock": "⚠️ 배정 실패 조율",
    "temp_confirmed": "📌 임시 예약 확정",
    "temp_expired": "⌛ 임시 예약 만료",
}


def _left_pane(semester: Semester) -> None:
    st.subheader("⚙️ 학기 설정")

    with st.form("admin_sem_form"):
        c1, c2 = st.columns(2)
        with c1:
            open_d = st.date_input(
                "신청 시작일",
                value=(semester.application_open_at or datetime.now()).date(),
            )
            open_t = st.time_input(
                "신청 시작 시각",
                value=(semester.application_open_at or datetime.now()).time().replace(microsecond=0),
            )
            deadline_d = st.date_input(
                "신청 마감일",
                value=(semester.application_deadline_at or datetime.now() + timedelta(days=14)).date(),
            )
            deadline_t = st.time_input(
                "신청 마감 시각",
                value=(semester.application_deadline_at or datetime.now() + timedelta(days=14)).time().replace(microsecond=0),
            )
        with c2:
            publish_d = st.date_input(
                "배정 공개일",
                value=(semester.assignment_publish_at or datetime.now() + timedelta(days=21)).date(),
            )
            publish_t = st.time_input(
                "배정 공개 시각",
                value=(semester.assignment_publish_at or datetime.now() + timedelta(days=21)).time().replace(microsecond=0),
            )
            st.caption(
                "프로토타입에서는 날짜가 도래해도 자동으로 잠기지 않습니다. "
                "필드는 정보용으로 저장됩니다."
            )

        saved = st.form_submit_button("💾 학기 설정 저장", use_container_width=True)
        if saved:
            update_semester_dates(
                semester.id,
                application_open_at=datetime.combine(open_d, open_t),
                application_deadline_at=datetime.combine(deadline_d, deadline_t),
                assignment_publish_at=datetime.combine(publish_d, publish_t),
            )
            st.success("학기 설정이 저장되었습니다.")
            st.rerun()

    st.markdown("---")
    st.subheader("📊 학기 현황")

    with SessionLocal() as s:
        n_apps = s.scalars(
            select(Application).where(Application.semester_id == semester.id)
        ).all()
        n_assign = s.scalars(
            select(Assignment).where(Assignment.semester_id == semester.id)
        ).all()
        n_temp = s.scalars(select(TempReservation)).all()
        n_mail_pending = s.scalars(
            select(MailLog).where(MailLog.status == "pending")
        ).all()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("신청", len(n_apps))
    c2.metric("배정", len(n_assign))
    c3.metric("임시예약", len(n_temp))
    c4.metric("발송 대기 메일", len(n_mail_pending))


def _right_pane() -> None:
    st.subheader("📬 메일 발송함 (시뮬레이션)")
    st.caption("실제 SMTP 발송은 하지 않습니다. 어드민이 '발송' 버튼을 누르면 `mail_logs`에 sent 로 기록됩니다.")

    with SessionLocal() as s:
        pending = list(
            s.scalars(
                select(MailLog).where(MailLog.status == "pending").order_by(MailLog.queued_at)
            ).all()
        )
        sent = list(
            s.scalars(
                select(MailLog).where(MailLog.status == "sent").order_by(MailLog.sent_at.desc())
            ).all()
        )

    if pending:
        st.markdown(f"### 대기 중 ({len(pending)})")
        if st.button(f"📨 대기 메일 전체 발송 ({len(pending)}건)", type="primary", key="mail_send_all"):
            n = mark_all_pending_sent(triggered_by="admin")
            st.success(f"{n}건 시뮬레이션 발송 완료")
            st.rerun()

        for log in pending:
            label = EVENT_LABELS.get(log.event_kind, log.event_kind)
            with st.expander(
                f"{label} · {log.subject} · 수신: {', '.join(log.recipients)}",
                expanded=False,
            ):
                st.caption(f"queued at {log.queued_at:%Y-%m-%d %H:%M:%S}")
                st.text_area(
                    "본문 미리보기",
                    value=log.body,
                    height=140,
                    disabled=True,
                    key=f"mail_body_{log.id}",
                )
                c1, c2 = st.columns([1, 5])
                with c1:
                    if st.button(
                        "안내 메일을 보내겠습니까? ✉️ 보내기",
                        key=f"mail_send_{log.id}",
                    ):
                        mark_sent(log.id, triggered_by="admin")
                        st.success("발송 완료(시뮬레이션)")
                        st.rerun()
    else:
        st.info("대기 중인 메일이 없습니다.")

    st.markdown("---")
    st.markdown(f"### 발송 완료 ({len(sent)})")
    if not sent:
        st.caption("아직 발송된 메일이 없습니다.")
        return
    rows = [
        {
            "ID": log.id,
            "이벤트": EVENT_LABELS.get(log.event_kind, log.event_kind),
            "수신자": ", ".join(log.recipients),
            "제목": log.subject,
            "발송 시각": log.sent_at.strftime("%Y-%m-%d %H:%M:%S") if log.sent_at else "",
        }
        for log in sent
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True, height=280)


def render(semester: Semester) -> None:
    left, right = st.columns([1, 1], gap="medium")
    with left:
        _left_pane(semester)
    with right:
        _right_pane()
