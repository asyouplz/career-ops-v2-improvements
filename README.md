# Career Ops V2

채용 공고 수집, 중복·마감 판정, 지원 이력 관리와 보고를 위한 독립 실행형 저장소입니다.
**이 저장소만 내려받아 의존성을 설치하면 됩니다. 원본 저장소를 별도로 설치하거나
외부 원본 폴더를 연결할 필요가 없습니다.**

[Career-Ops 원본](https://github.com/santifer/career-ops)의 실행 엔진 중 V2가 사용하는
부분과 한국 채용 사이트 관련 수정분을 `engine/`에 포함했습니다. 원본의 전체 문서,
프로필, 홍보 자료, 데모, 테스트 모음은 포함하지 않습니다.
원본 코드의 MIT 고지는 [engine/LICENSE](engine/LICENSE)에 보존했습니다.

## 준비와 설치

Node.js 22.13 이상, Python 3.11 이상이 필요합니다. 아래는 macOS/Linux 기준입니다.

```bash
git clone https://github.com/asyouplz/career-ops-v2-improvements.git
cd career-ops-v2-improvements
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm ci --ignore-scripts
npm run browser:install
npm run setup
npm run preview
```

`npm run setup`은 현재 설치 위치와 실행기를 찾아 로컬 설정을 만들고, 빈 지원 이력과
수집 대기열을 초기화합니다. 개인 경력이나 가상 공고는 생성하지 않습니다.
이미 있는 설정과 지원 이력은 덮어쓰지 않습니다. 저장소를 다른 위치로 옮겼다면
기존 `config/runtime.json`에 저장된 경로를 확인해야 합니다.

`npm run preview`는 네트워크 수집과 메일·Slack 연동 없이 V2 흐름을 실행합니다.
설정이 비어 있으면 후보 0건이 정상입니다. 실행 결과는 로컬 `artifacts/`에 저장됩니다.

## 내 검색 조건 입력

설치 후 생성된 다음 **로컬 파일**을 편집합니다. Git에는 올라가지 않습니다.

- `engine/portals.yml`: 수집 사이트, 검색어, 제목·지역 필터
- `engine/config/profile.yml`: 목표 직무와 개인별 조건
- `engine/cv.md`: 실제 이력서 내용
- `config/linkedin_queries.json`: LinkedIn 공개 공고 검색 조건
- `config/profile_evidence.json`: 근거 파일과 대조할 경력 사실. 필요할 때 직접 입력
- `config/runtime.json`: 실행 경로, 재추천 유예 기간, 선택적 메일·Slack 설정

초기 `portals.yml`에는 원티드·사람인·리멤버·잡코리아가 모두 비활성 상태로 들어 있습니다.
사용할 출처의 `searchKeywords`에 원하는 검색어를 넣고 `enabled: true`로 바꿉니다.
개인별 직무나 지역은 코드에 기본값으로 넣지 않습니다. 원하는 필터도 직접 설정하세요.
LinkedIn은 `queries`에 `name`, `keywords`, `location`을 지정합니다.

## 실행 명령

| 명령 | 동작 |
|---|---|
| `npm run preview` | 네트워크 없이 V2 실행 흐름 확인 |
| `npm start` | 설정된 출처를 수집·검토하는 dry-run. 지원 이력 반영·Slack 발송 없음 |
| `npm run scan -- --dry-run --verify` | 수집기만 실행해 신규 공고와 활성 상태를 미리 확인 |
| `npm run scan -- --verify` | 수집 결과를 로컬 대기열과 수집 이력에 저장 |
| `npm run verify` | 실제 로컬 지원 이력의 중복·링크·상태 무결성 확인 |
| `npm run tracker -- sync --check` | 지원 이력 파싱 확인, 파생 색인은 쓰지 않음 |
| `npm run tracker -- sync` | 지원 이력에서 로컬 SQLite 색인 생성·갱신 |

`verify`와 `tracker`는 실제 운영 데이터 관리 도구입니다. 테스트 모음이나 가상 데이터를
실행하는 명령이 아닙니다. 수집 사이트의 접근 제한이나 마감 상태에 따라 결과가 달라질 수 있습니다.

## 메일·Slack·일정 실행은 선택 사항

독립 실행은 **원본 Career-Ops 저장소에 의존하지 않는다**는 뜻입니다.
외부 계정이나 서비스까지 인증 없이 사용할 수 있다는 뜻은 아닙니다.

- 공고 수집, 활성 판정, 이력 관리는 이 저장소와 설치한 의존성으로 실행합니다.
- 메일 검토에는 사용자가 별도로 인증한 Codex CLI의 Gmail 연결이 필요합니다.
- Slack 전송에는 설정된 Hermes CLI와 Slack 연결이 필요합니다.
- 예약 실행이 필요하면 사용자의 스케줄러에서 이 저장소의 명령을 호출합니다.

메일은 읽기 전용으로 검토하며, Slack은 초기 설정에서 비활성 상태입니다.
실제 상태 변경에는 `activation_mode: "apply"`와 `CAREER_OPS_V2_ENABLE_APPLY=1`이
모두 필요합니다. Slack 운영 진입점은 `entrypoint.py`이며, 전송 설정을 확인한 뒤
별도로 활성화합니다. 설치 과정에서 운영 일정이나 외부 계정을 자동으로 변경하지 않습니다.

## 포함 범위와 개인정보

- `engine/`: V2가 호출하는 수집기, 출처별 모듈, 활성 판정, 이력 관리와 공통 의존 코드
- `src/`, `legacy/`: V2 실행 흐름, LinkedIn·메일 연결, Slack 보고
- `scripts/`: 설치 위치 자동 연결과 초기화, 공통 실행 명령
- `config/*.example.*`: 개인정보와 구직 선호가 없는 설정 양식

원본의 CV/PDF 생성·대시보드 등 V2 실행에 필요하지 않은 부가 기능은 포함하지 않습니다.
테스트 소스·테스트 데이터·자체 테스트 모드·데모도 배포하지 않습니다.
이력서, 프로필, 지원 이력, 메일, 인증정보, 실행 결과는 Git에서 제외합니다.
공개 전에는 코드 본문과 커밋 작성자·메시지를 함께 확인해야 합니다.
