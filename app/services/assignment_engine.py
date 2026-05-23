"""정규 신청 배정 엔진.

정책 (기획안 §6 + 강의실 구분/교과구분 확장):
    - 우선순위: 도착순(FCFS). created_at 오름차순, 동률이면 application_id 오름차순.
    - 같은 (요일, 시간대) 슬롯에서 같은 종류·충분한 수용인원 강의실 후보를 모은다.
    - 하드 규칙
        1) class_format 이 "이론" 이면 실기/실습 전용 강의실은 후보에서 제외한다.
        2) 전공-XXX + (실기 또는 이론+실기) 신청은 전공 건물(공학→공학관 /
           무용→낙산관 / 회화→지선관 / 패션+디자인→창의관) 에서만 배정 가능.
           해당 건물에 후보가 없으면 fallback 없이 즉시 조건매칭 불가(deadlock).
           이유: 전공 실기 수업은 해당 학과 전용 시설(실기실/디자인개발실/염색실 등)을
           전제로 하므로 다른 건물로 떨어지면 의미가 없다.
    - 소프트 규칙(우선 풀 → fallback 풀 순) — 위 하드 규칙에 걸리지 않는 신청에만 적용
        1) 전공 수업이면 매칭 전공 건물 후보를 1순위 풀에 둔다.
        2) 사용자가 신청서에서 고른 희망 건물 후보를 2순위 풀에 둔다.
        3) 그 외 후보를 3순위(fallback) 풀에 둔다.
       각 풀에서 best-fit(가장 작은 적합 강의실)을 시도하고, 비면 다음 풀로 넘어간다.
    - 후보 풀을 random.shuffle 로 섞어 특정 강의실 편중 방지(타이브레이커).
    - 모든 풀이 비면 deadlock(=조건매칭 불가) 으로 마킹, 메일 이벤트 큐에 적재.

프로토타입 단순화:
    - "주차"는 충돌 판정에 사용하지 않는다(같은 학기 내 같은 요일/시간이면 충돌).
    - 같은 강의실은 같은 (요일, 시간대) 슬롯에 1건만 들어간다.
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Optional

from sqlalchemy import select

from app.db import SessionLocal
from app.demos._shared import is_practice_room, preferred_building_for, split_time_slots
from app.models import Application, Assignment, Classroom
from app.services.mailer import queue_mail


def _conflicts(occupied: set, days: list[str], time_slots: list[str]) -> bool:
    return any((d, ts) in occupied for d in days for ts in time_slots)


def _mark_occupied(occupied: set, days: list[str], time_slots: list[str]) -> None:
    for d in days:
        for ts in time_slots:
            occupied.add((d, ts))


def _pick_best_fit(candidates: list[Classroom]) -> Optional[Classroom]:
    if not candidates:
        return None
    random.shuffle(candidates)
    candidates.sort(key=lambda c: c.capacity)
    return candidates[0]


def run_assignment(semester_id: int, seed: Optional[int] = None) -> dict:
    """학기의 모든 신청을 배정. 기존 배정은 초기화 후 다시 실행한다."""
    if seed is not None:
        random.seed(seed)

    with SessionLocal() as s:
        # 모든 신청 → 도착순
        apps = list(
            s.scalars(
                select(Application)
                .where(Application.semester_id == semester_id)
                .order_by(Application.created_at, Application.id)
            ).all()
        )

        # 기존 배정 초기화
        existing = s.scalars(
            select(Assignment).where(Assignment.semester_id == semester_id)
        ).all()
        for a in existing:
            s.delete(a)
        for app in apps:
            if app.status in ("assigned", "deadlock"):
                app.status = "pending"
        s.flush()

        classrooms = list(s.scalars(select(Classroom)).all())
        room_occupied: dict[int, set] = defaultdict(set)

        assigned_count = 0
        deadlock_count = 0
        details: list[dict] = []

        for app in apps:
            theory_only = app.class_format == "이론"
            app_slots = split_time_slots(app.time_slot)
            major_building = preferred_building_for(app.course_category)
            user_building = app.building

            # 하드 규칙 2: 전공-XXX + (실기 또는 이론+실기) 는 전공 건물 전용.
            # major_building 이 None 이면(=교양) 적용되지 않는다.
            major_practice_locked = (
                major_building is not None
                and app.class_format in ("실기", "이론+실기")
            )

            # 기본 후보: 종류 일치 + 수용 충분 + 슬롯 비어있음 + (이론이면 실기/실습 제외)
            #            + 전공-실기 잠금이면 전공 건물 외 제외
            base = [
                c
                for c in classrooms
                if c.room_type == app.requested_type
                and c.capacity >= app.capacity_needed
                and not _conflicts(room_occupied[c.id], app.days, app_slots)
                and not (theory_only and is_practice_room(c.room_type))
                and not (major_practice_locked and c.building != major_building)
            ]

            tier_major = [c for c in base if major_building and c.building == major_building]
            tier_user = [
                c for c in base
                if user_building
                and c.building == user_building
                and c not in tier_major
            ]
            tier_rest = [c for c in base if c not in tier_major and c not in tier_user]

            chosen: Optional[Classroom] = None
            chosen_tier = None
            for tier_name, tier in (
                ("major", tier_major),
                ("user", tier_user),
                ("rest", tier_rest),
            ):
                chosen = _pick_best_fit(list(tier))
                if chosen is not None:
                    chosen_tier = tier_name
                    break

            if chosen is None:
                app.status = "deadlock"
                deadlock_count += 1
                reason_parts = []
                if theory_only:
                    reason_parts.append("이론 수업은 실기/실습실 배정 불가")
                if major_practice_locked:
                    reason_parts.append(
                        f"하드 규칙: 전공-실기 수업은 {major_building} 전용 "
                        "(다른 건물 fallback 금지)"
                    )
                elif major_building:
                    reason_parts.append(f"전공 건물({major_building}) 우선 시도")
                if user_building and not major_practice_locked:
                    reason_parts.append(f"희망 건물({user_building}) 시도")
                reason_text = " / ".join(reason_parts) if reason_parts else "조건 부합 강의실 없음"
                details.append(
                    {
                        "application_id": app.id,
                        "applicant": app.applicant_name,
                        "result": "deadlock",
                        "course_category": app.course_category,
                        "requested_building": app.building,
                        "reason": reason_text,
                    }
                )
                queue_mail(
                    event_kind="deadlock",
                    related_id=app.id,
                    recipients=[app.email, "office@hansung.ac.kr"],
                    subject=f"[강의실 예약] 배정 실패 안내 (신청 ID #{app.id})",
                    body=(
                        f"{app.applicant_name}님 및 담당 사무실 귀하,\n\n"
                        f"신청하신 '{app.requested_type}' 종류의 강의실이 요청 시간대"
                        f"({', '.join(app.days)} {app.time_slot})에 모두 점유되었거나"
                        f" 조건을 만족하는 후보가 없어 자동 배정이 어려웠습니다(배정 실패).\n"
                        f"- 사유: {reason_text}\n\n"
                        f"가용한 다른 시간대 또는 다른 종류/건물로 재신청 부탁드립니다."
                    ),
                    session=s,
                )
                continue

            assignment = Assignment(
                application_id=app.id,
                classroom_id=chosen.id,
                semester_id=semester_id,
                days=list(app.days),
                time_slot=app.time_slot,
                weeks=list(app.weeks),
                method="auto",
            )
            s.add(assignment)
            app.status = "assigned"
            _mark_occupied(room_occupied[chosen.id], app.days, app_slots)
            assigned_count += 1
            details.append(
                {
                    "application_id": app.id,
                    "applicant": app.applicant_name,
                    "result": "assigned",
                    "course_category": app.course_category,
                    "requested_building": app.building,
                    "classroom_code": chosen.code,
                    "classroom_name": chosen.name,
                    "classroom_building": chosen.building,
                    "tier": chosen_tier,
                }
            )
            queue_mail(
                event_kind="assigned",
                related_id=app.id,
                recipients=[app.email],
                subject=f"[강의실 예약] 배정 완료 (신청 ID #{app.id})",
                body=(
                    f"{app.applicant_name}님,\n\n"
                    f"신청하신 강의실이 배정되었습니다.\n\n"
                    f"- 강의/행사: {app.course_name}\n"
                    f"- 배정 강의실: {chosen.code} {chosen.name} "
                    f"({chosen.building or '-'} / 수용 {chosen.capacity}명)\n"
                    f"- 요일: {', '.join(app.days)}\n"
                    f"- 시간대: {app.time_slot}\n"
                ),
                session=s,
            )

        s.commit()

        # 전공 매칭률: 전공 신청 중 전공 건물(tier=major) 로 배정된 비율
        major_total = sum(
            1 for d in details
            if (d.get("course_category") or "").startswith("전공-")
        )
        major_match = sum(
            1 for d in details
            if (d.get("course_category") or "").startswith("전공-")
            and d.get("result") == "assigned" and d.get("tier") == "major"
        )
        return {
            "assigned": assigned_count,
            "deadlock": deadlock_count,
            "total": len(apps),
            "major_total": major_total,
            "major_match": major_match,
            "details": details,
        }
