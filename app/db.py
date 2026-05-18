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


def init_db() -> None:
    from app import models  # noqa: F401  models 등록을 위해 import
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations()
