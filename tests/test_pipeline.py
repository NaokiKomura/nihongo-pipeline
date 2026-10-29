"""jpipe と jp_hook の結合テスト。python3 -m unittest discover tests で実行する。

一時フォルダの中だけで動く。yomiyasu は実物を使い (NIHONGO_YOMIYASU_DIR か既定の場所)、見つからなければ飛ばす。
natural-japanese・uv・別のモデルは tests/stubs の代わりのスクリプトを使う。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills/nihongo-pipeline/scripts"
STUBS = Path(__file__).resolve().parent / "stubs"
sys.path.insert(0, str(SCRIPTS))
import jpcore  # noqa: E402

YOMIYASU = jpcore.find_yomiyasu(jpcore.DEFAULTS)

SOURCE = """# 社内勉強会の案内

結論から言うと、この勉強会は**全員に**おすすめです。**新人**にも**ベテラン**にも**役立ちます**。

この勉強会は、参加者の業務効率を劇的に向上させることができると言えるでしょう。

**日時**：11月12日 15時から16時まで。

受付と完了確認を分離する。申し込みは前日までに済ませてください。

当日は、前半で事例を紹介し、後半で参加者どうしの意見交換を行います。資料は前日にお送りします。会場は本社の3階の会議室で、オンラインでも参加できます。質問は申し込みのときに書いてください。
"""
POLISHED = """# 社内勉強会の案内

この勉強会に出ると、参加者の業務の効率は大きく上がるでしょう。

日時は11月12日の15時から16時までです。

申し込みを受け付けたら、その場で受付済みと返し、参加の確定はあとで連絡します。申し込みは前日までに済ませてください。

当日は、前半で事例を紹介し、後半で参加者どうしの意見交換を行います。資料は前日にお送りします。会場は本社の3階の会議室で、オンラインでも参加できます。質問は申し込みのときに書いてください。
"""


@unittest.skipUnless(YOMIYASU, "yomiyasu が見つからない")
class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jpipe-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = dict(
            HOME=str(self.home), PATH="/usr/bin:/bin", LANG="ja_JP.UTF-8",
            NIHONGO_PIPELINE_HOME=str(self.tmp / "state"),
            NIHONGO_PIPELINE_CONFIG=str(self.tmp / "config.json"),
            NIHONGO_YOMIYASU_DIR=str(YOMIYASU),
            NIHONGO_NJ_DIR=str(STUBS / "natural-japanese"),
            NIHONGO_UV=str(STUBS / "uv"),
        )
        self.src = self.tmp / "work" / "draft.md"
        self.src.parent.mkdir()
        self.src.write_text(SOURCE, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def jpipe(self, *args, env=None, ok=(0,)):
        r = subprocess.run([sys.executable, str(SCRIPTS / "jpipe.py")] + list(args), capture_output=True,
                           text=True, env=dict(self.env, **(env or {})))
        self.assertIn(r.returncode, ok, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def hook(self, mode, data, env=None):
        r = subprocess.run([sys.executable, str(SCRIPTS / "jp_hook.py"), mode], input=json.dumps(data),
                           capture_output=True, text=True, env=dict(self.env, **(env or {})))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def state(self):
        rid = (self.tmp / "state/current").read_text().strip()
        return json.loads((self.tmp / "state/runs" / rid / "state.json").read_text())

    def act_as_stage(self, text, questions="なし", env=None):
        """jp-stage の代わりに、brief の指示どおり出力・検査・報告を行う。"""
        st = self.state()
        s = next(x for x in st["stages"] if x["status"] == "pending")
        brief = Path(s["brief"]).read_text(encoding="utf-8")
        Path(s["output"]).write_text(text, encoding="utf-8")
        m = re.search(r"^YOMIYASU_STATE_DIR=(\S+) (python3 .*)$", brief, re.M)
        if m:
            r = subprocess.run(m.group(2), shell=True, capture_output=True, text=True,
                               env=dict(self.env, YOMIYASU_STATE_DIR=m.group(1)))
            self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        Path(s["report"]).write_text("### 変えたところ\n- 直した\n\n### 書き手に確かめたい点\n- {}\n".format(questions),
                                     encoding="utf-8")
        return brief

    # ---------- 工程 ----------

    def test_revise_without_any_external_model(self):
        """agy も gemini も codex もない環境で、エラーにならず最後まで進む。"""
        out = self.jpipe("init", "--route", "revise", "--src", str(self.src))
        self.assertIn("polish", out)
        brief = self.act_as_stage(POLISHED, questions="参加の確定の連絡は誰が出すか")
        self.assertIn("yomiyasu", brief)
        self.assertIn("SKILL.md", brief)
        out = self.jpipe("done")
        self.assertIn("check", out)
        out = self.jpipe("run")  # check
        out = self.jpipe("run")  # review: 別のモデルがないので省く
        self.assertIn("省きました", out)
        st = self.state()
        review = next(x for x in st["stages"] if x["name"] == "review")
        self.assertEqual(review["status"], "skipped")
        if any(x["name"] == "fix" for x in st["stages"]):
            self.act_as_stage(POLISHED.replace("大きく上がるでしょう", "大きく上がるでしょう (見込み)"))
            self.jpipe("done")
            self.jpipe("run")  # recheck
        out = self.jpipe("next")
        self.assertIn("参加の確定の連絡は誰が出すか", out)
        out = self.jpipe("finish", "--out", str(self.src))
        self.assertIn("照合: 合った", out)
        self.assertIn("別のモデルによる判定を省いた", out)
        self.assertTrue(jpcore_certified(self.env, self.src))

    def test_done_refuses_missing_output_and_unverified_final(self):
        self.jpipe("init", "--route", "revise", "--src", str(self.src), "--no-review")
        out = self.jpipe("done", ok=(1,))
        self.assertIn("ないか空", out)
        st = self.state()
        s = st["stages"][0]
        Path(s["output"]).write_text(POLISHED, encoding="utf-8")
        Path(s["report"]).write_text("### 書き手に確かめたい点\nなし\n", encoding="utf-8")
        out = self.jpipe("done", ok=(1,))  # 検査を実行していない
        self.assertIn("検査の記録と合いません", out)

    def test_review_drops_quotes_not_in_text(self):
        reviewer = self.tmp / "reviewer.sh"
        reviewer.write_text("#!/bin/sh\ncat <<'EOF'\n```json\n"
                            '[{"line": 3, "quote": "業務の効率は大きく上がる", "type": "過剰圧縮型", "reason": "r"},'
                            ' {"line": 9, "quote": "本文にない言葉", "type": "意味破綻型", "reason": "r"}]\n```\nEOF\n')
        reviewer.chmod(0o755)
        env = {"NIHONGO_REVIEWER": str(reviewer)}
        self.jpipe("init", "--route", "revise", "--src", str(self.src), env=env)
        self.act_as_stage(POLISHED)
        self.jpipe("done", env=env)
        self.jpipe("run", env=env)
        out = self.jpipe("run", env=env)
        self.assertIn("別のモデルの指摘: 1 件", out)
        st = self.state()
        self.assertTrue(any(x["name"] == "fix" for x in st["stages"]))

    def test_failing_external_model_is_skipped(self):
        bad = self.tmp / "bad.sh"
        bad.write_text("#!/bin/sh\necho 'auth error' >&2\nexit 3\n")
        bad.chmod(0o755)
        env = {"NIHONGO_REVIEWER": str(bad)}
        self.jpipe("init", "--route", "revise", "--src", str(self.src), env=env)
        self.act_as_stage(POLISHED)
        self.jpipe("done", env=env)
        self.jpipe("run", env=env)
        out = self.jpipe("run", env=env)
        self.assertIn("省きました", out)
        self.assertIn("auth error", out)

    def test_external_draft_falls_back_to_claude(self):
        bad = self.tmp / "bad.sh"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        facts = self.tmp / "work/facts.md"
        facts.write_text("勉強会は11月12日15時から。申し込みは前日まで。\n", encoding="utf-8")
        env = {"NIHONGO_REVIEWER": str(bad)}
        self.jpipe("init", "--route", "new", "--material", str(facts), "--draft", "external", env=env)
        self.act_as_stage("## 主メッセージ\n勉強会の日時と申し込みの期限を知らせる。\n")
        self.jpipe("done", env=env)
        out = self.jpipe("run", env=env)
        self.assertIn("Claude の下書きに切り替えます", out)
        st = self.state()
        self.assertEqual([x["name"] for x in st["stages"]][:3], ["design", "draft_external", "draft"])

    def test_init_without_external_model_switches_draft(self):
        facts = self.tmp / "work/facts.md"
        facts.write_text("事実のメモ\n", encoding="utf-8")
        out = self.jpipe("init", "--route", "new", "--material", str(facts), "--draft", "external")
        self.assertIn("下書きは Claude が担当します", out)
        self.assertNotIn("draft_external", [x["name"] for x in self.state()["stages"]])

    def test_finish_refuses_changed_destination(self):
        self.jpipe("init", "--route", "revise", "--src", str(self.src), "--no-review")
        self.act_as_stage(POLISHED)
        self.jpipe("done")
        self.jpipe("run")
        st = self.state()
        if any(x["name"] == "fix" for x in st["stages"]):
            self.act_as_stage(POLISHED)
            self.jpipe("done")
            self.jpipe("run")
        self.src.write_text(SOURCE + "\n利用者が途中で足した行。\n", encoding="utf-8")
        out = self.jpipe("finish", "--out", str(self.src), ok=(2,))
        self.assertIn("--force", out)

    # ---------- certify と hook ----------

    def test_certify_and_accept(self):
        out = self.jpipe("certify", str(self.src), ok=(1,))
        self.assertIn("指摘", out)
        out = self.jpipe("certify", str(self.src), "--accept", "テスト用の原稿")
        self.assertIn("理由つきで受け入れ", out)

    def test_hooks_block_unchecked_document_once(self):
        sid = "t1"
        r = self.hook("prompt", {"session_id": sid, "prompt": "来週の勉強会の案内文書を書いて"})
        ctx = r["hookSpecificOutput"]["additionalContext"]
        self.assertIn("init --route new", ctx)
        self.assertIn("返答の日本語の決まり", ctx)
        self.hook("post-tool", {"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(self.src)}})
        r = self.hook("stop", {"session_id": sid})
        self.assertEqual(r["decision"], "block")
        self.assertIn(str(self.src.resolve()), r["reason"])
        self.assertIsNone(self.hook("stop", {"session_id": sid, "stop_hook_active": True}))
        # 次のターンで検査を通せば止めない
        self.hook("prompt", {"session_id": sid, "prompt": "続けて"})
        self.hook("post-tool", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(self.src)}})
        self.jpipe("certify", str(self.src), "--accept", "テスト")
        self.assertIsNone(self.hook("stop", {"session_id": sid}))

    def test_hooks_skip_excluded_and_nopipe(self):
        sid = "t2"
        skill = self.tmp / "work/.claude/skills/x/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(SOURCE * 3, encoding="utf-8")
        self.hook("post-tool", {"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(skill)}})
        self.assertIsNone(self.hook("stop", {"session_id": sid}))
        self.assertIsNone(self.hook("prompt", {"session_id": sid, "prompt": "資料を書いて #nopipe"}))
        self.hook("post-tool", {"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(self.src)}})
        self.assertIsNone(self.hook("stop", {"session_id": sid}))

    def test_chat_feedback_reaches_next_prompt(self):
        sid = "t3"
        reply = ("結論から言うと、**この方法**が一番よい。\n\n- **速い**\n- **安い**\n- **確実**\n\n"
                 + "この手法は文章の質を劇的に底上げし、読み手の心に刺さる体験を生み出します。" * 10)
        self.assertIsNone(self.hook("stop", {"session_id": sid, "last_assistant_message": reply}))
        r = self.hook("prompt", {"session_id": sid, "prompt": "ありがとう、次はどうする？"})
        self.assertIn("前の返答を lint にかけた", r["hookSpecificOutput"]["additionalContext"])
        r = self.hook("prompt", {"session_id": sid, "prompt": "了解です"})
        self.assertNotIn("前の返答を lint", r["hookSpecificOutput"]["additionalContext"])

    def test_chat_block_mode(self):
        (self.tmp / "config.json").write_text(json.dumps({"chat_mode": "block", "chat_block_warn": 2}))
        reply = "結論から言うと、**この方法**が一番よい。**速い**。**安い**。**確実**。" + "読み手の心に刺さる体験を生み出します。" * 16
        r = self.hook("stop", {"session_id": "t4", "last_assistant_message": reply})
        self.assertEqual(r["decision"], "block")

    def test_hook_never_raises(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "jp_hook.py"), "stop"], input="not json",
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.tmp / "state/hook-errors.log").is_file())

    def test_english_prompt_gets_nothing(self):
        self.assertIsNone(self.hook("prompt", {"session_id": "t5", "prompt": "write a README"}))


def jpcore_certified(env, path):
    code = "import sys; sys.path.insert(0, {!r}); import jpcore; print(jpcore.certified({!r}))".format(str(SCRIPTS), str(path))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    return r.stdout.strip() == "True"


if __name__ == "__main__":
    unittest.main()
