# Career Ops V2 Improvements

[Career-Ops 원본](https://github.com/santifer/career-ops)을 별도로 설치해 연결하는
V2 자동화 확장 코드입니다. 원본 전체 코드, 원본 문서·템플릿·이미지·폰트,
테스트 코드·데이터·데모는 이 저장소에 포함하지 않습니다.

원본 설치와 라이선스는 [원본 README](https://github.com/santifer/career-ops#readme)와
[원본 LICENSE](https://github.com/santifer/career-ops/blob/main/LICENSE)를 참고하세요.
이 저장소는 원본의 공식 배포판이나 원본을 포함한 독립 실행 패키지가 아닙니다.

## 포함된 확장 기능

- 수집 결과와 기존 지원 이력을 병합하고, 중복·마감 여부를 확인합니다.
- LinkedIn 공개 공고를 제한된 범위에서 확인하고, 한국 채용 사이트의 활성 신호를 판정합니다.
- 메일의 지원 상태 변경 근거를 검토하고, 불확실한 항목은 검토 대기열에 남깁니다.
- 게시일·최초 발견일을 반영하고, 전송이 확인된 추천의 재전송을 일정 기간 보류합니다.
- Slack에 후보 목록, 상태 변경, 수집 결과를 나누어 보고합니다.

이 기능들은 별도로 준비한 원본 수집기, 로컬 설정 및 외부 연동에 의존합니다.
원본 수집기에 적용했던 수정 파일은 동봉하지 않으며, 원본의 모든 버전과
동작이 같거나 바로 호환된다고 보장하지 않습니다.

## 파일 구성

- `entrypoint.py`: 운영 설정을 확인한 뒤 실행하는 진입점
- `src/`: V2 실행 흐름, LinkedIn 연동, 공고 활성 판정, Slack 보고
- `legacy/career_ops_daily_scan.py`: 별도 설치된 수집기를 호출하는 호환 어댑터
- `config/*.example.json`: 개인 값이 없는 설정 양식
- `.github/workflows/no-user-data.yml`: 허용한 배포 파일 외의 유입을 막는 공개 범위 점검

## 설치와 연결

Python 3.11 이상과 Node.js가 필요합니다. 원본의 의존성과 브라우저 설치는
원본 문서를 따릅니다. 원본은 **이 저장소 바깥의 별도 폴더**에 설치하세요.
이 저장소 안에 원본을 복사하거나 하위 모듈로 추가하지 않습니다.

1. [원본 설치 안내](https://github.com/santifer/career-ops#readme)에 따라 원본을 준비합니다.
   수집할 사이트, 프로필, 이력서, 지원 이력은 원본의 로컬 환경에서 설정합니다.
2. `config/runtime.example.json`을 `config/runtime.json`으로 복사합니다.
   `production_project_root`에는 별도로 설치한 원본 폴더의 절대 경로를 입력합니다.
   `node_bin`, `codex_bin`, `production_hermes_home` 등도 실제 환경에 맞게 입력합니다.
   `legacy_prerun_script`는 이 저장소의 `legacy/career_ops_daily_scan.py`를 가리킵니다.
3. `config/linkedin_queries.example.json`을 `config/linkedin_queries.json`으로 복사합니다.
   `queries`에는 `name`, `keywords`, `location`이 있는 검색 항목을 입력하고,
   제목·지역·우선순위 조건도 직접 설정합니다. 빈 양식으로는 수집을 시작하지 않습니다.
4. `config/profile_evidence.example.json`을 `config/profile_evidence.json`으로 복사하고
   실제 자료로 확인한 경력 근거만 입력합니다.
5. 외부 원본의 `scan.mjs`, `check-liveness.mjs`, `tracker.mjs`, `set-status.mjs`,
   `verify-pipeline.mjs` 등 호출 대상과 의존성이 준비되어 있는지 확인합니다.
   LinkedIn 입력 연동은 `scan.mjs`의 필터·중복·추가 API를 사용하므로, 설치한
   원본 버전의 내보내기 함수와 호환 여부를 확인해야 합니다.
   `identity_resolver_script`와 기존 검색 보조 스크립트도 별도 준비 대상입니다.
   준비되지 않은 연동은 해당 기능을 사용할 수 없습니다.

V2 실행기는 설정의 원본 경로를 하위 명령에 전달합니다. 어댑터나 LinkedIn 명령을
직접 실행한다면 `CAREER_OPS_PROJECT_ROOT` 또는 지원되는 `--project-root` 옵션으로
외부 원본 경로를 명시합니다. 설정 없이 임의의 기존 운영 폴더를 선택하지 않습니다.

처음에는 `activation_mode: "dry-run"`과 `slack_delivery.enabled: false`를 유지하고
다음처럼 네트워크 접근 없이 설정을 확인합니다.

```bash
python3 src/career_ops_daily_v2.py --mode dry-run --skip-network
```

이 명령은 테스트 데이터나 가상 공고를 만들지 않지만, 로컬 `artifacts/`에 실행 결과를
저장할 수 있습니다. 실제 수집, 상태 변경과 Slack 전송은 연동 호환성과 설정을
확인한 뒤 별도로 활성화하세요.

## 공개 저장소의 범위

개인 경력·구직 선호·이력서·메일·지원 이력·Slack 채널 정보·인증정보·실행 결과는
공개 코드에 넣지 않습니다. 실제 설정 파일과 결과물은 Git에서 제외됩니다.
테스트 파일, 테스트 실행 모드, 가상 데이터와 데모 영상도 배포하지 않습니다.
PR의 파일 범위 점검은 개인정보 내용 전체를 검증하는 도구가 아니므로,
공개 전에는 변경 본문과 커밋 작성자·메시지도 별도로 확인해야 합니다.
