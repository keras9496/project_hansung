"""Claude 기반 AI 보조 기능 (강의실 예약 시스템).

세 가지 기능:
    1) 자연어 신청 파싱 — 사용자의 한 문장을 9개 필드로 추출
    2) 데드락 협상안 생성 — 시간/건물/포맷 대안 + 협상 메일 초안
    3) 신청-정책 일관성 검사 — 모순/이상 항목 경고

운영 모드:
    - live: Anthropic API 직접 호출 (.env 또는 Render 환경변수에 ANTHROPIC_API_KEY)
    - mock: 규칙 기반 더미 응답 (API 키 없을 때 자동 fallback, 시연 안전망)
    LLM_MODE=auto 면 키 유무에 따라 자동 결정.

성능:
    - 정책 문서(POLICY_DOC) + 시스템 프롬프트는 prompt caching 적용 → 두 번째 호출부터
      입력 토큰 단가 ~0.1배 (시연 시 다회 호출 비용 절감).
    - 모델은 CLAUDE_MODEL 환경변수로 교체 가능 (기본 claude-opus-4-7).
"""
from __future__ import annotations

import json
import random
import re
from typing import Optional

from pydantic import BaseModel, Field

from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, LLM_MODE

try:
    import anthropic  # type: ignore
    _HAS_SDK = True
except ImportError:
    anthropic = None  # type: ignore
    _HAS_SDK = False


# ──────────────────────────── 모드 판정 ────────────────────────────

def is_live() -> bool:
    if LLM_MODE == "mock":
        return False
    if LLM_MODE == "live":
        return _HAS_SDK and bool(ANTHROPIC_API_KEY)
    # auto
    return _HAS_SDK and bool(ANTHROPIC_API_KEY)


def mode_label() -> str:
    return "🟢 live (Claude API)" if is_live() else "🟡 mock (오프라인)"


_client: "anthropic.Anthropic | None" = None


def _client_or_none():
    global _client
    if not is_live():
        return None
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ──────────────────────── 도메인 메타 (정책 문서) ────────────────────────
# 이 블록은 시스템 프롬프트에 들어가 모든 호출에서 동일하게 재사용되므로 캐싱 효과가 큼.

POLICY_DOC = """\
# 한성대학교 강의실 예약 시스템 정책

## 강의실 구분 (건물)
- 상상관(공통), 공학관(공학계열 우선), 탐구관(공통), 낙산관(무용 우선),
  미래관(공통), 우촌관(공통), 지선관(회화 우선), 진리관(공통), 창의관(패션+디자인 우선)

## 교과구분 1 (course_category)
- "교양" 또는 "전공-공학" / "전공-무용" / "전공-회화" / "전공-패션+디자인"
- 전공 수업은 매칭 전공 건물(공학→공학관, 무용→낙산관, 회화→지선관, 패션+디자인→창의관) 우선 배정.

## 교과구분 2 (class_format)
- "이론" / "이론+실기" / "실기"
- 하드 규칙 1: 이론 수업은 실기실/실습실(room_type 에 "실기"/"실습" 포함) 배정 불가.
- 하드 규칙 2: 전공-XXX + (실기 또는 이론+실기) 신청은 전공 건물에서만 배정 가능
  (해당 학과 전용 시설 — 실기실/디자인개발실/염색실 등 — 을 전제로 함).
  전공 건물에 가능 후보가 없으면 다른 건물 fallback 없이 조건매칭 불가로 처리.

## 강의실 종류 (room_type) 예시
- "일반강의실", "계단식 강의실", "PC실습실", "실기실", "실습실", "디자인개발실",
  "염색실", "피아노실" 등.

## 시간 슬롯
- 요일: 월/화/수/목/금/토 중 1개 이상.
- 시간대: 1교시(09:00-10:15) … 8교시(19:30-20:45) 중 1개, 또는 **연속된 2개 교시** 까지.
  예: "2교시" 또는 "1-2교시", "1교시 + 2교시" (연속이 아닌 1교시+3교시는 불가).

## 주차 범위
- "전체 학기 (1-15주)" | "전반 (1-7주)" | "후반 (8-15주)"
"""

_SYSTEM_INTAKE = """\
당신은 한성대학교 강의실 예약 시스템의 신청 어시스턴트입니다.
사용자가 자연어로 작성한 강의실 예약 요청에서 정해진 필드를 JSON 으로 추출하세요.

용어 안내(매우 중요):
- "applicant_name" 은 **강의를 진행하는 강의자(교수) 본인의 이름** 입니다.
  대리 신청자(조교/사무실 직원 등)의 이름이 아니라 강의자의 이름을 채워주세요.
  문장에 "OO교수", "OO 강의자", "강의자: OO" 형태가 있으면 그 이름을 우선합니다.
- "affiliation" 은 강의자가 속한 **학과/부서** 입니다. (예: "컴퓨터공학부", "패션디자인학과")
  학과 정보가 없으면 null. 추측 금지.
- "time_slot" 은 단일 교시 또는 **연속된 2개 교시** 까지 허용합니다.
  - 단일: "2교시 (10:30-11:45)"
  - 연속 2교시: "1교시 (09:00-10:15) + 2교시 (10:30-11:45)" (구분자 ' + ')
  - "1-2교시", "1·2교시", "1교시,2교시" 같은 입력은 위 형식으로 정규화하세요.
  - 연속이 아니면(예: 1교시+3교시) 첫 번째 교시만 채우고 나머지는 notes 에 기록합니다.

- 명확하지 않은 필드는 null 로 두세요. 추측하지 말 것.
- 한국어 입력 / 한글 결과.

응답 JSON 스키마 (정확한 키 이름과 타입을 지킬 것):
{
  "applicant_name": str | null,        // 강의자(교수) 본인 이름
  "email": str | null,
  "affiliation": str | null,           // 학과/부서
  "course_name": str | null,
  "course_category": "교양" | "전공-공학" | "전공-무용" | "전공-회화" | "전공-패션+디자인" | null,
  "class_format": "이론" | "이론+실기" | "실기" | null,
  "building": (정책의 9개 건물 중 하나) | null,
  "requested_type": str | null,
  "capacity_needed": int | null,
  "days": list[str],                   // 월/화/수/목/금/토 의 부분집합
  "time_slot": str | null,             // 단일 또는 ' + ' 로 이은 연속 2교시
  "weeks_range": "전체 학기 (1-15주)" | "전반 (1-7주)" | "후반 (8-15주)" | null,
  "notes": str | null,
  "missing_fields": list[str]          // 위 핵심 필드 중 null 인 키 이름
}
"""

_SYSTEM_CONSISTENCY = """\
당신은 한성대 강의실 예약 시스템의 신청서 검토 어시스턴트입니다.
주어진 신청을 정책 문서와 대조해 **잠재적 모순/이상 패턴**만 한국어로 짧게 짚어내세요.
- 하드 규칙 위반(이론 + 실기실/실습실)은 시스템이 별도로 막으므로 굳이 적지 마세요.
- 다음과 같은 소프트 이상만 보고: 전공-건물 불일치, 인원-강의실종류 부적합 가능성,
  비고와 강의실종류의 모순, 시간/요일의 비현실성 등.
- 이상 없으면 warnings 를 빈 배열로 반환하세요.
- 각 경고는 1~2문장, 사실 기반, "~할 가능성이 있습니다." 식 권고 톤.

응답 JSON 스키마:
{ "warnings": list[str] }
"""

_SYSTEM_DEADLOCK = """\
당신은 한성대 강의실 예약 사무실의 협상 보조원입니다.
자동 배정에 실패한(시스템 내부 코드 'deadlock', **사용자 노출 용어는 '재배정 필요'**)
신청에 대해, 주어진 가용 후보와 정책을 바탕으로
구체적이고 실행 가능한 대안 2~3개를 한국어로 제안하세요.
협상 메일과 description 본문에는 '데드락' 이 아니라 '재배정 필요' 표현을 사용합니다.

각 alternative.kind 는 다음 중 하나:
  - "time_shift" : 같은 강의실종류에서 다른 요일 또는 다른 교시
  - "building_swap" : 같은 시간대에 다른 건물의 같은 종류 강의실
  - "format_relax" : 이론 수업이면 일반강의실 대신 강당식/계단식 시도 등

각 대안은 가능한 한 명시적인 후보(강의실 코드/이름/시간)를 포함하세요.
끝으로, 신청자에게 보낼 협상 메일 초안(한국어, 정중한 톤, 200~300자) 1개를 작성하세요.

응답 JSON 스키마:
{
  "alternatives": [
    {
      "kind": "time_shift" | "building_swap" | "format_relax",
      "description": str,
      "suggested_classroom_code": str | null,
      "suggested_classroom_name": str | null,
      "suggested_days": list[str],
      "suggested_time_slot": str | null
    }
  ],
  "negotiation_email": str
}
"""


# ──────────────────────────── Pydantic 스키마 ────────────────────────────


class ExtractedApplication(BaseModel):
    applicant_name: Optional[str] = None
    email: Optional[str] = None
    affiliation: Optional[str] = None
    course_name: Optional[str] = None
    course_category: Optional[str] = None
    class_format: Optional[str] = None
    building: Optional[str] = None
    requested_type: Optional[str] = None
    capacity_needed: Optional[int] = None
    days: list[str] = Field(default_factory=list)
    time_slot: Optional[str] = None
    weeks_range: Optional[str] = None
    notes: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)


class ConsistencyWarnings(BaseModel):
    warnings: list[str] = Field(default_factory=list)


class DeadlockAlternative(BaseModel):
    kind: str  # "time_shift" | "building_swap" | "format_relax"
    description: str
    suggested_classroom_code: Optional[str] = None
    suggested_classroom_name: Optional[str] = None
    suggested_days: list[str] = Field(default_factory=list)
    suggested_time_slot: Optional[str] = None


class DeadlockProposal(BaseModel):
    alternatives: list[DeadlockAlternative] = Field(default_factory=list)
    negotiation_email: str = ""


class IntakeExamples(BaseModel):
    examples: list[str] = Field(default_factory=list)


# ────────────────────────── 공용 호출 헬퍼 ──────────────────────────


def _cached_system(system_text: str) -> list[dict]:
    """정책 문서 + 기능별 시스템 프롬프트를 캐시 가능한 블록으로 구성."""
    return [
        {"type": "text", "text": POLICY_DOC},
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _call_parse(system_text: str, user_text: str, schema, max_tokens: int = 1024):
    """JSON 응답 + 클라이언트측 Pydantic 검증.

    Anthropic 의 structured outputs(`messages.parse` / `output_config.format`) 는
    첫 호출 시 서버에서 스키마를 컴파일하느라 수십~수백 초가 걸릴 수 있다.
    데모 환경에서는 치명적이라, 프롬프트로 JSON 만 받게 하고 Pydantic 으로
    클라이언트에서 검증한다. 응답 자체는 일반 Haiku 호출이라 ~1초.
    """
    client = _client_or_none()
    if client is None:
        return None
    instr = (
        "\n\n반드시 **JSON 객체만** 으로 응답하세요. 코드 펜스(```), 설명, 인사말 모두 금지. "
        "필드명과 타입은 위 시스템 프롬프트의 스키마 정의를 그대로 따릅니다. "
        "정보가 없으면 해당 필드는 null 또는 빈 배열로 두세요."
    )
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=_cached_system(system_text),
            messages=[{"role": "user", "content": user_text + instr}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        text = _strip_code_fence(text)
        if not text:
            return _LLMError("empty response")
        data = json.loads(text)
        return schema.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — 시연 안정성 우선
        return _LLMError(repr(exc))


class _LLMError:
    def __init__(self, msg: str) -> None:
        self.message = msg


# ──────────────────────────── 1) 예시 생성 ────────────────────────────


_MOCK_EXAMPLES = [
    "강의자: 김민수 교수 (minsu@hansung.ac.kr, 컴퓨터공학부). 자료구조 수업, 공학 전공 이론, 월수 1-2교시 연속 30명, 공학관 일반강의실 희망합니다.",
    "박서연 교수 / seoyeon@hansung.ac.kr / 패션디자인학과. 패션 일러스트(전공-패션+디자인 실기), 화목 4-5교시 20명, 창의관 디자인개발실, 전반 7주만 사용합니다.",
    "이지훈 교수(jihoon@hansung.ac.kr, 교양교육원). 글쓰기와 토론(교양 이론) 60명, 월요일 5교시, 상상관 계단식 강의실. 빔프로젝터 필요.",
]


def generate_intake_examples(n: int = 1) -> list[str]:
    """챗봇 도입부에 보여줄 자연어 신청 예시 n개. live 면 매번 새로 생성, mock 이면 고정."""
    if not is_live():
        # mock 도 매번 다른 예시가 보이도록 셔플
        pool = list(_MOCK_EXAMPLES)
        random.shuffle(pool)
        return pool[:n]

    user_text = (
        f"한성대 강의실 예약을 자연어로 신청하는 짧은 한국어 예시 문장 {n}개를 만들어주세요.\n"
        "- 각 예시는 한 문장(긴 경우 두 문장)으로, 정책 문서의 핵심 필드를 자연스럽게 녹여 적습니다.\n"
        "- 화자는 강의를 직접 진행하는 **강의자(교수) 본인** 입니다. 'OO교수' 또는 '강의자: OO' 형태로 자기 이름을 적도록 하세요.\n"
        "- **한국 성명**(예: 김민수 교수)과 **학교 이메일**(예: id@hansung.ac.kr), **소속 학과명** 을 반드시 포함하세요.\n"
        "- 시간대는 단일 교시(예: 2교시) 또는 **연속된 2교시**(예: 1-2교시) 를 자연스럽게 섞어 사용하세요.\n"
        "- 매번 다른 학과/건물/시간/이름을 사용하세요."
    )
    res = _call_parse(
        "당신은 한성대 강의실 예약 시스템의 데모 도우미입니다. 한국어로 답하세요.",
        user_text,
        IntakeExamples,
        max_tokens=400,
    )
    if isinstance(res, IntakeExamples) and res.examples:
        return res.examples[:n]
    pool = list(_MOCK_EXAMPLES)
    random.shuffle(pool)
    return pool[:n]


# ─────────────────────── 2) 자연어 → 신청 필드 추출 ───────────────────────


_BUILDINGS = ["상상관", "공학관", "탐구관", "낙산관", "미래관", "우촌관", "지선관", "진리관", "창의관"]
_DAYS = ["월", "화", "수", "목", "금", "토"]
_CATS = ["교양", "전공-공학", "전공-무용", "전공-회화", "전공-패션+디자인"]
_FMTS = ["이론", "이론+실기", "실기"]
_TIME_RANGE_RE = re.compile(r"(\d)\s*[-~]\s*(\d)\s*교시")
_TIME_NUM_RE = re.compile(r"(\d)\s*교시")


def _extract_time_slot_string(text: str) -> str | None:
    """텍스트에서 단일 또는 연속 2교시 표현을 찾아 ' + ' 결합 형태로 반환.

    UI 단의 _normalize_time_slot 이 다시 정식 라벨로 확장하므로
    여기서는 "1교시" 또는 "1교시 + 2교시" 같은 정규화 직전 형태만 만든다.
    """
    m = _TIME_RANGE_RE.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 8 and 1 <= b <= 8 and abs(a - b) == 1:
            lo, hi = sorted((a, b))
            return f"{lo}교시 + {hi}교시"
    nums = [int(x) for x in _TIME_NUM_RE.findall(text) if 1 <= int(x) <= 8]
    if not nums:
        return None
    if len(nums) >= 2:
        uniq = sorted(set(nums))
        if len(uniq) >= 2 and uniq[1] - uniq[0] == 1:
            return f"{uniq[0]}교시 + {uniq[1]}교시"
    return f"{nums[0]}교시"


def _mock_extract(user_text: str) -> ExtractedApplication:
    text = user_text
    out = ExtractedApplication()

    # 강의자 이름 (mock 은 한글 이름을 안정적으로 못 잡지만, 가능한 패턴만)
    name_m = re.search(r"강의자\s*[:：]?\s*([가-힣]{2,4})\s*교수?", text)
    if not name_m:
        name_m = re.search(r"([가-힣]{2,4})\s*교수", text)
    if name_m:
        out.applicant_name = name_m.group(1)

    # 이메일
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    if m:
        out.email = m.group(0)

    # 소속(학과) — 흔한 접미사 매칭
    aff_m = re.search(r"([가-힣A-Za-z0-9+]+(?:학과|학부|교육원|대학원))", text)
    if aff_m:
        out.affiliation = aff_m.group(1)

    # 인원
    m = re.search(r"(\d{1,3})\s*명", text)
    if m:
        try:
            out.capacity_needed = int(m.group(1))
        except ValueError:
            pass

    # 건물
    for b in _BUILDINGS:
        if b in text:
            out.building = b
            break

    # 카테고리
    if "교양" in text:
        out.course_category = "교양"
    else:
        for c in _CATS[1:]:
            major = c.split("-", 1)[1]
            if major in text or c in text:
                out.course_category = c
                break

    # 포맷
    if "이론+실기" in text or "이론 + 실기" in text:
        out.class_format = "이론+실기"
    elif "실기" in text:
        out.class_format = "실기"
    elif "이론" in text:
        out.class_format = "이론"

    # 강의실 종류 (대표적인 것만)
    for rt in ["계단식 강의실", "PC실습실", "디자인개발실", "염색실", "피아노실",
               "실습실", "실기실", "일반강의실"]:
        if rt in text:
            out.requested_type = rt
            break

    # 요일
    out.days = [d for d in _DAYS if d in text]

    # 시간대 (단일 또는 연속 2교시)
    out.time_slot = _extract_time_slot_string(text)

    # 주차
    if "전반" in text:
        out.weeks_range = "전반 (1-7주)"
    elif "후반" in text:
        out.weeks_range = "후반 (8-15주)"
    elif "학기" in text or "전체" in text:
        out.weeks_range = "전체 학기 (1-15주)"

    # 누락 항목 계산
    core = {
        "applicant_name": out.applicant_name,
        "affiliation": out.affiliation,
        "course_category": out.course_category,
        "class_format": out.class_format,
        "building": out.building,
        "requested_type": out.requested_type,
        "capacity_needed": out.capacity_needed,
        "days": out.days or None,
        "time_slot": out.time_slot,
    }
    out.missing_fields = [k for k, v in core.items() if not v]
    return out


def extract_application_fields(user_text: str) -> ExtractedApplication:
    if not is_live():
        return _mock_extract(user_text)
    res = _call_parse(_SYSTEM_INTAKE, user_text, ExtractedApplication, max_tokens=900)
    if isinstance(res, ExtractedApplication):
        return res
    # live 실패 → mock 로 graceful degrade
    return _mock_extract(user_text)


# ─────────────────────── 3) 신청 일관성 검사 ───────────────────────


def _mock_consistency(app: dict) -> list[str]:
    warnings: list[str] = []
    cat = app.get("course_category") or ""
    bld = app.get("building") or ""
    fmt = app.get("class_format") or ""
    rt = app.get("requested_type") or ""
    cap = int(app.get("capacity_needed") or 0)

    major_to_building = {
        "공학": "공학관", "무용": "낙산관", "회화": "지선관", "패션+디자인": "창의관",
    }
    if cat.startswith("전공-"):
        major = cat[len("전공-"):]
        expected = major_to_building.get(major)
        if expected and bld and bld != expected:
            warnings.append(
                f"{cat} 전공 수업인데 희망 건물이 {bld} 입니다. "
                f"전공 건물({expected}) 이 비어 있다면 그쪽이 우선 배정될 가능성이 있습니다."
            )

    if fmt == "실기" and rt and "실기" not in rt and "실습" not in rt and "디자인" not in rt and "염색" not in rt and "피아노" not in rt:
        warnings.append(
            f"실기 수업인데 강의실 종류가 '{rt}' 입니다. 실기실/실습실/스튜디오류가 더 적합할 수 있습니다."
        )

    if rt == "PC실습실" and cap > 60:
        warnings.append(f"PC실습실 종류는 보통 60석 이내입니다. 인원 {cap} 명이면 좌석이 부족할 수 있습니다.")

    if "계단식" in rt and cap < 40:
        warnings.append(f"계단식 강의실은 대형 강의용입니다. 인원 {cap} 명에는 일반강의실이 더 적합할 수 있습니다.")

    return warnings


def check_consistency(application: dict) -> list[str]:
    if not is_live():
        return _mock_consistency(application)

    user_text = (
        "다음 신청 내용을 정책 문서와 대조해 소프트 이상만 한국어로 짚어주세요.\n"
        f"```json\n{json.dumps(application, ensure_ascii=False, indent=2)}\n```"
    )
    res = _call_parse(_SYSTEM_CONSISTENCY, user_text, ConsistencyWarnings, max_tokens=600)
    if isinstance(res, ConsistencyWarnings):
        return res.warnings
    return _mock_consistency(application)


# ─────────────────────── 4) 데드락 협상안 생성 ───────────────────────


def _mock_deadlock(app: dict, free_pool: list[dict]) -> DeadlockProposal:
    alts: list[DeadlockAlternative] = []
    if free_pool:
        picks = random.sample(free_pool, k=min(2, len(free_pool)))
        for p in picks:
            alts.append(
                DeadlockAlternative(
                    kind="time_shift" if p.get("same_building") else "building_swap",
                    description=(
                        f"{p['days']} {p['time_slot']} 에 {p['classroom_code']} "
                        f"{p['classroom_name']}({p.get('building', '-')}) 가 비어 있습니다."
                    ),
                    suggested_classroom_code=p["classroom_code"],
                    suggested_classroom_name=p["classroom_name"],
                    suggested_days=p["days"].split(",") if isinstance(p["days"], str) else list(p["days"]),
                    suggested_time_slot=p["time_slot"],
                )
            )
    email = (
        f"{app.get('applicant_name', '강의자')} 교수님,\n\n"
        f"신청하신 '{app.get('course_name', '강의')}' 강의실이 요청 시간대에 모두 점유되거나 "
        f"조건을 만족하는 후보가 없어 자동 배정이 어려운 상태(재배정 필요)입니다.\n"
        "아래 대안 중 가능한 옵션을 회신해주시면 즉시 배정을 진행하겠습니다.\n"
        + ("\n".join(f"- {a.description}" for a in alts) if alts else "- 다른 시간대 또는 다른 건물을 검토 부탁드립니다.\n")
        + "\n\n감사합니다.\n한성대 학사 사무실"
    )
    return DeadlockProposal(alternatives=alts, negotiation_email=email)


def suggest_deadlock_alternatives(
    application: dict,
    free_pool: list[dict],
    occupied_pool: list[dict] | None = None,
) -> DeadlockProposal:
    """데드락 신청 1건에 대해 대안 2~3개 + 협상 메일 초안 생성.

    free_pool: 같은 학기 내 같은 종류/유사 종류의 비어 있는 슬롯 후보.
        각 원소: {classroom_code, classroom_name, building, days, time_slot, capacity, same_building(bool)}
    occupied_pool: 같은 시간대를 점유한 신청 목록(협상 대상 후보, 선택).
    """
    if not is_live():
        return _mock_deadlock(application, free_pool)

    payload = {
        "application": application,
        "free_slot_candidates": free_pool[:30],
        "competing_applications": (occupied_pool or [])[:10],
    }
    user_text = (
        "재배정 필요(자동 재배정 필요) 신청 1건과 가용 후보를 제공합니다. "
        "대안 2~3개와 협상 메일 초안 1개를 한국어로 만들어주세요.\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n```"
    )
    res = _call_parse(_SYSTEM_DEADLOCK, user_text, DeadlockProposal, max_tokens=1200)
    if isinstance(res, DeadlockProposal):
        return res
    return _mock_deadlock(application, free_pool)
