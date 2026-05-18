"""관리자 활용 현황 대시보드.

두 개의 원형 차트로 한눈에 보여준다:
    1) 강의실 사용률 — 전체 강의실 중 1건 이상 배정이 들어간 강의실 비율
    2) 시간 점유율 — 전체 (강의실 × 요일 × 교시) 슬롯 중 점유된 비율
       (정규 배정 + 활성 임시 예약 합산)

좌/우 분할 없이 한 페이지로 채워, 진입 직후 가독성을 최우선으로 한다.
"""
from __future__ import annotations

from datetime import timedelta

import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from app.db import SessionLocal
from app.demos._shared import DAYS, TIME_SLOTS
from app.models import (
    Application,
    Assignment,
    Classroom,
    Semester,
    TempReservation,
)


_PALETTE = {
    "used": "#1f77b4",       # 진한 파랑 — 사용 중
    "unused": "#e6e6e6",     # 옅은 회색 — 미사용
    "occupied": "#d62728",   # 진한 빨강 — 점유
    "free": "#cfe2f3",       # 옅은 파랑 — 가용
}

DAYS_PER_WEEK = len(DAYS)
SLOTS_PER_DAY = len(TIME_SLOTS)


# ─────────────────────────── 데이터 수집 ───────────────────────────


def _collect_stats(semester_id: int) -> dict:
    with SessionLocal() as s:
        total_classrooms = s.query(Classroom).count()
        assignments = list(
            s.scalars(
                select(Assignment).where(Assignment.semester_id == semester_id)
            ).all()
        )
        applications = list(
            s.scalars(
                select(Application).where(Application.semester_id == semester_id)
            ).all()
        )
        temps_active = list(
            s.scalars(
                select(TempReservation).where(TempReservation.status == "active")
            ).all()
        )

    # 사용 중인 강의실 = 1건 이상 정규 배정이 있거나, 활성 임시예약이 있는 강의실
    used_room_ids: set[int] = {a.classroom_id for a in assignments}
    used_room_ids.update(t.classroom_id for t in temps_active)
    used_classrooms = len(used_room_ids)
    unused_classrooms = max(0, total_classrooms - used_classrooms)

    # 슬롯 = 강의실 × 요일 × 교시 (학기 단위 주간 슬롯)
    total_slots = total_classrooms * DAYS_PER_WEEK * SLOTS_PER_DAY

    # 정규 배정 점유: 한 건은 len(days) × 1 교시 만큼 점유
    occupied_by_assignments = sum(len(a.days) for a in assignments)

    # 임시 예약 점유: start_at/end_at 으로부터 (요일 × 교시) 슬롯을 근사 환산
    # 단순화: 1건 = (걸친 일 수) × (걸친 교시 길이 ≈ 1.25시간 단위 ceil) — 데모 수준
    occupied_by_temps = 0
    for t in temps_active:
        if t.end_at <= t.start_at:
            continue
        days_span = max(1, (t.end_at.date() - t.start_at.date()).days + 1)
        hours = max(1.0, (t.end_at - t.start_at).total_seconds() / 3600.0)
        slot_count = max(1, int(round(hours / 1.25)))  # 1교시 ≈ 1시간 15분
        occupied_by_temps += days_span * slot_count

    occupied_slots = min(total_slots, occupied_by_assignments + occupied_by_temps)
    free_slots = max(0, total_slots - occupied_slots)

    return {
        "total_classrooms": total_classrooms,
        "used_classrooms": used_classrooms,
        "unused_classrooms": unused_classrooms,
        "total_slots": total_slots,
        "occupied_slots": occupied_slots,
        "free_slots": free_slots,
        "occupied_by_assignments": occupied_by_assignments,
        "occupied_by_temps": occupied_by_temps,
        "assignments_count": len(assignments),
        "applications_count": len(applications),
        "active_temps_count": len(temps_active),
    }


# ─────────────────────────── 차트 ───────────────────────────


def _pie(labels: list[str], values: list[int], colors: list[str], center_text: str) -> go.Figure:
    fig = go.Figure(
        go.Pie(
            labels=labels,
            values=values,
            hole=0.55,
            marker={"colors": colors, "line": {"color": "#ffffff", "width": 2}},
            textinfo="label+percent",
            texttemplate="<b>%{label}</b><br>%{value:,}건<br>%{percent}",
            textposition="outside",
            sort=False,
            pull=[0.04, 0],
        )
    )
    fig.update_layout(
        annotations=[
            {
                "text": center_text,
                "x": 0.5, "y": 0.5,
                "font": {"size": 22, "color": "#222"},
                "showarrow": False,
                "align": "center",
            }
        ],
        showlegend=False,
        height=380,
        margin={"l": 20, "r": 20, "t": 30, "b": 30},
    )
    return fig


# ─────────────────────────── 렌더 ───────────────────────────


def render(semester: Semester) -> None:
    st.subheader("📊 강의실 활용 현황")
    st.caption(
        f"학기: **{semester.year}-{semester.term}학기** — "
        "정규 배정과 활성 임시 예약을 합산해 보여줍니다."
    )

    stats = _collect_stats(semester.id)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("총 강의실", f"{stats['total_classrooms']:,}")
    m2.metric("사용 중", f"{stats['used_classrooms']:,}")
    m3.metric("정규 배정", f"{stats['assignments_count']:,}")
    m4.metric("활성 임시예약", f"{stats['active_temps_count']:,}")

    st.markdown("---")

    if stats["total_classrooms"] == 0:
        st.warning("강의실 마스터 데이터가 비어 있습니다. `scripts/seed_classrooms.py` 를 먼저 실행하세요.")
        return

    c1, c2 = st.columns(2, gap="large")

    with c1:
        used = stats["used_classrooms"]
        total = stats["total_classrooms"]
        pct = (used / total * 100) if total else 0
        st.markdown("#### 🏫 강의실 사용률")
        st.caption("1건 이상 정규 배정 또는 활성 임시예약이 잡힌 강의실 비율")
        fig = _pie(
            labels=["사용 중", "미사용"],
            values=[used, stats["unused_classrooms"]],
            colors=[_PALETTE["used"], _PALETTE["unused"]],
            center_text=f"{pct:.1f}%",
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            f"전체 {total:,}개 중 **{used:,}개 사용** "
            f"(미사용 {stats['unused_classrooms']:,}개)"
        )

    with c2:
        occ = stats["occupied_slots"]
        total_slots = stats["total_slots"]
        pct = (occ / total_slots * 100) if total_slots else 0
        st.markdown("#### ⏱ 시간 점유율")
        st.caption(
            f"전체 (강의실 × 요일 {DAYS_PER_WEEK}일 × 교시 {SLOTS_PER_DAY}개) 슬롯 중 점유 비율"
        )
        fig = _pie(
            labels=["점유", "가용"],
            values=[occ, stats["free_slots"]],
            colors=[_PALETTE["occupied"], _PALETTE["free"]],
            center_text=f"{pct:.1f}%",
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            f"총 {total_slots:,}개 슬롯 중 **{occ:,}개 점유** "
            f"(정규 {stats['occupied_by_assignments']:,} + 임시 {stats['occupied_by_temps']:,})"
        )

    st.markdown("---")
    st.caption(
        "ℹ️ '시간 점유율' 은 학기 단위 주간 슬롯 기준입니다. 임시 예약은 (걸친 일수 × 1.25시간 단위) "
        "로 환산해 합산합니다 (시연용 근사)."
    )
