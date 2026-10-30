import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccs  # noqa: E402


def line(**r) -> str:
    return json.dumps(r, ensure_ascii=False)


def assistant(*blocks, ts="2026-10-30T10:00:00Z", **extra):
    return line(type="assistant", timestamp=ts, message={"content": list(blocks)}, **extra)


def user(content, **extra):
    return line(type="user", timestamp="2026-10-30T10:00:00Z", message={"content": content}, **extra)


def text(t):
    return {"type": "text", "text": t}


def tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def index_of(*lines) -> dict:
    e = ccs.new_entry()
    for raw in lines:
        ccs.absorb(e, raw)
    return e


class AbsorbTest(unittest.TestCase):
    def test_title_prompt_and_last_text(self):
        e = index_of(
            line(type="ai-title", aiTitle="첫 제목"),
            user("고쳐줘"),
            assistant(text("고쳤습니다.")),
            line(type="ai-title", aiTitle="바뀐 제목"),
            line(type="last-prompt", lastPrompt="고쳐줘"),
        )
        self.assertEqual(e["title"], "바뀐 제목")
        self.assertEqual(e["last_prompt"], "고쳐줘")
        self.assertEqual(e["last_text"], "고쳤습니다.")
        self.assertEqual(e["last_role"], "assistant")

    def test_sidechain_and_meta_are_ignored(self):
        e = index_of(
            assistant(text("본 대화")),
            assistant(text("서브에이전트"), isSidechain=True),
            user("hook 주입", isMeta=True),
        )
        self.assertEqual(e["last_text"], "본 대화")
        self.assertEqual(e["last_role"], "assistant")

    def test_touched_files_and_commit(self):
        e = index_of(
            assistant(tool("Edit", file_path="/r/a.py"), tool("Write", file_path="/r/b.py")),
            assistant(tool("Bash", command="git add -A && git commit -m x")),
        )
        self.assertEqual(list(e["touched"]), ["/r/a.py", "/r/b.py"])
        self.assertTrue(e["committed"])

    def test_wakeup_and_stop(self):
        e = index_of(assistant(tool("ScheduleWakeup", delaySeconds=600), ts="2026-10-30T10:00:00Z"))
        self.assertAlmostEqual(e["wakeup_at"], ccs.parse_ts("2026-10-30T10:10:00Z"))
        ccs.absorb(e, assistant(tool("ScheduleWakeup", stop=True)))
        self.assertIsNone(e["wakeup_at"])

    def test_tool_result_marks_role(self):
        e = index_of(assistant(tool("Read", file_path="/x")), user([{"type": "tool_result", "content": "..."}]))
        self.assertEqual(e["last_role"], "tool_result")
        self.assertEqual(e["last_user_ts"], "")  # 도구 결과는 사용자가 말을 건 것이 아니다

    def test_broken_line_is_skipped(self):
        e = index_of("{not json", assistant(text("ok")))
        self.assertEqual(e["last_text"], "ok")


class IncrementalIndexTest(unittest.TestCase):
    def test_reads_only_appended_part(self):
        with tempfile.TemporaryDirectory() as d:
            ccs.CCS_HOME = Path(d)
            f = Path(d) / "s.jsonl"
            f.write_text(assistant(text("하나")) + "\n" + '{"type":"assist')  # 끝 줄은 쓰는 중
            idx = ccs.TranscriptIndex()
            e = idx.get(f)
            self.assertEqual(e["last_text"], "하나")
            first = e["offset"]
            with open(f, "a") as fh:
                fh.write('ant"}\n' + assistant(text("둘")) + "\n")
            e = idx.get(f)
            self.assertEqual(e["last_text"], "둘")
            self.assertGreater(e["offset"], first)
            idx.save()
            self.assertEqual(ccs.TranscriptIndex().get(f)["last_text"], "둘")


class ClassifyTest(unittest.TestCase):
    def session(self, **kw):
        base = dict(session_id="s", cwd="/r", live=True, status="idle", last_role="assistant", last_text="끝났습니다.")
        base.update(kw)
        return ccs.Session(**base)

    def judge(self, s, e=None):
        ccs.classify(s, e or ccs.new_entry(), time.time())
        return s.verdict

    def test_done(self):
        self.assertEqual(self.judge(self.session()), "done")

    def test_busy_blocks(self):
        self.assertEqual(self.judge(self.session(status="busy")), "block")

    def test_background_blocks(self):
        s = self.session(bg=["npm run dev"])
        self.assertEqual(self.judge(s), "block")
        self.assertIn("백그라운드", s.blockers[0])

    def test_future_wakeup_blocks(self):
        e = ccs.new_entry()
        e["wakeup_at"] = time.time() + 600
        self.assertEqual(self.judge(self.session(), e), "block")

    def test_past_wakeup_is_ignored(self):
        e = ccs.new_entry()
        e["wakeup_at"] = time.time() - 600
        self.assertEqual(self.judge(self.session(), e), "done")

    def test_question_means_resume(self):
        self.assertEqual(self.judge(self.session(last_text="1번부터 만들어 볼까요?")), "resume")

    def test_interrupted_means_resume(self):
        self.assertEqual(self.judge(self.session(last_role="user")), "resume")

    def test_own_dirty_files_mean_resume_but_others_are_notes(self):
        git = {"top": "/r", "branch": "main", "head": "abc", "dirty": ["/r/a.py", "/r/z.py"], "unpushed": 0}
        s = self.session(git=git, mine_dirty=["/r/a.py"])
        self.assertEqual(self.judge(s), "resume")
        self.assertEqual(s.notes, ["repo에 다른 미커밋 1개"])

    def test_unpushed_counts_only_if_session_committed(self):
        git = {"top": "/r", "branch": "feat", "head": "abc", "dirty": [], "unpushed": 2}
        self.assertEqual(self.judge(self.session(git=dict(git))), "done")
        e = ccs.new_entry()
        e["committed"] = True
        self.assertEqual(self.judge(self.session(git=dict(git)), e), "resume")

    def test_headless_job(self):
        self.assertEqual(self.judge(self.session(headless=True, status="busy")), "job")

    def test_dead_is_stale(self):
        self.assertEqual(self.judge(self.session(live=False)), "stale")


class NoteTest(unittest.TestCase):
    NOTE = """# 외부 API 해지 요청 재송신 처리
상태: 대기: 상대 회사 회신
## 지금 상태
- 재송신 요청함
## 다음에 열면 할 것
1. 회신 메일 확인 후 상태 조회
- 두 번째 일
"""

    def test_parse_note(self):
        self.assertEqual(ccs.parse_note(self.NOTE),
                         {"goal": "외부 API 해지 요청 재송신 처리", "state": "대기: 상대 회사 회신", "next": "회신 메일 확인 후 상태 조회"})

    def test_state_kinds(self):
        self.assertEqual(ccs.note_state_kind("대기: 리뷰"), "waiting")
        self.assertEqual(ccs.note_state_kind("끝남"), "done")
        self.assertEqual(ccs.note_state_kind("진행 중"), "doing")

    def test_save_and_latest(self):
        with tempfile.TemporaryDirectory() as d:
            ccs.CCS_HOME = Path(d)
            ccs.save_note("sid", self.NOTE)
            n = ccs.latest_note("sid")
            self.assertEqual(n["state"], "대기: 상대 회사 회신")
            self.assertEqual(ccs.latest_note("other"), {})

    def judge(self, note, **kw):
        base = dict(session_id="s", cwd="/r", live=True, status="idle", last_role="assistant",
                    last_text="회신 초안입니다.", note=note)
        base.update(kw)
        s = ccs.Session(**base)
        ccs.classify(s, ccs.new_entry(), time.time())
        return s

    def test_waiting_note_turns_clean_looking_session_yellow(self):
        s = self.judge({"state": "대기: 상대 회사 회신", "stale": False})
        self.assertEqual(s.verdict, "resume")
        self.assertEqual(s.pending, ["메모: 대기: 상대 회사 회신"])

    def test_done_note_overrides_question_heuristic(self):
        self.assertEqual(self.judge({"state": "끝남", "stale": False}, last_text="더 할까요?").verdict, "done")

    def test_stale_note_falls_back_to_heuristics(self):
        self.assertEqual(self.judge({"state": "끝남", "stale": True}, last_text="더 할까요?").verdict, "resume")

    def test_summary_prefers_note(self):
        rec = {"note": {"goal": "목표", "next": "다음 일", "at": time.time()}, "last_prompt": "요청", "last_text": "응답"}
        lines = ccs.summary_lines(rec)
        self.assertTrue(lines[0].startswith("메모(방금): 목표"))
        self.assertEqual(lines[1], "다음: 다음 일")
        self.assertEqual(ccs.summary_lines({"last_prompt": "요청", "last_text": "앞\n\n결론"}),
                         ["요청: 요청", "마지막: 결론"])


class LaunchPlanTest(unittest.TestCase):
    def rec(self, kind, **w):
        return {"session_id": kind, "name": kind, "title": "", "cwd": "/tmp", "where": {"kind": kind, **w}}

    def plan(self, records, usable):
        saved = {k: cls.available for k, cls in ccs.LAUNCHERS.items()}
        try:
            for k, cls in ccs.LAUNCHERS.items():
                cls.available = (lambda ok: lambda self: ok)(k in usable or k == "print")
            return {l.name: ([r["name"] for r in recs], [r["name"] for r in moved])
                    for l, recs, moved in ccs.plan_launch(records, "auto")}
        finally:
            for k, cls in ccs.LAUNCHERS.items():
                cls.available = saved[k]

    def test_each_goes_home(self):
        recs = [self.rec("cmux"), self.rec("tmux"), self.rec("orca"), self.rec("app", app="iTerm2")]
        self.assertEqual(self.plan(recs, {"cmux", "tmux", "orca"}),
                         {"cmux": (["cmux", "app"], ["app"]), "tmux": (["tmux"], []), "orca": (["orca"], [])})

    def test_missing_terminal_falls_back_to_cmux(self):
        recs = [self.rec("orca"), self.rec("tmux")]
        self.assertEqual(self.plan(recs, {"cmux", "tmux"}),
                         {"cmux": (["orca"], ["orca"]), "tmux": (["tmux"], [])})

    def test_nothing_running_prints(self):
        self.assertEqual(self.plan([self.rec("cmux")], set()), {"print": (["cmux"], ["cmux"])})

    def test_orca_where_needs_unique_title(self):
        orca = {"A": [{"handle": "h1", "worktree": "/w"}], "B": [{"handle": "h2", "worktree": "/w"}] * 2}
        self.assertEqual(ccs.orca_where("A", orca), {"kind": "orca", "handle": "h1", "worktree": "/w"})
        self.assertIsNone(ccs.orca_where("B", orca))
        self.assertIsNone(ccs.orca_where("", orca))


class HelpersTest(unittest.TestCase):
    def test_resume_argv_keeps_safe_flags_only(self):
        argv = ("claude --settings /u/.cmuxterm/s.json --mcp-config={\"a\":{\"b\":\"/x y\"}} "
                "--dangerously-skip-permissions --model opus --resume old fix it").split()
        self.assertEqual(ccs.resume_argv(argv, "NEW"),
                         ["claude", "--dangerously-skip-permissions", "--model", "opus", "--resume", "NEW"])

    def test_resume_argv_drops_print_mode(self):
        argv = "claude -p /nightly-report --session-id x --permission-mode auto".split()
        self.assertEqual(ccs.resume_argv(argv, "N"), ["claude", "--permission-mode", "auto", "--resume", "N"])

    def test_porcelain_z_with_rename(self):
        out = " M a.py\0R  new.py\0old.py\0?? dir/\0"
        self.assertEqual(ccs.parse_porcelain_z(out, "/r"), ["/r/a.py", "/r/new.py", "/r/dir/"])

    def test_touched_dirty_matches_untracked_dir(self):
        self.assertEqual(ccs.touched_dirty(["/r/dir/x.py", "/r/clean.py"], ["/r/dir/"]), ["/r/dir/x.py"])

    def test_bash_payload(self):
        cmd = ("/bin/zsh -c source /u/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null || true && "
               "export X='1' && eval 'npm run dev > /tmp/log 2>&1' < /dev/null && pwd -P >| /tmp/cwd")
        self.assertEqual(ccs.bash_payload(cmd), "npm run dev > /tmp/log 2>&1")

    def test_prog_label(self):
        self.assertEqual(ccs.prog_label("/opt/Python /v/bin/job-runner start"), "job-runner start")

    def test_last_paragraph_skips_code(self):
        self.assertEqual(ccs.last_paragraph("앞\n\n결론입니다?\n\n```\ncode\n```"), "결론입니다?")

    def test_same_start_normalizes_spaces(self):
        p = ccs.Proc(1, 0, "ttys001", "Fri Oct 3 13:15:46 2026", "claude")
        self.assertTrue(ccs.same_start({"procStart": "Fri Oct  3 13:15:46 2026"}, p))
        self.assertFalse(ccs.same_start({"procStart": "Fri Oct  3 13:15:47 2026"}, p))


if __name__ == "__main__":
    unittest.main()
