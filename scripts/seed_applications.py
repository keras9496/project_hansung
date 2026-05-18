"""정규 신청(강좌) 100개 데모 시드.

목적
    - 프로토타입 데모용으로 다양한 강의실 종류 · 요일 · 시간대에 분산된
      현실적인 신청 100건을 `applications` 테이블에 적재한다.
    - 일부 인기 슬롯은 의도적으로 경합시켜 배정 단계에서 데드락이
      자연스럽게 발생하도록 설계한다 (시연용).

사용
    python scripts/seed_applications.py             # 100건 추가
    python scripts/seed_applications.py --count 50  # 개수 지정
    python scripts/seed_applications.py --reset     # 기존 신청/배정/메일 초기화 후 시드

전제
    - 강의실 마스터가 이미 시드되어 있어야 한다 (`seed_classrooms.py` 선행).
    - 기본 학기(2026-1)에 적재한다. 학기가 없으면 생성한다.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal, init_db  # noqa: E402
from app.demos._shared import (  # noqa: E402
    BUILDING_NAMES,
    CLASS_FORMATS,
    COURSE_CATEGORIES,
    MAJOR_BUILDING_MAP,
    is_practice_room,
)
from app.models import (  # noqa: E402
    Application,
    Assignment,
    Classroom,
    MailLog,
)
from app.services.mailer import queue_mail  # noqa: E402
from app.services.semester_service import ensure_demo_semester  # noqa: E402


# ─────────────────────────────── 데이터 풀 ────────────────────────────────

# 한국식 이름 풀 (성 × 이름 조합)
SURNAMES = [
    "김", "이", "박", "최", "정", "강", "조", "윤", "장", "임",
    "한", "오", "서", "신", "권", "황", "안", "송", "류", "전",
]
GIVEN_NAMES = [
    "민준", "서연", "지호", "하윤", "준우", "지유", "도윤", "수아", "예준", "지아",
    "시우", "서윤", "주원", "하은", "건우", "유나", "현우", "민서", "지훈", "예린",
    "성민", "수빈", "지원", "은서", "재현", "다은", "승현", "채원", "은우", "윤서",
]

AFFILIATIONS = [
    "컴퓨터공학부", "AI응용학과", "정보통신공학과", "산업경영공학과",
    "기계시스템공학과", "신소재공학과", "건축공학부", "디자인학부",
    "패션디자인학과", "ICT디자인학부", "경영학부", "글로벌비즈니스학과",
    "회계학과", "국제무역학과", "경제학과", "법학과",
    "행정학과", "사회복지학과", "영어영문학과", "한국어문학부",
    "역사문화학부", "교양교육원", "기초과학부",
    "프로그래밍 동아리 HSCC", "창업 동아리 START", "총학생회",
    "댄스 동아리 PLAY", "밴드 동아리 HARMONY",
]

# 카테고리별 강의명 — 강의실 종류와 어느 정도 매칭되도록 분류
COURSE_NAMES = {
    "lecture": [
        "선형대수학", "이산수학", "확률과 통계", "미적분학", "물리학 개론",
        "화학 개론", "경영학원론", "회계원리", "마케팅 원론", "조직행동론",
        "거시경제학", "미시경제학", "현대 한국 사회", "철학의 이해",
        "서양 미술사", "한국 근현대사", "환경과 인간", "심리학 입문",
        "법학 개론", "헌법", "민법총칙", "행정법", "사회복지개론",
        "영어 회화 1", "비즈니스 영어", "글쓰기와 토론", "고전 읽기",
        "데이터와 사회", "정보화 사회와 윤리",
    ],
    "pc": [
        "프로그래밍 기초", "자료구조", "알고리즘", "데이터베이스 시스템",
        "운영체제", "컴퓨터 네트워크", "웹 프로그래밍", "모바일 앱 개발",
        "Python 데이터 분석", "머신러닝 입문", "딥러닝 응용",
        "정보보호 개론", "오픈소스 SW", "캡스톤 디자인", "소프트웨어 공학",
        "AutoCAD 실습", "3D 모델링", "UI/UX 프로토타이핑",
    ],
    "lab": [
        "전자회로 실험", "회로이론 실습", "디지털 논리 실습",
        "물리 실험", "화학 실험", "재료역학 실험", "유체역학 실험",
        "기계요소 설계", "센서공학 실습", "임베디드 시스템 실습",
    ],
    "studio": [
        "디자인 스튜디오 1", "디자인 스튜디오 2", "패션 일러스트",
        "텍스타일 디자인", "염색 실습", "제품 디자인 워크숍",
        "건축 설계 스튜디오", "공간 디자인",
    ],
    "auditorium": [
        "전공 특강 시리즈", "산업체 초청 강연", "공학과 사회",
        "리더십 세미나", "신입생 오리엔테이션 강좌", "취업 전략 특강",
    ],
}

# 강의실 종류 카테고리 매핑 (room_type 패턴 → 카테고리)
def categorize_room_type(room_type: str) -> str:
    rt = room_type
    if "계단식" in rt:
        return "auditorium"
    if "PC" in rt:
        return "pc"
    if "실험" in rt or rt == "실습실" or rt == "실기실":
        return "lab"
    if "디자인" in rt or "염색" in rt:
        return "studio"
    return "lecture"


# 요일 패턴 — 실제 시간표 관습 (월수·화목 페어가 가장 흔함)
DAY_PATTERNS = [
    (["월", "수"], 18),
    (["화", "목"], 18),
    (["월", "수", "금"], 8),
    (["화", "목", "금"], 4),
    (["월"], 6),
    (["화"], 6),
    (["수"], 6),
    (["목"], 6),
    (["금"], 8),
    (["토"], 3),
]

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
# 인기 시간대(2·4교시)에 가중치 — 데드락 시연을 자연스럽게 유도
TIME_SLOT_WEIGHTS = [3, 6, 2, 6, 4, 3, 2, 1]

WEEK_RANGES = [
    ("전체 학기 (1-15주)", list(range(1, 16)), 80),
    ("전반 (1-7주)", list(range(1, 8)), 10),
    ("후반 (8-15주)", list(range(8, 16)), 10),
]

NOTE_SAMPLES = [
    None, None, None, None,  # 대부분은 비고 없음
    "빔프로젝터 필요",
    "콘센트 충분한 좌석 배치 요청",
    "조별 토론 가능한 책상 배열",
    "녹화 장비 사용 예정",
    "외부 강사 초청, 마이크 필요",
    "실습용 도구 반입 예정",
]


# ─────────────────────────────── 시드 로직 ────────────────────────────────

def _random_name(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + rng.choice(GIVEN_NAMES)


def _email_from_name(name: str, idx: int) -> str:
    # 한글 이름은 ASCII로 변환할 수 없으니 인덱스 기반 가짜 ID 사용
    return f"applicant{idx:03d}@hansung.ac.kr"


def _pick_room_type(rng: random.Random, types_pool: list[tuple[str, int]]) -> str:
    """수용 가능한 강의실이 많은 타입일수록 더 자주 신청되도록 가중 추첨."""
    types, weights = zip(*types_pool)
    return rng.choices(types, weights=weights, k=1)[0]


def _pick_capacity(rng: random.Random, room_type: str, max_capacity: int) -> int:
    """강의실 종류·최대 수용 인원에 맞춘 그럴듯한 필요 인원."""
    cat = categorize_room_type(room_type)
    if cat == "auditorium":
        base = rng.randint(60, min(120, max(60, max_capacity)))
    elif cat == "pc":
        base = rng.randint(20, min(55, max(20, max_capacity)))
    elif cat == "lab":
        base = rng.randint(15, min(35, max(15, max_capacity)))
    elif cat == "studio":
        base = rng.randint(15, min(35, max(15, max_capacity)))
    else:  # lecture
        base = rng.randint(20, min(70, max(20, max_capacity)))
    return min(base, max(1, max_capacity))


def _pick_course_name(rng: random.Random, room_type: str) -> str:
    cat = categorize_room_type(room_type)
    pool = COURSE_NAMES.get(cat, COURSE_NAMES["lecture"])
    suffix = rng.choice(["", "", "", " (1분반)", " (2분반)", " (A반)", " (B반)"])
    return rng.choice(pool) + suffix


def _pick_days(rng: random.Random) -> list[str]:
    patterns, weights = zip(*DAY_PATTERNS)
    return list(rng.choices(patterns, weights=weights, k=1)[0])


def _pick_time_slot(rng: random.Random) -> str:
    return rng.choices(TIME_SLOTS, weights=TIME_SLOT_WEIGHTS, k=1)[0]


def _pick_weeks(rng: random.Random) -> tuple[str, list[int]]:
    labels, ranges, weights = zip(*WEEK_RANGES)
    idx = rng.choices(range(len(labels)), weights=weights, k=1)[0]
    return labels[idx], ranges[idx]


# 카테고리 → 교과구분/희망 건물 분포
def _pick_course_category(rng: random.Random, room_type: str) -> str:
    cat = categorize_room_type(room_type)
    # 일반 강의실/계단식은 교양 비중↑, 실기/스튜디오/PC는 전공 비중↑
    if cat in ("lecture", "auditorium"):
        return rng.choices(
            COURSE_CATEGORIES,
            weights=[60, 15, 8, 8, 9],
            k=1,
        )[0]
    if cat == "studio":
        return rng.choices(
            COURSE_CATEGORIES,
            weights=[10, 5, 5, 5, 75],
            k=1,
        )[0]
    if cat == "lab":
        return rng.choices(
            COURSE_CATEGORIES,
            weights=[10, 70, 5, 5, 10],
            k=1,
        )[0]
    # pc
    return rng.choices(
        COURSE_CATEGORIES,
        weights=[20, 55, 5, 5, 15],
        k=1,
    )[0]


def _pick_class_format(rng: random.Random, room_type: str) -> str:
    if is_practice_room(room_type):
        # 실기/실습실은 이론 단독 불가 → 이론+실기 또는 실기
        return rng.choices(["이론+실기", "실기"], weights=[40, 60], k=1)[0]
    cat = categorize_room_type(room_type)
    if cat in ("lecture", "auditorium"):
        return rng.choices(CLASS_FORMATS, weights=[80, 15, 5], k=1)[0]
    return rng.choices(CLASS_FORMATS, weights=[40, 40, 20], k=1)[0]


def _pick_building(rng: random.Random, category: str) -> str:
    """전공이면 전공 건물을 70% 확률로, 아니면 공통 건물 분포."""
    if category.startswith("전공-"):
        major = category[len("전공-"):]
        preferred = MAJOR_BUILDING_MAP.get(major)
        if preferred and rng.random() < 0.7:
            return preferred
    return rng.choice(BUILDING_NAMES)


def _reset_demo_data(semester_id: int) -> None:
    with SessionLocal() as s:
        s.query(Assignment).filter(Assignment.semester_id == semester_id).delete()
        s.query(Application).filter(Application.semester_id == semester_id).delete()
        s.query(MailLog).delete()
        s.commit()


def seed_applications(count: int = 100, reset: bool = False, seed: int = 42) -> dict:
    init_db()
    semester = ensure_demo_semester()

    if reset:
        _reset_demo_data(semester.id)

    rng = random.Random(seed)

    with SessionLocal() as s:
        classrooms = list(s.scalars(select(Classroom)).all())
        if not classrooms:
            raise RuntimeError(
                "강의실 마스터가 비어있습니다. 먼저 `python scripts/seed_classrooms.py` 를 실행하세요."
            )

        # 타입별 통계 (가중치 + 최대 수용 인원)
        type_stats: dict[str, dict] = {}
        for c in classrooms:
            t = type_stats.setdefault(c.room_type, {"count": 0, "max_cap": 0})
            t["count"] += 1
            t["max_cap"] = max(t["max_cap"], c.capacity)
        types_pool = [(t, st["count"]) for t, st in type_stats.items()]

        # created_at 분포: 신청 마감 직전 ~ 2주 전 사이로 흩뿌림 (FCFS 시연용)
        now = datetime.now()
        base_time = now - timedelta(days=14)

        inserted = 0
        cat_counter: dict[str, int] = {}
        type_counter: dict[str, int] = {}

        for i in range(1, count + 1):
            room_type = _pick_room_type(rng, types_pool)
            max_cap = type_stats[room_type]["max_cap"]
            capacity_needed = _pick_capacity(rng, room_type, max_cap)
            course_name = _pick_course_name(rng, room_type)
            days = _pick_days(rng)
            time_slot = _pick_time_slot(rng)
            _, weeks = _pick_weeks(rng)
            course_category = _pick_course_category(rng, room_type)
            class_format = _pick_class_format(rng, room_type)
            building = _pick_building(rng, course_category)

            applicant = _random_name(rng)
            email = _email_from_name(applicant, i)
            affiliation = rng.choice(AFFILIATIONS)
            note = rng.choice(NOTE_SAMPLES)

            # 신청 순서가 FCFS 시연에 의미를 갖도록 일정 간격 + 약간의 jitter
            offset_minutes = int(i * (14 * 24 * 60) / max(count, 1))
            jitter = rng.randint(-30, 30)
            created_at = base_time + timedelta(minutes=offset_minutes + jitter)

            app = Application(
                semester_id=semester.id,
                applicant_name=applicant,
                email=email,
                affiliation=affiliation,
                course_name=course_name,
                requested_type=room_type,
                capacity_needed=capacity_needed,
                building=building,
                course_category=course_category,
                class_format=class_format,
                days=days,
                time_slot=time_slot,
                weeks=weeks,
                notes=note,
                status="pending",
                created_at=created_at,
            )
            s.add(app)
            s.flush()

            queue_mail(
                event_kind="confirmation",
                related_id=app.id,
                recipients=[email],
                subject=f"[강의실 예약] 신청 접수 확인 (신청 ID #{app.id})",
                body=(
                    f"{applicant}님,\n\n"
                    f"강의실 예약 신청이 접수되었습니다.\n\n"
                    f"- 신청 ID: {app.id}\n"
                    f"- 강의/행사: {course_name}\n"
                    f"- 강의실 종류: {room_type} ({capacity_needed}명)\n"
                    f"- 요일/시간: {', '.join(days)} {time_slot}\n\n"
                    "배정 결과는 추후 안내드립니다."
                ),
                session=s,
            )

            inserted += 1
            cat_counter[categorize_room_type(room_type)] = (
                cat_counter.get(categorize_room_type(room_type), 0) + 1
            )
            type_counter[room_type] = type_counter.get(room_type, 0) + 1

        s.commit()

    return {
        "inserted": inserted,
        "semester": f"{semester.year}-{semester.term}",
        "by_category": cat_counter,
        "by_type_top": sorted(type_counter.items(), key=lambda x: -x[1])[:8],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="정규 신청(강좌) 데모 시드")
    parser.add_argument("--count", type=int, default=100, help="생성할 신청 개수 (기본 100)")
    parser.add_argument("--reset", action="store_true", help="기존 신청/배정/메일 초기화 후 시드")
    parser.add_argument("--seed", type=int, default=42, help="랜덤 시드 (재현용)")
    args = parser.parse_args()

    result = seed_applications(count=args.count, reset=args.reset, seed=args.seed)

    print(f"[seed_applications] 학기: {result['semester']}, 적재: {result['inserted']}건")
    print(f"  카테고리별: {result['by_category']}")
    print(f"  타입 Top8:")
    for t, n in result["by_type_top"]:
        print(f"    - {t}: {n}건")


if __name__ == "__main__":
    main()
