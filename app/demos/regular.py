"""정규 신청 — AI 자연어 입력 전용 데모.

좌측: 🤖 AI 한 번에 입력 → (모든 필수 필드 추출 시) 확인 패널 → 신청 완료.
우측: 현재 학기에 등록된 신청 테이블(가장 최근이 상단).

AI 보조 기능 (Claude API):
    - 자연어 한 문장 → 필드 추출 (extract_application_fields)
    - 동적 예시 생성 (generate_intake_examples)
    - 확정 직전 정책 일관성 검사 (check_consistency)
"""
from __future__ import annotations

import re

import streamlit as st
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Application, Semester
from app.services.llm import (
    ExtractedApplication,
    check_consistency,
    extract_application_fields,
    generate_intake_examples,
    is_live as llm_is_live,
    mode_label as llm_mode_label,
)
from app.services.mailer import queue_mail

from app.demos._shared import (
    BUILDING_LABELS,
    BUILDING_NAMES,
    CLASS_FORMATS,
    COURSE_CATEGORIES,
    DAYS,
    TIME_SLOTS,
    WEEK_PRESETS,
    get_classroom_types,
    preferred_building_for,
)


# ────────────────────────── 상태 ──────────────────────────


def _init_state() -> None:
    if "reg_draft" not in st.session_state:
        st.session_state.reg_draft = None
    if "reg_done" not in st.session_state:
        st.session_state.reg_done = False
    if "reg_last_app_id" not in st.session_state:
        st.session_state.reg_last_app_id = None
    if "reg_intake_text" not in st.session_state:
        st.session_state.reg_intake_text = ""
    if "reg_intake_examples" not in st.session_state:
        st.session_state.reg_intake_examples = []


def _reset_state() -> None:
    st.session_state.reg_draft = None
    st.session_state.reg_done = False
    st.session_state.reg_last_app_id = None
    st.session_state["reg_consistency_warnings"] = []
    st.session_state.pop("reg_consistency_for", None)
    # reg_intake_text 는 위젯 key — instantiate 된 뒤엔 직접 수정 불가. pending 키로 다음 rerun 에 반영.
    st.session_state["_pending_intake_text"] = ""


# ────────────────────────── 정규화 헬퍼 ──────────────────────────


_TIME_NUM_RE = re.compile(r"(\d+)\s*교시")


def _normalize_time_slot(value: str | None) -> str | None:
    """LLM 이 '2교시' 또는 '2교시 (10:30-11:45)' 어느 쪽으로 줘도 정식 라벨로 변환."""
    if not value:
        return None
    if value in TIME_SLOTS:
        return value
    m = _TIME_NUM_RE.search(value)
    if not m:
        return None
    n = m.group(1)
    for slot in TIME_SLOTS:
        if slot.startswith(f"{n}교시"):
            return slot
    return None


def _normalize_extracted(ex: ExtractedApplication) -> dict:
    """LLM 결과를 draft 에 들어갈 수 있는 정식 형태로 정리. 인식 안 된 항목은 빠진다."""
    draft: dict = {}
    if ex.applicant_name:
        draft["name"] = ex.applicant_name.strip()
    if ex.email and "@" in ex.email and "." in ex.email:
        draft["email"] = ex.email.strip()
    if ex.affiliation:
        draft["affiliation"] = ex.affiliation.strip()
    if ex.course_name:
        draft["course_name"] = ex.course_name.strip()
    if ex.course_category in COURSE_CATEGORIES:
        draft["course_category"] = ex.course_category
    if ex.class_format in CLASS_FORMATS:
        draft["class_format"] = ex.class_format
    if ex.building in BUILDING_NAMES:
        draft["building"] = ex.building
    if ex.requested_type:
        types = get_classroom_types()
        if ex.requested_type in types:
            draft["requested_type"] = ex.requested_type
        else:
            match = next((t for t in types if ex.requested_type in t or t in ex.requested_type), None)
            if match:
                draft["requested_type"] = match
    if isinstance(ex.capacity_needed, int) and ex.capacity_needed > 0:
        draft["capacity_needed"] = ex.capacity_needed
    if ex.days:
        valid = [d for d in ex.days if d in DAYS]
        if valid:
            draft["days"] = valid
    ts = _normalize_time_slot(ex.time_slot)
    if ts:
        draft["time_slot"] = ts
    if ex.weeks_range in WEEK_PRESETS:
        draft["weeks"] = WEEK_PRESETS[ex.weeks_range]
        draft["weeks_display"] = ex.weeks_range
    if ex.notes:
        draft["notes"] = ex.notes.strip()
    return draft


# ────────────────────────── 필수 필드 검사 ──────────────────────────

# 추출 결과에 반드시 있어야 다음 단계로 넘어가는 필드.
_REQUIRED_FIELDS: list[tuple[str, str]] = [
    ("name", "성함"),
    ("email", "이메일"),
    ("course_name", "강의/행사 명"),
    ("course_category", "교과구분 1 (교양/전공)"),
    ("class_format", "교과구분 2 (이론/이론+실기/실기)"),
    ("building", "강의실 구분(건물)"),
    ("requested_type", "강의실 종류"),
    ("capacity_needed", "필요 인원"),
    ("days", "사용 요일"),
    ("time_slot", "사용 시간대"),
    ("weeks", "사용 주차"),
]


def _missing_fields(draft: dict) -> list[str]:
    """누락된 필수 필드의 사람이 읽을 수 있는 라벨 목록."""
    return [
        label for key, label in _REQUIRED_FIELDS
        if key not in draft or draft[key] in (None, "", [])
    ]


def _apply_defaults(draft: dict) -> None:
    """선택 필드의 기본값 채움 (in-place). 필수 필드는 손대지 않는다."""
    draft.setdefault("affiliation", "미지정")
    draft.setdefault("notes", None)
    # weeks 가 미입력이면 전체 학기로 기본
    if "weeks" not in draft:
        draft["weeks"] = WEEK_PRESETS["전체 학기 (1-15주)"]
        draft["weeks_display"] = "전체 학기 (1-15주)"


# ────────────────────────── 요약 / 저장 ──────────────────────────


def _summary_text(d: dict) -> str:
    return (
        f"- **신청자**: {d.get('name')} ({d.get('email')})\n"
        f"- **소속**: {d.get('affiliation')}\n"
        f"- **강의/행사**: {d.get('course_name')}\n"
        f"- **교과구분 1**: {d.get('course_category')}\n"
        f"- **교과구분 2**: {d.get('class_format')}\n"
        f"- **강의실 구분(건물)**: {d.get('building')}\n"
        f"- **강의실 종류**: {d.get('requested_type')}\n"
        f"- **필요 인원**: {d.get('capacity_needed')}명\n"
        f"- **요일**: {', '.join(d.get('days', []))}\n"
        f"- **시간대**: {d.get('time_slot')}\n"
        f"- **주차**: {d.get('weeks_display')}\n"
        f"- **비고**: {d.get('notes') or '없음'}"
    )


def _save_application(semester: Semester, draft: dict) -> int:
    with SessionLocal() as s:
        app = Application(
            semester_id=semester.id,
            applicant_name=draft["name"],
            email=draft["email"],
            affiliation=draft.get("affiliation"),
            course_name=draft["course_name"],
            requested_type=draft["requested_type"],
            capacity_needed=draft["capacity_needed"],
            building=draft.get("building"),
            course_category=draft.get("course_category"),
            class_format=draft.get("class_format"),
            days=draft["days"],
            time_slot=draft["time_slot"],
            weeks=draft["weeks"],
            notes=draft.get("notes"),
            status="pending",
        )
        s.add(app)
        s.flush()
        queue_mail(
            event_kind="confirmation",
            related_id=app.id,
            recipients=[draft["email"]],
            subject=f"[강의실 예약] 신청 접수 확인 (신청 ID #{app.id})",
            body=(
                f"{draft['name']}님,\n\n"
                f"강의실 예약 신청이 접수되었습니다.\n\n"
                f"- 신청 ID: {app.id}\n"
                f"- 강의/행사: {draft['course_name']}\n"
                f"- 강의실 종류: {draft['requested_type']} ({draft['capacity_needed']}명)\n"
                f"- 요일/시간: {', '.join(draft['days'])} {draft['time_slot']}\n\n"
                "배정 결과는 추후 안내드립니다."
            ),
            session=s,
        )
        s.commit()
        return app.id


# ────────────────────────── 패널 1: AI 자연어 입력 ──────────────────────────


def _render_intake_panel(semester: Semester) -> None:
    """자연어 한 문장 → 필드 추출. 모든 필수 필드가 채워지면 확인 패널로 진행."""
    # 위젯 instantiate 전에 pending 값 적용
    if "_pending_intake_text" in st.session_state:
        st.session_state.reg_intake_text = st.session_state.pop("_pending_intake_text")

    with st.expander(f"🤖 AI로 한 번에 입력 ({llm_mode_label()})", expanded=True):
        st.markdown(
            "**이름·이메일·소속·강의명·교과구분·건물·강의실 종류·인원·요일·시간을 한 문장으로 적어주세요.** "
            "Claude 가 필드를 추출합니다. 누락 항목이 있으면 무엇이 빠졌는지 알려드립니다."
        )

        st.text_area(
            "자연어 신청",
            key="reg_intake_text",
            height=140,
            label_visibility="collapsed",
            placeholder="예: 다음 학기 월수 2교시 30명 공학 전공 이론 수업, 공학관 일반강의실 희망합니다. 김민수 minsu@hansung.ac.kr, 컴퓨터공학부.",
        )

        b1, b2 = st.columns([3, 1])
        with b1:
            do_extract = st.button(
                "강의실 예약하기",
                type="primary",
                use_container_width=True,
                key="reg_intake_submit",
                disabled=not st.session_state.reg_intake_text.strip(),
            )
        with b2:
            if st.button("✖ 비우기", use_container_width=True, key="reg_intake_clear"):
                st.session_state["_pending_intake_text"] = ""
                st.rerun()

        # 예시 1건 (참고)
        st.markdown("---")
        head_l, head_r = st.columns([3, 1])
        with head_l:
            st.markdown("💡 **예시** — 클릭하면 위 입력란에 채워집니다")
        with head_r:
            if st.button("🔄 새 예시", use_container_width=True, key="reg_intake_refresh"):
                st.session_state.reg_intake_examples = generate_intake_examples(1)
                st.rerun()

        if not st.session_state.reg_intake_examples:
            st.session_state.reg_intake_examples = generate_intake_examples(1)

        for i, ex in enumerate(st.session_state.reg_intake_examples[:1]):
            if st.button(ex, key=f"reg_intake_ex_{i}", use_container_width=True):
                st.session_state["_pending_intake_text"] = ex
                st.rerun()

        if do_extract:
            text = st.session_state.reg_intake_text.strip()
            with st.spinner("Claude 가 신청 내용을 분석 중입니다…"):
                ex = extract_application_fields(text)
            draft = _normalize_extracted(ex)
            missing = _missing_fields(draft)
            if missing:
                st.error(
                    "다음 정보가 빠졌습니다. 입력 문장에 추가해서 다시 시도해주세요:\n\n"
                    + "\n".join(f"  · {m}" for m in missing)
                )
                with st.expander("지금까지 추출된 항목 보기", expanded=False):
                    if draft:
                        st.markdown(
                            "\n".join(
                                f"- **{k}**: {v}" for k, v in draft.items()
                                if k != "weeks"
                            )
                        )
                    else:
                        st.caption("추출된 항목이 없습니다.")
                return

            _apply_defaults(draft)
            st.session_state.reg_draft = draft
            st.rerun()


# ────────────────────────── 패널 2: 확인 ──────────────────────────


def _render_confirm_panel(semester: Semester) -> None:
    draft = st.session_state.reg_draft
    if draft is None:
        return

    st.markdown("### 📋 신청 내용 확인")
    st.markdown(_summary_text(draft))

    # 일관성 검사 (draft 변경 시에만 다시 호출)
    if st.session_state.get("reg_consistency_for") != id(draft):
        with st.spinner("Claude 가 신청 내용을 점검 중입니다…"):
            st.session_state["reg_consistency_warnings"] = check_consistency(draft)
        st.session_state["reg_consistency_for"] = id(draft)
    warnings = st.session_state.get("reg_consistency_warnings", [])
    if warnings:
        st.warning(
            f"🤖 AI 검토 — 잠재적 이상 {len(warnings)}건 (참고용, 그대로 진행 가능):\n\n"
            + "\n".join(f"- {w}" for w in warnings)
        )

    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ 신청 완료", type="primary", use_container_width=True, key="reg_submit"):
            app_id = _save_application(semester, draft)
            st.session_state.reg_last_app_id = app_id
            st.session_state.reg_done = True
            st.session_state.reg_draft = None
            st.rerun()
    with c2:
        if st.button("↺ 처음부터 다시", use_container_width=True, key="reg_restart_confirm"):
            _reset_state()
            st.rerun()


# ────────────────────────── 우측 패널 — DB 미러 ──────────────────────────


def _render_right_pane(semester: Semester) -> None:
    st.subheader("🗄️ 신청 등록 DB (실시간)")
    with SessionLocal() as s:
        apps = list(
            s.scalars(
                select(Application)
                .where(Application.semester_id == semester.id)
                .order_by(Application.created_at.desc())
            ).all()
        )

    c1, c2, c3 = st.columns(3)
    c1.metric("총 신청", len(apps))
    c2.metric("대기", sum(1 for a in apps if a.status == "pending"))
    c3.metric("배정 완료", sum(1 for a in apps if a.status == "assigned"))

    if not apps:
        st.info("아직 신청이 없습니다. 왼쪽에서 자연어로 신청해 보세요.")
        return

    rows = [
        {
            "ID": a.id,
            "신청자": a.applicant_name,
            "강의/행사": a.course_name,
            "교과1": a.course_category or "-",
            "교과2": a.class_format or "-",
            "건물": a.building or "-",
            "강의실종류": a.requested_type,
            "인원": a.capacity_needed,
            "요일": ", ".join(a.days),
            "시간대": a.time_slot,
            "상태": a.status,
            "접수": a.created_at.strftime("%m-%d %H:%M:%S"),
        }
        for a in apps
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True, height=440)


# ────────────────────────── 엔트리 ──────────────────────────


def render(semester: Semester) -> None:
    _init_state()

    left, right = st.columns([1, 1], gap="medium")

    with left:
        st.subheader("📝 강의실 예약 신청")

        if st.session_state.reg_done:
            st.success(
                f"✅ 신청이 접수되었습니다 — 신청 ID **#{st.session_state.reg_last_app_id}**"
            )
            st.info("접수 확인 메일이 발송 대기열에 추가되었습니다. (어드민 페이지에서 시뮬레이션 발송 가능)")
            if st.button("➕ 새 신청 시작", use_container_width=True, key="reg_restart_done"):
                _reset_state()
                st.rerun()
        elif st.session_state.reg_draft is not None:
            _render_confirm_panel(semester)
        else:
            _render_intake_panel(semester)

    with right:
        _render_right_pane(semester)
