# llm-wiki

`llm-wiki`는 저장소 옆에 Markdown 기반 작업 기억을 둔다. 세션 기록과 검토 전 후보를 분리하고, 코드에 없는 맥락만 짧게 남긴다. 위키를 git 중앙 저장소의 clone으로 두면 여러 머신이 `sync`로 같은 기록을 나눠 쓴다.

위키는 증거가 아니다. 지시문, `AGENTS.md`, 현재 소스 코드·테스트·검증 결과가 위키보다 앞선다. 비밀값, 토큰, 쿠키, 비밀번호는 기록하지 않는다.

## 설치

```sh
uv tool install --reinstall git+ssh://git@github.com/chlee1001/llm-wiki-cli.git
# 로컬 clone에서 고친 소스를 바로 쓰려면
uv tool install --reinstall --editable /path/to/llm-wiki-cli
# 또는
pipx install /path/to/llm-wiki-cli
```

Python 3.11 이상이 필요하다. GitHub 저장소가 private이므로 GitHub 주소로 설치할 때는 그 저장소를 읽을 수 있는 SSH 키가 있어야 한다.

## 에이전트에게 워크스페이스 설정 맡기기

다른 워크스페이스의 에이전트 세션에 이 저장소의 경로나 GitHub 주소를 주고 "이 워크스페이스에 llm-wiki를 설정해 줘"라고 하면, 에이전트는 이 절을 따른다. 사용자가 설정을 요청한 경우에만 진행한다. 결정이 필요한 값(중앙 저장소 주소, 분류)은 추측하지 않고 묻는다. `WS`는 설정할 워크스페이스의 절대 경로다.

1. 이미 설정됐는지 본다. `WS`에서 `llm-wiki status --json`이 성공하고 `workspace`가 `WS`이며 `managed`가 모두 `current`이면 할 일이 없다. 그 결과만 보고하고 끝낸다.
2. CLI를 확인한다. `llm-wiki init --help`에 `--wiki`가 없거나 `llm-wiki`가 없으면 위 설치 절의 명령으로 설치한다. 받은 것이 로컬 경로면 `--editable` 명령을, GitHub 주소면 `git+ssh` 명령을 쓴다. 설치 뒤 `llm-wiki init --help`에 `--wiki`가 있는지 다시 확인한다.
3. 중앙 위키 clone을 정한다. `~/.config/llm-wiki/roots.json`(`XDG_CONFIG_HOME`이 있으면 그 아래)에 이미 있는 `wiki` 경로가 하나면 그것을 쓴다. 여럿이면 사용자에게 고르게 한다. 없으면 사용자에게 중앙 저장소 주소를 받아 `~/wiki/<저장소 이름>`에 clone하고 `llm-wiki sync --pull-only --root <clone> --json`을 실행한다. 빈 저장소라면 `init --central`이 필요하다는 것을 알리고 사용자가 정하게 한다.
4. 프로젝트 이름과 분류를 정한다. 이름은 사용자가 주지 않았으면 `WS`의 디렉터리 이름을 쓴다. clone의 `wiki.toml`에 같은 이름이 이미 있으면 다른 워크스페이스가 쓰는 프로젝트일 수 있으니 이어 쓸지 사용자에게 확인한다. 분류(`company`, `personal` 등)는 사용자에게 묻는다.
5. 연결한다.

```sh
llm-wiki init --path WS --wiki CLONE --project NAME --category CATEGORY --json
```

6. 결과의 `next`가 `null`이 아니면 그 명령(`llm-wiki sync --json`)을 `WS`에서 실행해 새 프로젝트를 중앙 저장소에 올린다. `secret_detected`, `conflict` 같은 오류가 나면 멈추고 `recovery`를 보여 준다.
7. 확인한다. `WS` 아래에서 `llm-wiki status --json`의 `current_project`가 `NAME`이고 `managed`가 모두 `current`인지, `llm-wiki brief`가 출력되는지 본다.
8. 보고한다. 만들거나 고친 파일(`created`, `updated`), `roots.json`에서 바뀐 매핑(`previous`), `sync` 결과를 그대로 전한다. `AGENTS.md`가 이미 있었다면 마커 블록만 덧붙였고 나머지는 그대로라는 점, 워크스페이스에 남아 있는 로컬 `llm-wiki/` 기록은 건드리지 않았다는 점도 알린다. 워크스페이스 저장소에 지침 파일을 커밋할지는 사용자가 정하므로 커밋하지 않는다.

설정은 머신마다 따로 한다. `roots.json`은 머신 로컬 파일이라 다른 머신에서는 3~7단계를 다시 해야 한다.

## 빠른 시작

한 저장소에는 `single` 모드를 쓴다.

```sh
cd my-repo
llm-wiki init --mode single
llm-wiki status
llm-wiki wrap --topic "검색 추가" --task "검색 구현" --completed "text 검색 추가" --verification "unittest 통과"
```

여러 작은 저장소를 담은 폴더에는 `umbrella` 모드를 쓴다. 바로 아래에서 `.git` 디렉터리나 파일을 찾고 프로젝트로 등록한다. 이름이 `llm-wiki`인 디렉터리는 대소문자와 관계없이 프로젝트로 등록하지 않는다.

```sh
cd workspace
llm-wiki init --mode umbrella
cd service-a
llm-wiki search 인증
llm-wiki promote --title "확인 필요" --body "근거를 더 확인한다"
```

`add-project NAME --path REL`로 umbrella 위키에 프로젝트를 더 등록한다. `single` 모드에서는 프로젝트를 추가할 수 없다. 현재 디렉터리에서 위로 올라가며 가장 가까운 `llm-wiki/wiki.toml`을 찾는다. 프로젝트 안의 위키는 상위 umbrella 위키보다 우선한다. 디렉터리 이름은 대소문자를 구분하지 않으므로 `LLM-Wiki/`도 찾는다. 한 위치에 대소문자만 다른 디렉터리가 여럿이면 `wiki.toml`이 있는 쪽, 그다음 이름이 정확히 `llm-wiki`인 쪽을 쓴다. 다만 그중 하나라도 legacy 위키면 그 위치에서 `init`은 거부한다. 목록을 읽을 권한이 없는 디렉터리에서는 정확히 `llm-wiki`라는 이름만 확인한다. `init`이 만드는 `AGENTS.md`와 `.gjc` 규칙 블록은 실제 위키 디렉터리 이름을 가리킨다.

## 명령

| 명령 | 설명 |
| --- | --- |
| `init` | 레이아웃을 만들고 `--mode single` 또는 `--mode umbrella`를 설정한다. 새 위키에서 `--mode`를 생략하면 `single`이다. `--name`, `--path`, `--no-agents`, `--no-gjc`를 쓸 수 있다. `--central`은 중앙 저장소 clone 자체를 위키로 만든다([중앙 저장소와 여러 머신](#중앙-저장소와-여러-머신)). `--mode`, `--no-agents`, `--no-gjc`와 함께 쓰면 거부한다. `--wiki CLONE --project NAME [--category TEXT]`는 `--path` 워크스페이스를 중앙 위키에 연결한다([워크스페이스 연결](#워크스페이스-연결)). 결과의 `created`는 새로 만든 파일, `updated`는 이미 있던 관리 파일 중 마커 블록 갱신으로 바이트가 바뀐 파일이다. |
| `add-project NAME` | 프로젝트와 프로젝트 전용 디렉터리를 등록한다. `--path PATH`로 프로젝트 경로를, `--category TEXT`로 분류를 정한다. |
| `category PROJECT [VALUE]` | 프로젝트 분류를 보여 주거나 `VALUE`로 바꾼다. 빈 문자열이면 분류가 없다. |
| `status` | 현재 위키, 프로젝트별 Markdown 수와 분류(`category`), QMD 설치 여부와 관리 파일 상태(`managed`), 갱신 명령(`refresh`)을 보여 준다. |
| `brief` | 작업 시작용 요약을 출력한다. 열린 인수인계, 검토 대기 후보, 최근 세션을 기본 16384바이트 안에 담는다. `--project`, `--max-bytes N`, `--json`을 쓸 수 있다. 한도는 출력 본문(`text`)에 적용되고, `--json` 결과는 같은 내용을 항목별로도 담아 더 길다. 읽지 못한 기록과 중복 take 경고는 종류마다 5개까지만 본문에 싣고, 전체 목록은 `--json`의 `unreadable`과 `duplicate_takes`에 있다. |
| `search QUERY` | 현재 프로젝트와 공유 문서를 검색한다. `--project NAME` 또는 `-n N`으로 범위와 개수를 정하고, `--all-projects`는 전체를, `--category TEXT`는 그 분류의 프로젝트만 검색한다(공유 문서 제외). `--engine auto\|qmd\|text`로 검색 엔진을 고른다. 결과마다 문서 머리말의 `type`, `status`, `created`를 함께 보여 주고, 값이 없으면 `null`이다. |
| `qmd-sync` | QMD collection과 context를 만들고 갱신한다. `--embed`를 붙이면 임베딩까지 만든다. |
| `wrap` | 작업 종료 기록을 세션 문서와 로그에 남긴다. `--handoff`가 있으면 같은 이름의 인수인계 파일을 `handoffs/`에 따로 만들고 세션 문서와 서로 링크한다. 토큰, 비밀번호, 쿠키 값은 `[REDACTED_...]`로 가린다. `--topic`, `--task`, `--completed`는 필수이고 `--verification`, `--handoff`, `--not-tested`, `--agent`, `--project`, `--candidate`를 쓸 수 있다. |
| `promote` | 검토 전 후보를 `inbox/candidates/`에 남긴다. `wrap`과 같은 규칙으로 비밀값을 가린다. `--title`, `--body`는 필수이고 `--source`, `--project`, `--shared`를 쓸 수 있다. `--source`가 있으면 상태가 `candidate-with-evidence`, 없으면 `needs-review`이고 출처 칸에 `None recorded.`를 쓴다. |
| `take HANDOFF_ID` | 인수인계를 가져갔다고 표시한다. `--agent NAME`(기본 `gjc`), `--project`를 쓸 수 있다. 프로젝트를 정하지 못하면 `wrap`과 같이 공유 영역의 인수인계를 찾는다. |
| `review [PATH]` | 경로를 주면 후보 하나의 검토 재료(머리말, 본문, 출처, 세션 링크)를, 없으면 검토 대기 후보 목록을 출력한다. 파일은 바꾸지 않는다. |
| `accept PATH` | 후보를 `accepted`로 결정한다. `--decided-by owner`와 `--reason`이 필요하다. |
| `reject PATH` | 후보를 지우지 않고 `rejected`로 결정한다. `--decided-by NAME`과 `--reason`이 필요하다. |
| `sync` | 위키 변경을 커밋하고 중앙 저장소와 맞춘다. `--pull-only`는 받아오기만 한다. |

모든 명령에 `--json`을 붙일 수 있다. `init`을 제외한 명령에서 `--root DIR`는 위키 탐색 시작 위치를 바꾸며, `LLM_WIKI_ROOT` 환경 변수도 쓸 수 있다. 둘 다 없으면 `roots.json` 매핑, 그다음 현재 디렉터리에서 위로 찾기 순서다.

### 에이전트용 JSON 입력

`wrap`과 `promote`는 `--input-json -`로 JSON 객체 하나를 표준 입력에서 읽는다. `-` 대신 JSON 파일 경로도 쓸 수 있다. 본문에 줄바꿈, 따옴표, 한글이 있어도 셸 인자를 조합하지 않아도 된다. 한 번의 호출에는 명령 하나만 실행한다.

```sh
llm-wiki wrap --input-json - --json <<'JSON'
{"topic":"검색 점검","task":"여러 줄 본문 확인","completed":"기록 완료","candidate":["후보 하나","후보 둘"]}
JSON

llm-wiki promote --input-json - --json <<'JSON'
{"title":"확인 필요","body":"줄바꿈과 \"따옴표\"를 그대로 기록한다.","source":["src/app.py:12"]}
JSON
```

`wrap` 객체는 문자열 `topic`, `task`, `completed`를 반드시 넣고, 문자열 `verification`, `handoff`, `not_tested`, `agent`, `project`와 문자열 배열 `candidate`를 더 넣을 수 있다. `promote` 객체는 문자열 `title`, `body`를 반드시 넣고, 문자열 배열 `source`, 문자열 `project`, 불리언 `shared`를 더 넣을 수 있다. `project`와 `shared: true`는 함께 쓸 수 없다. JSON 입력과 같은 명령의 본문 옵션(`--topic`, `--body`, `--candidate` 등)은 섞을 수 없다.

성공 결과는 기존처럼 표준 출력의 JSON이다. `--json`을 명시하면 인수·JSON 입력 오류도 표준 출력의 `{"error":"..."}` JSON으로 받고 종료 코드 `2`를 쓴다. `--json` 없이 오류가 나면 표준 오류에 출력한다. JSON이 객체가 아니거나, 문법이 틀리거나, 중복·알 수 없는 키가 있거나, 필드 타입이나 필수 필드가 맞지 않으면 위키를 쓰기 전에 거부한다. 오류 메시지는 입력 본문이나 비밀값을 되풀이하지 않는다.

`wrap`의 `candidate`로 만든 후보에는 머리말 `session:`에 원본 세션 기록의 위키 내 경로가 붙는다. 이 링크는 후보가 어느 세션에서 나왔는지만 알려 줄 뿐 내용을 검증했다는 뜻이 아니다. 출처는 `None recorded.`, 상태는 `needs-review`로 남는다. 근거를 붙이려면 확인한 위치를 `promote`의 `source`로 넘겨 새 후보를 만든다.

`wrap`과 `promote`에서 현재 디렉터리로 프로젝트를 찾지 못하면 공유 영역 `shared/`에 쓴다. `promote --shared`도 공유 영역을 명시한다. 세션 폴더(`sessions/YYYY/MM`), 파일 이름의 시각, `created`, `log.md`는 모두 로컬 시간을 쓰며 `created`에는 시간대 오프셋이 붙는다.

비밀값 가리기는 정규식 기반 휴리스틱이다. `password=값`, `"token": "값"`처럼 키 뒤에 오는 값은 한 줄 안에서 가리고, 따옴표 안의 백슬래시는 이스케이프로 본다. 그래서 `'abc\'`처럼 백슬래시로 끝나는 값은 줄 끝까지 가린다. 닫히지 않은 따옴표도 줄 끝까지 가리므로 `"PASSWORD='x'" -e "USER=bob"`처럼 따옴표가 겹치면 뒤쪽 내용까지 가려질 수 있다. 구분자는 `=`, `:`, `:=`, `=>`를 인식하고, `SECRET_KEY`나 `client_secret`처럼 키 이름에 앞뒤 글자가 붙어도 비밀 키로 본다. 이 때문에 `password_hint`, `secretary` 같은 키의 값도 가려진다. `<password>값</password>` 같은 태그, 줄 중간의 `Authorization: Basic`과 `Cookie:` 헤더도 가린다. `Cookie` 헤더는 `curl -H "Cookie: ..."`처럼 따옴표 인자 안에 있으면 닫는 따옴표까지 가린다. 다만 닫는 따옴표 바로 뒤가 공백, 구분 기호(`,;:)]}`), 줄 끝 중 하나가 아니면(`"Cookie: a=b"tail`처럼 글자가 붙으면) 줄 끝까지 가린다. `"Cookie": "..."` 같은 JSON 키를 포함한 나머지 경우에도 줄 끝까지 가린다. `mysid=값`, `xsessionid=값`, `x-session-id = 값`처럼 이름에 `session`이 들어가거나 `sid`로 끝나는 키의 값은 `=` 앞뒤에 공백이 있어도 토큰으로 가린다. 그래서 `ssid=`, `possession=` 값도 가려진다. `session = requests.Session()` 같은 코드 줄도 가려지므로, 이런 줄을 직접 적은 문서는 sync가 `secret_detected`로 멈춘다. 값에 섞인 짝 없는 따옴표는 줄 끝까지 가린다. 다만 따옴표 뒤에 남은 줄이 256자보다 짧고 따옴표 바로 뒤에 공백이나 구분 기호가 오면, `password=x",y next`의 `x",y`처럼 그다음 공백까지만 가린다. 이때도 `password=x",y'z next`처럼 그 공백 전에 따옴표가 하나 더 나오면 줄 끝까지 가린다. 그 밖에 `redis://:pw@host`처럼 사용자 이름이 빈 URL 인증 정보도 가린다. `-----BEGIN ... PRIVATE KEY-----` 블록만 여러 줄을 가리고, 키·값, 태그, 인증 헤더(`Bearer`, `Authorization: Basic`, `Cookie`) 가리기는 줄 단위다. 키 뒤 값이 다음 줄에서 시작하거나(`password:` 다음 줄, YAML 블록 `password: |`) 태그 값이 여러 줄이면 가리지 못하므로 기록 전에 직접 지운다. CRLF 줄 끝은 LF와 같게 보고, 줄 중간의 CR 하나는 공백으로 본다. `wrap --candidate` 문자열은 한 줄로 합친 제목과 원래 줄을 지킨 본문에 함께 들어가므로, 키와 값이 줄바꿈으로 갈라져 있으면 제목에서만 가려지고 본문에는 값이 남는다. `--agent`, `--decided-by`, `--reason`처럼 한 줄로 저장하는 값은 줄을 먼저 합치고 제어 문자를 공백으로 바꾼 뒤 가린다.

## 중앙 저장소와 여러 머신

owner의 GitHub private 저장소 하나(`my-llm-wiki`)를 위키로 쓴다. 회사 프로젝트와 개인 프로젝트는 같은 위키에 두고 `category`(`company`, `personal`)로 나눈다. 저장소 루트가 곧 위키 디렉터리이고 `wiki.toml`이 루트에 있다. 접근은 이 저장소에 등록한 deploy key로 한다. 저장소 생성과 키 등록은 owner가 직접 한다. 이 도구는 GitHub 설정을 바꾸지 않는다.

첫 머신에서는 빈 저장소를 clone한 뒤 위키로 만들고 올린다.

```sh
git clone git@github-llm-wiki:OWNER/my-llm-wiki.git ~/wiki/my-llm-wiki
llm-wiki init --central --path ~/wiki/my-llm-wiki --name my-llm-wiki
llm-wiki add-project api --category company --root ~/wiki/my-llm-wiki
llm-wiki sync --root ~/wiki/my-llm-wiki
```

`init --central`은 `.git`만 있는 빈 clone이나 이미 `wiki.toml`이 있는 clone에서만 동작한다. `wiki.toml`, `index.md`, `log.md`, `shared/`, `.gitattributes`(`log.md merge=union`), `.gitignore`(`.llm-wiki-tmp/`)만 만들고 `AGENTS.md`나 `.gjc` 파일은 만들지 않는다. 프로젝트 안의 `llm-wiki/` 디렉터리가 git 저장소일 때 거부하는 기존 규칙은 그대로다.

다른 머신에서는 clone한 뒤 받아오기만 하면 된다. `wiki.toml`이 아직 없는 새 clone에서도 동작한다.

```sh
git clone git@github-llm-wiki:OWNER/my-llm-wiki.git ~/wiki/my-llm-wiki
llm-wiki sync --pull-only --root ~/wiki/my-llm-wiki
```

작업 디렉터리와 위키를 잇는 매핑은 `$XDG_CONFIG_HOME/llm-wiki/roots.json`(없으면 `~/.config/llm-wiki/roots.json`)에 둔다. 머신마다 경로가 다르므로 이 파일은 저장소에 넣지 않는다.

```json
{"version": 1, "roots": {"/Users/me/work/api": {"wiki": "/Users/me/wiki/my-llm-wiki", "project": "api"}}}
```

경로는 절대 경로나 `~`로 시작해야 한다. 작업 디렉터리 아래 어디에서 실행해도 가장 긴 접두 경로가 고른 매핑을 쓴다. 등록하지 않은 `project`를 가리키면 `add-project`를 먼저 실행하라는 오류를 낸다.

### 워크스페이스 연결

새 워크스페이스는 명령 하나로 중앙 위키에 연결한다. 머신마다 워크스페이스마다 한 번 실행한다.

```sh
llm-wiki init --path ~/work/api --wiki ~/wiki/my-llm-wiki --project api --category company
llm-wiki sync --root ~/wiki/my-llm-wiki
```

`init --path DIR --wiki CLONE --project NAME`은 다음을 한다. 쓰기 전에 clone의 `wiki.toml`, 프로젝트 이름, 분류, 워크스페이스의 지침 파일 마커를 모두 확인하고, 하나라도 문제가 있으면 아무것도 쓰지 않는다.

1. `NAME`이 clone에 없으면 `add-project`처럼 등록한다. `--category`를 주면 새 프로젝트에 붙이고, 이미 있는 프로젝트라면 그 값으로 바꾼다.
2. `roots.json`에 `DIR` → (`CLONE`, `NAME`) 항목을 쓴다. 다른 항목은 그대로 두고, `DIR`에 이미 다른 매핑이 있으면 바꾼 뒤 이전 값을 결과의 `previous`로 알려 준다.
3. `DIR`에 `AGENTS.md`, `.gjc/rules/llm-wiki.md`, `.gjc/skills/llm-wiki-wrap/SKILL.md`를 만들거나 마커 블록만 갱신한다. 로컬 `llm-wiki/` 위키는 만들지 않고, 남아 있는 로컬 `llm-wiki/`도 건드리지 않는다. `--no-agents`, `--no-gjc`로 묶음을 건너뛸 수 있다.

1번에서 clone이 바뀌면 결과의 `next`가 `llm-wiki sync --json`이다. 다른 머신이 새 프로젝트를 보려면 이 `sync`가 필요하다. 이미 연결된 워크스페이스에서 같은 명령을 다시 실행하면 아무것도 바뀌지 않는다. `--wiki`는 `--central`, `--mode`, `--name`과 함께 쓸 수 없다.

지침은 위키 경로 대신 `roots.json` 매핑을 가리키므로 머신마다 clone 경로가 달라도 같은 내용이 된다. 에이전트에게는 설정을 직접 하지 말라고 안내한다. `status --json`이 `No llm-wiki found`나 `roots.json` 오류를 내면 사용자에게 알리고 위키 없이 작업을 이어 가며, `init`·`add-project`를 실행하거나 `roots.json`을 고치지 않는다.

이미 매핑한 디렉터리에서는 `init --path DIR`만으로 지침을 갱신한다. 이때 `--mode`와 `--name`은 거부한다. 매핑한 디렉터리 아래에서 `status --json`을 실행하면 `root`는 clone 경로, `workspace`는 매핑한 디렉터리이고, `managed`는 그 작업 디렉터리의 지침 파일 세 개를 비교한다.

```sh
llm-wiki init --path ~/work/api
```

여러 저장소를 담은 상위 폴더라면 상위 폴더와 하위 저장소를 각각 연결한다. 가장 긴 접두 경로가 이기므로 하위 저장소 안에서는 그 프로젝트로 기록된다. 지침은 에이전트가 읽는 위치에 한 벌만 두면 되므로 하위 저장소에는 `--no-agents --no-gjc`를 붙여도 된다.

`sync`는 다음 순서로 동작한다.

1. 작업 트리, 아직 push하지 않은 커밋, 스테이지한 인덱스를 차례로 비밀값 검사한다. 하나라도 걸리면 커밋과 push 없이 `secret_detected`로 멈춘다. 기준은 `wrap`·`promote`와 같은 가리기 규칙이 파일 내용을 바꾸는지다. 이미 `[REDACTED_...]`로 가린 값은 다시 걸리지 않지만, 자리표시자에 글자가 바로 붙어 있으면(`[REDACTED_SECRET]hunter2`) 비밀값으로 본다. 따옴표 값을 가린 자리 뒤에 구분 기호로 이어진 내용(`password="x",hunter2`의 `,hunter2`)은 가리기 규칙과 같이 남긴다. 이미 로컬 커밋에 들어간 값은 owner가 이력을 직접 정리해야 한다. 이 도구는 이력을 다시 쓰지 않고 검사를 건너뛰는 옵션도 없다.
2. 위키 안 변경 전부를 커밋 하나로 묶는다. 메시지는 `llm-wiki sync: <host> <ISO 시각>`이다. 변경이 없으면 커밋을 건너뛴다. git identity가 없으면 `no_identity`로 멈춘다.
3. `pull --rebase` 뒤 push한다. 충돌은 자동으로 풀지 않는다. `conflict` 오류에 충돌 파일 목록과 복구 방법(`recovery`)이 담긴다. 충돌을 풀고 `git rebase --continue`나 `git rebase --abort`를 마칠 때까지 `sync`와 기록 명령(`wrap`, `promote`, `take`, `accept`, `reject`)은 거부된다.

오류는 `--json`에서 `{"error": "<메시지>", "code": "<코드>", ...}` 형태이고 종료 코드는 1이다. 코드에 따라 `conflicts`, `paths`, `recovery` 같은 필드가 더 붙는다. 네트워크가 없으면 `sync --pull-only`는 `status: offline`을 돌려준다. 위키가 중앙 저장소 clone이 아니면 `guard` 오류(`reason`이 `toplevel` 또는 `no_remote`)가 나므로 그런 위키에서는 `sync`를 쓰지 않는다. 충돌을 풀지 않은 채 다시 실행해도 `guard` 오류(`reason: rebase_in_progress`)가 난다. `reason`이 `missing_directory`(위키 경로가 없음)나 `no_git`(git 미설치)이면 설정이나 설치 문제이니 고친 뒤 다시 실행한다. git의 자격 증명 프롬프트와 편집기는 끄고, 모든 git 호출은 120초가 지나면 `git_timeout`으로 끊는다. ssh가 키 암호를 물으면 그 시간 동안 기다리므로 deploy key는 암호 없이 쓰거나 ssh-agent에 올려 둔다. 커밋은 `--no-verify`로 `pre-commit`·`commit-msg` 훅을 건너뛰고 `commit.gpgsign=false`로 서명을 끈다. push는 `--no-verify`로 `pre-push` 훅만 건너뛴다. 그 밖의 훅(`prepare-commit-msg`, `post-commit` 등)과 `pull --rebase`가 다시 만드는 커밋의 서명, `push.gpgSign` 같은 설정은 사용자 git 설정을 따른다.

## 인수인계

`wrap --handoff`는 세션 문서의 `## Handoff` 절을 그대로 두고 `handoffs/<세션 파일 이름>`에 인수인계 파일을 하나 더 만든다. 대응하는 `.taken.*` 표시 파일이 없으면 열린 인수인계다. `take ID --agent NAME`은 기존 파일을 고치지 않고 `handoffs/<ID>.taken.<agent>.<host>` 파일을 새로 만든다. 이미 받아 온 표시 파일에 같은 agent가 있으면 어느 머신에서 만든 것이든 다시 가져가기를 거부한다. 프로젝트 밖에서 쓴 인수인계는 `shared/handoffs/`에 생긴다. `take`는 현재 프로젝트의 `handoffs/`에서 ID를 먼저 찾고, 없으면 `shared/handoffs/`에서 찾는다.

이 보장은 약하다. 두 머신이 서로 `sync`하기 전에 같은 인수인계를 가져가면 git으로는 막을 수 없다. 표시 파일이 둘 생기고 `brief`가 `duplicate take`로 경고한다.

`brief`는 먼저 한도의 절반을 열린 인수인계에, 3/16씩을 후보와 최근 세션에 나눠 최신순으로 채운다. 남은 자리는 아직 생략이 있는 섹션에 인수인계, 후보, 세션 순으로 준다. 항목은 통째로만 빼고 섹션마다 생략 개수를 적는다. 위키가 git clone이면 `.git/FETCH_HEAD` 시각을 보고 한 번도 받아오지 않았거나 24시간이 지났을 때 경고한다.

## 후보 상태

후보는 `inbox/candidates/`에 남고 상태만 바뀐다. `knowledge/`로 옮기거나 복사하지 않는다.

| 상태 | 뜻 |
| --- | --- |
| `needs-review` | 출처 없이 만든 검토 전 후보 |
| `candidate-with-evidence` | `--source`로 출처를 붙인 검토 전 후보 |
| `accepted` | owner가 받아들였다. `accept --decided-by owner`로만 바뀐다 |
| `rejected` | 받아들이지 않았다. 파일은 지우지 않는다 |

결정하면 머리말의 `status` 줄이 바뀌고 `decided_by`, `decided_at`, `reason`이 붙는다. `decided_by`와 `reason`은 한 줄짜리 JSON 문자열이고 `decided_at`은 `created`처럼 따옴표 없는 ISO 시각이다. 결정자와 사유도 비밀값을 가린다. 닫는 `---` 아래 본문은 바이트 단위로 그대로다. 이미 결정한 후보는 `이미 결정된 후보입니다: <path> (<status>)`로 거부한다. 위키 안의 `*/inbox/candidates/*.md` 실제 파일만 받으며 심볼릭 링크나 위키 밖 경로는 거부한다. 결정은 `log.md`에도 남는다.

## 기록 불변 규칙

세션 기록과 인수인계 파일은 만든 뒤 고치지 않는다. 새 파일은 같은 이름이 이미 있으면 실패하는 방식(`O_EXCL`에 해당하는 하드 링크 생성)으로 만들어 두 프로세스가 서로 덮어쓰지 않는다. 예외는 둘이다. `log.md`는 끝에 이어 쓰기만 하고 git에서는 `merge=union`으로 합친다. 후보는 `accept`나 `reject`가 머리말만 원자적으로 바꾼다. 같은 후보를 두 머신에서 동시에 결정하지 않도록 정리는 한 번에 한 머신에서 한다.

## 프로젝트 분류

`wiki.toml`의 프로젝트마다 `category`를 둔다. 새 프로젝트는 빈 값이고, 이 키가 없는 예전 `wiki.toml`도 그대로 읽는다. 폴더는 분류와 관계없이 `projects/<이름>/` 한 단계로 평평하다. 분류 값은 64자 이하이고 제어 문자를 넣을 수 없다.

```sh
llm-wiki category api backend
llm-wiki search 캐시 --category backend
```

## owner 작업: 기록 이전과 실제 왕복 확인

아래는 owner가 직접 할 일이다. 도구나 에이전트가 대신 실행하지 않는다.

1. GitHub에 private 저장소 `my-llm-wiki`를 만든다. 공개 저장소로 만들었다면 아무것도 올리기 전에 private으로 바꾼다.
2. 머신(M3, VM)마다 deploy key를 만들어 쓰기 권한으로 등록하고, `~/.ssh/config`에 호스트 별칭(`github-llm-wiki`)과 `IdentityFile`을 적는다. 한 저장소에 같은 deploy key를 두 번 등록할 수 없으므로 머신마다 키를 따로 만든다.
3. 위 순서대로 첫 머신에서 `init --central`과 `add-project`, `sync`를 실행하고 다른 머신에서 `sync --pull-only`를 실행한다. 두 머신에 `roots.json`을 둔다.
4. 이 저장소 안의 `llm-wiki/` 기록을 `my-llm-wiki`로 옮긴다. `my-llm-wiki`에서 `add-project llm-wiki-cli --category personal`을 실행한 뒤 `llm-wiki/projects/llm-wiki-cli/`의 `sessions/`, `inbox/`, `knowledge/` 파일을 같은 경로로 복사한다(`cp -Rpn`). 공유 영역 파일도 `shared/`로 복사한다. 원래 `log.md` 항목은 옮기지 않는다. 복사한 뒤 원본과 사본의 파일 수와 `shasum -a 256` 값을 비교하고, `sync`가 성공하고 다른 머신의 `sync --pull-only`에서도 같은 파일이 보이는지 확인한다. 그다음에만 원래 `llm-wiki/`를 지우고, `llm-wiki init --path <작업 디렉터리> --wiki <clone> --project llm-wiki-cli`로 이 저장소 작업 디렉터리를 연결한다.
5. 왕복 확인: M3에서 `wrap --handoff` 후 `sync`, VM에서 `sync --pull-only`와 `brief`로 인수인계가 보이는지, `take` 후 `sync`, M3에서 `sync --pull-only` 뒤 그 인수인계가 열린 목록에서 빠졌는지 확인한다. VM에서도 `wrap` 후 `sync`해 양쪽 `log.md`에 두 머신의 항목이 모두 남는지 본다.
6. 중단 확인: 비밀값이 든 파일을 하나 만들어 `sync`가 `secret_detected`로 멈추고 원격이 그대로인지 확인한 뒤 그 파일을 지운다.

## QMD는 선택 사항

QMD를 설치했다면 다음 명령으로 프로젝트별 collection을 만든다. collection 이름에는 위키 경로 해시가 들어가므로 이름이 같은 서로 다른 위키가 충돌하지 않는다.

```sh
llm-wiki qmd-sync
llm-wiki qmd-sync --embed
```

`qmd-sync`는 collection과 context를 만든 뒤 `qmd update`를 실행하며, `--embed`를 붙이면 이어서 `qmd embed`도 실행한다. 두 명령은 이 위키만이 아니라 컴퓨터에 등록된 모든 QMD collection에 적용된다.

`search --engine auto`는 필요한 collection이 있을 때만 QMD를 쓴다. QMD가 없거나 collection 또는 호출에 문제가 있으면 text 검색으로 돌아간다. `--engine qmd`는 QMD가 없거나 준비되지 않았을 때 오류를 낸다. QMD 설치 안내는 `npm install -g @tobilu/qmd` 또는 <https://github.com/tobi/qmd>에 있다.

## 에이전트 연결

`init`은 루트 `AGENTS.md`에 `<!-- llm-wiki:begin -->` 블록을 만들거나 갱신한다. `.gjc/rules/llm-wiki.md`도 만든다. 이 규칙은 작업을 시작할 때 `sync --pull-only`, `brief`, 검색 순서로 실행하고 마칠 때 `wrap` 뒤 `sync`를 실행하라고 안내한다. 후보의 `accept`와 `reject`는 owner가 지시한 경우에만 실행하게 한다. 중앙 저장소가 아닌 위키에서는 `sync`가 `guard` 오류를 내므로 건너뛰고, 충돌이 남아 생긴 `guard`(`rebase_in_progress`)에서는 멈추고 사용자에게 알린다. `.gjc/skills/llm-wiki-wrap/SKILL.md`도 만든다. 사용자가 "세션 마무리"나 "세션 종료"라고 하면 에이전트가 이 스킬을 따라 `llm-wiki wrap`으로 세션 기록을 남기고 `llm-wiki sync`로 올린다. 충돌이나 비밀값 오류가 나면 직접 풀지 않고 사용자에게 `recovery`를 보여 준다. 이 동작은 에이전트에게 주는 지침일 뿐 자동 훅이 아니다. 에이전트가 따르지 않으면 기록이 남지 않고, `wrap`은 중복을 검사하지 않아 같은 세션을 두 번 실행하면 기록도 두 개가 된다. 기록을 저장해도 임시 파일, 컨테이너, 서버는 정리되지 않는다. 스킬은 정리를 따로 수행하고 실제로 없어졌는지 확인한 뒤에만 정리했다고 보고하라고 안내한다. 생성된 지침은 관찰한 사실과 추정·미검증 항목을 `handoff`까지 나눠 쓰고, `verification`에 실제 명령, 결과, 근거 위치를 적도록 요구한다. `index.md`의 프로젝트 목록은 `<!-- llm-wiki:projects:begin -->`와 `<!-- llm-wiki:projects:end -->` 사이만 갱신한다. `--no-agents`, `--no-gjc`로 만들지 않을 수 있다. `--no-gjc`는 규칙과 스킬을 모두 건너뛴다.

에이전트는 `index.md`를 먼저 읽고 현재 프로젝트 범위에서 검색한다. 확신이 낮은 내용은 `promote`로 후보에 둔다. `promote`는 `knowledge/`에 쓰지 않으며 `reviewed`나 `stable` 상태로 바꾸지 않는다. 세션 기록은 `created` 시점의 관찰이다. 최신 기록이라고 자동으로 맞는 것도 아니므로 현재 코드와 실행 결과로 다시 확인한다. 이전 세션 기록은 고쳐 쓰지 않는다.

## 관리 파일 갱신

`init`이 만드는 파일 네 개(`AGENTS.md`, `.gjc/rules/llm-wiki.md`, `.gjc/skills/llm-wiki-wrap/SKILL.md`, 위키 디렉터리의 `README.md`)는 CLI를 업데이트해도 저절로 바뀌지 않는다. `roots.json`으로 매핑한 작업 디렉터리는 위키 `README.md`를 뺀 세 개만 관리한다. `llm-wiki status --json`의 `managed` 목록은 파일마다 `<!-- llm-wiki:begin -->`부터 `<!-- llm-wiki:end -->`까지의 블록을 지금 설치된 CLI가 만드는 내용과 비교한 상태를 보여 준다. 패키지 버전은 비교하지 않는다. `status`는 아무 파일도 쓰지 않는다.

| 상태 | 뜻 | `init` 재실행 시 |
| --- | --- | --- |
| `current` | 블록이 현재 CLI 출력과 같다 | 그대로 둔다 |
| `stale` | 블록이 현재 CLI 출력과 다르다 | 블록만 교체한다 |
| `missing` | 파일이 없다 | 새로 만든다 |
| `unmarked` | 마커가 없다 | `AGENTS.md`만 끝에 블록을 덧붙이고 나머지는 그대로 둔다 |
| `legacy` | 마커 도입 전 CLI가 만든 위키 `README.md`와 바이트가 같다 | 마커가 있는 새 README로 교체한다 |
| `invalid` | 마커 짝이 맞지 않거나 두 쌍 이상이다 | 아무것도 쓰지 않고 실패한다 |
| `unreadable` | 읽을 수 없거나 UTF-8이 아니다 | 아무것도 쓰지 않고 실패한다 |

`stale`은 "설치된 CLI와 다르다"는 뜻일 뿐 어느 쪽이 새것인지는 알려 주지 않는다. 설치된 CLI가 파일을 만든 CLI보다 오래되었을 수 있으니 먼저 CLI를 업데이트한다. 그다음 `refresh` 필드에 나온 명령을 실행한다. `stale`·`legacy` 파일이 없거나 `invalid`·`unreadable` 파일이 있으면 `refresh`는 `null`이다. 명령에는 `missing`이나 `unmarked`만 있는 묶음을 건너뛰도록 `--no-agents`, `--no-gjc`가 붙는다.

```sh
uv tool install --reinstall /path/to/llm-wiki-cli
llm-wiki status --json   # refresh 필드의 명령을 그대로 실행한다
```

로컬 경로에서 설치할 때는 `--force`가 아니라 `--reinstall`을 쓴다. uv 0.9.5로 격리된 도구·캐시 디렉터리에서 재현해 보니, 버전을 올리지 않고 소스만 고친 패키지는 `--force`로는 캐시된 빌드가 다시 설치되어 수정이 반영되지 않았고 `--reinstall`로는 반영되었다. 다른 uv 버전에서는 결과가 다를 수 있다.

`index.md`는 관리 파일에 들어가지 않는다. `init`은 `index.md`가 없을 때만 만들고, 이미 있는 파일은 `status`와 `init` 모두 비교하거나 고치지 않는다. 프로젝트 목록 블록은 `add-project`만 갱신한다.

마커 안을 손으로 고친 내용은 갱신 때 덮어쓴다. 마커 밖 바이트(줄바꿈 형식 포함)는 그대로 둔다. 규칙과 스킬 파일의 front matter가 CLI 출력과 다르면 `front_matter: differs`로 알려 주기만 하고 고치지 않는다. 손으로 고친 legacy README는 바이트가 달라 `unmarked`로 보이며 `init`이 건드리지 않는다. 직접 고치거나 지운 뒤 `init`을 다시 실행한다. `AGENTS.md`나 GJC 파일이 `invalid`·`unreadable`이면 손으로 고치거나 `--no-agents`·`--no-gjc`로 건너뛴다. 위키 `README.md`는 건너뛸 옵션이 없으므로 고치거나 지운 뒤 다시 실행해야 한다.

이 비교 기능이 없는 예전 CLI로 만든 작업 공간도 CLI를 새로 설치한 뒤 `llm-wiki status --json`의 `refresh` 명령을 한 번 실행하면 갱신된다. 옵션 없는 `init`은 건너뛴 그룹을 다시 만들고 마커 없는 `AGENTS.md`에 블록을 덧붙이므로 갱신 용도로 쓰지 않는다. 이전 버전 CLI에는 이 안내가 없으므로 첫 갱신은 사람이 직접 실행해야 한다.

## 기존 위키

`init`을 기존 위키에서 다시 실행하면 기존 모드를 유지한다. `--mode`를 명시하고 기존 모드와 다르면 결과에 `note`가 포함된다.

legacy 위키도 디렉터리 이름을 대소문자 구분 없이 `llm-wiki`와 비교한다. 그 안에 `wiki.toml` 없이 `SCHEMA.md` 또는 `index.md`가 있으면 읽기 전용 legacy 위키다. `search`와 `status`만 쓸 수 있다. legacy 위키 자체나 그 아래, 또는 기존 `llm-wiki/wiki.toml` 디렉터리 안에서 `init`을 실행하면 거부한다. `wiki.toml` 없는 `llm-wiki` 디렉터리가 git 저장소(`.git` 있음)여도 그 위에 위키를 만들지 않는다. 쓰기 명령은 다른 위치에서 `init`을 실행하거나 `wiki.toml`을 직접 추가하라고 안내한다.

## 테스트

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests
```
