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

여러 작은 저장소를 담은 폴더에는 `umbrella` 모드를 쓴다. 바로 아래에서 `.git` 디렉터리나 파일을 찾고 프로젝트로 등록한다.

```sh
cd workspace
llm-wiki init --mode umbrella
cd service-a
llm-wiki search 인증
llm-wiki promote --title "확인 필요" --body "근거를 더 확인한다"
```

`add-project NAME --path REL`로 umbrella 위키에 프로젝트를 더 등록한다. `single` 모드에서는 프로젝트를 추가할 수 없다. 현재 디렉터리에서 위로 올라가며 가장 가까운 `llm-wiki/wiki.toml`을 찾는다. 프로젝트 안의 위키는 상위 umbrella 위키보다 우선한다.

## 명령

| 명령 | 설명 |
| --- | --- |
| `init` | 레이아웃을 만들고 단일/umbrella 모드를 설정한다. `--name`, `--path`를 쓸 수 있다. |
| `add-project` | 프로젝트와 프로젝트 전용 디렉터리를 등록한다. |
| `status` | 현재 위키, 프로젝트, Markdown 수, QMD 설치 여부를 보여 준다. |
| `search QUERY` | 현재 프로젝트와 공유 문서를 검색한다. `--project NAME` 또는 `-n N`으로 범위와 개수를 정하고, `--all-projects`는 전체를 검색한다. |
| `qmd-sync` | QMD collection과 context를 만들고 갱신한다. |
| `wrap` | 작업 종료 기록을 세션 문서와 로그에 남긴다. `--handoff`, `--not-tested`, `--agent`, `--project`, `--candidate`를 쓸 수 있다. |
| `promote` | 검토 전 후보를 `inbox/candidates/`에 남긴다. `--source`, `--project`, `--shared`를 쓸 수 있다. |

모든 명령에 `--json`을 붙일 수 있다. `init`을 제외한 명령에서 `--root DIR`는 위키 탐색 시작 위치를 바꾸며, `LLM_WIKI_ROOT` 환경 변수도 쓸 수 있다.

`wrap`과 `promote`에서 현재 디렉터리로 프로젝트를 찾지 못하면 공유 영역 `shared/`에 쓴다. `promote --shared`도 공유 영역을 명시한다.

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

legacy 위키 디렉터리 이름은 대소문자를 구분하지 않고 `llm-wiki`와 일치할 때 인식한다. 그 안에 `wiki.toml` 없이 `SCHEMA.md` 또는 `index.md`가 있으면 읽기 전용 legacy 위키다. `search`와 `status`만 쓸 수 있다. legacy 위키 자체나 그 아래, 또는 기존 `llm-wiki/wiki.toml` 디렉터리 안에서 `init`을 실행하면 거부한다. 쓰기 명령은 다른 위치에서 `init`을 실행하거나 `wiki.toml`을 직접 추가하라고 안내한다.

## 테스트

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests
```
