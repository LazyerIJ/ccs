#!/usr/bin/env python3
"""ccs — 열려 있는 Claude Code 세션을 git 처럼 다룬다: status / show / stash / pop.

판정 근거는 전부 로컬에 이미 있는 것만 쓴다.
  - ~/.claude/sessions/<pid>.json   살아 있는 세션 목록(pid·sessionId·cwd·busy/idle)
  - ~/.claude/projects/*/<id>.jsonl  대화 원본(제목·마지막 응답·고친 파일·/loop 예약)
  - ps                              프로세스 생존·자식 프로세스(백그라운드 Bash)·tty
  - git                             미커밋·미푸시
  - cmux / tmux                     tty → 탭 위치, 복원
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()
CCS_HOME = Path(os.environ.get("CCS_HOME") or "~/.ccs").expanduser()
CACHE_VERSION = 2

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
QUESTION_RE = re.compile(r"[?？]")
SNAPSHOT_MARK = "/shell-snapshots/snapshot-"  # Claude 의 Bash 도구가 띄우는 셸의 표식

# resume 명령을 다시 만들 때 값까지 살려 둘 플래그. 나머지 맨 토큰(초기 프롬프트 등)은 버린다.
VALUE_FLAGS = {
    "--model", "--permission-mode", "--add-dir", "--settings", "--mcp-config",
    "--plugin-dir", "--agent", "--effort", "--fallback-model", "--setting-sources",
    "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
}
DROP_WITH_VALUE = {"--resume", "-r", "--session-id", "--name", "-n"}
DROP_FLAGS = {"--continue", "-c", "--fork-session", "--print", "-p"}

VERDICTS = {
    "block": ("🔴", "끄면 안 됨"),
    "resume": ("🟡", "이어서 할 일 있음"),
    "done": ("🟢", "꺼도 됨"),
    "job": ("🤖", "자동 실행 작업 (끄면 중단됨)"),
    "stale": ("⚪", "이미 종료됨"),
}


# ───────────────────────────── 공통 ─────────────────────────────

def run(cmd: list[str], cwd: str | None = None, env: dict | None = None, timeout: float = 10) -> str | None:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def parse_ts(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def ago(epoch: float | None, now: float | None = None) -> str:
    if not epoch:
        return ""
    d = int((now or time.time()) - epoch)
    if d < 60:
        return "방금"
    if d < 3600:
        return f"{d // 60}분 전"
    if d < 86400:
        return f"{d // 3600}시간 전"
    return f"{d // 86400}일 전"


def one_line(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def last_paragraph(text: str) -> str:
    """응답의 마지막 문단(코드 블록·표 줄은 건너뜀). 결론이나 질문이 대개 여기 있다."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    for p in reversed(paras):
        if not p.startswith(("```", "|", "---")):
            return p
    return paras[-1] if paras else ""


def short_path(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


# ───────────────────────────── 프로세스 ─────────────────────────────

@dataclass
class Proc:
    pid: int
    ppid: int
    tty: str
    lstart: str
    command: str


def ps_table() -> dict[int, Proc]:
    # TZ=UTC: 세션 목록의 procStart 가 UTC 문자열이라 같은 기준으로 맞춘다.
    env = {**os.environ, "TZ": "UTC", "LC_ALL": "C"}
    out = run(["ps", "-axo", "pid=,ppid=,tty=,lstart=,command="], env=env)
    procs: dict[int, Proc] = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        procs[pid] = Proc(pid, ppid, parts[2], " ".join(parts[3:8]), parts[8])
    return procs


def is_claude(p: Proc) -> bool:
    head = p.command.split()[:2]
    return any(os.path.basename(t) == "claude" for t in head) or "@anthropic-ai/claude-code" in p.command


def ancestors(pid: int, procs: dict[int, Proc]) -> list[int]:
    chain, seen = [], set()
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        chain.append(pid)
        pid = procs[pid].ppid
    return chain


def prog_label(command: str) -> str:
    """'/…/Python /…/bin/job-runner start' → 'job-runner start'. 인터프리터 경로는 건너뛴다."""
    tokens = command.split()
    interp = re.compile(r"^(python[\d.]*|Python|node|uv|uvx|bun|deno|npx|npm|run|exec|tool)$")
    while len(tokens) > 1 and interp.match(os.path.basename(tokens[0])):
        tokens = tokens[1:]
    return one_line(" ".join([os.path.basename(tokens[0])] + tokens[1:]) if tokens else "", 40)


def bash_children(pid: int, procs: dict[int, Proc], exclude: set[int]) -> list[str]:
    """Claude 의 Bash 도구가 띄운 셸 중 아직 살아 있는 것의 실제 명령. exclude: ccs 자신을 띄운 셸."""
    found = []
    for p in procs.values():
        if p.ppid == pid and SNAPSHOT_MARK in p.command and p.pid not in exclude:
            found.append(one_line(bash_payload(p.command), 80))
    return found


def bash_payload(command: str) -> str:
    """Bash 도구 셸의 준비 코드(snapshot source·export·setopt…)를 걷어 내고 실제 명령만 남긴다."""
    m = re.search(r"\beval (['\"])(.*)\1 < /dev/null", command, re.S)
    if m:
        return m.group(2).replace("'\\''", "'")
    if " eval " in command:
        return command.split(" eval ", 1)[1]
    setup = ("source ", "export ", "setopt ", ":", "{ \\builtin", "\\builtin", "true")
    parts = [x.strip() for x in command.split(" && ")]
    real = [x for x in parts if x and not x.startswith(setup) and SNAPSHOT_MARK not in x]
    return " && ".join(real) or command


# ───────────────────────────── 대화 원본 색인 ─────────────────────────────

def new_entry() -> dict:
    return {
        "offset": 0, "title": "", "last_prompt": "", "last_text": "", "last_role": "",
        "last_tool": "", "last_ts": "", "pr_url": "", "touched": {}, "committed": False,
        "wakeup_at": None, "crons": 0, "last_user_ts": "",
    }


def absorb(e: dict, raw: bytes | str) -> None:
    """대화 원본 한 줄을 색인에 반영한다. 읽는 필드는 최소한으로 한정한다."""
    try:
        r = json.loads(raw)
    except ValueError:
        return
    if not isinstance(r, dict):
        return
    t = r.get("type")
    if t == "ai-title":
        e["title"] = r.get("aiTitle") or e["title"]
    elif t == "last-prompt":
        e["last_prompt"] = r.get("lastPrompt") or e["last_prompt"]
    elif t == "pr-link":
        e["pr_url"] = r.get("prUrl") or e["pr_url"]
    elif t in ("user", "assistant") and not r.get("isSidechain"):
        if r.get("isMeta") or r.get("isCompactSummary"):
            return
        content = (r.get("message") or {}).get("content")
        ts = r.get("timestamp") or ""
        if t == "user":
            _absorb_user(e, content, ts)
        else:
            _absorb_assistant(e, content, ts)
        e["last_ts"] = ts or e["last_ts"]


def _absorb_user(e: dict, content, ts: str) -> None:
    if isinstance(content, str):
        e["last_role"], e["last_user_ts"] = "user", ts
        return
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            e["last_role"] = "tool_result"
        elif b.get("type") == "text":
            e["last_role"], e["last_user_ts"] = "user", ts


def _absorb_assistant(e: dict, content, ts: str) -> None:
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and (b.get("text") or "").strip():
            e["last_text"] = b["text"]
            e["last_role"] = "assistant"
        elif b.get("type") == "tool_use":
            e["last_role"] = "tool_use"
            e["last_tool"] = name = b.get("name") or ""
            inp = b.get("input") or {}
            if name in EDIT_TOOLS:
                path = inp.get("file_path") or inp.get("notebook_path")
                if path:
                    e["touched"][path] = 1
            elif name == "Bash" and "git commit" in (inp.get("command") or ""):
                e["committed"] = True
            elif name == "ScheduleWakeup":
                start = parse_ts(ts)
                if inp.get("stop") or start is None:
                    e["wakeup_at"] = None
                else:
                    e["wakeup_at"] = start + float(inp.get("delaySeconds") or 0)
            elif name == "CronCreate":
                e["crons"] += 1
            elif name == "CronDelete":
                e["crons"] = max(0, e["crons"] - 1)


class TranscriptIndex:
    """대화 원본을 늘어난 만큼만 읽는다(offset 캐시). 152MB 짜리도 두 번째부터는 즉시."""

    def __init__(self) -> None:
        self.path = CCS_HOME / "cache" / "transcripts.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        if self.data.get("_v") != CACHE_VERSION:
            self.data = {"_v": CACHE_VERSION}
        self.changed = False

    def get(self, path: Path) -> dict:
        try:
            size = path.stat().st_size
        except OSError:
            return new_entry()
        key = str(path)
        e = self.data.get(key)
        if not e or size < e["offset"]:
            e = new_entry()
        if size > e["offset"]:
            with open(path, "rb") as f:
                f.seek(e["offset"])
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1
            for raw in chunk[:end].splitlines():
                absorb(e, raw)
            e["offset"] += end
            self.data[key] = e
            self.changed = True
        return e

    def save(self) -> None:
        if not self.changed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False))
        tmp.replace(self.path)


def find_transcript(session_id: str) -> Path | None:
    hits = list((CLAUDE_DIR / "projects").glob(f"*/{session_id}.jsonl")) if session_id else []
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


# ───────────────────────────── git ─────────────────────────────

def parse_porcelain_z(out: str, top: str) -> list[str]:
    items, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # 이름 바뀐 항목은 원래 경로가 하나 더 붙는다
        items.append(os.path.join(top, path))
    return items


def git_info(cwd: str, cache: dict) -> dict:
    top = (run(["git", "-C", cwd, "rev-parse", "--show-toplevel"]) or "").strip()
    if not top:
        return {}
    if top in cache:
        return cache[top]
    g = ["git", "-C", top]
    info = {
        "top": top,
        "branch": (run(g + ["rev-parse", "--abbrev-ref", "HEAD"]) or "").strip(),
        "head": (run(g + ["rev-parse", "--short", "HEAD"]) or "").strip(),
        "dirty": parse_porcelain_z(run(g + ["status", "--porcelain=v1", "-z"]) or "", top),
        "unpushed": None,
        "upstream": True,
    }
    up = run(g + ["rev-list", "--count", "@{u}..HEAD"])
    if up is None:
        info["upstream"] = False
        if (run(g + ["remote"]) or "").strip():
            up = run(g + ["rev-list", "--count", "HEAD", "--not", "--remotes"])
    if up is not None and up.strip().isdigit():
        info["unpushed"] = int(up.strip())
    cache[top] = info
    return info


def touched_dirty(touched: list[str], dirty: list[str]) -> list[str]:
    exact = set(dirty)
    dirs = [d for d in dirty if d.endswith("/")]  # untracked 폴더는 통째로 나온다
    return [p for p in touched if p in exact or any(p.startswith(d) for d in dirs)]


# ───────────────────────────── 터미널 위치 ─────────────────────────────

def cmux_bin() -> str | None:
    found = shutil.which("cmux")
    app = "/Applications/cmux.app/Contents/Resources/bin/cmux"
    return found or (app if os.path.exists(app) else None)


def cmux(*args: str, timeout: float = 10) -> str | None:
    b = cmux_bin()
    return run([b, *args], env={**os.environ, "CMUX_QUIET": "1"}, timeout=timeout) if b else None


def norm_tty(tty: str) -> str:
    return tty.removeprefix("/dev/")


def cmux_surfaces() -> dict[str, dict]:
    out = cmux("tree", "--all", "--json")
    try:
        tree = json.loads(out) if out else {}
    except ValueError:
        return {}
    where = {}
    for win in tree.get("windows") or []:
        for ws in win.get("workspaces") or []:
            for pane in ws.get("panes") or []:
                for s in pane.get("surfaces") or []:
                    if s.get("tty"):
                        where[norm_tty(s["tty"])] = {
                            "kind": "cmux", "workspace": ws.get("title") or "",
                            "workspace_id": ws.get("id"), "ws_index": ws.get("index", 0),
                            "pane": pane.get("index", 0), "tab": s.get("index_in_pane", 0),
                            "surface": s.get("ref"),
                        }
    return where


def tmux_panes() -> dict[str, dict]:
    if not shutil.which("tmux"):
        return {}
    fmt = "#{pane_tty}\t#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}"
    where = {}
    for line in (run(["tmux", "list-panes", "-a", "-F", fmt]) or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            where[norm_tty(parts[0])] = {
                "kind": "tmux", "session": parts[1], "window": int(parts[2] or 0),
                "window_name": parts[3], "pane": int(parts[4] or 0),
            }
    return where


def app_of(pid: int, procs: dict[int, Proc]) -> dict:
    for a in ancestors(pid, procs)[1:]:
        m = re.search(r"/([^/]+)\.app/Contents/", procs[a].command)
        if m:
            return {"kind": "app", "app": m.group(1)}
    return {}


def orca_bin() -> str | None:
    found = shutil.which("orca")
    app = "/Applications/Orca.app/Contents/Resources/bin/orca"
    return found or (app if os.path.exists(app) else None)


def orca_terminals() -> dict[str, list[dict]]:
    """Orca 의 Claude 탭: 제목 → [{handle, worktree}]. Orca 는 pid·tty 를 주지 않아 탭 제목(= 세션 제목)으로 잇는다."""
    b = orca_bin()
    out = run([b, "terminal", "list", "--json"], timeout=5) if b else None
    try:
        terms = json.loads(out)["result"]["terminals"] if out else []
    except (ValueError, KeyError, TypeError):
        return {}
    by_title: dict[str, list[dict]] = {}
    for t in terms:
        if t.get("agentIdentity") == "claude" and t.get("writable"):
            title = re.sub(r"^[^\w\s]+\s*", "", t.get("title") or "").strip()  # 앞의 상태 글리프(✳ ◑) 제거
            by_title.setdefault(title, []).append({"handle": t["handle"], "worktree": t.get("worktreePath")})
    return by_title


def orca_where(title: str, orca: dict[str, list[dict]]) -> dict | None:
    hits = orca.get(title) if title else None
    if hits and len(hits) == 1 and hits[0]["worktree"]:
        return {"kind": "orca", **hits[0]}
    return None


def where_label(w: dict) -> str:
    kind = w.get("kind")
    if kind == "cmux":
        return f"cmux:{w.get('workspace') or '?'}#{w.get('tab', 0) + 1}"
    if kind == "tmux":
        return f"tmux:{w.get('session')}:{w.get('window')}"
    if kind == "orca":
        return f"Orca:{os.path.basename(w.get('worktree') or '')}"
    if kind == "app":
        return w.get("app", "")
    if kind == "job":
        return f"← {w.get('parent', '')}"
    return ""


# ───────────────────────────── 세션 수집·판정 ─────────────────────────────

@dataclass
class Session:
    session_id: str
    cwd: str
    pid: int | None = None
    name: str = ""
    status: str = "unknown"
    live: bool = False
    headless: bool = False
    is_self: bool = False
    started_at: float | None = None
    argv: list[str] = field(default_factory=list)
    tty: str = ""
    where: dict = field(default_factory=dict)
    transcript: str = ""
    title: str = ""
    last_prompt: str = ""
    last_text: str = ""
    last_role: str = ""
    last_at: float | None = None
    pr_url: str = ""
    git: dict = field(default_factory=dict)
    mine_dirty: list[str] = field(default_factory=list)
    bg: list[str] = field(default_factory=list)
    note: dict = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    verdict: str = ""

    @property
    def label(self) -> str:
        return self.name or self.session_id[:8]


def load_registry() -> list[dict]:
    d = CLAUDE_DIR / "sessions"
    regs = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            regs.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return regs


def same_start(reg: dict, p: Proc) -> bool:
    """pid 재사용을 거른다. procStart 가 없으면 통과."""
    want = reg.get("procStart")
    return not want or " ".join(want.split()) == p.lstart


def live_session_ids(procs: dict[int, Proc] | None = None) -> set[str]:
    procs = procs if procs is not None else ps_table()
    ids = set()
    for reg in load_registry():
        p = procs.get(reg.get("pid"))
        if p and is_claude(p) and same_start(reg, p):
            ids.add(reg.get("sessionId", ""))
    return ids


# ───────────────────────────── 인수인계 메모 (/ccs-note) ─────────────────────────────

NOTE_TEMPLATE = """# <이 세션의 목표 한 줄>
상태: 진행 중 | 대기: <무엇을 기다리는지> | 끝남
## 지금 상태
## 다음에 열면 할 것
## 주의 · 실패한 시도
## 관련 파일 · 명령
"""


def notes_dir() -> Path:
    return CCS_HOME / "notes"


def parse_note(text: str) -> dict:
    """메모에서 목록에 보일 세 가지(목표·상태·다음 할 일 첫 줄)만 뽑는다. 나머지는 원문 그대로 보인다."""
    goal = state = nxt = section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            section = line[3:]
        elif line.startswith("# ") and not goal:
            goal = line[2:].strip()
        elif re.match(r"상태\s*[:：]", line) and not state:
            state = re.split(r"[:：]", line, maxsplit=1)[1].strip()
        elif section.startswith("다음") and not nxt:
            nxt = re.sub(r"^([-*]|\d+[.)])\s*", "", line)
    return {"goal": goal, "state": state, "next": nxt}


def note_state_kind(state: str) -> str:
    s = state.replace(" ", "")
    if s.startswith(("끝", "완료", "done")):
        return "done"
    if s.startswith(("대기", "waiting")):
        return "waiting"
    return "doing"


def save_note(session_id: str, text: str) -> Path:
    d = notes_dir() / session_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text.strip() + "\n")
    return path


def latest_note(session_id: str) -> dict:
    d = notes_dir() / session_id
    files = sorted(d.glob("*.md")) if session_id and d.is_dir() else []
    if not files:
        return {}
    f = files[-1]
    try:
        text = f.read_text()
        at = datetime.strptime(f.stem, "%Y%m%d-%H%M%S").timestamp()
    except (OSError, ValueError):
        return {}
    return {"at": at, "path": str(f), "text": text, "stale": False, **parse_note(text)}


def classify(s: Session, e: dict, now: float) -> None:
    if not s.live:
        s.verdict = "stale"
        return
    if s.headless:
        s.verdict = "job"
        return
    if s.status == "busy":
        s.blockers.append("응답 생성 중")
    elif s.status not in ("idle", "unknown"):
        s.blockers.append(f"상태: {s.status}")
    if s.bg:
        more = f" 외 {len(s.bg) - 1}개" if len(s.bg) > 1 else ""
        kind = "명령 실행 중" if s.status == "busy" else "백그라운드 실행 중"
        s.blockers.append(f"{kind}: {s.bg[0]}{more}")
    if e.get("wakeup_at") and e["wakeup_at"] > now:
        at = datetime.fromtimestamp(e["wakeup_at"]).strftime("%H:%M")
        s.blockers.append(f"/loop 예약: {at}에 다시 깨어남")
    if e.get("crons"):
        s.blockers.append(f"세션 예약 작업 {e['crons']}개 (끄면 사라짐)")

    # 메모를 쓴 뒤로 사용자가 더 말을 걸지 않았다면, 마지막 응답 모양보다 세션 자신의 말을 믿는다.
    note = s.note if s.note and not s.note.get("stale") else None
    kind = note_state_kind(note["state"]) if note and note.get("state") else ""
    if s.status != "busy" and kind:
        if kind != "done":
            s.pending.append(f"메모: {note['state']}")
    elif s.status != "busy":
        if s.last_role == "assistant" and QUESTION_RE.search(s.last_text[-300:]):
            s.pending.append("Claude가 답을 기다림")
        elif s.last_role == "tool_use" and e.get("last_tool") == "AskUserQuestion":
            s.pending.append("Claude가 선택지를 띄워 둠")
        elif s.last_role in ("user", "tool_result", "tool_use"):
            s.pending.append("응답 도중 멈춤")

    g = s.git
    if g:
        if s.mine_dirty:
            s.pending.append(f"이 세션이 고친 파일 {len(s.mine_dirty)}개 미커밋")
        others = len(g["dirty"]) - len(s.mine_dirty)
        if others > 0:
            s.notes.append(f"repo에 다른 미커밋 {others}개")
        if g.get("unpushed"):
            msg = f"푸시 안 한 커밋 {g['unpushed']}개 ({g['branch']})"
            (s.pending if e.get("committed") else s.notes).append(msg)

    s.verdict = "block" if s.blockers else "resume" if s.pending else "done"


def collect(include_stale: bool = False) -> list[Session]:
    procs = ps_table()
    index = TranscriptIndex()
    gitcache: dict = {}
    terms = {**tmux_panes(), **cmux_surfaces()}
    mine = set(ancestors(os.getpid(), procs))
    now = time.time()
    orca: dict | None = None
    sessions = []
    for reg in load_registry():
        pid = reg.get("pid")
        p = procs.get(pid)
        live = bool(p) and is_claude(p) and same_start(reg, p)
        if not live and not include_stale:
            continue
        s = Session(
            session_id=reg.get("sessionId", ""), cwd=reg.get("cwd", ""), pid=pid,
            name=reg.get("name", ""), status=reg.get("status", "unknown") if live else "stale",
            live=live, is_self=pid in mine,
            started_at=(reg.get("startedAt") or 0) / 1000 or None,
        )
        if live:
            s.argv = p.command.split()
            s.headless = reg.get("entrypoint", "").startswith("sdk") or bool({"-p", "--print"} & set(s.argv))
            s.tty = norm_tty(p.tty)
            parent = procs.get(p.ppid)
            if s.headless and parent:
                s.where = {"kind": "job", "parent": prog_label(parent.command)}
            else:
                s.where = terms.get(s.tty) or app_of(pid, procs)
            s.bg = bash_children(pid, procs, exclude=mine)
        path = find_transcript(s.session_id)
        e = index.get(path) if path else new_entry()
        if path:
            s.transcript = str(path)
        s.title, s.last_prompt, s.last_text = e["title"], e["last_prompt"], e["last_text"]
        s.title = s.title or one_line(s.last_prompt, 60)
        s.last_role, s.pr_url = e["last_role"], e["pr_url"]
        s.last_at = parse_ts(e["last_ts"])
        s.note = latest_note(s.session_id)
        if s.note:
            asked = parse_ts(e.get("last_user_ts") or "")
            s.note["stale"] = bool(asked and asked > s.note["at"] + 5)
        if live and s.cwd and os.path.isdir(s.cwd):
            s.git = git_info(s.cwd, gitcache)
            if s.git:
                s.mine_dirty = touched_dirty(list(e["touched"]), s.git["dirty"])
        if s.where.get("app") == "Orca":
            if orca is None:
                orca = orca_terminals()
            s.where = orca_where(s.title, orca) or s.where
        classify(s, e, now)
        sessions.append(s)
    index.save()
    order = {"block": 0, "job": 1, "resume": 2, "done": 3, "stale": 4}
    sessions.sort(key=lambda s: (order[s.verdict], -(s.last_at or 0)))
    return sessions


# ───────────────────────────── resume 명령 ─────────────────────────────

def unsafe_token(t: str) -> bool:
    """ps 가 따옴표를 지워 원형을 되살릴 수 없는 토큰, 또는 터미널 래퍼가 매번 새로 넣는 값."""
    return any(c in t for c in "{}\"'") or "/.cmuxterm/" in t


def resume_argv(argv: list[str], session_id: str) -> list[str]:
    """원래 실행 플래그(권한 모드·모델 등)는 살리고, 세션 지정·초기 프롬프트·되살릴 수 없는 값은 버린다."""
    out, i = ["claude"], 1
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        has_val = nxt is not None and not nxt.startswith("-")
        if unsafe_token(a):
            i += 1
        elif a in VALUE_FLAGS and has_val and unsafe_token(nxt):
            i += 2
        elif a in DROP_WITH_VALUE:
            i += 2 if has_val else 1
        elif a in DROP_FLAGS or any(a.startswith(f + "=") for f in DROP_WITH_VALUE):
            i += 1
        elif a in VALUE_FLAGS and has_val:
            out += [a, nxt]
            i += 2
        elif a.startswith("-"):
            out.append(a)
            i += 1
        else:
            i += 1
    return out + ["--resume", session_id]


def resume_cmd(rec: dict) -> str:
    argv = resume_argv(rec.get("argv") or ["claude"], rec["session_id"])
    return f"cd {shlex.quote(rec['cwd'])} && {shlex.join(argv)}"


# ───────────────────────────── 출력 ─────────────────────────────

class Ansi:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @classmethod
    def wrap(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.on and s else s

    dim = classmethod(lambda c, s: c.wrap("2", s))
    bold = classmethod(lambda c, s: c.wrap("1", s))
    red = classmethod(lambda c, s: c.wrap("31", s))
    yellow = classmethod(lambda c, s: c.wrap("33", s))
    green = classmethod(lambda c, s: c.wrap("32", s))


def print_session(s: Session, verbose: bool) -> None:
    icon, _ = VERDICTS[s.verdict]
    if s.verdict == "job":
        print(f"  {icon} {Ansi.bold(s.label)}  {one_line(s.title, 48)}  {Ansi.dim(where_label(s.where))}")
        return
    me = Ansi.bold(" (이 세션)") if s.is_self else ""
    where = where_label(s.where)
    head = f"  {icon} {Ansi.bold(s.label)}{me}  {one_line(s.title, 48)}"
    tail = "  ".join(x for x in (Ansi.dim(where), Ansi.dim(ago(s.last_at))) if x)
    print(f"{head}  {tail}")
    reasons = [Ansi.red(x) for x in s.blockers] + [Ansi.yellow(x) for x in s.pending]
    reasons += [Ansi.dim(x) for x in s.notes]
    print(f"     {Ansi.dim(short_path(s.cwd))}" + (" · " + " · ".join(reasons) if reasons else ""))
    if verbose or s.verdict == "resume":
        for line in summary_lines(stash_record(s)):
            print(f"     {Ansi.dim(line)}")


def summary_lines(rec: dict) -> list[str]:
    """이 세션이 무엇을 하던 중인지. 메모가 있으면 메모, 없으면 마지막 요청·응답."""
    note = rec.get("note") or {}
    if note.get("goal") or note.get("next"):
        stale = " (메모 뒤에 대화가 더 있음)" if note.get("stale") else ""
        lines = [f"메모({ago(note.get('at'))}): {one_line(note.get('goal', ''), 80)}{stale}"]
        if note.get("next"):
            lines.append(f"다음: {one_line(note['next'], 86)}")
        return lines
    lines = []
    if rec.get("last_prompt"):
        lines.append(f"요청: {one_line(rec['last_prompt'], 86)}")
    if rec.get("last_text"):
        lines.append(f"마지막: {one_line(last_paragraph(rec['last_text']), 84)}")
    return lines


def cmd_status(args) -> int:
    sessions = collect(include_stale=args.all)
    if args.json:
        print(json.dumps([asdict(s) for s in sessions], ensure_ascii=False, indent=2))
        return 0
    live = [s for s in sessions if s.live]
    counts = {k: sum(1 for s in live if s.verdict == k) for k in ("block", "job", "resume", "done")}
    print(Ansi.bold(f"열린 세션 {len(live)}개") + "  " + " · ".join(
        f"{VERDICTS[k][0]} {VERDICTS[k][1].split(' (')[0]} {n}" for k, n in counts.items() if n))
    current = None
    for s in sessions:
        if s.verdict != current:
            current = s.verdict
            print(f"\n{VERDICTS[current][0]} {Ansi.bold(VERDICTS[current][1])}")
        print_session(s, args.verbose)
    unnoted = [s for s in live if s.verdict != "job" and not s.is_self and (not s.note or s.note.get("stale"))]
    if unnoted:
        print(Ansi.dim(f"\n메모가 없거나 오래된 세션 {len(unnoted)}개 — `ccs note --ask` 로 각 세션에 /ccs-note 를 보낼 수 있습니다."))
    if not live:
        print("\n열린 세션이 없습니다.")
    elif counts["block"] == 0 and counts["job"] == 0:
        print("\n" + Ansi.green("끄면 안 되는 세션은 없습니다.") + " `ccs stash` 로 목록을 저장해 두면 재부팅 뒤 `ccs pop` 으로 다시 엽니다.")
    return 0


def match_sessions(sessions: list[Session], q: str) -> list[Session]:
    ql = q.lower()
    exact = [s for s in sessions if s.name == q or s.session_id.lower().startswith(ql)]
    return exact or [s for s in sessions if ql in s.title.lower() or ql in s.cwd.lower()]


def cmd_show(args) -> int:
    hits = match_sessions(collect(include_stale=True), args.query)
    if not hits:
        print(f"'{args.query}' 에 맞는 세션이 없습니다.", file=sys.stderr)
        return 1
    for s in hits:
        icon, word = VERDICTS[s.verdict]
        print(f"{icon} {Ansi.bold(s.label)} — {word}")
        rows = [
            ("제목", s.title), ("세션", s.session_id), ("pid", str(s.pid or "")),
            ("위치", where_label(s.where) or s.tty), ("cwd", s.cwd),
            ("git", f"{s.git.get('branch')} @ {s.git.get('head')}" if s.git else ""),
            ("PR", s.pr_url), ("마지막 활동", ago(s.last_at)), ("대화 파일", s.transcript),
        ]
        for k, v in rows:
            if v:
                print(f"  {k:<8} {v}")
        for title, items, paint in (("끄면 안 되는 이유", s.blockers, Ansi.red),
                                    ("이어서 할 일", s.pending, Ansi.yellow), ("참고", s.notes, Ansi.dim)):
            if items:
                print(f"  {title}: " + " · ".join(paint(x) for x in items))
        for path in s.mine_dirty:
            print(f"    - {short_path(path)}")
        for cmd in s.bg:
            print(f"    $ {cmd}")
        print_detail(stash_record(s))
    return 0


def print_detail(rec: dict) -> None:
    """show 와 stash show 가 같이 쓰는 뒷부분: 메모 원문(없으면 마지막 요청·응답)과 다시 여는 명령."""
    note = rec.get("note") or {}
    if note.get("text"):
        stale = Ansi.yellow(" — 메모 뒤에 대화가 더 있음") if note.get("stale") else ""
        print(f"  메모 ({ago(note.get('at'))}){stale}")
        for line in note["text"].rstrip().splitlines():
            print(f"    {line}")
    if rec.get("last_prompt"):
        print(f"  마지막 요청: {one_line(rec['last_prompt'], 200)}")
    if rec.get("last_text") and not note.get("text"):
        print(f"  마지막 응답: {one_line(rec['last_text'][-600:], 300)}")
    print(f"  다시 열기: {resume_cmd(rec)}\n")


# ───────────────────────────── stash ─────────────────────────────

def stash_dir() -> Path:
    return CCS_HOME / "stash"


def stash_record(s: Session) -> dict:
    return {
        "session_id": s.session_id, "name": s.name, "title": s.title, "cwd": s.cwd,
        "verdict": s.verdict, "blockers": s.blockers, "pending": s.pending, "notes": s.notes,
        "last_prompt": s.last_prompt[:500], "last_text": s.last_text[-800:],
        "argv": s.argv, "where": s.where, "note": s.note,
        "git": {k: s.git.get(k) for k in ("top", "branch", "head")} if s.git else {},
    }


def load_stash(stash_id: str | None) -> tuple[Path, dict] | None:
    files = sorted(stash_dir().glob("*.json"))
    if stash_id:
        files = [f for f in files if f.stem.startswith(stash_id)]
    if not files:
        return None
    return files[-1], json.loads(files[-1].read_text())


def cmd_stash(args) -> int:
    if args.stash_cmd == "list":
        return stash_list()
    if args.stash_cmd == "show":
        return stash_show(args.query, args.id)
    every = [s for s in collect() if s.live]
    sessions = [s for s in every if s.verdict != "job"]
    jobs = len(every) - len(sessions)
    if jobs:
        print(f"{Ansi.red('주의')} 자동 실행 작업 {jobs}개는 탭이 아니라 저장하지 않습니다. 끄면 중단됩니다.")
    if not sessions:
        print("열린 세션이 없어 저장할 것이 없습니다.")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    data = {
        "id": stamp, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(), "message": args.message or "",
        "sessions": [stash_record(s) for s in sessions],
    }
    stash_dir().mkdir(parents=True, exist_ok=True)
    path = stash_dir() / f"{stamp}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    counts = {k: sum(1 for s in sessions if s.verdict == k) for k in ("block", "resume", "done")}
    print(f"stash {stamp} 저장: 세션 {len(sessions)}개 → {short_path(str(path))}")
    print("  " + " · ".join(f"{VERDICTS[k][0]} {n}" for k, n in counts.items()))
    for s in sessions:
        if s.verdict == "block":
            print(f"  {Ansi.red('주의')} {s.label}: " + " · ".join(s.blockers))
    if args.close:
        return close_sessions(sessions, force=args.force, yes=args.yes)
    print("\n재부팅 뒤 `ccs pop` 으로 다시 엽니다. 지금 닫으려면 `ccs stash --close`.")
    return 0


def stash_list() -> int:
    files = sorted(stash_dir().glob("*.json"), reverse=True)
    if not files:
        print("저장된 stash 가 없습니다.")
        return 0
    for f in files:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        ss = d.get("sessions", [])
        icons = "".join(VERDICTS.get(r.get("verdict"), ("?",))[0] for r in ss)
        popped = Ansi.dim(" (pop 함)") if d.get("popped") else ""
        print(f"{d['id']}  세션 {len(ss)}개 {icons}  {d.get('message', '')}{popped}")
    return 0


def stash_show(query: str | None, stash_id: str | None) -> int:
    """재부팅 뒤에도 볼 수 있게, 저장해 둔 기록만으로 보여 준다."""
    loaded = load_stash(stash_id)
    if not loaded:
        print("저장된 stash 가 없습니다.", file=sys.stderr)
        return 1
    _, data = loaded
    recs = data["sessions"]
    if query:
        q = query.lower()
        recs = [r for r in recs if r["name"] == query or r["session_id"].lower().startswith(q)] or \
               [r for r in recs if q in (r["title"] or "").lower() or q in r["cwd"].lower()]
    print(f"stash {data['id']} {data.get('message', '')}".rstrip() + f"  ({data.get('created', '')})\n")
    for r in recs:
        icon, word = VERDICTS.get(r["verdict"], ("?", ""))
        print(f"{icon} {Ansi.bold(r['name'] or r['session_id'][:8])}  {r['title']}  — {word}")
        print(f"  cwd      {r['cwd']}")
        if r.get("git"):
            print(f"  git      {r['git'].get('branch')} @ {r['git'].get('head')}")
        reasons = r.get("blockers", []) + r.get("pending", [])
        if reasons:
            print("  이유     " + " · ".join(reasons))
        print_detail(r)
    return 0 if recs else 1


def close_sessions(sessions: list[Session], force: bool, yes: bool) -> int:
    targets = [s for s in sessions if not s.is_self and (s.verdict != "block" or force)]
    skipped = [s for s in sessions if s.verdict == "block" and not force]
    if not targets:
        print("닫을 세션이 없습니다.")
        return 0
    print(f"\n닫을 세션 {len(targets)}개 (대화는 파일에 남아 있어 `ccs pop` 으로 그대로 이어집니다):")
    for s in targets:
        print(f"  {VERDICTS[s.verdict][0]} {s.label}  {one_line(s.title, 50)}")
    for s in skipped:
        print(f"  {Ansi.dim('건너뜀')} {s.label} — {' · '.join(s.blockers)}  (닫으려면 --force)")
    if not yes:
        if not sys.stdin.isatty():
            print("확인할 수 없는 환경입니다. --yes 를 붙여 다시 실행하세요.", file=sys.stderr)
            return 1
        if input("닫을까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다.")
            return 1
    for s in targets:
        try:
            os.kill(s.pid, signal.SIGTERM)
            print(f"  닫음 {s.label}")
        except OSError as err:
            print(f"  실패 {s.label}: {err}")
    if any(s.is_self for s in sessions):
        print("이 세션은 직접 종료하세요(/exit).")
    return 0


# ───────────────────────────── pop ─────────────────────────────

def pick(records: list[dict]) -> list[dict]:
    default = {i for i, r in enumerate(records) if r["verdict"] in ("block", "resume")}
    for i, r in enumerate(records):
        mark = "x" if i in default else " "
        icon = VERDICTS.get(r["verdict"], ("?",))[0]
        print(f"  [{mark}] {i + 1:>2} {icon} {r['name'] or r['session_id'][:8]}  "
              f"{one_line(r['title'], 44)}  {Ansi.dim(short_path(r['cwd']))}")
        reasons = r.get("blockers", []) + r.get("pending", [])
        if r.get("git", {}).get("branch"):
            reasons.append(f"브랜치 {r['git']['branch']}")
        for line in ([" · ".join(reasons)] if reasons else []) + summary_lines(r):
            print(f"         {Ansi.dim(line)}")
    if not sys.stdin.isatty():
        print("\n번호를 고를 수 없는 환경입니다. --all 이나 --only 를 쓰세요.")
        return []
    ans = input("\n열 번호 (예: 1 3 5 · a=전부 · Enter=체크된 것 · q=취소): ").strip().lower()
    if ans == "q":
        return []
    if ans == "a":
        return records
    if not ans:
        return [r for i, r in enumerate(records) if i in default]
    nums = {int(x) - 1 for x in re.findall(r"\d+", ans)}
    return [r for i, r in enumerate(records) if i in nums]


class Launcher:
    """다시 여는 방법 하나. 기본은 명령만 출력한다."""
    name = "print"

    def available(self) -> bool:
        return True

    def open(self, records: list[dict], dry: bool) -> None:
        for r in records:
            print(resume_cmd(r))


def rec_label(r: dict) -> str:
    return f"{r['name'] or r['session_id'][:8]}  {one_line(r['title'], 40)}"


class CmuxLauncher(Launcher):
    """원래 cmux 워크스페이스 이름별로 묶어 탭을 원래 순서대로 다시 연다."""
    name = "cmux"

    def available(self) -> bool:
        return cmux("workspace", "list", timeout=3) is not None

    def open(self, records: list[dict], dry: bool) -> None:
        existing = {}
        for line in (cmux("workspace", "list") or "").splitlines():
            m = re.match(r"\s*\*?\s*(workspace:\d+)\s+(.*?)(\s+\[selected\])?$", line)
            if m:
                existing.setdefault(m.group(2).strip(), m.group(1))
        groups: dict[str, list[dict]] = {}
        for r in sorted(records, key=lambda r: (r["where"].get("ws_index", 99), r["where"].get("pane", 0),
                                                r["where"].get("tab", 0))):
            w = r["where"]
            title = w.get("workspace") if w.get("kind") == "cmux" else "ccs pop"
            groups.setdefault(title or "ccs pop", []).append(r)
        for title, items in groups.items():
            ref = existing.get(title)
            for r in items:
                cmd = resume_cmd(r)
                if ref:
                    argv = ["new-surface", "--workspace", ref, "--command", cmd]
                else:
                    argv = ["workspace", "create", "--name", title, "--cwd", r["cwd"], "--command", cmd]
                if dry:
                    print("cmux " + shlex.join(argv))
                    ref = ref or f"<{title}>"
                    continue
                out = cmux(*argv) or ""
                m = re.search(r"workspace:\d+", out)
                ref = ref or (m.group(0) if m else None)
                ok = "열림" if out.startswith("OK") else "실패"
                print(f"  {ok} cmux:{title} › {rec_label(r)}")


class TmuxLauncher(Launcher):
    """원래 tmux 세션 이름으로 연다. 그 세션이 없으면 같은 이름으로 새로 만든다(분리 상태)."""
    name = "tmux"

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def open(self, records: list[dict], dry: bool) -> None:
        made: set[str] = set()
        for r in sorted(records, key=lambda r: (r["where"].get("session", ""), r["where"].get("window", 0))):
            w = r["where"]
            sess = w.get("session") if w.get("kind") == "tmux" else "ccs"
            win = (w.get("window_name") if w.get("kind") == "tmux" else "") or (r["name"] or r["session_id"][:8])[:20]
            cmd = resume_cmd(r)
            exists = sess in made or (not dry and run(["tmux", "has-session", "-t", f"={sess}"]) is not None)
            if exists:
                argv = ["tmux", "new-window", "-t", f"{sess}:", "-n", win, "-c", r["cwd"], cmd]
            else:
                argv = ["tmux", "new-session", "-d", "-s", sess, "-n", win, "-c", r["cwd"], cmd]
            made.add(sess)
            if dry:
                print(shlex.join(argv))
                continue
            ok = run(argv) is not None
            print(f"  {'열림' if ok else '실패'} tmux:{sess} › {rec_label(r)}")
        if made and not dry and not os.environ.get("TMUX"):
            print("  붙으려면: " + " · ".join(f"tmux attach -t {x}" for x in sorted(made)))


class OrcaLauncher(Launcher):
    """원래 Orca 작업공간(worktree)에 탭을 연다. Orca 탭의 작업공간은 세션 cwd 와 다를 수 있어 저장해 둔 값을 쓴다."""
    name = "orca"

    def available(self) -> bool:
        b = orca_bin()
        return bool(b) and run([b, "terminal", "list", "--json", "--limit", "1"], timeout=5) is not None

    def open(self, records: list[dict], dry: bool) -> None:
        for r in records:
            w = r["where"]
            worktree = w.get("worktree") if w.get("kind") == "orca" else r["cwd"]
            argv = [orca_bin() or "orca", "terminal", "create", "--worktree", f"path:{worktree}",
                    "--title", r["title"] or r["name"], "--command", resume_cmd(r), "--json"]
            if dry:
                print(shlex.join(["orca", *argv[1:]]))
                continue
            out = run(argv, timeout=20)
            try:
                ok = bool(out) and json.loads(out).get("ok")
            except ValueError:
                ok = False
            print(f"  {'열림' if ok else '실패'} Orca:{os.path.basename(worktree)} › {rec_label(r)}")


LAUNCHERS = {"cmux": CmuxLauncher, "tmux": TmuxLauncher, "orca": OrcaLauncher, "print": Launcher}


def plan_launch(records: list[dict], via: str) -> list[tuple[Launcher, list[dict], list[dict]]]:
    """세션마다 원래 터미널로 보낸다. 원래 터미널을 쓸 수 없으면 쓸 수 있는 것으로 돌린다.
    반환: (launcher, 열 기록, 그중 원래 자리가 아닌 곳으로 가는 기록)."""
    if via != "auto":
        return [(LAUNCHERS[via](), records, [])]
    pool, ok = {k: cls() for k, cls in LAUNCHERS.items()}, {}

    def usable(kind: str) -> bool:
        if kind not in ok:
            ok[kind] = pool[kind].available()
        return ok[kind]

    fallback = next(k for k in ("cmux", "tmux", "print") if (k != "tmux" or os.environ.get("TMUX")) and usable(k))
    plan: dict[str, tuple[list[dict], list[dict]]] = {}
    for r in records:
        origin = r["where"].get("kind")
        dest = origin if origin in ("cmux", "tmux", "orca") and usable(origin) else fallback
        recs, moved = plan.setdefault(dest, ([], []))
        recs.append(r)
        if dest != origin:
            moved.append(r)
    return [(pool[k], recs, moved) for k, (recs, moved) in plan.items()]


def origin_label(r: dict) -> str:
    w = r["where"]
    return where_label(w) or "알 수 없는 터미널"


def cmd_pop(args) -> int:
    loaded = load_stash(args.stash_id)
    if not loaded:
        print("저장된 stash 가 없습니다. 먼저 `ccs stash`.", file=sys.stderr)
        return 1
    path, data = loaded
    live = live_session_ids()
    records = [r for r in data["sessions"] if r["session_id"] not in live]
    already = len(data["sessions"]) - len(records)
    print(f"stash {data['id']} {data.get('message', '')}".rstrip()
          + (f"  ({already}개는 이미 열려 있어 뺐습니다)" if already else ""))
    if not records:
        print("다시 열 세션이 없습니다.")
        return 0
    if args.all:
        chosen = records
    elif args.only:
        chosen = [r for r in records if r["verdict"] in args.only.split(",")]
    else:
        chosen = pick(records)
    gone = [r for r in chosen if not os.path.isdir(r["cwd"])]
    for r in gone:
        print(f"  {Ansi.yellow('건너뜀')} {r['name'] or r['session_id'][:8]}: 폴더가 없습니다 "
              f"({short_path(r['cwd'])}, worktree 를 지웠나요?). 직접 열려면: claude --resume {r['session_id']}")
    chosen = [r for r in chosen if r not in gone]
    if not chosen:
        return 0
    plan = plan_launch(chosen, args.via)
    for launcher, recs, moved in plan:
        print(f"\n{launcher.name} — {len(recs)}개")
        for r in moved:
            print(f"  {Ansi.yellow('자리 바뀜')} {r['name'] or r['session_id'][:8]}: 원래 {origin_label(r)} → {launcher.name}")
        launcher.open(recs, dry=args.dry_run)
    if not args.dry_run and any(l.name != "print" for l, _, _ in plan):
        data["popped"] = datetime.now().astimezone().isoformat(timespec="seconds")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


# ───────────────────────────── note ─────────────────────────────

def self_session_id() -> str | None:
    """이 명령을 부른 Claude 세션(조상 프로세스)의 id."""
    procs = ps_table()
    chain = set(ancestors(os.getpid(), procs))
    for reg in load_registry():
        p = procs.get(reg.get("pid"))
        if reg.get("pid") in chain and p and same_start(reg, p):
            return reg.get("sessionId")
    return None


def cmd_note(args) -> int:
    if args.ask:
        return ask_notes(args)
    if args.template:
        print(NOTE_TEMPLATE, end="")
        return 0
    sid = args.session or self_session_id()
    if not sid:
        print("어느 세션의 메모인지 모릅니다. Claude 세션 안에서 실행하거나 --session <id> 를 주세요.", file=sys.stderr)
        return 1
    text = sys.stdin.read()
    parsed = parse_note(text)
    if not parsed["goal"]:
        print("메모 첫 줄에 '# 목표' 가 없습니다. 양식: ccs note --template", file=sys.stderr)
        return 1
    path = save_note(sid, text)
    print(f"저장: {short_path(str(path))}")
    print(f"  목표: {parsed['goal']}\n  상태: {parsed['state'] or '(없음)'}\n  다음: {parsed['next'] or '(없음)'}")
    return 0


def note_route(s: Session) -> list[str] | None:
    """세션 탭에 '/ccs-note' 를 입력하는 명령. 입력할 길이 없는 터미널이면 None."""
    w = s.where
    if w.get("kind") == "cmux" and w.get("surface") and cmux_bin():
        return [cmux_bin(), "send", "--surface", w["surface"], "/ccs-note\\n"]
    if w.get("kind") == "tmux":
        return ["tmux", "send-keys", "-t", f"{w['session']}:{w['window']}.{w['pane']}", "/ccs-note", "Enter"]
    if w.get("kind") == "orca" and w.get("handle") and orca_bin():
        return [orca_bin(), "terminal", "send", "--terminal", w["handle"], "--text", "/ccs-note", "--enter"]
    return None


def ask_notes(args) -> int:
    sessions = [s for s in collect() if s.live and s.verdict != "job" and not s.is_self]
    if not args.all:
        sessions = [s for s in sessions if not s.note or s.note.get("stale")]
    busy = [s for s in sessions if s.status == "busy"]
    ready = [s for s in sessions if s.status != "busy"]
    routed = [(s, note_route(s)) for s in ready]
    targets = [(s, r) for s, r in routed if r]
    manual = [s for s, r in routed if not r]
    if not sessions:
        print("메모를 새로 받을 세션이 없습니다. 이미 있는 메모까지 새로 받으려면 --all.")
        return 0
    print(f"/ccs-note 를 보낼 세션 {len(targets)}개 (세션마다 Claude 응답 한 번 분량이 듭니다):")
    for s, _ in targets:
        print(f"  {VERDICTS[s.verdict][0]} {s.label}  {one_line(s.title, 44)}  {Ansi.dim(where_label(s.where))}")
    for s in busy:
        print(f"  {Ansi.dim('건너뜀')} {s.label} — 응답 중")
    for s in manual:
        print(f"  {Ansi.dim('직접 입력')} {s.label} — {where_label(s.where) or s.tty} 탭에서 /ccs-note")
    if not targets or args.dry_run:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("확인할 수 없는 환경입니다. --yes 를 붙여 다시 실행하세요.", file=sys.stderr)
            return 1
        if input("보낼까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다.")
            return 1
    for s, route in targets:
        # cmux send 는 입력 중인 글·열린 질문 창이 있으면 보내지 않고 실패한다.
        ok = run(route, env={**os.environ, "CMUX_QUIET": "1"}) is not None
        print(f"  {'보냄' if ok else '실패(입력 중인 글이나 질문 창이 있음)'} {s.label}")
    print("잠시 뒤 `ccs` 로 메모를 확인하세요.")
    return 0


# ───────────────────────────── main ─────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ccs", description="Claude Code 세션을 git 처럼: status / show / stash / pop")
    sub = ap.add_subparsers(dest="cmd")

    st = sub.add_parser("status", help="열린 세션을 모아 꺼도 되는지 판정")
    st.add_argument("--json", action="store_true")
    st.add_argument("-a", "--all", action="store_true", help="이미 종료된 세션 기록도 보기")
    st.add_argument("-v", "--verbose", action="store_true", help="모든 세션의 마지막 응답 보기")

    sh = sub.add_parser("show", help="세션 하나 자세히 (이름·id 앞자리·제목 일부)")
    sh.add_argument("query")

    sa = sub.add_parser("stash", help="열린 세션 목록을 저장 (--close 로 닫기까지)")
    sa.add_argument("stash_cmd", nargs="?", choices=["list", "show"],
                    help="list: 저장 이력 · show [세션]: 저장해 둔 내용 보기(재부팅 뒤에도)")
    sa.add_argument("query", nargs="?", help="show: 이름·id 앞자리·제목 일부")
    sa.add_argument("--id", help="show: stash id (기본: 가장 최근)")
    sa.add_argument("-m", "--message")
    sa.add_argument("--close", action="store_true", help="저장 뒤 🟢·🟡 세션 닫기")
    sa.add_argument("--force", action="store_true", help="🔴 세션까지 닫기")
    sa.add_argument("-y", "--yes", action="store_true", help="닫기 확인 생략")

    po = sub.add_parser("pop", help="stash 한 세션을 원래 탭 위치로 다시 열기")
    po.add_argument("stash_id", nargs="?", help="기본: 가장 최근")
    po.add_argument("--all", action="store_true", help="고르지 않고 전부")
    po.add_argument("--only", help="판정으로 고르기, 예: block,resume")
    po.add_argument("--via", choices=["auto", *LAUNCHERS], default="auto", help="auto: 세션마다 원래 터미널로")
    po.add_argument("--dry-run", action="store_true", help="실행할 명령만 보기")

    no = sub.add_parser("note", help="인수인계 메모 저장(표준 입력) · --ask 로 열린 세션들에 /ccs-note 보내기")
    no.add_argument("--session", help="메모를 붙일 세션 id (기본: 이 명령을 부른 세션)")
    no.add_argument("--template", action="store_true", help="메모 양식 출력")
    no.add_argument("--ask", action="store_true", help="메모 없는 세션 탭에 /ccs-note 를 입력")
    no.add_argument("--all", action="store_true", help="--ask: 메모가 이미 있는 세션에도")
    no.add_argument("--dry-run", action="store_true", help="--ask: 보낼 대상만 보기")
    no.add_argument("-y", "--yes", action="store_true", help="--ask: 확인 생략")

    argv = list(sys.argv[1:] if argv is None else argv)
    # 하위 명령 없이 부르면 status: `ccs`, `ccs -v`, `ccs --json`
    if not argv or (argv[0] not in sub.choices and argv[0] not in ("-h", "--help")):
        argv.insert(0, "status")
    args = ap.parse_args(argv)
    commands = {"status": cmd_status, "show": cmd_show, "stash": cmd_stash, "pop": cmd_pop, "note": cmd_note}
    return commands[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
