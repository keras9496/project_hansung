"""정규 신청 — AI 자연어 입력 + 누락 시 챗봇 보강.

좌측 흐름:
    1) 🤖 AI 한 번에 입력 (자연어 한 문장 → 필드 추출)
    2) 누락 필드가 있으면 → 💬 챗봇이 빠진 항목만 차례로 묻는다
    3) 모든 필수 필드가 모이면 → 📋 확인 패널 (요약 + AI 일관성 검사 + 신청 완료)
우측: 현재 학기에 등록된 신청 테이블 (가장 최근이 상단).

AI 보조 (Claude API):
    - 자연어 → 필드 추출 (extract_application_fields)
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
    are_consecutive_slots,
    get_classroom_types,
    join_time_slots,
    preferred_building_for,
    slot_number,
    split_time_slots,
)


# ────────────────────── 필드 정의 (필수 vs 선택) ──────────────────────

# (key, missing_label, chat_prompt)
# 신청서는 교과목 1건 = 강의자(교수) 1명이 작성한다는 가정.
# 조교/대리 신청 시에도 이름은 강의자 본인을 입력한다.
_REQUIRED_FIELDS: list[tuple[str, str, str]] = [
    ("name", "강의자 성함", "**강의자(교수) 본인의 성함**을 알려주세요. (대리 신청이어도 강의자 본인 이름)"),
    ("email", "이메일", "**이메일**을 알려주세요. (예: id@hansung.ac.kr)"),
    ("affiliation", "강의자 소속(학과)", "강의자의 **소속 학과**를 알려주세요. (예: 컴퓨터공학부 / 패션디자인학과)"),
    ("course_name", "강의/행사 명", "**강의 또는 행사 명**을 알려주세요."),
    ("course_category", "교과구분 1 (교양/전공)", "**교과구분 1**을 선택해주세요. (교양 / 전공-XXX)"),
    ("class_format", "교과구분 2 (이론/이론+실기/실기)", "**교과구분 2**를 선택해주세요. (이론 / 이론+실기 / 실기)"),
    ("building", "강의실 구분(건물)", "희망 **강의실 구분(건물)**을 선택해주세요."),
    ("requested_type", "강의실 종류", "필요한 **강의실 종류**를 선택해주세요."),
    ("capacity_needed", "필요 인원", "**필요 수용 인원**은 몇 명인가요?"),
    ("days", "사용 요일", "**사용 요일**을 모두 선택해주세요."),
    ("time_slot", "사용 시간대", "**사용 시간대**를 선택해주세요. (1교시 또는 연속된 2교시까지 선택 가능)"),
    # weeks / notes 는 자동 기본값 처리 (챗봇이 묻지 않음)
]

_PROMPT_BY_KEY: dict[str, str] = {k: p for k, _l, p in _REQUIRED_FIELDS}
_LABEL_BY_KEY: dict[str, str] = {k: l for k, l, _p in _REQUIRED_FIELDS}


# ────────────────────────── 상태 ──────────────────────────


def _init_state() -> None:
    if "reg_draft" not in st.session_state:
        st.session_state.reg_draft = None
    if "reg_chat" not in st.session_state:
        st.session_state.reg_chat = []
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
    st.session_state.reg_chat = []
    st.session_state.reg_done = False
    st.session_state.reg_last_app_id = None
    st.session_state["reg_consistency_warnings"] = []
    st.session_state.pop("reg_consistency_for", None)
    st.session_state["_pending_intake_text"] = ""


def _append_bot(content: str) -> None:
    st.session_state.reg_chat.append({"role": "assistant", "content": content})


def _append_user(content: str) -> None:
    st.session_state.reg_chat.append({"role": "user", "content": content})


# ────────────────────────── 정규화 / 누락 검사 ──────────────────────────


_TIME_NUM_RE = re.compile(r"(\d+)\s*교시")
_TIME_RANGE_RE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*교시")


def _slot_label_for(n: int) -> str | None:
    for slot in TIME_SLOTS:
        if slot.startswith(f"{n}교시"):
            return slot
    return None


def _normalize_time_slot(value: str | None) -> str | None:
    """단일/다중 교시 표현을 정식 라벨로 정규화.

    반환은 단일 라벨 또는 ' + ' 로 이은 연속 2교시 라벨.
    """
    if not value:
        return None
    # 이미 정식 라벨 또는 ' + ' 결합된 정식 라벨이면 그대로
    if value in TIME_SLOTS:
        return value
    if " + " in value:
        parts = [p.strip() for p in value.split(" + ")]
        normalized_parts: list[str] = []
        for p in parts:
            sub = _normalize_time_slot(p)
            if sub:
                normalized_parts.extend(split_time_slots(sub))
        unique_sorted = sorted(set(normalized_parts), key=lambda s: slot_number(s) or 99)
        if not unique_sorted:
            return None
        if len(unique_sorted) >= 2 and are_consecutive_slots(unique_sorted[:2]):
            return join_time_slots(unique_sorted[:2])
        return unique_sorted[0]

    # 범위 표기: "1-2교시", "1~2교시"
    rm = _TIME_RANGE_RE.search(value)
    if rm:
        a, b = int(rm.group(1)), int(rm.group(2))
        if a > b:
            a, b = b, a
        if 1 <= a <= 8 and 1 <= b <= 8 and (b - a) == 1:
            sa, sb = _slot_label_for(a), _slot_label_for(b)
            if sa and sb:
                return join_time_slots([sa, sb])
            return sa or sb

    # 다중 "N교시" 패턴
    all_nums = [int(x) for x in _TIME_NUM_RE.findall(value) if 1 <= int(x) <= 8]
    if not all_nums:
        return None
    uniq = sorted(set(all_nums))
    if len(uniq) >= 2 and uniq[1] - uniq[0] == 1:
        sa, sb = _slot_label_for(uniq[0]), _slot_label_for(uniq[1])
        if sa and sb:
            return join_time_slots([sa, sb])
    return _slot_label_for(uniq[0])


def _normalize_extracted(ex: ExtractedApplication) -> dict:
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


def _next_missing_key(draft: dict) -> str | None:
    """다음에 챗봇이 물어볼 필수 필드 key. 더 없으면 None."""
    for key, _label, _prompt in _REQUIRED_FIELDS:
        if key not in draft or draft[key] in (None, "", []):
            return key
    return None


def _missing_labels(draft: dict) -> list[str]:
    return [
        label for key, label, _ in _REQUIRED_FIELDS
        if key not in draft or draft[key] in (None, "", [])
    ]


def _apply_defaults(draft: dict) -> None:
    # affiliation 은 필수 필드로 승격됐으므로 자동 기본값 미적용.
    draft.setdefault("notes", None)
    if "weeks" not in draft:
        draft["weeks"] = WEEK_PRESETS["전체 학기 (1-15주)"]
        draft["weeks_display"] = "전체 학기 (1-15주)"


# ────────────────────────── 요약 / 저장 ──────────────────────────


def _summary_text(d: dict) -> str:
    return (
        f"- **강의자(교수)**: {d.get('name')} ({d.get('email')})\n"
        f"- **소속(학과)**: {d.get('affiliation')}\n"
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
    if "_pending_intake_text" in st.session_state:
        st.session_state.reg_intake_text = st.session_state.pop("_pending_intake_text")

    with st.expander(f"🤖 AI로 한 번에 입력 ({llm_mode_label()})", expanded=True):
        st.markdown(
            "**강의자(교수) 본인 이름·이메일·소속 학과·강의명·교과구분·건물·강의실 종류·인원·요일·시간을 한 문장으로 적어주세요.** "
            "Claude 가 필드를 추출합니다. 누락된 항목은 아래 챗봇이 차례로 묻습니다. "
            "시간대는 단일 교시 또는 **연속된 2교시**(예: 1-2교시) 까지 가능합니다."
        )

        st.text_area(
            "유저 친화적 신청",
            key="reg_intake_text",
            height=140,
            label_visibility="collapsed",
            placeholder="예: 강의자 김민수 교수 (minsu@hansung.ac.kr, 컴퓨터공학부). 자료구조 수업, 공학 전공 이론, 월수 1-2교시 30명, 공학관 일반강의실 희망합니다.",
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

        # 예시 1건
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
            st.session_state.reg_draft = draft

            # 챗봇 히스토리 초기화
            extracted_lines = [
                f"- **{k}**: {v}" for k, v in draft.items() if k not in ("weeks", "weeks_display")
            ]
            preview = "\n".join(extracted_lines) if extracted_lines else "_(추출된 항목 없음)_"

            missing = _missing_labels(draft)
            if missing:
                next_key = _next_missing_key(draft)
                intro = (
                    "AI가 다음 항목을 자동 추출했습니다:\n\n"
                    f"{preview}\n\n"
                    f"빠진 항목 {len(missing)}개를 차례로 여쭤보겠습니다.\n\n"
                    f"---\n\n{_PROMPT_BY_KEY[next_key]}"
                )
            else:
                intro = (
                    "AI가 모든 항목을 자동 추출했습니다:\n\n"
                    f"{preview}\n\n"
                    "확인 단계로 이동합니다."
                )
            st.session_state.reg_chat = [
                {"role": "user", "content": text},
                {"role": "assistant", "content": intro},
            ]
            st.rerun()


# ────────────────────────── 패널 2: 챗봇 (누락 보강) ──────────────────────────


def _render_chat_history() -> None:
    chat_container = st.container(height=400, border=True)
    with chat_container:
        for msg in st.session_state.reg_chat:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])


def _render_field_widget(key: str, draft: dict):
    """단계별 입력 위젯. 반환값은 폼이 제출됐을 때의 (normalized, display)."""
    widget_key = f"chat_in_{key}"
    if key in ("name", "email", "course_name", "affiliation"):
        return st.text_input(_LABEL_BY_KEY.get(key, key), key=widget_key)
    if key == "course_category":
        return st.selectbox("교과구분 1", COURSE_CATEGORIES, key=widget_key)
    if key == "class_format":
        return st.selectbox("교과구분 2", CLASS_FORMATS, key=widget_key)
    if key == "building":
        pref = preferred_building_for(draft.get("course_category"))
        default_idx = BUILDING_NAMES.index(pref) if pref in BUILDING_NAMES else 0
        return st.selectbox(
            "강의실 구분(건물)",
            BUILDING_NAMES,
            index=default_idx,
            format_func=lambda b: BUILDING_LABELS.get(b, b),
            key=widget_key,
            help="전공 수업은 전공 건물이 우선 추천됩니다.",
        )
    if key == "requested_type":
        types = get_classroom_types() or ["(강의실 없음)"]
        return st.selectbox("강의실 종류", types, key=widget_key)
    if key == "capacity_needed":
        return st.number_input("필요 인원", min_value=1, max_value=300, value=30, step=1, key=widget_key)
    if key == "days":
        return st.multiselect("요일", DAYS, default=["월"], key=widget_key)
    if key == "time_slot":
        return st.multiselect(
            "시간대 (1교시 또는 연속된 2교시까지)",
            TIME_SLOTS,
            default=[TIME_SLOTS[0]],
            max_selections=2,
            key=widget_key,
            help="연속이 아닌 교시(예: 1교시+3교시)는 선택할 수 없습니다.",
        )
    return None


def _validate_and_normalize(key: str, val) -> tuple[bool, str, object, str]:
    """반환: (ok, error_msg, normalized, display)"""
    if key in ("name", "course_name"):
        v = (val or "").strip()
        if not v:
            return False, "값을 입력해주세요.", None, ""
        return True, "", v, v
    if key == "affiliation":
        v = (val or "").strip()
        if not v:
            return False, "강의자 소속(학과)을 입력해주세요.", None, ""
        return True, "", v, v
    if key == "email":
        v = (val or "").strip()
        if "@" not in v or "." not in v:
            return False, "올바른 이메일을 입력해주세요.", None, ""
        return True, "", v, v
    if key in ("course_category", "class_format", "building", "requested_type"):
        return True, "", val, val
    if key == "capacity_needed":
        return True, "", int(val), f"{int(val)}명"
    if key == "days":
        if not val:
            return False, "요일을 한 개 이상 선택해주세요.", None, ""
        return True, "", list(val), ", ".join(val)
    if key == "time_slot":
        slots = list(val) if isinstance(val, list) else ([val] if val else [])
        if not slots:
            return False, "시간대를 한 개 이상 선택해주세요.", None, ""
        if len(slots) > 2:
            return False, "연속된 2교시까지만 선택할 수 있습니다.", None, ""
        if not are_consecutive_slots(slots):
            return False, "선택한 교시는 연속이어야 합니다. (예: 1교시+2교시 OK / 1교시+3교시 X)", None, ""
        normalized = join_time_slots(slots)
        return True, "", normalized, normalized
    return False, "알 수 없는 단계", None, ""


def _render_chat_panel(semester: Semester) -> None:
    draft = st.session_state.reg_draft
    if draft is None:
        return

    _render_chat_history()

    next_key = _next_missing_key(draft)
    if next_key is None:
        return  # confirm 로 라우팅 (render() 에서 분기)

    with st.form(f"chat_form_{next_key}", clear_on_submit=True):
        st.markdown(_PROMPT_BY_KEY[next_key])
        val = _render_field_widget(next_key, draft)
        c1, c2 = st.columns([3, 1])
        with c1:
            submitted = st.form_submit_button("다음 ▶", type="primary", use_container_width=True)
        with c2:
            cancel = st.form_submit_button("↺ 처음부터", use_container_width=True)

    if cancel:
        _reset_state()
        st.rerun()
        return

    if submitted:
        ok, err, normalized, display = _validate_and_normalize(next_key, val)
        if not ok:
            st.error(err)
            return
        draft[next_key] = normalized
        _append_user(display)
        next_after = _next_missing_key(draft)
        if next_after is None:
            _append_bot("감사합니다. 모든 정보가 모였습니다. 확인 단계로 이동합니다.")
        else:
            _append_bot(_PROMPT_BY_KEY[next_after])
        st.rerun()


# ────────────────────────── 패널 3: 확인 ──────────────────────────


def _render_confirm_panel(semester: Semester) -> None:
    draft = st.session_state.reg_draft
    if draft is None:
        return

    _apply_defaults(draft)

    st.markdown("### 📋 신청 내용 확인")
    st.markdown(_summary_text(draft))

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
            st.session_state.reg_chat = []
            st.rerun()
    with c2:
        if st.button("↺ 처음부터 다시", use_container_width=True, key="reg_restart_confirm"):
            _reset_state()
            st.rerun()


# ────────────────────────── 우측 패널 ──────────────────────────


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
        st.info("아직 신청이 없습니다. 왼쪽에서 유저 친화적으로 신청해 보세요.")
        return

    rows = [
        {
            "ID": a.id,
            "강의자": a.applicant_name,
            "소속(학과)": a.affiliation or "-",
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
            if _next_missing_key(st.session_state.reg_draft) is None:
                _render_confirm_panel(semester)
            else:
                _render_chat_panel(semester)
        else:
            _render_intake_panel(semester)

    with right:
        _render_right_pane(semester)
