# 한성대 강의실 예약 시스템 — 후속 작업 가이드

이 문서는 **후속 작업을 맡는 Claude / 개발자** 가 프로젝트의 현 상태를 빠르게 파악하기 위한 핵심 레퍼런스입니다. 코드를 읽기 전에 이 파일을 먼저 훑어 주세요.

---

## 1. 프로젝트 컨텍스트

- **목적**: 한성대학교 학사 사무실의 정규 학기 강의실 자동 배정 + 임시 사용 신청 + 운영자 어드민을 한 화면에서 시연할 수 있는 챗봇 기반 프로토타입.
- **제출처**: 한성대학교 **AI 프런티어 사업**. 따라서 단순 CRUD 가 아니라 **Claude API 를 의미 있게 녹인 AI 기능 3종** 이 핵심 차별점.
- **단계**: 프로토타입(시연 우선). 안정성보다 **데모 임팩트**와 **시연 안전성**(네트워크 끊김 대비) 을 더 중시한다.
- **기술 스택**: Streamlit + SQLAlchemy + SQLite + Anthropic Python SDK + Plotly.

`01_개발기획안.md`, `02_개발계획.md` 에 기획·계획서가 있지만 구현은 이미 기획안을 넘어 확장됐다 — 갭이 있을 때는 코드와 본 문서가 우선이다.

---

## 2. 실행 방법

```bash
# 1) 의존성
pip install -r requirements.txt

# 2) 강의실 마스터 시드 (.xlsx → DB)  ← 한 번만
python scripts/seed_classrooms.py

# 3) 데모용 신청 100건 시드 (옵션)
python scripts/seed_applications.py --reset

# 4) 실행
streamlit run app/main.py
```

기본 학기는 `app/services/semester_service.ensure_demo_semester()` 가 `2026-1학기` 를 자동 생성. 새 학기를 추가하려면 어드민 화면에서 직접.

DB 는 SQLite 단일 파일: `data/reservations.db`. 스키마가 바뀌어도 **DB 를 지울 필요 없음** — `init_db()` 가 누락 컬럼만 `ALTER TABLE ADD COLUMN` 한다 (§4 참고).

---

## 3. 디렉토리 구조

```
한성대프로젝트/
├── app/
│   ├── main.py              # 진입점 + 라우팅 (landing → user/admin 분기)
│   ├── config.py            # env 로딩 (ANTHROPIC_API_KEY, LLM_MODE, CLAUDE_MODEL …)
│   ├── db.py                # SQLAlchemy 엔진 + 가벼운 마이그레이션
│   ├── models.py            # ORM 모델 (Classroom, Application, Assignment, …)
│   ├── demos/               # ← 각 화면 (시연 단위로 1파일 = 1탭)
│   │   ├── _shared.py       # 공용 상수 (요일, 교시, 건물, 카테고리, 헬퍼)
│   │   ├── regular.py       # 정규 신청 챗봇 + AI NLU intake + 일관성 검사
│   │   ├── temp.py          # 임시 사용 챗봇
│   │   ├── assignment.py    # 배정 실행 + AI 데드락 협상안 생성
│   │   ├── admin.py         # 학기 설정 + 메일 시뮬레이션
│   │   └── dashboard.py     # 관리자 진입 시 활용 현황 원형차트
│   └── services/
│       ├── llm.py           # ★ Claude API 래퍼 (live/mock, prompt caching)
│       ├── assignment_engine.py  # FCFS + 3-tier best-fit 배정
│       ├── semester_service.py
│       ├── temp_service.py
│       └── mailer.py        # 메일 시뮬레이션 (DB 로그)
├── scripts/
│   ├── seed_classrooms.py   # .xlsx 마스터 → DB upsert
│   └── seed_applications.py # 데모용 신청 100건 랜덤 생성
├── data/reservations.db     # SQLite (gitignore)
├── 강의실 데이터.xlsx        # 153개 강의실 마스터
├── 2025학년도 강의실 사용률.xlsx  # 관리소속 보강용
├── render.yaml              # Render 배포 정의
├── .env / .env.example      # 환경변수 (.env 는 gitignore)
└── requirements.txt
```

**의도**: `demos/` 는 화면(좌/우 2분할 UI)에 1:1 대응, `services/` 는 도메인 로직. UI 코드와 로직 코드를 섞지 말 것.

> **주의**: `assignment_engine.py` 가 `app.demos._shared` 를 import 한다 (소프트 룰의 매핑이 거기 있어서). 아키텍처적으로는 어색하지만 프로토타입 단계에서 의도된 단순화다. 본격 분리하려면 `_shared.py` 의 도메인 메타를 `app/domain.py` 같은 곳으로 옮겨야 한다.

---

## 4. 데이터 모델 (`app/models.py`)

| 테이블 | 핵심 필드 | 메모 |
|---|---|---|
| `Classroom` | code, name, room_type, capacity, **building**, managing_dept | building 은 코드 prefix(1F→상상관, DF→공학관 등)로 자동 추론 |
| `Application` | applicant_name, email, course_name, requested_type, capacity_needed, days(JSON), time_slot, weeks(JSON), **building**, **course_category**, **class_format**, status, created_at | status: pending / assigned / deadlock |
| `Assignment` | application_id(unique), classroom_id, days, time_slot, weeks, method | method: auto / random / manual |
| `TempReservation` | classroom_id, start_at, end_at, status | active / expired / cancelled |
| `MailLog` | event_kind, recipients(JSON), subject, body, status | pending → sent (어드민이 수동 발송 시뮬레이션) |
| `Semester` | year, term, dates, status | 기본 학기 자동 생성 |

### 가벼운 마이그레이션

`app/db.py` 의 `_apply_lightweight_migrations()` 가 `_PENDING_COLUMNS` 리스트를 순회해 누락된 컬럼을 `ALTER TABLE` 로 더한다. **새 컬럼을 모델에 추가했으면 이 리스트에도 한 줄 추가**해야 기존 DB 가 자동 마이그레이션된다. drop/rename 은 지원하지 않는다 — 그건 사용자에게 알리고 DB 백업 후 마이그레이션 도구(alembic) 도입 여부를 묻는다.

```python
_PENDING_COLUMNS: list[tuple[str, str, str]] = [
    ("classrooms", "building", "VARCHAR(32)"),
    ("applications", "building", "VARCHAR(32)"),
    ("applications", "course_category", "VARCHAR(32)"),
    ("applications", "class_format", "VARCHAR(16)"),
]
```

---

## 5. 도메인 메타데이터 (`app/demos/_shared.py`)

핵심 상수와 헬퍼가 여기 모여 있다. 새 코드가 직접 문자열을 하드코딩하지 말고 여기서 import 한다.

- `DAYS` — `["월","화","수","목","금","토"]` (6요일)
- `TIME_SLOTS` — `"1교시 (09:00-10:15)" … "8교시 (19:30-20:45)"` (8교시)
- `WEEK_PRESETS` — `"전체 학기 (1-15주)"`, `"전반 (1-7주)"`, `"후반 (8-15주)"`
- `BUILDINGS` — `(이름, 설명, 우선배정 전공키)` 튜플 9개
  - 상상관(공통), **공학관(공학)**, 탐구관(공통), **낙산관(무용)**, 미래관(공통), 우촌관(공통), **지선관(회화)**, 진리관(공통), **창의관(패션+디자인)**
- `BUILDING_NAMES`, `BUILDING_LABELS` — UI 셀렉트박스용 파생
- `COURSE_CATEGORIES` — `["교양", "전공-공학", "전공-무용", "전공-회화", "전공-패션+디자인"]`
- `CLASS_FORMATS` — `["이론", "이론+실기", "실기"]`
- `MAJOR_BUILDING_MAP` — `{"공학":"공학관", "무용":"낙산관", "회화":"지선관", "패션+디자인":"창의관"}`
- `CODE_PREFIX_TO_BUILDING` — `{"1F":"상상관", "DF":"공학관", "HF":"탐구관", "HB":"탐구관", "MF":"낙산관", "EF":"창의관", "CF":"지선관", "AF":"우촌관", "BF":"진리관", "JB":"미래관"}`

### 헬퍼

- `is_practice_room(room_type)` → bool (room_type 에 "실기" 또는 "실습" 포함)
- `preferred_building_for(course_category)` → 전공이면 매칭 건물, 아니면 None
- `major_of(course_category)` → 전공 키만 추출 ("공학", "무용", …)
- `infer_building_from_code(code)` → code prefix 로 건물 추론
- `get_classroom_types()` → DB 에서 현재 등록된 room_type unique 리스트

---

## 6. 화면 구조 / 네비게이션 (`app/main.py`)

```
진입 (landing)
  └─ 두 개의 큰 버튼 (높이 260px, font 2.4rem 900 weight)
       ├─ 📅 강의실 예약하기  → section="user"
       │     └─ 사이드바: 정규 신청 (챗봇) / 임시 사용 (챗봇)
       └─ 🛠️ 관리자 시스템   → section="admin"
             └─ 사이드바: 📊 활용 현황 / 🧮 배정 실행 / ⚙️ 학기 설정·메일
```

- 상태는 `st.session_state["section"]` 으로 관리 (`landing` | `user` | `admin`)
- 사이드바 상단의 **⬅️ 처음 화면으로** 버튼이 어떤 화면에서든 landing 으로 복귀
- 데모 데이터 초기화(`🧹` 버튼)는 **관리자 경로에서만** 노출
- 모든 demo render 함수는 `def render(semester: Semester) -> None` 시그니처

**중요한 UX 결정**: 진입 화면의 버튼 텍스트는 `\n\n` 두 줄로 구성(아이콘 → 메인 라벨 → 서브 설명). CSS 의 `font-size: 2.4rem !important` 가 `<p>` 태그까지 강제 적용.

---

## 7. 배정 엔진 (`app/services/assignment_engine.py`)

### 정책

1. **우선순위**: 도착순 FCFS (created_at 오름차순, 동률은 id 오름차순)
2. **하드 규칙**: `class_format == "이론"` 이면 `is_practice_room()` 인 강의실 제외
3. **소프트 규칙** (3-tier — 위에서 아래로 시도, 비면 다음 tier)
   - **tier_major**: 전공 수업이면 매칭 전공 건물 후보만
   - **tier_user**: 신청자가 고른 희망 건물 후보 (major 와 중복 제거)
   - **tier_rest**: 그 외 모든 후보
4. 각 tier 안에서 무작위 셔플 + 최소 capacity best-fit 선택
5. 모든 tier 가 비면 `deadlock` 으로 마킹 + 메일 큐 적재

### 호출 인터페이스

```python
result = run_assignment(semester_id, seed=42)
# {
#   "assigned": int, "deadlock": int, "total": int,
#   "details": [{"application_id", "applicant", "result", "tier"?, "classroom_code"?, "classroom_building"?, "reason"?}]
# }
```

`assignment.py` 의 실행 상세 로그는 `details[].tier` 로 어떤 풀에서 잡혔는지 보여준다 (전공건물 / 희망건물 / 그 외).

### 같은 학기 내 충돌 모델

- 슬롯 단위 = `(요일, 시간대)`
- 한 강의실의 같은 슬롯은 1건만 가능
- **주차는 충돌 판정에 사용하지 않음** (프로토타입 단순화 — 전반/후반 동시 사용을 명시적으로 허용하려면 별도 확장)

---

## 8. AI 기능 (Claude API) ⭐

여기가 프로젝트의 핵심 차별점. 후속 작업에서 가장 자주 손대게 될 영역이다.

### 8.1 모듈 구조 (`app/services/llm.py`)

```
llm.py
├── 모드 판정
│   ├── is_live() / mode_label()      # auto | live | mock
│   └── _client_or_none()             # lazy Anthropic 클라이언트
├── 도메인 메타 (시스템 프롬프트에 들어가는 정책 문서)
│   ├── POLICY_DOC                    # 강의실/교과/시간 규칙
│   └── _SYSTEM_INTAKE / _CONSISTENCY / _DEADLOCK
├── Pydantic 스키마
│   ├── ExtractedApplication          # NLU intake 결과
│   ├── ConsistencyWarnings           # 일관성 검사 결과
│   ├── DeadlockProposal              # 데드락 협상안
│   └── IntakeExamples                # 예시 생성 결과
├── 공용 호출 헬퍼
│   ├── _cached_system(system_text)   # prompt caching 적용
│   └── _call_parse(system, user, schema, max_tokens)
└── 4개 공개 함수
    ├── generate_intake_examples(n) → list[str]
    ├── extract_application_fields(text) → ExtractedApplication
    ├── check_consistency(application) → list[str]
    └── suggest_deadlock_alternatives(application, free_pool, occupied_pool) → DeadlockProposal
```

### 8.2 모드 전환 (live ↔ mock)

```python
LLM_MODE=auto   # API 키가 있으면 live, 없으면 mock (기본)
LLM_MODE=live   # 강제 live (키 없으면 호출 실패 → mock 로 graceful degrade)
LLM_MODE=mock   # 강제 mock (개발 중 비용 절약 / 시연 안전망)
```

**핵심 설계**: live 호출이 어떤 이유로든 실패하면(`except Exception`) **mock 응답으로 graceful degrade**. 시연 중 네트워크 끊김에도 UI 가 깨지지 않는다.

mock 응답은 단순 규칙 기반이지만 핵심 필드는 잡힌다:
- `extract_application_fields` mock: 이메일/인원/건물/카테고리/포맷/요일/시간/주차 정규식 추출
- `check_consistency` mock: 전공-건물 불일치, 실기-일반강의실, PC실습실 인원 초과, 계단식-소형 인원
- `suggest_deadlock_alternatives` mock: free_pool 에서 무작위 2개 선택 + 기본 메일 템플릿

### 8.3 모델 / 비용

- 기본 모델: **`claude-haiku-4-5`** (속도/비용 균형) — `CLAUDE_MODEL` 환경변수로 교체
- 더 높은 추론 품질이 필요하면 `CLAUDE_MODEL=claude-opus-4-7` (속도 ~3배 느림, 비용 ~5배)
- 정책 문서(`POLICY_DOC`) + 기능별 시스템 프롬프트(`_SYSTEM_*`)는 **prompt caching** 적용 — 두 번째 호출부터 입력 토큰 단가 ~0.1배
- `_cached_system()` 이 `[{...POLICY_DOC}, {...SYSTEM, cache_control: ephemeral}]` 형태로 시스템 블록 구성. cache_control 위치는 변경 금지(첫 호출 후 5분 TTL 으로 캐시 생성).

### 8.4 기능별 매핑

#### 기능 1 — 자연어 신청 파싱 (NLU intake)
- **위치**: `app/demos/regular.py` → `_render_intake_panel()`
- **흐름**: 챗봇 시작 직전 expander 노출 → `generate_intake_examples(3)` 로 예시 3개 → 사용자가 자연어 한 문장 입력 → `extract_application_fields(text)` → `_normalize_extracted()` 로 정식 라벨로 변환 (예: "2교시" → "2교시 (10:30-11:45)") → `_next_missing_step()` 로 빠진 첫 단계로 점프
- **UX**: 예시 클릭 → 텍스트박스 자동 채움. "💡 예시 새로고침" 으로 LLM 이 새 예시 생성.
- **graceful degrade**: live 실패 시 mock 결과를 사용. mock 은 한국어 이름/학과는 못 잡으니 챗봇이 마저 물어본다.

#### 기능 2 — 데드락 협상안
- **위치**: `app/demos/assignment.py` → `_render_deadlock_card()` + `_build_pools_for()`
- **흐름**: 데드락 표 아래에 건별 expander → "AI 협상안 생성" 클릭 → `_build_pools_for()` 가 같은 학기의 모든 배정을 스캔해 free pool(시간 이동/건물 교체 후보) + occupied pool(경합 신청) 구성 → `suggest_deadlock_alternatives()` 호출 → `DeadlockProposal` 결과 (대안 2~3개 + 협상 메일 초안) 를 UI 에 렌더
- **결과 종류**: `time_shift` / `building_swap` / `format_relax`
- **캐싱**: `st.session_state[f"deadlock_proposal_{app.id}"]` 에 저장. "재생성" 버튼으로 다시 호출.
- **메일 초안**: 편집 가능한 textarea 로 노출, 사용자가 어드민 메일함으로 복사해 발송 (현재는 자동 발송 연결 없음 — 후속 작업 후보).

#### 기능 3 — 신청-정책 일관성 검사
- **위치**: `app/demos/regular.py` → `_render_input()` 의 `confirm` 단계
- **흐름**: confirm 단계 진입 시 `check_consistency(draft)` 1회 호출 → 경고 리스트를 `st.warning` 로 비차단 표시 → 사용자가 그대로 "신청 완료" 가능
- **캐싱**: `id(draft)` 으로 동일 draft 에 대해 중복 호출 방지
- **하드 규칙(이론→실기실)은 검사하지 않음** — 시스템이 별도로 막으므로 LLM 은 소프트 이상만 본다

#### 기능 보조 — 예시 생성
- **위치**: `generate_intake_examples(n)` (llm.py)
- live 면 매번 새 예시 생성, mock 이면 고정 3개 반환
- `_MOCK_EXAMPLES` 는 9개 필드를 다양하게 커버하도록 사전 작성됨

### 8.5 새 AI 기능 추가 시 패턴

1. `llm.py` 에 Pydantic 스키마 추가
2. `_SYSTEM_<기능>` 시스템 프롬프트 작성 (간결하게, 정책 문서는 자동으로 앞에 붙음)
3. 공개 함수 작성 — live 분기와 mock 분기를 둘 다 구현 (graceful degrade 필수)
4. `_call_parse(_SYSTEM_<기능>, user_text, Schema, max_tokens=…)` 호출
5. UI 측은 `with st.spinner(...):` 로 감싸기 (응답 1~3초)
6. 결과 캐싱은 `st.session_state` 활용

---

## 9. 배포 (Render)

- **render.yaml** 이 자동 감지됨. push 후 Render → New → Web Service → repo 선택
- 환경변수 중 `sync: false` 두 개만 대시보드에서 입력:
  - `ANTHROPIC_API_KEY` — Anthropic Console 에서 발급
  - `ADMIN_TOKEN` — 임의 값
- 나머지(`LLM_MODE=live`, `CLAUDE_MODEL=claude-opus-4-7` 등)는 render.yaml 에 박혀 있음
- Render starter plan 기준. 트래픽 증가 시 plan 업그레이드
- **빌드 명령**: `pip install -r requirements.txt`
- **시작 명령**: `streamlit run app/main.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`

### 첫 배포 후 해야 할 일

1. Render 셸에서 `python scripts/seed_classrooms.py` 1회 실행 (강의실 마스터 적재)
2. 데모용이면 `python scripts/seed_applications.py --reset` 으로 100건 시드
3. 브라우저 접속 → 진입 화면 확인

> **SQLite + Render 의 단점**: Render 무료/starter plan 의 디스크는 재배포 시 휘발될 수 있음. 운영 단계 진입 시 PostgreSQL (Render Postgres or Supabase) 로 옮겨야 함.

---

## 10. 시드 / 개발 데이터

### `seed_classrooms.py`
- `강의실 데이터.xlsx` → 153개 강의실 upsert (code 기준)
- 코드 prefix 로 building 자동 매핑 (1F=상상관 …)
- 재실행 안전 (upsert)

### `seed_applications.py`
- 데모용 100건 신청 생성 — 카테고리/포맷/건물 분포가 강의실 종류에 맞게 자연스러움
- `--reset` 옵션: 기존 신청/배정/메일 초기화 후 시드
- `--count N`, `--seed N` 지정 가능
- 인기 시간대(2·4교시) 가중치로 의도적으로 데드락 시연이 생기도록 설계됨

---

## 11. 알려진 한계 / 디자인 결정

| 항목 | 현 상태 | 비고 |
|---|---|---|
| 마이그레이션 | add column only (`_apply_lightweight_migrations`) | drop/rename 필요하면 alembic 도입 검토 |
| 주차 충돌 | 미고려 (같은 요일/시간이면 무조건 충돌) | 전반/후반 동시 사용 허용은 별도 확장 |
| 임시예약 슬롯 환산 | 근사값 (걸친 일수 × 1.25시간 단위) | dashboard.py 의 시간 점유율은 참고치 |
| 메일 발송 | 시뮬레이션만 (DB 로그) | 실제 SMTP 는 mailer.py 에서 구현 필요 |
| AI 데드락 메일 자동 발송 | 미연결 (사용자가 textarea 에서 복사) | 어드민 메일함 직접 연결은 후속 작업 후보 |
| `assignment_engine` → `demos._shared` import | 의도된 단순화 | 본격 분리 시 `_shared` 의 도메인 메타를 `app/domain.py` 로 이동 |
| 인증 | 없음 | 학사 사무실/학생 분리는 미구현 |
| 테스트 | 없음 | 시연용 mock 데이터로 검증 |
| 동시성 | 없음 (단일 사용자 가정) | Streamlit `session_state` 는 사용자별이지만 DB 락은 없음 |
| SQLite | 단일 파일, 동시 쓰기 약함 | 운영 진입 시 PostgreSQL |

---

## 12. 후속 작업 시 주의사항

### 코드 스타일
- `from __future__ import annotations` 항상 사용 (모든 파일)
- 타입 힌트 적극 사용 (`list[dict]`, `Optional[X]`)
- docstring 은 한국어. 주석은 "왜" 만 적고 "무엇" 은 코드로
- Streamlit 위젯은 반드시 `key=` 명시 (rerun 시 상태 보존)

### UI 작업
- demo render 함수는 좌/우 2분할(`st.columns([1,1], gap="medium")`) 패턴이 기본
- 단, `dashboard.py` 는 단일 컬럼 풀폭 (정보 밀도 우선)
- 차트는 Plotly + `st.plotly_chart(fig, use_container_width=True)`
- 한국어 라벨, 이모지 라벨링 OK

### AI 기능 작업
- **새 기능 추가 전 § 8.5 참고**
- **graceful degrade 필수** — live 실패 시 mock 응답이 반드시 있어야 시연이 안 깨진다
- 새 시스템 프롬프트는 정책 문서 뒤에 캐싱 가능하게 작성
- 비용 우려되면 `max_tokens` 작게 잡고 prompt 짧게
- Pydantic 스키마로 출력 강제 — `client.messages.parse()` 사용

### DB 작업
- 새 컬럼 추가 시 `_PENDING_COLUMNS` 에도 추가
- 마이그레이션이 add column 만 지원함을 사용자에게 알릴 것
- session 은 `with SessionLocal() as s:` 패턴 (autocommit 없음)

### 시연 환경
- 데모 직전 `python scripts/seed_applications.py --reset` 으로 신선한 상태 만들기
- LLM live 호출은 1~3초 지연 → `st.spinner()` 필수
- 네트워크 불안정한 환경에서는 `LLM_MODE=mock` 으로 사전 전환

### 사용자 컨텍스트
- 사용자는 **한성대 학생/연구자** 로, 이 시스템을 AI 프런티어 사업에 제출하려 한다
- "AI 가 진짜로 결정/창의를 보태는가" 가 신규 기능 평가의 1순위 기준
- 챗봇 껍데기에 LLM 을 끼우는 식 (예: "안녕하세요" 응답을 LLM 으로) 은 가치 낮음 — 자연어 파싱·협상·일관성 검사처럼 **사람이 직접 했을 때 부담스러운 작업**에 우선 적용

---

## 13. 참고 — 최근 변경 이력 (요약)

타임스탬프 순(2026-05-18 기준)이 아닌 **개념적 묶음** 순서:

1. **진입 화면 분리** — landing 에 두 개의 큰 버튼(강의실 예약하기 / 관리자 시스템). 두 경로별로 사이드바 메뉴 분기. 글자 크기 2.4rem 900 weight 로 가시성 강조.
2. **도메인 확장** — 9개 건물(상상관/공학관/탐구관/낙산관/미래관/우촌관/지선관/진리관/창의관), 교과구분 1(교양/전공-X), 교과구분 2(이론/이론+실기/실기) 도입. 모델/시드/UI/엔진까지 일관 반영.
3. **배정 엔진 확장** — 이론→실기실 하드 금지 + 전공건물/희망건물/그 외 3-tier 소프트 우선.
4. **AI 기능 3종** (Claude API):
   - 자연어 신청 파싱 (정규 신청 챗봇 도입부)
   - 데드락 협상안 + 메일 초안 (배정 화면 데드락 카드)
   - 신청-정책 일관성 검사 (신청 확정 직전 경고)
   - 모든 기능에 `live` ↔ `mock` 자동 전환 + graceful degrade
5. **활용 현황 대시보드** — 관리자 진입 시 첫 화면. 강의실 사용률 + 시간 점유율 원형차트(Plotly).
6. **배포 준비** — `render.yaml`, `.env.example`, prompt caching.

---

**문의/맥락이 더 필요하면**: `01_개발기획안.md` (도메인 깊이), `02_개발계획.md` (단계별 일정). 이 두 문서는 초기 기획안이라 현 구현과 갭이 있으므로 참고만 한다.
