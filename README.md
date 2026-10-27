# llm-wiki

`llm-wiki`는 저장소 옆에 Markdown 기반 작업 기억을 둔다. 세션 기록과 검토 전 후보를 분리하고, 코드에 없는 맥락만 짧게 남긴다.

위키는 증거가 아니다. 지시문, `AGENTS.md`, 현재 소스 코드·테스트·검증 결과가 위키보다 앞선다. 비밀값, 토큰, 쿠키, 비밀번호는 기록하지 않는다.

## 설치

```sh
uv tool install /path/to/llm-wiki-cli
# 또는
pipx install /path/to/llm-wiki-cli
```

Python 3.11 이상이 필요하다.

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
| `init` | 레이아웃을 만들고 `--mode single` 또는 `--mode umbrella`를 설정한다. 새 위키에서 `--mode`를 생략하면 `single`이다. `--name`, `--path`, `--no-agents`, `--no-gjc`를 쓸 수 있다. |
| `add-project NAME` | 프로젝트와 프로젝트 전용 디렉터리를 등록한다. `--path PATH`로 프로젝트 경로를 정한다. |
| `status` | 현재 위키, 프로젝트, Markdown 수, QMD 설치 여부를 보여 준다. |
| `search QUERY` | 현재 프로젝트와 공유 문서를 검색한다. `--project NAME` 또는 `-n N`으로 범위와 개수를 정하고, `--all-projects`는 전체를 검색한다. `--engine auto\|qmd\|text`로 검색 엔진을 고른다. |
| `qmd-sync` | QMD collection과 context를 만들고 갱신한다. `--embed`를 붙이면 임베딩까지 만든다. |
| `wrap` | 작업 종료 기록을 세션 문서와 로그에 남긴다. 토큰, 비밀번호, 쿠키 값은 `[REDACTED_...]`로 가린다. `--topic`, `--task`, `--completed`는 필수이고 `--verification`, `--handoff`, `--not-tested`, `--agent`, `--project`, `--candidate`를 쓸 수 있다. |
| `promote` | 검토 전 후보를 `inbox/candidates/`에 남긴다. `wrap`과 같은 규칙으로 비밀값을 가린다. `--title`, `--body`는 필수이고 `--source`, `--project`, `--shared`를 쓸 수 있다. |

모든 명령에 `--json`을 붙일 수 있다. `init`을 제외한 명령에서 `--root DIR`는 위키 탐색 시작 위치를 바꾸며, `LLM_WIKI_ROOT` 환경 변수도 쓸 수 있다.

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

`wrap`과 `promote`에서 현재 디렉터리로 프로젝트를 찾지 못하면 공유 영역 `shared/`에 쓴다. `promote --shared`도 공유 영역을 명시한다. 세션 폴더(`sessions/YYYY/MM`), 파일 이름의 시각, `created`, `log.md`는 모두 로컬 시간을 쓰며 `created`에는 시간대 오프셋이 붙는다.

비밀값 가리기는 정규식 기반 휴리스틱이다. `password=값`, `"token": "값"`처럼 키 뒤에 오는 값은 한 줄 안에서 가리고, 따옴표 안의 백슬래시는 이스케이프로 본다. 그래서 `'abc\'`처럼 백슬래시로 끝나는 값은 줄 끝까지 가린다. 닫히지 않은 따옴표도 줄 끝까지 가리므로 `"PASSWORD='x'" -e "USER=bob"`처럼 따옴표가 겹치면 뒤쪽 내용까지 가려질 수 있다. 구분자는 `=`, `:`, `:=`, `=>`를 인식하고, `SECRET_KEY`나 `client_secret`처럼 키 이름에 앞뒤 글자가 붙어도 비밀 키로 본다. 이 때문에 `password_hint`, `secretary` 같은 키의 값도 가려진다. `<password>값</password>` 같은 태그, 줄 중간의 `Authorization: Basic`과 `Cookie:` 헤더도 가린다. `Cookie` 헤더는 `curl -H "Cookie: ..."`처럼 따옴표 인자 안에 있으면 닫는 따옴표까지 가린다. 다만 닫는 따옴표 바로 뒤가 공백, 구분 기호(`,;:)]}`), 줄 끝 중 하나가 아니면(`"Cookie: a=b"tail`처럼 글자가 붙으면) 줄 끝까지 가린다. `"Cookie": "..."` 같은 JSON 키를 포함한 나머지 경우에도 줄 끝까지 가린다. `mysid=`, `xsessionid=`, `x-session-id=`처럼 이름에 `session`이 들어가거나 `sid`로 끝나는 키의 값은 토큰으로 가린다. 그래서 `ssid=`, `possession=` 값도 가려진다. 값에 섞인 짝 없는 따옴표는 줄 끝까지 가린다. 다만 따옴표 뒤에 남은 줄이 256자보다 짧고 따옴표 바로 뒤에 공백이나 구분 기호가 오면, `password=x",y next`의 `x",y`처럼 그다음 공백까지만 가린다. 이때도 `password=x",y'z next`처럼 그 공백 전에 따옴표가 하나 더 나오면 줄 끝까지 가린다. 그 밖에 `redis://:pw@host`처럼 사용자 이름이 빈 URL 인증 정보도 가린다. YAML 블록(`password: |` 다음 줄)처럼 여러 줄에 걸친 값은 가리지 못하므로 기록 전에 직접 지운다.

## QMD는 선택 사항

QMD를 설치했다면 다음 명령으로 프로젝트별 collection을 만든다. collection 이름에는 위키 경로 해시가 들어가므로 이름이 같은 서로 다른 위키가 충돌하지 않는다.

```sh
llm-wiki qmd-sync
llm-wiki qmd-sync --embed
```

`qmd-sync`는 collection과 context를 만든 뒤 `qmd update`를 실행하며, `--embed`를 붙이면 이어서 `qmd embed`도 실행한다. 두 명령은 이 위키만이 아니라 컴퓨터에 등록된 모든 QMD collection에 적용된다.

`search --engine auto`는 필요한 collection이 있을 때만 QMD를 쓴다. QMD가 없거나 collection 또는 호출에 문제가 있으면 text 검색으로 돌아간다. `--engine qmd`는 QMD가 없거나 준비되지 않았을 때 오류를 낸다. QMD 설치 안내는 `npm install -g @tobilu/qmd` 또는 <https://github.com/tobi/qmd>에 있다.

## 에이전트 연결

`init`은 루트 `AGENTS.md`에 `<!-- llm-wiki:begin -->` 블록을 만들거나 갱신한다. `.gjc/rules/llm-wiki.md`도 만들며, 작업 시작 전 검색하고 종료 때 `wrap`을 실행하라는 규칙을 담는다. `index.md`의 프로젝트 목록은 `<!-- llm-wiki:projects:begin -->`와 `<!-- llm-wiki:projects:end -->` 사이만 갱신한다. `--no-agents`, `--no-gjc`로 만들지 않을 수 있다.

에이전트는 `index.md`를 먼저 읽고 현재 프로젝트 범위에서 검색한다. 확신이 낮은 내용은 `promote`로 후보에 둔다. `promote`는 `knowledge/`에 쓰지 않으며 `reviewed`나 `stable` 상태로 바꾸지 않는다.

## 기존 위키

`init`을 기존 위키에서 다시 실행하면 기존 모드를 유지한다. `--mode`를 명시하고 기존 모드와 다르면 결과에 `note`가 포함된다.

legacy 위키도 디렉터리 이름을 대소문자 구분 없이 `llm-wiki`와 비교한다. 그 안에 `wiki.toml` 없이 `SCHEMA.md` 또는 `index.md`가 있으면 읽기 전용 legacy 위키다. `search`와 `status`만 쓸 수 있다. legacy 위키 자체나 그 아래, 또는 기존 `llm-wiki/wiki.toml` 디렉터리 안에서 `init`을 실행하면 거부한다. `wiki.toml` 없는 `llm-wiki` 디렉터리가 git 저장소(`.git` 있음)여도 그 위에 위키를 만들지 않는다. 쓰기 명령은 다른 위치에서 `init`을 실행하거나 `wiki.toml`을 직접 추가하라고 안내한다.

## 테스트

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests
```
