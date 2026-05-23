"""한성대 강의실 예약 챗봇 — 프로토타입 데모 진입점.

진입 화면에서 사용자/관리자 경로를 분기한다.
- 강의실 예약하기: 정규 신청 + 임시 사용
- 관리자 시스템: 배정 실행 + 학기 설정·메일
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Application,
    Assignment,
    Classroom,
    MailLog,
    Semester,
    TempReservation,
)
from app.services.semester_service import ensure_demo_semester, list_semesters  # noqa: E402
from app.demos import admin as demo_admin  # noqa: E402
from app.demos import assignment as demo_assignment  # noqa: E402
from app.demos import dashboard as demo_dashboard  # noqa: E402
from app.demos import regular as demo_regular  # noqa: E402
from app.demos import temp as demo_temp  # noqa: E402


USER_MODES = {
    "📝 정규 신청 (챗봇)": demo_regular.render,
    "⏱️ 임시 사용 (챗봇)": demo_temp.render,
}

ADMIN_MODES = {
    "📊 활용 현황": demo_dashboard.render,
    "🧮 배정 실행": demo_assignment.render,
    "⚙️ 학기 설정·메일": demo_admin.render,
}


def _ensure_demo_classrooms() -> None:
    """강의실 마스터가 비어 있으면 .xlsx → DB 시드를 자동 실행한다.

    Why: Render 신규 배포(디스크 초기화)에서도 셸 접속 없이 곧바로 시연 가능한 상태가 되도록.
    """
    with SessionLocal() as s:
        n_room = s.query(Classroom).count()
    if n_room == 0:
        from scripts.seed_classrooms import seed as _seed_classrooms
        _seed_classrooms()


def _ensure_demo_applications(semester: Semester) -> None:
    """데모 시드 학기에 신청이 0건이면 150건을 자동 시드한다.

    Why: 시연 시작 시 항상 ~150건이 존재해야 배정/데드락 데모가 의미를 갖는다.
    리셋 후에도 자동 복원되도록 매 부트스트랩에서 0건이면 다시 채운다.
    '다음학기 배정하기' 로 만든 새 학기는 이 시드 대상이 아님(별도 0건 시작).
    """
    with SessionLocal() as s:
        n_apps = s.query(Application).filter(Application.semester_id == semester.id).count()
    if n_apps == 0:
        from scripts.seed_applications import seed_applications
        seed_applications(count=150, reset=False, seed=42)


def _bootstrap() -> Semester:
    init_db()
    _ensure_demo_classrooms()
    semester = ensure_demo_semester()
    _ensure_demo_applications(semester)
    return semester


def _reset_demo_data(semester_id: int) -> None:
    """프로토타입 데모를 새로 녹화할 때 사용. 강의실 마스터·학기는 유지."""
    with SessionLocal() as s:
        s.query(Assignment).filter(Assignment.semester_id == semester_id).delete()
        s.query(Application).filter(Application.semester_id == semester_id).delete()
        s.query(TempReservation).delete()
        s.query(MailLog).delete()
        s.commit()
    for key in list(st.session_state.keys()):
        if key.startswith(("reg_", "temp_", "assign_")):
            del st.session_state[key]


def _go(section: str) -> None:
    st.session_state["section"] = section


def _render_landing() -> None:
    st.markdown(
        """
        <div style="text-align:center; padding: 1.5rem 0 2.5rem 0;">
            <h1 style="font-size: 2.4rem; margin-bottom: 0.4rem;">🏫 한성대 강의실 예약 시스템</h1>
            <p style="font-size: 1.05rem; color: #555;">아래에서 이용하실 메뉴를 선택해주세요.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <style>
        div[data-testid="stButton"] > button {
            height: 260px;
            font-size: 2.4rem;
            font-weight: 900;
            border-radius: 18px;
            border: 2px solid #e0e0e0;
            line-height: 1.5;
            letter-spacing: -0.02em;
        }
        div[data-testid="stButton"] > button p {
            font-size: 2.4rem !important;
            font-weight: 900 !important;
        }
        div[data-testid="stButton"] > button:hover {
            border-color: #1f77b4;
            background-color: #f5faff;
            color: #1f77b4;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, c_left, c_mid, c_right, _ = st.columns([1, 3, 0.4, 3, 1])
    with c_left:
        st.button(
            "📅\n\n강의실 예약하기\n\n정규 신청 · 임시 사용",
            key="enter_user",
            use_container_width=True,
            on_click=_go,
            args=("user",),
        )
    with c_right:
        st.button(
            "🛠️\n\n관리자 시스템\n\n배정 실행 · 학기 설정",
            key="enter_admin",
            use_container_width=True,
            on_click=_go,
            args=("admin",),
        )


def _sidebar_section(section: str, semester: Semester) -> tuple[str, Semester]:
    is_admin = section == "admin"
    if is_admin:
        st.sidebar.title("🛠️ 관리자 시스템")
        modes = ADMIN_MODES
    else:
        st.sidebar.title("📅 강의실 예약하기")
        modes = USER_MODES

    st.sidebar.caption("프로토타입 — 프로모션용")

    if st.sidebar.button("⬅️ 처음 화면으로", use_container_width=True):
        _go("landing")
        st.rerun()

    st.sidebar.markdown("---")
    mode = st.sidebar.radio("메뉴", list(modes.keys()), index=0)

    semesters = list_semesters()
    labels = {f"{s.year}-{s.term}학기": s for s in semesters}
    label_keys = list(labels.keys())
    # 새 학기 생성 직후 해당 학기를 자동 선택하기 위해 세션 상태를 사용한다.
    # 옵션에 없는 값이 남아 있으면 selectbox 가 예외를 던지므로 먼저 정리한다.
    preselect = st.session_state.get("sidebar_semester_label")
    if preselect is not None and preselect not in labels:
        del st.session_state["sidebar_semester_label"]
        preselect = None
    default_idx = label_keys.index(preselect) if preselect in labels else 0
    chosen_label = st.sidebar.selectbox(
        "학기", label_keys, index=default_idx, key="sidebar_semester_label"
    )
    chosen = labels[chosen_label]

    st.sidebar.markdown("---")
    st.sidebar.markdown("**현재 정책**")
    st.sidebar.markdown("- 메일: 어드민 트리거 시뮬레이션")
    st.sidebar.markdown("- 마감/공개일: 어드민에서 설정")

    if is_admin:
        st.sidebar.markdown("---")
        st.sidebar.markdown("**데모 데이터 관리**")
        if st.sidebar.button("🧹 신청/배정/임시예약/메일 초기화", use_container_width=True):
            _reset_demo_data(chosen.id)
            st.sidebar.success("초기화 완료")
            st.rerun()

    with SessionLocal() as s:
        n_apps = s.query(Application).count()
        n_assign = s.query(Assignment).count()
        n_temp = s.query(TempReservation).count()
        n_mail = s.query(MailLog).count()
        n_room = s.query(Classroom).count()

    st.sidebar.caption(
        f"강의실 {n_room} · 신청 {n_apps} · 배정 {n_assign} · 임시 {n_temp} · 메일 {n_mail}"
    )

    return mode, chosen


def _render_section(section: str, semester: Semester) -> None:
    mode, semester = _sidebar_section(section, semester)

    section_label = "강의실 예약하기" if section == "user" else "관리자 시스템"
    st.title(f"한성대 강의실 예약 시스템 — {section_label}")
    st.caption(
        f"학기: **{semester.year}-{semester.term}학기** · 메뉴: **{mode}** · "
        "왼쪽은 사용자/운영자 인터페이스, 오른쪽은 실시간 DB 상태"
    )

    modes = USER_MODES if section == "user" else ADMIN_MODES
    modes[mode](semester)


def main() -> None:
    st.set_page_config(
        page_title="한성대 강의실 예약 시스템",
        layout="wide",
        initial_sidebar_state="auto",
    )

    semester = _bootstrap()

    section = st.session_state.get("section", "landing")
    if section not in {"landing", "user", "admin"}:
        section = "landing"

    if section == "landing":
        _render_landing()
        return

    _render_section(section, semester)


if __name__ == "__main__":
    main()
