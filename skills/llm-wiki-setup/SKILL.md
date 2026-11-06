---
name: llm-wiki-setup
description: "Use when the user asks to set up, connect, or fix llm-wiki in the current workspace (llm-wiki 설정, 위키 연결, llm-wiki 셋팅), or when `llm-wiki status --json` reports a stale/unset workspace. Connects any workspace to the central wiki via llm-wiki-cli."
---

# llm-wiki 워크스페이스 설정

어느 워크스페이스에서든 사용자가 llm-wiki 설정을 요청하면 이 스킬을 따른다. 요청이 없으면 설정하지 않는다.

## 절차의 기준

절차 원문은 이 저장소 `README.md`의 "에이전트에게 워크스페이스 설정 맡기기" 절이다. 이 스킬 파일은 그 절로 안내만 하고 절차를 복제하지 않는다. CLI가 바뀌면 README가 기준이다.

- 이 스킬이 들어 있는 저장소의 `README.md`(이 파일 기준 `../../README.md`)를 읽는다. 스킬 폴더만 따로 복사해 두어 그 파일이 없으면 `https://github.com/chlee1001/llm-wiki-cli`의 같은 절을 읽는다.
- 중앙 위키 clone은 `~/.config/llm-wiki/roots.json`(`XDG_CONFIG_HOME`이 있으면 그 아래)의 `wiki` 값으로 확인한다.

## 절차

1. `WS`를 현재 워크스페이스 절대 경로로 정한다.
2. README 절을 읽고 8단계를 그대로 따른다.
3. 중앙 위키는 `roots.json`에 이미 있는 `wiki` 경로를 쓴다. 없으면 사용자에게 중앙 저장소 주소를 묻는다.
4. 프로젝트 이름과 분류(`company`, `personal` 등)는 사용자가 주지 않았으면 묻는다. 이름 기본값은 `WS` 디렉터리 이름이다.
5. 연결 명령은 `llm-wiki init --path WS --wiki CLONE --project NAME --category CATEGORY --json`이다. 결과의 `next`가 있으면 `WS`에서 `llm-wiki sync --json`을 실행한다.
6. 끝나면 `llm-wiki status --json`의 `current_project`와 `managed`가 모두 `current`인지 확인하고 `created`/`updated`/`previous`를 보고한다.

## 지킬 것

- 이미 설정돼 있으면(`status --json` 성공, `managed` 전부 `current`) 결과만 보고하고 끝낸다.
- `roots.json`을 직접 고치지 않는다. 위키 파일을 손으로 쓰지 않는다.
- `secret_detected`, `conflict`, `push_rejected`가 나면 멈추고 `recovery`를 사용자에게 보여 준다.
- `AGENTS.md`/`.gjc/` 산출물을 워크스페이스 저장소에 커밋하지 않는다. 커밋은 사용자가 정한다.
- 비밀값을 출력하거나 기록하지 않는다.
- 설정은 머신마다 따로다. 다른 머신에서는 그 머신에서 다시 한다.
