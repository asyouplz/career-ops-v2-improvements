# Career Desk 화면 개발

`web/`에는 현재 대시보드의 React 화면과 공개 안내 페이지가 들어 있습니다. 운영 데이터, 실제 메일, 로그인 비밀번호, Google 인증 정보는 포함하지 않습니다. 화면은 같은 주소의 `/api`를 호출하며 지원 기록과 메일 처리는 대시보드 서버가 담당합니다.

## 설치와 빌드

Node.js 22.13 이상과 npm이 필요합니다. 저장소 루트에서 다음을 실행합니다.

```sh
npm --prefix dashboard/web ci
npm --prefix dashboard/web run build
```

배포할 정적 파일은 **`dashboard/web/dist/client/`**에 생성됩니다. `index.html`, 자바스크립트와 스타일 파일, `about.html`, `privacy.html`, `terms.html`을 함께 제공해야 합니다. `dist/server/`는 빌드 과정의 생성물이며 Python 대시보드 서버에 배포할 화면 경로가 아닙니다. 빌드 결과와 의존성 폴더는 Git에 포함하지 않습니다.

현재 화면은 고정된 vinext 버전을 사용합니다. private Sites 플러그인이나 Cloudflare 계정 없이 빌드할 수 있습니다.

## 개발 중 사용

기본 설치·빌드와 비밀번호 설정을 마친 뒤, API 서버에서 허용할 주소를 화면 개발
서버 주소와 맞춥니다. 첫 번째 터미널에서 실행합니다.

```sh
DASHBOARD_PUBLIC_ORIGIN=http://127.0.0.1:5173 npm run dashboard
```

별도 터미널에서 화면 서버를 시작합니다.

```sh
npm --prefix dashboard/web run dev -- --port 5173 --strictPort
```

`http://127.0.0.1:5173`에 접속합니다. `/api` 요청은 `http://127.0.0.1:9121`로 전달됩니다.
화면 주소의 호스트·포트를 바꾸면 `DASHBOARD_PUBLIC_ORIGIN`도 같은 주소로 바꿔야
로그인과 상태 변경 요청이 허용됩니다. API 서버의 포트가 다르면
`dashboard/web/vite.config.ts`의 proxy 대상을 함께 변경하세요.
`npm --prefix dashboard/web start`는 빌드된 화면 미리보기이며 API 서버를 대신하지 않습니다.

```sh
npm --prefix dashboard/web run lint
npm --prefix dashboard/web run typecheck
```

## 화면에서 지원하는 동작

- 추천·지원현황·지원보류·지원제외를 각각 표시합니다. 보류·제외는 최근 5개, 출처 필터와 더보기를 제공합니다.
- 추천·지원보류·지원제외 카드는 본문을 끌거나 이동 버튼을 눌러 분류할 수 있습니다. 모바일에서는 카드를 길게 누릅니다. 링크·버튼·입력창의 기본 동작과 일반 스크롤을 유지합니다.
- 지원현황과 실제 지원 이력이 있는 철회 건은 드래그 이동에서 제외합니다. 이동을 저장한 뒤 10초 동안 실행 취소를 제공합니다. 서버의 버전 확인이 충돌한 경우 최신 기록을 표시합니다.
- 메일 동기화 진행, 최근 성공 시점, 처리 수와 오류를 표시하고 공고 세부 내역에서 메일 근거와 원본 링크를 확인합니다.

## 공개 안내 페이지를 배포하기 전에

`web/public/about.html`, `privacy.html`, `terms.html`은 **설치용 안내 서식**입니다. 실제 운영자의 개인정보나 연락처는 넣지 않았습니다. 설치 운영자는 자신의 문의처, Google 동의 화면의 앱 이름, 실제 연결 서비스, 저장 위치, 보관·삭제 방식과 시행일을 입력하고 운영 방식과 일치하는지 확인해야 합니다. 공개 안내 페이지는 로그인 없이 제공되므로 개인 지원 기록을 넣으면 안 됩니다.

안내 페이지를 수정한 후 다시 빌드해 `dist/client/` 전체를 배포하세요. Gmail 연결은 읽기 전용으로 사용하며 Google OAuth 설정은 서버 설치 안내를 따릅니다.
