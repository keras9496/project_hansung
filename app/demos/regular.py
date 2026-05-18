"""정규 신청 챗봇 데모.

좌측: 챗봇과 폼 위젯이 교차하는 대화. '신청 완료' 클릭 시 즉시 DB 저장.
우측: 현재 학기에 등록된 신청 테이블(가장 최근 신청이 상단).

AI 보조 기능 (Claude API):
    - 도입부 "AI로 한 번에 입력" → 자연어 → 9개 필드 추출 → 누락분만 추가 질문.
    - confirm 단계 → 신청-정책 일관성 검사(소프트 경고).
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


STEPS = [
    ("name", "안녕하세요. 강의실 예약 챗봇입니다. 먼저 **신청자 성함**을 알려주세요."),
    ("email", "**이메일**을 알려주세요. (예: id@hansung.ac.kr)"),
    ("affiliation", "**소속**(학과·동아리·부서)을 알려주세요."),
    ("course_name", "**강의 또는 행사 명**을 알려주세요."),
    ("course_category", "**교과구분 1**을 선택해주세요. (교양 / 전공-전공계열)"),
    ("class_format", "**교과구분 2**를 선택해주세요. (이론 / 이론+실기 / 실기)"),
    ("building", "희망 **강의실 구분(건물)**을 선택해주세요."),
    ("requested_type", "필요한 **강의실 종류**를 선택해주세요."),
    ("capacity_needed", "**필요 수용 인원**은 몇 명인가요?"),
    ("days", "**사용 요일**을 모두 선택해주세요."),
    ("time_slot", "**사용 시간대**를 선택해주세요."),
    ("weeks", "**사용 주차 범위**를 선택해주세요."),
    ("notes", "**추가 요구사항**이 있으면 적어주세요. (없으면 비워두세요)"),
    ("confirm", "입력하신 내용을 확인해주세요. 아래 **'신청 완료'** 버튼을 누르면 접수됩니다."),
]


STEP_KEYS = [k for k, _ in STEPS]


def _init_state() -> None:
    if "reg_step" not in st.session_state:
        _reset_state()
    if "reg_intake_text" not in st.session_state:
        st.session_state.reg_intake_text = ""
    if "reg_intake_examples" not in st.session_state:
        st.session_state.reg_intake_examples = []


def _reset_state() -> None:
    st.session_state.reg_step = 0
    st.session_state.reg_draft = {}
    st.session_state.reg_chat = [{"role": "assistant", "content": STEPS[0][1]}]
    st.session_state.reg_done = False
    # reg_intake_text 는 위젯의 key 이므로 instantiate 된 뒤엔 직접 수정 불가.
    # pending 키로 두면 다음 rerun 시 intake 패널이 받아 적용한다.
    st.session_state["_pending_intake_text"] = ""


# ─────────────────────────── 정규화 헬퍼 ───────────────────────────


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
        # 정확히 일치하면 사용, 아니면 부분 매칭으로 첫 후보 픽
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


def _next_missing_step(draft: dict) -> int:
    """draft 에 비어 있는 첫 STEP 인덱스. 전부 차 있으면 confirm.

    notes 는 한 번 물어본 뒤(키가 draft 에 들어오면 값이 None 이어도) 더 묻지 않는다.
    이미 채워진 단계는 건너뛴다 (intake 가 미리 채운 필드를 다시 묻지 않게 하기 위함).
    """
    for i, key in enumerate(STEP_KEYS):
        if key == "confirm":
            return i
        if key == "notes":
            # 한 번 물어봐서 어떤 값이든(빈 답 포함) 받았으면 skip
            if "notes" in draft:
                continue
            return i
        if key not in draft or draft[key] in (None, "", []):
            return i
    return STEP_KEYS.index("confirm")


def _append_bot(content: str) -> None:
    st.session_state.reg_chat.append({"role": "assistant", "content": content})


def _append_user(content: str) -> None:
    st.session_state.reg_chat.append({"role": "user", "content": content})


def _summary_text(d: dict) -> str:
    return (
        "입력하신 내용을 확인해주세요:\n\n"
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
        f"- **비고**: {d.get('notes') or '없음'}\n\n"
        "아래 **'신청 완료'** 버튼을 누르면 접수됩니다."
    )


def _validate(key: str, val):
    """반환: (ok, error_msg, normalized, display)"""
    if key == "name":
        v = (val or "").strip()
        if not v:
            return False, "성함을 입력해주세요.", None, None
        return True, "", v, v
    if key == "email":
        v = (val or "").strip()
        if "@" not in v or "." not in v:
            return False, "올바른 이메일 형식이 아닙니다.", None, None
        return True, "", v, v
    if key == "affiliation":
        v = (val or "").strip() or "미지정"
        return True, "", v, v
    if key == "course_name":
        v = (val or "").strip()
        if not v:
            return False, "강의/행사 명을 입력해주세요.", None, None
        return True, "", v, v
    if key == "course_category":
        if val not in COURSE_CATEGORIES:
            return False, "교과구분 1을 선택해주세요.", None, None
        return True, "", val, val
    if key == "class_format":
        if val not in CLASS_FORMATS:
            return False, "교과구분 2를 선택해주세요.", None, None
        return True, "", val, val
    if key == "building":
        if val not in BUILDING_NAMES:
            return False, "강의실 구분(건물)을 선택해주세요.", None, None
        return True, "", val, BUILDING_LABELS.get(val, val)
    if key == "requested_type":
        return True, "", val, val
    if key == "capacity_needed":
        return True, "", int(val), f"{int(val)}명"
    if key == "days":
        if not val:
            return False, "최소 한 개 이상의 요일을 선택해주세요.", None, None
        return True, "", list(val), ", ".join(val)
    if key == "time_slot":
        return True, "", val, val
    if key == "weeks":
        normalized = WEEK_PRESETS.get(val, list(range(1, 16)))
        return True, "", normalized, val
    if key == "notes":
        v = (val or "").strip()
        return True, "", (v or None), (v or "없음")
    return False, "알 수 없는 단계", None, None


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


def _render_chat() -> None:
    for msg in st.session_state.reg_chat:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


def _render_intake_panel(semester: Semester) -> None:
    """챗봇 시작 전, 자연어 한 문장으로 9개 필드를 한 번에 채우는 패널."""
    if st.session_state.reg_step > 0:
        return  # 이미 진행 중이면 노출하지 않음

    # ⚠ Streamlit 제약: 위젯이 instantiate 된 뒤엔 session_state[key] 를 직접 수정 불가.
    # 예시/비우기 버튼 핸들러는 _pending_intake_text 에 값을 두고 rerun → 이 위치에서 적용.
    if "_pending_intake_text" in st.session_state:
        st.session_state.reg_intake_text = st.session_state.pop("_pending_intake_text")

    with st.expander(f"🤖 AI로 한 번에 입력 ({llm_mode_label()})", expanded=True):
        st.markdown(
            "**이름·이메일·소속·강의명·교과구분·건물·강의실 종류·인원·요일·시간을 한 문장으로 적어주세요.** "
            "Claude 가 필드를 추출하고, 빠진 부분만 챗봇이 마저 물어봅니다."
        )

        # 1순위: 텍스트 입력 — 사용자가 곧바로 클릭해 작성 시작
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

        # 2순위: 예시 1건 (참고용) — 아래에 작게
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
            if not draft:
                st.error("추출된 필드가 없습니다. 더 구체적으로 적어주세요.")
                return
            st.session_state.reg_draft = draft
            # 챗봇 로그에 사용자가 한 문장으로 입력한 흐름을 기록
            _append_user(text)
            picked = ", ".join(f"{k}={draft[k]}" for k in draft if k != "weeks")
            _append_bot(
                "AI가 다음 항목을 자동으로 추출했습니다:\n\n"
                f"`{picked}`\n\n"
                "누락된 항목만 이어서 여쭤보겠습니다."
            )
            next_step = _next_missing_step(draft)
            st.session_state.reg_step = next_step
            next_key, next_q = STEPS[next_step]
            if next_key == "confirm":
                _append_bot(_summary_text(draft))
            else:
                _append_bot(next_q)
            st.rerun()


def _render_input(semester: Semester) -> None:
    step = st.session_state.reg_step
    if step >= len(STEPS):
        return

    key, _ = STEPS[step]
    draft = st.session_state.reg_draft

    # 확정 단계
    if key == "confirm":
        # 일관성 검사 (캐시 — confirm 진입 시 1회만 호출)
        cache_key = "reg_consistency_for"
        if st.session_state.get(cache_key) != id(draft):
            with st.spinner("Claude 가 신청 내용을 점검 중입니다…"):
                st.session_state["reg_consistency_warnings"] = check_consistency(draft)
            st.session_state[cache_key] = id(draft)
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
                _append_user("신청 완료를 클릭했습니다.")
                _append_bot(
                    f"신청이 접수되었습니다. **신청 ID #{app_id}**\n\n"
                    "접수 확인 메일이 발송 대기열에 추가되었습니다. (어드민 페이지에서 시뮬레이션 발송 가능)"
                )
                st.session_state.reg_step = len(STEPS)
                st.session_state.reg_done = True
                st.rerun()
        with c2:
            if st.button("↺ 처음부터 다시", use_container_width=True, key="reg_restart_confirm"):
                _reset_state()
                st.rerun()
        return

    with st.form(f"reg_step_form_{step}", clear_on_submit=True):
        if key == "name":
            val = st.text_input("성함", key=f"in_{key}_{step}")
        elif key == "email":
            val = st.text_input("이메일", key=f"in_{key}_{step}")
        elif key == "affiliation":
            val = st.text_input("소속", key=f"in_{key}_{step}")
        elif key == "course_name":
            val = st.text_input("강의/행사 명", key=f"in_{key}_{step}")
        elif key == "course_category":
            val = st.selectbox("교과구분 1", COURSE_CATEGORIES, key=f"in_{key}_{step}")
        elif key == "class_format":
            val = st.selectbox("교과구분 2", CLASS_FORMATS, key=f"in_{key}_{step}")
        elif key == "building":
            pref = preferred_building_for(draft.get("course_category"))
            default_idx = BUILDING_NAMES.index(pref) if pref in BUILDING_NAMES else 0
            val = st.selectbox(
                "강의실 구분(건물)",
                BUILDING_NAMES,
                index=default_idx,
                format_func=lambda b: BUILDING_LABELS.get(b, b),
                key=f"in_{key}_{step}",
                help="전공 수업은 해당 전공 건물이 자동으로 우선 추천됩니다.",
            )
        elif key == "requested_type":
            types = get_classroom_types()
            val = st.selectbox("강의실 종류", types, key=f"in_{key}_{step}")
        elif key == "capacity_needed":
            val = st.number_input("필요 인원", min_value=1, max_value=200, value=30, step=1, key=f"in_{key}_{step}")
        elif key == "days":
            val = st.multiselect("요일", DAYS, default=["월"], key=f"in_{key}_{step}")
        elif key == "time_slot":
            val = st.selectbox("시간대", TIME_SLOTS, key=f"in_{key}_{step}")
        elif key == "weeks":
            val = st.selectbox("주차 범위", list(WEEK_PRESETS.keys()), key=f"in_{key}_{step}")
        elif key == "notes":
            val = st.text_area("비고 (선택)", placeholder="예: 빔프로젝터 필요", key=f"in_{key}_{step}")
        else:
            val = None

        submitted = st.form_submit_button("다음 ▶")
        if submitted:
            ok, msg, normalized, display = _validate(key, val)
            if not ok:
                st.error(msg)
                return
            draft[key] = normalized
            if key == "weeks":
                draft["weeks_display"] = display
            _append_user(display)
            # 다음 빈 단계로 점프 — 이미 채워진 단계(intake 로 채워졌거나 weeks 등)는 건너뛴다.
            st.session_state.reg_step = _next_missing_step(draft)
            next_key, next_q = STEPS[st.session_state.reg_step]
            if next_key == "confirm":
                _append_bot(_summary_text(draft))
            else:
                _append_bot(next_q)
            st.rerun()


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
        st.info("아직 신청이 없습니다. 왼쪽 챗봇에서 신청을 진행해 보세요.")
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


def render(semester: Semester) -> None:
    _init_state()

    left, right = st.columns([1, 1], gap="medium")

    with left:
        st.subheader("💬 신청자 챗봇")
        _render_intake_panel(semester)
        chat_container = st.container(height=520, border=True)
        with chat_container:
            _render_chat()
        _render_input(semester)

        if st.session_state.get("reg_done"):
            if st.button("➕ 새 신청 시작", use_container_width=True, key="reg_restart_done"):
                _reset_state()
                st.rerun()

    with right:
        _render_right_pane(semester)
