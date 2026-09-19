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

## Career Desk 대시보드

공고를 추천·지원현황·지원보류·지원제외로 나누어 관리하는 모바일 대응 화면입니다.
추천은 지원·보류·제외 이력을 먼저 반영한 뒤 사이트마다 최대 5개를 보여 줍니다.
카드를 보류·제외로 옮기면 같은 출처의 다음 후보를 채웁니다. 보류와 제외는 각각
출처 필터, 최근 5개, 접힌 지난 기록과 더보기를 제공합니다.

위의 기본 설치를 마친 뒤 다음을 실행합니다.

```bash
npm run dashboard:install
npm run dashboard:build
npm run dashboard:password
npm run dashboard
```

브라우저에서 `http://127.0.0.1:9121`에 접속해 설정한 비밀번호로 로그인합니다.
비밀번호 원문은 저장하지 않고 해시만 Git에서 제외된 `config/dashboard.json`에
보관합니다. 첫 설치는 빈 화면이며, 실제 공고는 수집을 설정한 뒤 채워집니다.
기존 지원 이력으로 화면 데이터를 먼저 만들려면 아래를 실행할 수 있습니다.

```bash
python3 src/dashboard_bootstrap.py --runtime config/runtime.json \
  --output engine/data/dashboard-candidates.json
```

추천·지원보류·검토 단계의 지원제외 카드는 드래그하거나 이동 메뉴로 분류합니다.
모바일 길게 누르기, 키보드 이동, 10초 실행 취소를 지원하며, 그 사이 변경된 기록은
취소로 덮어쓰지 않습니다. 실제 지원현황과 지원 이력이 있는 철회 건은 끌 수 없습니다.
분류 이동은 지원서를 제출하거나 실제 지원을 취소하지 않습니다.

인터넷에서 접속하려면 서버 앞에 HTTPS 프록시 또는 터널을 두고
`DASHBOARD_PUBLIC_ORIGIN=https://your-dashboard.example npm run dashboard`처럼
실제 주소를 지정합니다. 서버는 로컬 주소에만 바인딩합니다. 로그인과 Gmail 연동은
별도 설정이며, Gmail 없이도 직접 공고와 지원 상태를 관리할 수 있습니다.
현재는 **한 사용자·한 Gmail 계정용 설치**입니다. 여러 사용자가 함께 쓰는 서비스의
계정별 데이터 격리는 구현되어 있지 않습니다.

화면의 소개·개인정보·이용 안내는 설치용 양식입니다. 외부에 제공하기 전에
[화면 설정 안내](dashboard/WEB.md)에 따라 운영 주체·연락처·실제 데이터 처리 내용을
작성합니다. 공개 정책 문서 자체가 접근 제어를 대신하지는 않습니다.

## Gmail 동기화

직접 Gmail 연결은 읽기 전용 권한으로 받은 메일과 보낸 답장을 확인합니다.
처음에는 최근 1년의 채용 관련 대화와 기존 지원 기록에 연결된 대화를 확인하고,
이후에는 Gmail 변경 이력으로 바뀐 대화만 다시 읽습니다. 임시저장을 보낸 답장으로
취급하지 않으며, 일반 안내·광고를 지원보류나 탈락으로 분류하지 않도록 구분합니다.

Google에서 내려받은 OAuth 클라이언트 파일로 연결합니다. 비밀 파일은 저장소 밖에
둡니다. 자신의 서버에서 사용하는 Google 프로젝트와 Gmail API 설정이 필요합니다.

```bash
npm run mail:auth -- configure --client-file /private/path/google-client.json
npm run mail:auth -- authorize
npm run mail:auth -- verify
npm run mail:sync
```

마지막 명령은 **미리보기**이며 기존 지원 상태를 바꾸지 않습니다. 결과를 확인한 뒤
`npm run mail:sync -- --apply` 또는 대시보드의 메일 동기화를 사용하면 상태를 반영합니다.
원본 근거 문장과 메일 링크를 세부 내역에서 확인할 수 있습니다. 모호한 연결은
확인 필요로 남기고, 오래된 메일로 최근 수동 결정을 되돌리지 않습니다.
지원 후 기업의 답변을 기다리는 건은 지원현황에 유지하며, 불합격 결과도 지원현황에
기록합니다. 실제 철회가 확인된 건만 지원제외로 옮깁니다.

정기 V2 실행에도 연결하려면 인증을 마친 뒤 로컬 `config/runtime.json`의
`enable_dashboard_mail_sync`를 `true`로 지정합니다. 운영 반영 조건을 충족한 실행은
메일 동기화 → 상태 반영 → 공고 수집·추천 순서로 처리합니다. 수동 실행과 같은 잠금을
사용하므로 중복으로 동기화하지 않습니다. 인증·시간 측정·중단 복구 방법은
[Gmail 연결 안내](dashboard/backend/mail-sync/GMAIL_SETUP.md)에 설명되어 있습니다.
메일 캐시나 미리보기의 진행 지점을 운영 데이터에 복사하지 않습니다.

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

## 추천 순위와 표시 개수

`config/runtime.json`의 `max_candidates`로 추천 개수를 정합니다. 기본값과 상한은
5건입니다. 활성 상태·기존 지원 이력·재추천 유예 검사를 통과한 후보가 부족하면
그 수만 표시합니다. 결과 JSON 크기는 진단용으로 측정하며, 예전 설정의
`max_compact_payload_bytes`는 추천을 줄이는 기준으로 사용하지 않습니다.

순위는 원문 해시 대조를 통과한 `config/profile_evidence.json`의 경력 근거와
공고 제목·확보된 설명에 나타난 관련 표현을 먼저 비교합니다. 근거 하나당 4점,
최대 4개 근거를 점수에 반영하며 같은 표현의 반복은 추가 점수를 주지 않습니다.
`config/linkedin_queries.json`의 `profile_fit`에 선호·시니어·입문 직급 표현을
설정할 수 있습니다. 각각 +3점, +1점, −24점을 반영합니다. 신입·경력 등
복수 직급을 명시한 공고는 입문 전용 감점 대신 직급 확인 대상으로 표시합니다.
개인별 경력이나 직급 선호는 코드에 넣지 않습니다.

경력 관련 점수, 직급 확인 필요 여부, 지원 판단 근거와 기존 평가 순으로 비교하고
실제 게시일은 그 뒤의 보조 기준으로 사용합니다. 최초 수집일을 게시일로 대체하지
않습니다. 출처별 할당이나 기존 평가 공고의 별도 4건 상한 없이, 신규·기존 후보를
같은 기준으로 비교한 뒤 `max_liveness_checks` 내에서 활성 여부를 확인합니다.
근거 파일이 바뀌어 검증되지 않으면 경력 근거 가점을 주지 않고 확인이
필요하다고 표시합니다. 이 점수는 관련 표현에 대한 정렬 기준이며, 전체 직무기술서의
자격요건 충족이나 요구 연차를 판정하는 정밀 평가 점수가 아닙니다.

Slack의 `root_max_chars`는 설명과 지원 현황 미리보기를 줄이는 목표 길이입니다.
공고 링크를 빼서 길이를 맞추지 않습니다. 선택된 공고와 표시된 링크가 일치하는지
전송 전에 검사하고, 전송 가능한 메시지 길이를 넘으면 누락 전송 대신 중단합니다.

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
- 메일 동기화는 위의 직접 Gmail 연결을 사용할 수 있습니다. 기존 Codex CLI
  연결도 선택할 수 있으며, 직접 인증 파일이 있는 경우 그 연결을 우선 사용합니다.
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
- `dashboard/`: 화면, 로그인 서버, 읽기 전용 Gmail 동기화와 설치 안내

원본의 CV/PDF 생성 등 V2 실행에 필요하지 않은 부가 기능은 포함하지 않습니다.
테스트 소스·테스트 데이터·자체 테스트 모드·데모도 배포하지 않습니다.
이력서, 프로필, 지원 이력, 메일, 인증정보, 실행 결과는 Git에서 제외합니다.
공개 전에는 코드 본문과 커밋 작성자·메시지를 함께 확인해야 합니다.
