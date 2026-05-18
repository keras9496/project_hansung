import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
DB_PATH = PROJECT_ROOT / os.getenv("DB_PATH", "data/reservations.db")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")

CLASSROOM_XLSX = PROJECT_ROOT / "강의실 데이터.xlsx"
USAGE_XLSX = PROJECT_ROOT / "2025학년도 강의실 사용률.xlsx"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ─────────────────── LLM (Anthropic / Claude) ───────────────────
# LLM_MODE: "auto" (live if API key present, else mock) | "live" | "mock"
# Render 배포 시: ANTHROPIC_API_KEY 만 환경변수에 넣으면 자동으로 live.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODE = os.getenv("LLM_MODE", "auto").lower()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-7")
