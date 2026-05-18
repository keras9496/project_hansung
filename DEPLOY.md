# 배포 가이드 (GitHub → Render)

이 프로젝트는 이미 다음을 준비해 두었습니다:
- `render.yaml` — Render 가 자동 감지하는 배포 정의 (Python 3.12.10, starter plan, Streamlit 시작 명령, 첫 부팅 시 강의실 자동 시드)
- `requirements.txt` — 모든 의존성
- `runtime.txt` — Python 버전 핀
- `.gitignore` — `.env` / DB / 로컬 메모리 제외
- 첫 커밋도 이미 만들어져 있습니다 (`git log` 로 확인)

남은 작업은 **GitHub 에 올리기 → Render 에 연결 → 환경변수 2개 입력** 세 단계입니다.

---

## 1. GitHub repo 만들고 푸시

### 1-1. GitHub 에서 빈 repo 만들기

1. https://github.com/new 접속
2. Repository name: `hansung-classroom-reservation` (자유)
3. **Private** 권장 (시드된 강의실 데이터 보호 차원)
4. **README/`.gitignore`/license 추가 옵션은 모두 끄기** ← 이미 로컬에 있어 충돌 방지
5. **Create repository** 클릭

### 1-2. 로컬에서 push

GitHub 가 페이지 상단에 보여주는 URL 을 복사한 뒤, 프로젝트 폴더에서:

```bash
# HTTPS 방식 (가장 흔함)
git remote add origin https://github.com/<본인계정>/<repo이름>.git
git push -u origin main
```

처음 push 할 때 GitHub 계정 인증을 요구합니다 (브라우저 로그인 또는 personal access token).

> SSH 키를 미리 등록해 두었다면 URL 을 `git@github.com:<계정>/<repo>.git` 로 바꾸세요.

푸시 성공 확인: GitHub repo 페이지 새로고침 → 파일 트리가 보이면 완료.

---

## 2. Render 에 연결

### 2-1. Render 가입 / 로그인

https://dashboard.render.com — GitHub 계정으로 로그인하는 게 가장 편합니다.

### 2-2. New Web Service

1. 대시보드 우측 상단 **New +** → **Web Service**
2. **Connect a repository** → GitHub 권한 부여 → 방금 만든 repo 선택
3. Render 가 `render.yaml` 을 자동으로 감지하고 모든 설정을 가져옵니다. **거의 모든 필드가 자동으로 채워집니다.**
4. **Plan** 만 확인: `Starter` (월 $7) — `render.yaml` 에 명시돼 있지만 무료 trial 으로 시작도 가능
5. **Apply** 또는 **Create Web Service** 클릭

### 2-3. 환경변수 2개 입력

Render 가 첫 빌드를 시작하지만 **`ANTHROPIC_API_KEY` 와 `ADMIN_TOKEN` 이 비어 있으면 AI 기능이 mock 으로 동작**합니다. 이 두 개만 채워주면 됩니다.

1. 좌측 메뉴 **Environment**
2. 다음 두 개를 **Add Environment Variable** 로 추가:

| Key | Value | 비고 |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-api03-…` | https://console.anthropic.com → API Keys 에서 발급 |
| `ADMIN_TOKEN` | 본인이 정한 임의 문자열 | 어드민 보호용 (현재는 미사용, 추후 인증 추가 시 사용) |

3. **Save Changes** → 자동으로 재배포 트리거됨

### 2-4. 배포 로그 확인

좌측 메뉴 **Logs** → 다음 두 줄이 보이면 성공:

```
[seed] inserted=153, updated=0, total=153
…
You can now view your Streamlit app in your browser.
URL: http://0.0.0.0:10000
```

> 첫 빌드는 의존성 설치 때문에 **3~5분** 정도 걸립니다. 이후 재배포는 push 시 자동 트리거되며 1~2분.

### 2-5. 접속

좌측 상단의 `https://<service-name>.onrender.com` URL 을 클릭 → 진입 화면(강의실 예약하기 / 관리자 시스템) 이 보이면 끝.

---

## 3. (옵션) 데모용 신청 100건 시드

기본 배포에는 **강의실 마스터(153개)** 만 자동 시드됩니다. 데드락/협상안 데모를 보여주려면 신청 100건도 채워야 합니다.

### Render Shell 에서 실행

1. Render 대시보드 → 본인 서비스 → 좌측 메뉴 **Shell**
2. 터미널이 열리면:
   ```bash
   python scripts/seed_applications.py --reset
   ```
3. `[seed_applications] 학기: 2026-1, 적재: 100건` 메시지 확인

> Shell 은 starter plan 이상에서만 제공됩니다. Free plan 이라면 로컬에서 시드한 SQLite 파일을 SCP 로 올리거나, 일시적으로 startCommand 에 `seed_applications.py` 를 끼웠다가 뺍니다.

---

## 4. 이후 업데이트 워크플로우

```bash
# 1. 코드 수정
# 2. 커밋 + 푸시
git add .
git commit -m "변경 설명"
git push

# 3. Render 가 자동으로 감지 → 재빌드 → 재배포 (1~2분)
```

`render.yaml` 을 바꾸는 변경은 Render dashboard 에서 **Manual Deploy** → **Clear build cache & deploy** 가 필요할 수 있습니다.

---

## 5. 자주 부딪히는 문제

| 증상 | 원인 | 해결 |
|---|---|---|
| 빌드 실패: `Could not find a version that satisfies the requirement anthropic>=0.92` | 패키지 버전이 PyPI 에 없는 경우 거의 없음. 캐시 문제 | Manual Deploy → Clear build cache & deploy |
| 시작 직후 500: `sqlite3.OperationalError: unable to open database file` | `data/` 디렉토리가 없음 (보통 자동 생성됨) | `app/config.py` 의 `DB_PATH.parent.mkdir(parents=True, exist_ok=True)` 로 자동 처리되므로 발생 시 로그 추가 확인 |
| AI 기능이 mock 으로만 동작 | `ANTHROPIC_API_KEY` 미설정 또는 키 오타 | Environment 에서 키 재입력 → Save Changes |
| 페이지 새로고침 시 데이터가 사라짐 | Render starter plan 디스크가 재배포 시 휘발 | Persistent Disk 추가 (Render dashboard → Disks → Add Disk, mount path `/opt/render/project/src/data`) 또는 PostgreSQL 로 전환 |
| 한글 파일명/디렉토리 충돌 | Render 의 Linux 환경에서는 정상 작동, 일부 git GUI 에서만 표시 깨짐 | 무시 가능 |
| LLM 호출이 매번 5초 이상 | Opus 4.7 의 inference 시간 | 비용/속도 우선이면 `CLAUDE_MODEL=claude-haiku-4-5` 로 변경 (Environment 에서) |

---

## 6. 비용 예상 (2026-05 기준)

| 항목 | 단위 | 데모용 예상 사용량 | 월 비용 |
|---|---|---|---|
| Render Starter | 월 정액 | 1 service | $7 |
| Anthropic Haiku 4.5 (기본) | $1 / 1M input, $5 / 1M output | NLU intake 50회 + 데드락 협상 20회 + 일관성 검사 30회 ≈ 200K in / 50K out (prompt caching 적용 후) | ~$0.5 |
| **합계** | | | **~$8/월** |

더 높은 품질이 필요하면 `CLAUDE_MODEL=claude-opus-4-7` 로 전환 시 LLM 비용 ~$2-3/월 추가 (총 ~$10).

---

## 7. 보안 체크리스트 (push 전 마지막 확인)

- [x] `.env` 가 `.gitignore` 에 포함됨
- [x] 첫 커밋에 `.env` 가 들어가지 않았음 (`git ls-files | grep .env` 가 `.env.example` 만 반환해야 함)
- [x] `data/*.db` 도 제외됨
- [x] Anthropic API 키는 **Render Environment 에만** 입력 (코드/yaml/git 어디에도 평문으로 박지 않음)
- [ ] GitHub repo 는 Private (강의실 마스터 데이터 보호)

---

문의/막히는 부분 있으면 `CLAUDE.md` 의 § 9 (배포) 와 § 11 (알려진 한계) 를 함께 참고하세요.
