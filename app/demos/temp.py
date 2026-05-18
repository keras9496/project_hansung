"""임시 사용 챗봇 데모.

좌측: 챗봇으로 목적·인원·기간 입력 → 가용 강의실 후보 선택 → 확정.
우측: 임시 예약 DB(활성/만료/취소 구분).
"""
from __future__ import annotations

from datetime import datetime, date, time, timedelta

import streamlit as st
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Classroom, Semester, TempReservation
from app.services.temp_service import (
    create_temp_reservation,
    expire_due_reservations,
    find_available_rooms,
)
from app.demos._shared import get_classroom_types


STEPS = [
    ("email", "안녕하세요. 임시 사용 신청 챗봇입니다. **신청자 이메일**을 알려주세요."),
    ("purpose", "**이용 목적**을 알려주세요. (예: 학회 발표 리허설)"),
    ("room_type", "**희망 강의실 종류**를 선택해주세요. ('상관없음' 선택 시 종류 무관)"),
    ("capacity", "**필요 수용 인원**은 몇 명인가요?"),
    ("start_at", "**사용 시작 일시**를 선택해주세요."),
    ("end_at", "**사용 종료 일시**를 선택해주세요."),
    ("pick_room", "가용한 강의실 후보 중 사용할 강의실을 선택해주세요."),
    ("confirm", "입력하신 내용을 확인해주세요. 아래 **'예약 완료'** 버튼을 누르면 확정됩니다."),
]


def _init_state() -> None:
    if "temp_step" not in st.session_state:
        _reset_state()


def _reset_state() -> None:
    st.session_state.temp_step = 0
    st.session_state.temp_draft = {}
    st.session_state.temp_chat = [{"role": "assistant", "content": STEPS[0][1]}]
    st.session_state.temp_candidates = []
    st.session_state.temp_done = False


def _append_bot(content: str) -> None:
    st.session_state.temp_chat.append({"role": "assistant", "content": content})


def _append_user(content: str) -> None:
    st.session_state.temp_chat.append({"role": "user", "content": content})


def _render_chat() -> None:
    for msg in st.session_state.temp_chat:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


def _validate(key: str, val):
    if key == "email":
        v = (val or "").strip()
        if "@" not in v or "." not in v:
            return False, "올바른 이메일 형식이 아닙니다.", None, None
        return True, "", v, v
    if key == "purpose":
        v = (val or "").strip()
        if not v:
            return False, "이용 목적을 입력해주세요.", None, None
        return True, "", v, v
    if key == "room_type":
        return True, "", val if val != "상관없음" else None, val
    if key == "capacity":
        return True, "", int(val), f"{int(val)}명"
    if key == "start_at":
        return True, "", val, val.strftime("%Y-%m-%d %H:%M")
    if key == "end_at":
        return True, "", val, val.strftime("%Y-%m-%d %H:%M")
    return False, "알 수 없는 단계", None, None


def _format_room(c: Classroom) -> str:
    return f"{c.code} {c.name} ({c.room_type}, 수용 {c.capacity}명)"


def _render_input(semester: Semester) -> None:
    step = st.session_state.temp_step
    if step >= len(STEPS):
        return
    key, _ = STEPS[step]
    draft = st.session_state.temp_draft

    # 강의실 선택 단계
    if key == "pick_room":
        cands: list[Classroom] = st.session_state.temp_candidates
        if not cands:
            st.warning("해당 조건에 가용한 강의실이 없습니다. 처음부터 다시 시도해주세요.")
            if st.button("↺ 처음부터 다시", use_container_width=True, key="temp_restart_nocand"):
                _reset_state()
                st.rerun()
            return

        with st.form("temp_pick_form", clear_on_submit=True):
            labels = [_format_room(c) for c in cands]
            idx = st.selectbox("가용 강의실 후보", range(len(labels)), format_func=lambda i: labels[i])
            submitted = st.form_submit_button("선택 ▶")
            if submitted:
                chosen = cands[idx]
                draft["classroom_id"] = chosen.id
                draft["classroom_label"] = _format_room(chosen)
                _append_user(draft["classroom_label"])
                st.session_state.temp_step += 1
                _append_bot(
                    "입력하신 내용을 확인해주세요:\n\n"
                    f"- **이메일**: {draft['email']}\n"
                    f"- **목적**: {draft['purpose']}\n"
                    f"- **강의실**: {draft['classroom_label']}\n"
                    f"- **사용 기간**: {draft['start_at']:%Y-%m-%d %H:%M} ~ "
                    f"{draft['end_at']:%Y-%m-%d %H:%M}\n\n"
                    "아래 **'예약 완료'** 버튼을 누르면 확정됩니다."
                )
                st.rerun()
        return

    if key == "confirm":
        c1, c2 = st.columns(2)
        with c1:
            if st.button("✅ 예약 완료", type="primary", use_container_width=True, key="temp_submit"):
                reservation = create_temp_reservation(
                    classroom_id=draft["classroom_id"],
                    requester_email=draft["email"],
                    purpose=draft["purpose"],
                    capacity=draft["capacity"],
                    start_at=draft["start_at"],
                    end_at=draft["end_at"],
                )
                _append_user("예약 완료를 클릭했습니다.")
                _append_bot(
                    f"임시 예약이 확정되었습니다. **예약 ID #{reservation.id}**\n\n"
                    "종료 시각 이후 자동으로 해제되며, 확정 메일이 발송 대기열에 추가되었습니다."
                )
                st.session_state.temp_step = len(STEPS)
                st.session_state.temp_done = True
                st.rerun()
        with c2:
            if st.button("↺ 처음부터 다시", use_container_width=True, key="temp_restart_confirm"):
                _reset_state()
                st.rerun()
        return

    with st.form(f"temp_form_{step}", clear_on_submit=True):
        if key == "email":
            val = st.text_input("이메일")
        elif key == "purpose":
            val = st.text_input("이용 목적")
        elif key == "room_type":
            options = ["상관없음"] + get_classroom_types()
            val = st.selectbox("강의실 종류", options)
        elif key == "capacity":
            val = st.number_input("필요 인원", min_value=1, max_value=200, value=20, step=1)
        elif key == "start_at":
            today = date.today()
            c1, c2 = st.columns(2)
            d = c1.date_input("시작 날짜", value=today + timedelta(days=1))
            t = c2.time_input("시작 시각", value=time(10, 0))
            val = datetime.combine(d, t)
        elif key == "end_at":
            start = draft.get("start_at") or datetime.now()
            c1, c2 = st.columns(2)
            d = c1.date_input("종료 날짜", value=start.date())
            t = c2.time_input("종료 시각", value=(start + timedelta(hours=2)).time())
            val = datetime.combine(d, t)
        else:
            val = None

        submitted = st.form_submit_button("다음 ▶")
        if submitted:
            ok, msg, normalized, display = _validate(key, val)
            if not ok:
                st.error(msg)
                return
            draft[key] = normalized
            _append_user(display)
            st.session_state.temp_step += 1

            # 종료 일시 직후 → 가용성 조회
            if key == "end_at":
                if draft["end_at"] <= draft["start_at"]:
                    st.error("종료 시각이 시작 시각보다 빠릅니다. 다시 시도해주세요.")
                    _reset_state()
                    st.rerun()
                rooms = find_available_rooms(
                    start_at=draft["start_at"],
                    end_at=draft["end_at"],
                    capacity_needed=draft["capacity"],
                    room_type=draft.get("room_type"),
                )
                st.session_state.temp_candidates = rooms
                if rooms:
                    _append_bot(
                        f"조건에 맞는 강의실 **{len(rooms)}개**를 찾았습니다. "
                        "아래에서 사용할 강의실을 선택해주세요."
                    )
                else:
                    _append_bot(
                        "해당 조건에 맞는 가용 강의실이 없습니다. "
                        "다른 시간대 또는 종류로 다시 시도해주세요."
                    )
            else:
                next_q = STEPS[st.session_state.temp_step][1]
                _append_bot(next_q)
            st.rerun()


def _render_right_pane() -> None:
    st.subheader("🗄️ 임시 예약 DB")

    if st.button("⏱ 만료 일괄 처리 (스케줄러 시뮬레이션)", key="temp_expire"):
        n = expire_due_reservations()
        if n:
            st.success(f"{n}건 만료 처리됨")
        else:
            st.info("만료 대상 없음")

    with SessionLocal() as s:
        rows = list(
            s.scalars(
                select(TempReservation).order_by(TempReservation.start_at.desc())
            ).all()
        )
        classrooms = {c.id: c for c in s.scalars(select(Classroom)).all()}

    c1, c2, c3 = st.columns(3)
    c1.metric("전체", len(rows))
    c2.metric("활성", sum(1 for r in rows if r.status == "active"))
    c3.metric("만료", sum(1 for r in rows if r.status == "expired"))

    if not rows:
        st.info("아직 임시 예약이 없습니다.")
        return

    df = [
        {
            "ID": r.id,
            "장소": (
                f"{classrooms[r.classroom_id].code} {classrooms[r.classroom_id].name}"
                if r.classroom_id in classrooms
                else r.classroom_id
            ),
            "신청자": r.requester_email,
            "목적": r.purpose,
            "인원": r.capacity,
            "시작": r.start_at.strftime("%Y-%m-%d %H:%M"),
            "종료": r.end_at.strftime("%Y-%m-%d %H:%M"),
            "상태": r.status,
        }
        for r in rows
    ]
    st.dataframe(df, use_container_width=True, hide_index=True, height=420)


def render(semester: Semester) -> None:
    _init_state()
    left, right = st.columns([1, 1], gap="medium")
    with left:
        st.subheader("💬 임시 사용 챗봇")
        with st.container(height=520, border=True):
            _render_chat()
        _render_input(semester)
        if st.session_state.get("temp_done"):
            if st.button("➕ 새 임시 예약", use_container_width=True, key="temp_restart_done"):
                _reset_state()
                st.rerun()
    with right:
        _render_right_pane()
