# Gmail 직접 연결 설정

Gmail에서 메일을 읽고 지원 상태를 정리하는 수집 경로입니다. 메일을 보내거나 삭제하거나 읽음 상태로 바꾸지 않습니다. 첫 실행은 최근 1년의 지원 관련 메일과 기존 지원 기록에 연결된 대화를 확인합니다. 이후에는 Gmail의 변경 이력(`historyId`)을 조회해 바뀐 대화만 다시 읽습니다.

## 준비

- Python 3.10 이상과 기존 대시보드 서버·지원 기록 저장 기능이 필요합니다. 추가 Python 패키지는 없습니다.
- Google Cloud 프로젝트에서 Gmail API를 사용 설정하고 OAuth 클라이언트를 준비합니다. 개인 서버는 데스크톱 클라이언트를, 웹 화면에서 연결하려면 웹 애플리케이션 클라이언트를 사용합니다.
- 인증 파일과 메일 데이터는 저장소 및 정적 웹 배포 폴더 밖에 보관합니다. 인증 폴더는 소유자만 접근할 수 있도록 `0700`, 인증 파일은 `0600`이어야 합니다.

## 데스크톱 클라이언트로 연결

아래 명령의 파일 경로는 자신의 서버에 맞게 바꿉니다. Google에서 내려받은 클라이언트 파일을 가져옵니다. 원본은 수정하지 않습니다.

```sh
python3 mail-sync/gmail_auth.py configure \
  --client-file /private/path/google-client.json \
  --credentials /private/path/career-mail/gmail.json

python3 mail-sync/gmail_auth.py authorize \
  --credentials /private/path/career-mail/gmail.json --no-browser
```

두 번째 명령이 표시하는 Google 주소에서 계정 연결을 완료합니다. 요청 권한은 `gmail.readonly`입니다. 서버에 브라우저가 없다면 표시된 callback 포트로 SSH 로컬 포워딩을 먼저 연결한 뒤 본인 컴퓨터의 브라우저에서 Google 주소를 엽니다. 예를 들어 callback 포트가 `12345`이면 다음과 같습니다.

```sh
ssh -N -L 127.0.0.1:12345:127.0.0.1:12345 your-server
```

인증 화면은 10분간 유효합니다. 만료되면 인증 명령을 다시 실행하고 새 포트와 주소를 사용합니다. 브라우저 승인 후 도구가 반환하는 연결 상태만 확인하며, 인증 코드나 토큰을 로그 또는 이슈에 붙이지 않습니다.

기존 `authorized_user` 토큰 파일이 있으면 `configure`에 `--token-file /private/path/token.json --verify`를 추가할 수 있습니다. 클라이언트가 일치하고 Gmail 읽기 권한이 명시된 파일만 가져옵니다. 기존 승인이 철회됐거나 만료됐다면 `authorize`로 다시 연결합니다. 기존에 더 넓은 권한을 가진 승인을 가져와도 수집기는 읽기 API만 호출합니다.

## 웹 클라이언트로 연결

Google에 `https://your-dashboard.example/api/mail-connection/callback`을 승인된 리디렉션 URI로 등록합니다. 서버의 `DASHBOARD_PUBLIC_ORIGIN`도 동일한 출처로 설정해야 합니다. 이 설정과 웹 클라이언트 파일을 연결하면 로그인한 대시보드에 Gmail 연결 버튼이 표시됩니다. 연결 버튼은 로그인·CSRF 검사, 일회용 상태 값, PKCE를 사용합니다. 데스크톱 클라이언트는 위의 서버 인증 도구로 연결합니다.

## 미리보기와 운영 전환

서버 프로세스와 정기 실행에 같은 `CAREER_GMAIL_CREDENTIALS` 경로를 설정합니다. 기본 경로는 `~/.config/career-dashboard/gmail.json`입니다. `CAREER_MAIL_PROVIDER=gmail`은 직접 연결을 명시적으로 선택합니다. 기본 `auto`는 인증 파일이 없으면 기존 수집 경로를 유지하며, 인증 파일이 있지만 연결에 문제가 생긴 경우에는 오류를 표시합니다.

```sh
python3 mail-sync/gmail_auth.py verify \
  --credentials /private/path/career-mail/gmail.json

python3 mail-sync/worker.py --provider gmail \
  --gmail-credentials /private/path/career-mail/gmail.json \
  --project-root /private/path/career-data-project \
  --data-dir /private/path/career-data-project/data \
  --work-dir /private/path/career-mail-preview
```

`--apply` 없는 실행은 운영 데이터 밖의 미리보기 폴더에 결과와 캐시를 저장합니다. 미리보기 결과를 검토하고 운영 기록을 백업한 뒤 같은 명령에 `--apply`를 추가합니다. 운영 반영에서는 `--work-dir`을 사용하지 않으므로 최초 운영 실행이 자체 캐시를 만듭니다. 미리보기의 `cursor.json`을 운영 폴더로 복사하면 아직 반영하지 않은 메일을 건너뛸 수 있으므로 복사하지 않습니다.

첫 정상 반영 이후에는 `--fresh` 없이 실행합니다. `--fresh`는 전체 범위를 다시 검색하는 옵션입니다. `--reclassify-cache`는 저장된 원문을 현재 규칙으로 다시 분류하는 복구 옵션이며 평상시 실행에 추가하지 않습니다. 기존 메일 동기화 → 상태 반영 → 추천 선정 순서를 유지하고 같은 작업을 실행하는 별도 스케줄을 중복으로 만들지 않습니다.

## 소요 시간과 오류 확인

변경이 없는 실행은 계정 확인과 변경 이력 조회만 수행합니다. 실제 시간은 토큰 갱신, 새 메일 수, 대화 길이, 네트워크 응답에 따라 달라집니다. 첫 수집과 변경분 수집 시간을 구분해서 측정합니다.

```sh
python3 mail-sync/timing_report.py \
  --data-dir /private/path/career-data-project/data
```

화면에는 진행 단계·경과 시간·읽은 대화 수·마지막 정상 반영 시각이 표시됩니다. 데이터 폴더의 `mail-sync-history.jsonl`에는 실행별 요약이, 계정별 `provider-diagnostics`에는 요청별 시간·시도 횟수·오류 코드가 남습니다. 시간 보고서는 메일 본문 캐시를 열지 않습니다. 토큰은 진단 기록에 넣지 않습니다. 메일 캐시·미리보기·지원 기록은 공개 저장소에 포함하지 않습니다.

네트워크 오류와 요청 제한은 제한된 횟수만 재시도합니다. 중단 뒤 재실행하면 저장된 지점부터 이어집니다. 상태 반영에 실패하면 마지막 정상 확인 지점을 전진시키지 않습니다. Gmail 변경 이력이 만료되면 기존 기록을 보존하면서 정해진 조회 범위를 다시 검색합니다. 다른 Google 계정으로 바뀌면 기존 지원 기록과 섞이지 않도록 실행을 중단합니다.

Google OAuth 프로젝트가 외부 사용자용 테스트 상태라면 승인이 만료되어 재연결이 필요할 수 있습니다. 장기 운영은 Google의 게시 상태와 승인 조건을 확인해야 합니다. 다른 사람이 각자 설치하는 저장소 공개와 여러 사람의 Gmail을 한 서버에서 연결하는 서비스는 별개의 운영 방식입니다. 현재 저장소의 데이터 영역은 한 계정에 묶여 있으며, 공용 서비스로 제공하려면 사용자별 인증·데이터 격리가 별도로 필요합니다.

공식 문서: [Gmail 동기화](https://developers.google.com/workspace/gmail/api/guides/sync), [권한 범위](https://developers.google.com/workspace/gmail/api/auth/scopes), [OAuth 토큰 만료](https://developers.google.com/identity/protocols/oauth2#expiration).
