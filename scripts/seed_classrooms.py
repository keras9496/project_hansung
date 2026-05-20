"""강의실 데이터.xlsx → SQLite 시드.

- 마스터 파일을 읽어 classrooms 테이블에 upsert (code 기준).
- 2025학년도 강의실 사용률.xlsx에 있는 '관리소속' 컬럼을 함께 시드.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import CLASSROOM_XLSX, USAGE_XLSX  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.demos._shared import infer_building_from_code  # noqa: E402
from app.models import Classroom  # noqa: E402


# 원본 xlsx 의 수용인원이 비어 있는 강의실(낙산관 실기실, 진리관 실기실 등)을
# 0 으로 적재하면 배정 후보 필터(c.capacity >= 필요 인원)에서 무조건 제외되어
# 전공 건물 우선 규칙이 무력화된다. 보수적 기본값 30 으로 채운다.
_DEFAULT_CAPACITY_WHEN_MISSING = 30


def load_master() -> pd.DataFrame:
    df = pd.read_excel(CLASSROOM_XLSX)
    df = df.rename(columns={
        "강의실코드": "code",
        "강의실명": "name",
        "강의실 구분": "room_type",
        "수용인원": "capacity",
    })
    df["capacity"] = df["capacity"].fillna(_DEFAULT_CAPACITY_WHEN_MISSING).astype(int)
    df.loc[df["capacity"] <= 0, "capacity"] = _DEFAULT_CAPACITY_WHEN_MISSING
    return df[["code", "name", "room_type", "capacity"]]


def load_managing_dept() -> dict[str, str]:
    if not USAGE_XLSX.exists():
        return {}
    sheets = pd.read_excel(USAGE_XLSX, sheet_name=None)
    mapping: dict[str, str] = {}
    for _, df in sheets.items():
        if "강의실코드" in df.columns and "관리소속" in df.columns:
            for _, row in df[["강의실코드", "관리소속"]].dropna().iterrows():
                mapping[str(row["강의실코드"])] = str(row["관리소속"])
    return mapping


def seed() -> None:
    init_db()
    master = load_master()
    dept_map = load_managing_dept()

    inserted = updated = 0
    with SessionLocal() as session:
        for _, row in master.iterrows():
            code = str(row["code"])
            existing = session.scalar(select(Classroom).where(Classroom.code == code))
            managing_dept = dept_map.get(code)
            building = infer_building_from_code(code)
            if existing is None:
                session.add(Classroom(
                    code=code,
                    name=str(row["name"]),
                    room_type=str(row["room_type"]),
                    capacity=int(row["capacity"]),
                    building=building,
                    managing_dept=managing_dept,
                ))
                inserted += 1
            else:
                existing.name = str(row["name"])
                existing.room_type = str(row["room_type"])
                existing.capacity = int(row["capacity"])
                existing.building = building or existing.building
                if managing_dept:
                    existing.managing_dept = managing_dept
                updated += 1
        session.commit()
        total = session.scalar(select(Classroom).with_only_columns(Classroom.id).limit(1))  # noqa: F841
        count = session.query(Classroom).count()

    print(f"[seed] inserted={inserted}, updated={updated}, total={count}")


if __name__ == "__main__":
    seed()
