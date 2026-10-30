# ccs — Claude Code 세션을 git 처럼

여러 폴더·여러 터미널(cmux, Orca, tmux…)에 Claude Code 를 띄워 두면, 컴퓨터를 끄기 전에 탭을 하나씩 열어 "이거 꺼도 되나"를 확인하게 된다. `ccs` 는 열린 세션을 한 화면에 모아 **꺼도 되는지 판정**하고, 목록을 **저장(stash)** 했다가 재부팅 뒤 **각자 원래 터미널로 다시 연다(pop)**.

```
$ ccs
열린 세션 9개  🔴 끄면 안 됨 2 · 🤖 자동 실행 작업 1 · 🟡 이어서 할 일 있음 2 · 🟢 꺼도 됨 4

🔴 끄면 안 됨
  🔴 api-4d  결제 웹훅 재시도 설계  Orca:api  방금
     ~/src/api · 응답 생성 중
  🔴 web-1c  대시보드 차트 개편  cmux:dev#2  5분 전
     ~/src/web · 백그라운드 실행 중: npm run dev

🤖 자동 실행 작업 (끄면 중단됨)
  🤖 jobs-a0  /nightly-report  ← job-runner

🟡 이어서 할 일 있음
  🟡 infra-b9  배포 스크립트 정리  tmux:ops:0  2시간 전
     ~/src/infra · 이 세션이 고친 파일 3개 미커밋 · 푸시 안 한 커밋 1개 (chore/deploy)
     요청: 배포 스크립트에서 중복 단계 빼줘
     마지막: staging 에서 한 번 돌려 보고 main 에 합칠까요?
  🟡 support-7e  외부 API 해지 요청 재송신  cmux:dev#4  1일 전
     ~/src/support · 메모: 대기: 상대 회사 회신
     메모(1일 전): 외부 API 해지 요청 재송신 처리
     다음: 회신 메일 확인 후 상태 조회 API 다시 호출

🟢 꺼도 됨
  🟢 docs-c4  README 정리  cmux:dev#1  3시간 전
     ~/src/docs · repo에 다른 미커밋 2개
```

## 설치

```bash
git clone https://github.com/LazyerIJ/ccs.git ~/ccs
ln -s ~/ccs/ccs.py ~/.local/bin/ccs                     # PATH 에 있는 아무 곳
ln -s ~/ccs/skills/ccs ~/.claude/skills/ccs             # 세션 안에서 "컴퓨터 꺼도 돼?" 로 부르기
ln -s ~/ccs/skills/ccs-note ~/.claude/skills/ccs-note   # /ccs-note (note --ask 에 필요)
```

Python 3.10+ 표준 라이브러리만 쓴다. 외부 명령은 `ps`, `git`, 그리고 있으면 `cmux`, `tmux`, `orca`.

## 흐름

```bash
# 끄기 전
ccs                      # 판정 보기
ccs note --ask           # (선택) 메모 없는 세션들에 /ccs-note 를 보내 스스로 인수인계 메모를 쓰게 함
ccs stash -m "퇴근"      # 열린 세션 목록 저장
ccs stash --close        # (선택) 저장하고 🟢·🟡 세션 닫기

# 재부팅 뒤
ccs stash show           # 저장한 세션들이 뭘 하던 중이었는지
ccs pop                  # 번호로 골라 원래 터미널로 다시 열기 (🔴·🟡 미리 체크)
```

## 명령

| 명령 | 하는 일 |
|---|---|
| `ccs` / `ccs status [-v] [-a] [--json]` | 열린 세션 판정. `-v` 모든 세션의 메모·마지막 응답, `-a` 종료된 기록까지 |
| `ccs show <이름\|id앞자리\|제목일부>` | 한 세션 자세히: 미커밋 파일, 돌고 있는 명령, 메모 전문, 다시 여는 명령 |
| `ccs stash [-m 메모]` | 열린 세션 목록을 `~/.ccs/stash/<시각>.json` 에 저장(아무것도 닫지 않음) |
| `ccs stash --close [-y] [--force]` | 저장 뒤 🟢·🟡 세션 종료(SIGTERM). 🔴 는 `--force` 일 때만. 명령을 부른 세션은 안 닫음 |
| `ccs stash list` | 저장 이력 |
| `ccs stash show [세션] [--id]` | 저장해 둔 기록만으로 자세히 보기(재부팅 뒤에도) |
| `ccs pop [id] [--all \| --only resume,block] [--via auto\|cmux\|tmux\|orca\|print] [--dry-run]` | 저장한 세션을 각자 원래 터미널로 다시 열기. 이미 열린 세션·폴더가 사라진 세션은 뺀다 |
| `ccs note` (표준 입력) | 이 명령을 부른 세션에 인수인계 메모 저장. 세션 안의 `/ccs-note` 가 부른다 |
| `ccs note --ask [--all] [--dry-run] [-y]` | 메모가 없거나 오래된 세션 탭에 `/ccs-note` 를 입력(cmux·tmux·Orca) |

## 판정 기준

| 판정 | 조건 | 근거 |
|---|---|---|
| 🔴 끄면 안 됨 | 응답 생성 중 | `~/.claude/sessions/<pid>.json` 의 `status: busy` |
| | Claude 가 띄운 명령·서버가 살아 있음 | 세션 pid 의 자식 중 `shell-snapshots` 를 source 하는 셸 (MCP 서버는 제외) |
| | `/loop` 다음 깨어남이 미래 | 대화 원본의 마지막 `ScheduleWakeup` |
| | 세션 cron 이 남음 | `CronCreate` − `CronDelete` |
| 🤖 자동 작업 | `claude -p` 로 다른 프로그램이 띄운 세션 | `entrypoint: sdk-*` 또는 `-p` |
| 🟡 이어서 할 일 | Claude 가 질문으로 끝냄 | 마지막 응답 끝 300자에 `?` |
| | 응답 도중 멈춤·중단 | 마지막 기록이 사용자 메시지·도구 호출·도구 결과 |
| | **이 세션이 고친** 파일이 미커밋 | `Edit`·`Write` 한 경로 ∩ `git status` |
| | **이 세션이 커밋한** 브랜치가 미푸시 | `git commit` 을 돌린 적 있음 + `@{u}..HEAD` |
| | 메모 상태가 `대기…`·`진행 중` | `/ccs-note` |
| 🟢 꺼도 됨 | 위 어느 것도 아님 (메모 상태 `끝남` 이면 질문으로 끝났어도) | |

같은 repo 를 여러 세션이 쓰는 경우가 많아서, 다른 세션이 만든 미커밋은 "참고"로만 보이고 판정에 넣지 않는다.

## 인수인계 메모 (`/ccs-note`)

기계적인 신호로는 "상대 회사 회신 기다리는 중" 같은 상태를 모른다. 그래서 맥락을 가진 세션이 스스로 메모를 쓴다.

```
# <이 세션의 목표 한 줄>
상태: 진행 중 | 대기: <무엇을 기다리는지> | 끝남
## 지금 상태
## 다음에 열면 할 것      ← 첫 줄이 목록에 보인다
## 주의 · 실패한 시도
## 관련 파일 · 명령
```

- 저장 위치 `~/.ccs/notes/<session-id>/<시각>.md`. 가장 최근 것을 쓴다.
- 메모를 쓴 뒤 사용자가 그 세션에 다시 말을 걸면 "메모 뒤에 대화가 더 있음"으로 표시하고 판정은 원래 신호로 돌아간다.
- `--ask` 로 보낼 수 있는 탭: cmux(`cmux send`, 입력 중인 글·질문 창이 있으면 보내지 않음), tmux(`send-keys`), Orca(탭 제목이 세션 제목과 **하나만** 맞을 때 `orca terminal send`). 응답 중인 세션은 건너뛴다. 세션마다 Claude 응답 한 번 분량의 비용이 든다.

## 다시 열기 — 세션마다 원래 터미널로

| 원래 위치 | `ccs pop` 이 여는 곳 |
|---|---|
| cmux | 원래 워크스페이스 이름 · 원래 탭 순서. 같은 이름 워크스페이스가 있으면 그 안에 탭으로 |
| tmux | 원래 tmux 세션 이름 · 창 이름. 세션이 없으면 같은 이름으로 새로 만듦(분리 상태 → `tmux attach -t <이름>`) |
| Orca | 원래 Orca 작업공간(worktree)에 새 탭. Orca 탭의 작업공간은 세션 cwd 와 다를 수 있어 stash 때 탭 제목으로 찾아 저장 |
| 그 밖(iTerm 등)·찾지 못한 Orca 탭 | 켜져 있는 터미널로: cmux → (tmux 안이면) tmux → 명령 출력 |

- 원래 터미널이 꺼져 있으면 위 순서로 다른 곳에 열고 `자리 바뀜 … 원래 Orca:api → cmux` 로 알린다.
- `--via cmux|tmux|orca|print` 는 전부 한 곳으로 연다.
- 원래 실행 플래그(`--dangerously-skip-permissions`, `--model`, `--permission-mode` 등)는 살린다. ps 가 따옴표를 지워 되살릴 수 없는 값(JSON 인자)과 cmux 래퍼가 매번 새로 넣는 `--settings` 는 버린다.

## 어디서 도나

| | |
|---|---|
| macOS · Linux | 된다 (`ps`, `git`) |
| Windows | 아직 아니다 (`ps` 없음) |
| Claude Code 버전 | `~/.claude/sessions/*.json` 은 최근 버전(2.1.x)의 **비공식** 형식. 없으면 열린 세션을 못 찾는다 — 읽는 곳은 `load_registry`·`same_start` 두 함수뿐 |
| 설정 폴더 | `CLAUDE_CONFIG_DIR`, 저장 폴더 `CCS_HOME`(기본 `~/.ccs`) 을 따른다 |

`ccs` 는 Claude Code 의 파일을 **읽기만** 한다. 쓰는 곳은 `~/.ccs` 뿐이고, 세션을 닫거나(`stash --close`) 탭에 글을 입력하는(`note --ask`) 명령은 확인을 받고 실행한다.

## 한계

- 메모가 없는 세션의 판정은 기계적인 신호뿐이다.
- Orca 탭은 pid·tty 를 알 수 없어 제목으로 잇는다. 제목이 없거나 겹치면 `--ask`·Orca 복원 대상에서 빠진다.
- cmux 의 분할(pane) 배치, Orca 의 탭 순서는 살리지 않는다.
- 다른 기기로 옮기기는 하지 않는다 — 두 기기가 SSH 로 닿으면 `cmux session move` 를 보라.

## 테스트

```bash
python3 -m unittest -v tests/test_ccs.py
```
