# GitHub Pages 운영 시작

이 패키지는 ChatGPT 세션·개인 PC·OpenAI API 없이 GitHub Actions의 Python으로 수집하고 GitHub Pages로 제공합니다. 저장소와 공개 범위가 아직 정해지지 않아, 이번 제작에서는 실제 업로드·배포·예약 활성화를 하지 않았습니다. 아래 버튼 조작은 운영할 GitHub 계정의 소유자가 진행합니다. 비밀키나 유료 서비스는 필요하지 않습니다.

## 1. 공개할 패키지만 올리기

1. 제공된 **배포용 패키지**를 압축 해제합니다. 과거 ZIP 전체나 개인자료·원문 PDF·개발 실행 기록은 올리지 않습니다.
2. GitHub에서 로그인한 뒤 오른쪽 위 **+ → New repository**를 누릅니다.
3. **Owner**와 **Repository name**을 정하고 공개 범위를 확인합니다. 무료 GitHub Pages의 공개 저장소 운영에 동의하는 경우 **Public**을 선택합니다. 공개 저장소의 코드·운영 JSON·커밋 이력은 누구나 읽을 수 있습니다. 공개 범위에 동의하지 않으면 이 단계에서 업로드하지 말고 원하는 범위를 먼저 정합니다.
4. **Add a README file**을 선택하고 **Create repository**를 누릅니다.
5. **Code → Add file → Upload files**에서 패키지 안의 프로젝트 파일과 폴더를 올린 뒤 **Commit changes**를 누릅니다. ZIP 파일 하나를 올리는 방식은 작동하지 않습니다. `collector`, `data`, `deployment`, `ui`, `tests`, `build.py`가 저장소 바로 아래 있어야 합니다.
6. `.github/workflows/refresh-pages.yml`이 올라갔는지 확인합니다. 숨김 폴더가 빠졌다면 **Add file → Create new file**의 파일명에 `.github/workflows/refresh-pages.yml`을 넣고, 패키지의 같은 파일 내용을 그대로 붙여넣은 뒤 **Commit changes**를 누릅니다.

워크플로 파일을 올리는 것만으로 수집이나 Pages 배포는 실행되지 않습니다. 아래 저장소 변수가 없는 기본 상태에서는 수집·배포·예약 수집이 모두 꺼져 있습니다. cron 이벤트 자체가 발생해도 예약 작업은 건너뜁니다.

## 2. Pages와 실행 권한 설정

1. 저장소 **Settings → Actions → General → Workflow permissions**에서 **Read and write permissions → Save**를 선택합니다. 조직 정책으로 변경할 수 없으면 저장소 관리자에게 이 저장소의 Actions 데이터 커밋 권한을 요청해야 합니다.
2. **Settings → Pages → Build and deployment → Source**를 **GitHub Actions**로 선택합니다.
3. **Settings → Secrets and variables → Actions → Variables → New repository variable**에서 다음 세 변수를 각각 만듭니다. **Secrets 탭이 아닌 Variables 탭**입니다.

| Name | 첫 수동 실행 Value | 의미 |
|---|---|---|
| `COLLECTION_ENABLED` | `true` | 공식 출처 수집 허용 |
| `PAGES_DEPLOY_ENABLED` | `true` | 저장된 결과의 공개 배포 허용 |
| `AUTO_REFRESH_ENABLED` | `false` | 예약 수집은 아직 비활성 |

수집만 시험하고 공개 배포를 미루려면 `PAGES_DEPLOY_ENABLED`를 `false`로 두면 됩니다. 웹사이트 주소는 공개 배포 후에 생깁니다. 계정 비밀번호·개인 액세스 토큰·OpenAI 키를 파일이나 HTML에 넣지 않습니다. GitHub가 실행마다 발급하는 `GITHUB_TOKEN`만 사용합니다.

## 3. 첫 수동 운영 실행과 확인

1. 저장소 **Actions → Refresh and publish office jobs → Run workflow**를 누릅니다.
2. **Branch**는 기본 브랜치(보통 `main`)를 선택하고 **Run workflow**를 누릅니다. 다른 브랜치는 요청을 보내기 전에 거부합니다.
3. 새 실행을 열고 `collect` 작업의 **Collect within source limits and build public files**에서 수집 결과와 실제 요청 수를 확인합니다. `success`, `partial_success`, `failed`, 수집을 껐을 때 `skipped`를 구분합니다.
4. **Code → data**에서 `notices.json`, `refresh_state.json`, `refresh_status.json`의 새 커밋을 확인합니다. 실행 임시 폴더나 캐시가 아니라 **저장소 커밋이 다음 실행의 입력**입니다.
5. `deploy`가 성공하면 작업에 표시된 **github-pages URL** 또는 **Settings → Pages → Visit site**로 접속합니다. 예상 주소 형식은 `https://사용자명.github.io/저장소명/`이며, 실제 주소는 성공한 배포 화면을 기준으로 합니다.
6. PC와 휴대폰에서 현재 목록, 검색·지역 필터, 상세, 지난 공고 페이지를 열어 봅니다. 브라우저 새로고침 후 사이트가 게시된 `data/notices.json`을 다시 읽는지와 표시된 **마지막 수집 성공시각·이번 실행 상태·수집 범위**를 확인합니다. 사이트의 화면 생성시각과 개별 원문 확인시각은 다릅니다.

`deploy` 성공만으로 공식 출처 수집 성공을 판정하면 안 됩니다. 부분·전체 수집 실패에도 이전 정상 자료와 실패 상태를 게시할 수 있도록 구현했습니다. 마지막 `report` 작업은 부분·전체 수집 실패를 **실패(빨강)**로 표시합니다. `skipped`는 기존 자료 화면 생성이며 새 원격 수집의 성공이 아닙니다. GitHub 실행 환경에서 접근 제한이 나타나면 자동 요청을 중지하고 다음 실행에도 그 상태를 보존합니다.

## 4. 수동 실행 성공 후에만 6시간 예약 켜기

1. GitHub에서 실제 수집·저장·배포 성공과 접속 화면을 확인합니다.
2. **Settings → Secrets and variables → Actions → Variables**에서 `AUTO_REFRESH_ENABLED`의 연필 버튼을 누릅니다.
3. Value를 `true`로 바꾸고 **Update variable**을 누릅니다.
4. 다음 예약 시각 이후 **Actions**에서 이벤트가 `schedule`인 실행을 열고 `collect`, 저장 커밋, `deploy`, `report` 결과를 확인합니다. **변수 설정 완료와 실제 예약 실행 성공은 별개**입니다.

기본 cron은 `17 */6 * * *`(UTC 00:17·06:17·12:17·18:17, 한국 시간 09:17·15:17·21:17·03:17)입니다. GitHub 사정에 따라 지연될 수 있어 정각 실행을 보장하지 않습니다. 1회 최대 6요청, 재시도 없음이므로 예약 실행만으로는 하루 최대 24요청입니다. 수동 실행 횟수는 여기에 추가됩니다. 요청 상한·간격은 `data/refresh_config.json`의 검증된 범위를 따릅니다. 제한된 상세 처리량 때문에 매번 모든 공고를 확인하는 것은 아닙니다.

예약을 끄려면 `AUTO_REFRESH_ENABLED=false`, 모든 출처 요청을 끄려면 `COLLECTION_ENABLED=false`, 추가 배포를 끄려면 `PAGES_DEPLOY_ENABLED=false`로 바꿉니다. 배포 변수를 끄는 것만으로 이미 게시된 사이트가 삭제되지는 않습니다.

## 저장·장애 처리

- **지속 저장:** `data/notices.json`, `data/refresh_state.json`, `data/refresh_status.json`, 필요할 때 `data/refresh_error.json`만 자동 커밋합니다. 이전 결과는 데이터 및 Git 이력에 남습니다. 첨부 원문·HTTP 응답·개발 실행 기록은 공개 커밋과 Pages에서 제외합니다.
- **Pages 공개물:** `dist/index.html`, `dist/data/notices.json`, `dist/.nojekyll`만 배포합니다. 운영 대기열과 내부 실행 기록을 사이트 데이터에 넣지 않습니다.
- **중복 실행:** Actions 전체 실행을 같은 concurrency 그룹에서 순차 처리하고 Python 파일 잠금도 적용합니다. GitHub는 대기 중 실행을 합칠 수 있으므로 각 cron 회차가 반드시 실행된다는 뜻은 아닙니다.
- **부분 실패:** 성공한 공고 갱신을 보존하고 실패한 공고의 기존 정상 자료를 유지합니다. 치명적 예외는 이전 레코드를 복원하고 실패 상태를 저장합니다. 수집 실패를 공고 0건으로 바꾸지 않습니다.
- **저장 충돌:** 수집 중 사람이 저장소를 수정해 push가 충돌하면 강제 덮어쓰기·자동 병합·재시도를 하지 않고 배포를 멈춥니다. **Actions → 실패한 실행**의 이유를 확인한 뒤 기본 브랜치에서 **Run workflow**를 새로 누르면 최신 커밋으로 다시 시작합니다.
- **빌드/저장 실패:** 저장 커밋이 성공하지 않은 새 자료는 배포하지 않습니다. 파일 읽기·쓰기에는 UTF-8을 명시합니다. 복구 중 디스크 장애까지 발생하면 기존 게시 사이트를 유지하지만, 그 실패는 사이트에 새로 게시하지 못하므로 Actions에서 확인해야 합니다.
- **결과 모니터링:** 저장된 마감 공고도 재확인 대상에 남습니다. 최근 마감·결과 미확인을 우선하고 오래된 자료는 정한 간격·상한 안에서 순환합니다. 상세 표에서 공식 결과가 식별되면 모집단위별로 갱신합니다. 첨부문서만 바뀌거나 모집단위를 안전하게 연결하지 못한 정보는 미확인으로 남을 수 있습니다.

## 코드 실행과 검사

Python 3.10 이상, 추가 pip 설치 없이 프로젝트 폴더에서 실행합니다. 운영용 수집은 원격 요청을 보냅니다.

```text
python deployment/run_ci.py --collect true
```

저장된 데이터로 공개 화면만 생성할 때는 다음을 사용합니다. 수집 요청이 없습니다.

```text
python deployment/run_ci.py --collect false
```

`dist` 폴더는 `python -m http.server 8000 --directory dist`로 로컬 확인할 수 있습니다. 브라우저에서 `http://localhost:8000/`을 열고 종료할 때 `Ctrl+C`를 누릅니다. 이 로컬 서버는 운영 요건이 아니며, GitHub Pages에 배포한 뒤에는 PC가 꺼져 있어도 웹사이트와 갱신이 동작합니다.

네트워크 없는 공개 데이터·저장 검사는 다음 명령입니다. 과거 개발 ZIP의 전체 테스트는 비공개 원문 의존성이 있으므로 배포 패키지에서는 실행하지 않습니다.

```text
python -m unittest discover -s tests -p "test_public*.py"
python -m unittest discover -s tests -p "test_workflow.py"
```

워크플로 구조 검사의 PyYAML은 선택적인 개발 검증 도구입니다. 설치되지 않으면 YAML 검사만 건너뛰고, 수집·저장·화면 생성에는 영향을 주지 않습니다. 이 제작 환경의 오프라인 검사·실제 원격 수집·저장 원문 재생 결과와, 아직 수행하지 않은 GitHub 배포·예약 실행 검증은 배포 패키지의 검증 요약에서 구분합니다.
