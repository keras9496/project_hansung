"""배정 실행 데모.

좌측: 학기 신청 현황 + '지금 배정 실행' 버튼 + 직전 실행 로그.
우측: 배정 결과 테이블 + 데드락 목록 (AI 협상안 생성 포함).
"""
from __future__ import annotations

import streamlit as st
from sqlalchemy import select

from app.db import SessionLocal
from app.demos._shared import is_practice_room
from app.models import Application, Assignment, Classroom, Semester
from app.services.assignment_engine import run_assignment
from app.services.llm import (
    mode_label as llm_mode_label,
    suggest_deadlock_alternatives,
)


def _left_pane(semester: Semester) -> None:
    st.subheader("⚙️ 배정 실행")
    st.caption(f"대상 학기: **{semester.year}-{semester.term}학기**")

    with SessionLocal() as s:
        apps = list(
            s.scalars(select(Application).where(Application.semester_id == semester.id)).all()
        )

    total = len(apps)
    pending = sum(1 for a in apps if a.status == "pending")
    assigned = sum(1 for a in apps if a.status == "assigned")
    deadlock = sum(1 for a in apps if a.status == "deadlock")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("총 신청", total)
    c2.metric("대기", pending)
    c3.metric("배정", assigned)
    c4.metric("데드락", deadlock)

    st.markdown(
        "**배정 정책**  \n"
        "- 우선순위: 도착순(FCFS)  \n"
        "- 하드 규칙: 이론 수업은 실기/실습실 배정 불가  \n"
        "- 소프트 규칙: 전공 수업은 전공 건물(공학→공학관 / 무용→낙산관 / "
        "회화→지선관 / 패션+디자인→창의관) 우선 → 신청자 희망 건물 우선 → 그 외  \n"
        "- best-fit: 적합 강의실 중 수용인원이 가장 작은 강의실 우선"
    )

    c1, c2 = st.columns(2)
    with c1:
        run_clicked = st.button(
            "🚀 지금 배정 실행",
            type="primary",
            use_container_width=True,
            disabled=(total == 0),
            key="assign_run",
        )
    with c2:
        seeded = st.checkbox("재현 가능한 셔플(seed=42)", value=False, key="assign_seed")

    if run_clicked:
        result = run_assignment(semester.id, seed=42 if seeded else None)
        st.session_state["assign_last_result"] = result
        st.rerun()

    last = st.session_state.get("assign_last_result")
    if last:
        st.success(
            f"직전 실행: 총 {last['total']}건 중 **{last['assigned']}건 배정**, "
            f"**{last['deadlock']}건 데드락**"
        )
        tier_label = {"major": "전공건물", "user": "희망건물", "rest": "그 외"}
        with st.expander("실행 상세 로그", expanded=False):
            for d in last["details"]:
                if d["result"] == "assigned":
                    tier = tier_label.get(d.get("tier", ""), d.get("tier", ""))
                    bld = d.get("classroom_building") or "-"
                    st.write(
                        f"- #{d['application_id']} {d['applicant']} → "
                        f"{d['classroom_code']} {d['classroom_name']} ({bld}, {tier})"
                    )
                else:
                    reason = d.get("reason") or ""
                    st.write(
                        f"- #{d['application_id']} {d['applicant']} → **DEADLOCK**"
                        + (f" — {reason}" if reason else "")
                    )


def _right_pane(semester: Semester) -> None:
    st.subheader("📋 배정 결과 / 데드락")

    with SessionLocal() as s:
        rows = s.execute(
            select(
                Assignment.id,
                Application.id,
                Application.applicant_name,
                Application.course_name,
                Application.requested_type,
                Application.capacity_needed,
                Application.course_category,
                Application.class_format,
                Application.building,
                Classroom.code,
                Classroom.name,
                Classroom.capacity,
                Classroom.building,
                Application.days,
                Application.time_slot,
                Assignment.method,
            )
            .join(Application, Application.id == Assignment.application_id)
            .join(Classroom, Classroom.id == Assignment.classroom_id)
            .where(Assignment.semester_id == semester.id)
            .order_by(Application.id)
        ).all()

        deadlocks = list(
            s.scalars(
                select(Application).where(
                    Application.semester_id == semester.id,
                    Application.status == "deadlock",
                )
            ).all()
        )

    if rows:
        df = []
        for r in rows:
            (
                assign_id, app_id, applicant, course_name, requested_type,
                capacity_needed, course_category, class_format, app_building,
                room_code, room_name, room_capacity, room_building,
                days, time_slot, method,
            ) = r
            hint = ""
            if app_building and room_building and app_building != room_building:
                hint = f" (희망:{app_building})"
            df.append({
                "배정ID": assign_id,
                "신청ID": app_id,
                "신청자": applicant,
                "강의/행사": course_name,
                "교과1": course_category or "-",
                "교과2": class_format or "-",
                "종류": requested_type,
                "필요인원": capacity_needed,
                "배정 건물": f"{room_building or '-'}{hint}",
                "배정 강의실": f"{room_code} {room_name} (수용{room_capacity})",
                "요일": ", ".join(days),
                "시간대": time_slot,
                "방식": method,
            })
        st.markdown(f"**배정 완료: {len(rows)}건**")
        st.dataframe(df, use_container_width=True, hide_index=True, height=320)
    else:
        st.info("배정된 건이 없습니다. 왼쪽에서 '지금 배정 실행'을 눌러보세요.")

    if deadlocks:
        st.markdown("---")
        st.error(f"⚠️ 데드락 {len(deadlocks)}건 — 어드민 메일 발송함에서 조율 메일을 시뮬레이션 전송할 수 있습니다.")
        d_rows = [
            {
                "신청ID": d.id,
                "신청자": d.applicant_name,
                "이메일": d.email,
                "교과1": d.course_category or "-",
                "교과2": d.class_format or "-",
                "희망건물": d.building or "-",
                "종류": d.requested_type,
                "필요인원": d.capacity_needed,
                "요일": ", ".join(d.days),
                "시간대": d.time_slot,
            }
            for d in deadlocks
        ]
        st.dataframe(d_rows, use_container_width=True, hide_index=True)

        st.markdown(f"#### 🤖 AI 협상안 ({llm_mode_label()})")
        st.caption(
            "각 데드락 건을 펼쳐 'AI 협상안 생성' 을 누르면 Claude 가 가용 슬롯을 분석해 "
            "구체적인 대안 2~3개와 협상 메일 초안을 만들어 줍니다."
        )
        for d in deadlocks:
            _render_deadlock_card(semester, d)


# ─────────────────────── 데드락 협상안 (AI) ───────────────────────


def _render_deadlock_card(semester: Semester, app: Application) -> None:
    title = (
        f"#{app.id} {app.applicant_name} · {app.course_name} "
        f"· {app.requested_type} · {', '.join(app.days)} {app.time_slot}"
    )
    with st.expander(title, expanded=False):
        cache_key = f"deadlock_proposal_{app.id}"
        existing = st.session_state.get(cache_key)

        c1, c2 = st.columns([1, 1])
        with c1:
            run_clicked = st.button(
                "🤖 AI 협상안 생성", type="primary", use_container_width=True,
                key=f"deadlock_gen_{app.id}",
            )
        with c2:
            if existing and st.button("🗑 재생성", use_container_width=True, key=f"deadlock_clear_{app.id}"):
                st.session_state.pop(cache_key, None)
                st.rerun()

        if run_clicked:
            free_pool, occupied_pool = _build_pools_for(semester.id, app)
            payload = {
                "application_id": app.id,
                "applicant_name": app.applicant_name,
                "course_name": app.course_name,
                "course_category": app.course_category,
                "class_format": app.class_format,
                "building": app.building,
                "requested_type": app.requested_type,
                "capacity_needed": app.capacity_needed,
                "days": list(app.days),
                "time_slot": app.time_slot,
            }
            with st.spinner("Claude 가 가용 슬롯을 분석 중입니다…"):
                proposal = suggest_deadlock_alternatives(payload, free_pool, occupied_pool)
            st.session_state[cache_key] = proposal
            existing = proposal

        if existing is None:
            return

        if existing.alternatives:
            st.markdown("**제안 대안**")
            kind_label = {
                "time_shift": "🕐 시간 이동",
                "building_swap": "🏛 건물 교체",
                "format_relax": "🎯 포맷 조정",
            }
            for alt in existing.alternatives:
                head = kind_label.get(alt.kind, alt.kind)
                room = ""
                if alt.suggested_classroom_code:
                    room = (
                        f" — {alt.suggested_classroom_code} "
                        f"{alt.suggested_classroom_name or ''}".strip()
                    )
                slot = ""
                if alt.suggested_time_slot or alt.suggested_days:
                    days = ",".join(alt.suggested_days or [])
                    slot = f" / {days} {alt.suggested_time_slot or ''}".strip()
                st.markdown(f"- **{head}**{room}{slot}\n\n  {alt.description}")
        else:
            st.info("자동으로 찾은 대안이 없습니다. 메일 초안만 사용하세요.")

        if existing.negotiation_email:
            st.markdown("**협상 메일 초안**")
            st.text_area(
                "초안 (편집 후 어드민 메일함에 복사해 전송)",
                value=existing.negotiation_email,
                height=200,
                key=f"deadlock_email_{app.id}",
            )


def _build_pools_for(semester_id: int, app: Application) -> tuple[list[dict], list[dict]]:
    """이 데드락 신청에 대한 free / occupied 후보 풀을 구성.

    free_pool: 같은 종류·수용인원 이상·해당 시간대/다른 시간대에서 비어 있는 강의실 후보.
        - 같은 시간대 다른 강의실 (building_swap 후보)
        - 같은 강의실 다른 (요일/교시) (time_shift 후보)
    occupied_pool: 같은 시간대를 이미 차지하고 있는 다른 신청들 (협상 대상 후보).
    """
    with SessionLocal() as s:
        classrooms = list(s.scalars(select(Classroom)).all())
        all_apps = list(
            s.scalars(
                select(Application).where(Application.semester_id == semester_id)
            ).all()
        )
        assignments = list(
            s.scalars(
                select(Assignment).where(Assignment.semester_id == semester_id)
            ).all()
        )

    # (classroom_id, day, time_slot) → 누가 점유 중인가
    occupied: dict[tuple[int, str, str], int] = {}
    for asg in assignments:
        for d in asg.days:
            occupied[(asg.classroom_id, d, asg.time_slot)] = asg.application_id

    theory_only = app.class_format == "이론"

    # 적합 강의실 = 종류 일치 + 수용 충분 + (이론이면 실기/실습 제외)
    suitable = [
        c for c in classrooms
        if c.room_type == app.requested_type
        and c.capacity >= app.capacity_needed
        and not (theory_only and is_practice_room(c.room_type))
    ]

    free_pool: list[dict] = []

    # building_swap: 같은 요일/시간대, 다른 강의실(다른 건물 가능)
    for c in suitable:
        if any((c.id, d, app.time_slot) in occupied for d in app.days):
            continue
        free_pool.append({
            "classroom_code": c.code,
            "classroom_name": c.name,
            "building": c.building,
            "room_type": c.room_type,
            "capacity": c.capacity,
            "days": ",".join(app.days),
            "time_slot": app.time_slot,
            "kind_hint": "building_swap" if c.building != app.building else "same_building_swap",
        })

    # time_shift: 같은 강의실 대안 시간대 (인기 슬롯 외 일부)
    from app.demos._shared import TIME_SLOTS
    alt_slots = [t for t in TIME_SLOTS if t != app.time_slot]
    for c in suitable[:20]:  # 너무 커지지 않도록 상위 20개만
        for t in alt_slots[:4]:
            if all((c.id, d, t) not in occupied for d in app.days):
                free_pool.append({
                    "classroom_code": c.code,
                    "classroom_name": c.name,
                    "building": c.building,
                    "room_type": c.room_type,
                    "capacity": c.capacity,
                    "days": ",".join(app.days),
                    "time_slot": t,
                    "kind_hint": "time_shift",
                })
                break

    # 같은 시간대 점유 신청 (협상 대상 후보)
    occupied_apps_ids = {
        aid for (cid, d, t), aid in occupied.items()
        if t == app.time_slot and d in app.days
    }
    by_id = {a.id: a for a in all_apps}
    occupied_pool: list[dict] = []
    for aid in occupied_apps_ids:
        a = by_id.get(aid)
        if a is None:
            continue
        occupied_pool.append({
            "application_id": a.id,
            "applicant_name": a.applicant_name,
            "course_name": a.course_name,
            "course_category": a.course_category,
            "class_format": a.class_format,
            "requested_type": a.requested_type,
            "days": list(a.days),
            "time_slot": a.time_slot,
        })

    return free_pool[:30], occupied_pool[:10]


def render(semester: Semester) -> None:
    left, right = st.columns([1, 1], gap="medium")
    with left:
        _left_pane(semester)
    with right:
        _right_pane(semester)
