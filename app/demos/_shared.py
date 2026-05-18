"""데모 공용 상수와 헬퍼."""
from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Classroom

DAYS = ["월", "화", "수", "목", "금", "토"]

TIME_SLOTS = [
    "1교시 (09:00-10:15)",
    "2교시 (10:30-11:45)",
    "3교시 (12:00-13:15)",
    "4교시 (13:30-14:45)",
    "5교시 (15:00-16:15)",
    "6교시 (16:30-17:45)",
    "7교시 (18:00-19:15)",
    "8교시 (19:30-20:45)",
]

WEEK_PRESETS = {
    "전체 학기 (1-15주)": list(range(1, 16)),
    "전반 (1-7주)": list(range(1, 8)),
    "후반 (8-15주)": list(range(8, 16)),
}


# ────────────────────── 강의실 구분 / 교과 구분 메타 ──────────────────────

# (라벨, 설명, 우선배정 전공) — 우선배정 전공이 None 이면 공통
BUILDINGS: list[tuple[str, str, str | None]] = [
    ("상상관", "공통", None),
    ("공학관", "공학계열 우선배정", "공학"),
    ("탐구관", "공통", None),
    ("낙산관", "무용 우선배정", "무용"),
    ("미래관", "공통", None),
    ("우촌관", "공통", None),
    ("지선관", "회화 우선배정", "회화"),
    ("진리관", "공통", None),
    ("창의관", "패션/디자인 우선배정", "패션+디자인"),
]

BUILDING_NAMES: list[str] = [b[0] for b in BUILDINGS]
BUILDING_LABELS: dict[str, str] = {b[0]: f"{b[0]} ({b[1]})" for b in BUILDINGS}

# 전공 키 → 우선배정 건물
MAJOR_BUILDING_MAP: dict[str, str] = {
    major: name for name, _, major in BUILDINGS if major is not None
}

# 교과구분 1
COURSE_CATEGORIES: list[str] = [
    "교양",
    "전공-공학",
    "전공-무용",
    "전공-회화",
    "전공-패션+디자인",
]


def major_of(category: str | None) -> str | None:
    """course_category 가 전공이면 전공 키('공학','무용',...)를 반환. 아니면 None."""
    if not category or not category.startswith("전공-"):
        return None
    return category[len("전공-"):]


def preferred_building_for(category: str | None) -> str | None:
    """전공이면 매칭 건물 이름, 아니면 None."""
    major = major_of(category)
    if major is None:
        return None
    return MAJOR_BUILDING_MAP.get(major)


# 교과구분 2
CLASS_FORMATS: list[str] = ["이론", "이론+실기", "실기"]


def is_practice_room(room_type: str | None) -> bool:
    """room_type 이 실기/실습 전용이면 True (이론 수업 배정 불가)."""
    if not room_type:
        return False
    return ("실기" in room_type) or ("실습" in room_type)


# 강의실 코드 prefix → 건물명 (강의실 데이터.xlsx 기준)
CODE_PREFIX_TO_BUILDING: dict[str, str] = {
    "1F": "상상관",
    "AF": "우촌관",
    "BF": "진리관",
    "CF": "지선관",
    "DF": "공학관",
    "EF": "창의관",
    "HF": "탐구관",
    "HB": "탐구관",
    "JB": "미래관",
    "MF": "낙산관",
}


def infer_building_from_code(code: str | None) -> str | None:
    if not code:
        return None
    return CODE_PREFIX_TO_BUILDING.get(code[:2])


def get_classroom_types() -> list[str]:
    with SessionLocal() as s:
        rows = s.execute(
            select(Classroom.room_type).distinct().order_by(Classroom.room_type)
        ).all()
        return [r[0] for r in rows]
