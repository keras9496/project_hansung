from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import DB_PATH


class Base(DeclarativeBase):
    pass


engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# 새 컬럼이 모델에 추가됐을 때 기존 SQLite DB 를 부숴 없애지 않도록
# 누락된 컬럼만 ALTER TABLE 로 더한다(프로토타입 수준의 가벼운 마이그레이션).
_PENDING_COLUMNS: list[tuple[str, str, str]] = [
    ("classrooms", "building", "VARCHAR(32)"),
    ("applications", "building", "VARCHAR(32)"),
    ("applications", "course_category", "VARCHAR(32)"),
    ("applications", "class_format", "VARCHAR(16)"),
]


def _apply_lightweight_migrations() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _PENDING_COLUMNS:
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column in cols:
                continue
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))


def _normalize_classroom_capacity() -> None:
    """과거 강의실 마스터 시드의 capacity=0(원본 xlsx 결측) 행을 30 으로 보정.

    배정 후보 필터(c.capacity >= 필요 인원) 에서 0 인 강의실은 무조건 빠지므로
    낙산관·진리관 일부 실기실이 사실상 자동 배정 불가 상태였다.
    멱등하며, 정상 capacity 행은 손대지 않는다.
    """
    inspector = inspect(engine)
    if "classrooms" not in set(inspector.get_table_names()):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE classrooms SET capacity = 30 "
            "WHERE capacity IS NULL OR capacity <= 0"
        ))


def init_db() -> None:
    from app import models  # noqa: F401  models 등록을 위해 import
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations()
    _normalize_classroom_capacity()
