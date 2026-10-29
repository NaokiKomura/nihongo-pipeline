#!/usr/bin/env python3
"""jpipe — 日本語の文書を工程に分けて仕上げるハーネス。

エージェントは `init` で工程を組み、`next` が示す操作を1つずつ行い、`done` か `run` で次へ進む。
工程の順序、受け渡すファイル、通過の条件、差し戻しの回数はこのスクリプトが決める。

  jpipe doctor                          部品と別モデルの有無を表示する
  jpipe setup [--link]                  見つからない部品を取ってくる
  jpipe init --route new --material facts.md [--doc-type article] [--reader general] [--style desu]
  jpipe init --route revise --src draft.md
  jpipe init --route explain --src slide-text.md
  jpipe next   [--run ID]               次の工程と、そのための操作を表示する
  jpipe done   [--run ID]               サブエージェントの工程を終えたことを記録する
  jpipe run    [--run ID]               スクリプトの工程 (検査・別モデル) を実行する
  jpipe finish [--run ID] --out PATH    最終稿を書き出し、検査済みとして記録する
  jpipe certify FILE [--accept 理由]     書いたファイルを lint にかけ、通れば検査済みにする
  jpipe status [--run ID]

終了コード: 0 成功、1 条件に当たった (指摘あり・照合の不一致)、2 使い方か入力の誤り。
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jpcore as C  # noqa: E402

RUNS = C.STATE_HOME / "runs"
CURRENT = C.STATE_HOME / "current"

LABELS = {
    "design": "設計 (読み手・主メッセージ・見出し)",
    "draft": "下書き",
    "draft_external": "下書き (別のモデル)",
    "explain": "説明の組み立て (口頭で説明してから短くする)",
    "polish": "推敲 (yomiyasu)",
    "fix": "指摘への対応 (yomiyasu)",
    "check": "機械検査",
    "recheck": "機械検査 (対応後)",
    "review": "別のモデルによる判定",
    "confirm": "書き手への確認と書き出し",
}
STYLES = {"desu": "です・ます調", "dearu": "である調", "keep": "入力の文体を保つ"}


class UsageError(Exception):
    pass


# ---------- 状態 ----------

def run_dir(run_id):
    return RUNS / run_id


def load_state(run_id=None):
    if not run_id:
        try:
            run_id = CURRENT.read_text(encoding="utf-8").strip()
        except OSError:
            raise UsageError("進行中の工程がありません。先に jpipe init を実行してください。")
    p = run_dir(run_id) / "state.json"
    if not p.is_file():
        raise UsageError("工程 {} が見つかりません。jpipe status で一覧を確かめてください。".format(run_id))
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(st):
    st["updated"] = C.now()
    C._save_json(run_dir(st["id"]) / "state.json", st)


def current_stage(st):
    for s in st["stages"]:
        if s["status"] == "pending":
            return s
    return None


def stage_index(st, s):
    return st["stages"].index(s) + 1, len(st["stages"])


def last_text_output(st, before=None):
    """before より前で、本文を書き出した最後の工程の出力。"""
    out = st["files"].get("source") or st["files"].get("facts")
    for s in st["stages"]:
        if s is before:
            break
        if s["status"] == "done" and s.get("produces_text"):
            out = s["output"]
    return out


def p(st, name):
    return str(run_dir(st["id"]) / name)


# ---------- init ----------

def build_plan(st, cfg, args):
    route = st["route"]
    stages = []

    def agent(name, num, skill, produces_text=True):
        stages.append({"name": name, "kind": "agent", "skill": skill, "num": num,
                       "output": p(st, "{}_{}.md".format(num, name)),
                       "report": p(st, "{}_{}.report.md".format(num, name)),
                       "brief": p(st, "{}_{}.brief.md".format(num, name)),
                       "produces_text": produces_text, "status": "pending"})

    def script(name, num, ext="json"):
        stages.append({"name": name, "kind": "script", "num": num,
                       "output": p(st, "{}_{}.{}".format(num, name, ext)),
                       "produces_text": name == "draft_external", "status": "pending"})

    if route == "new":
        agent("design", "10", "natural-japanese", produces_text=False)
        if args.draft == "external":
            script("draft_external", "20", "md")
        else:
            agent("draft", "20", "natural-japanese")
        if args.explain:
            agent("explain", "30", "explain")
        agent("polish", "40", "yomiyasu")
    elif route == "revise":
        if args.explain:
            agent("explain", "30", "explain")
        agent("polish", "40", "yomiyasu")
    elif route == "explain":
        agent("explain", "30", "explain")
        agent("polish", "40", "yomiyasu")
    script("check", "50")
    if args.review:
        script("review", "60")
    stages.append({"name": "confirm", "kind": "human", "num": "90", "status": "pending"})
    return stages


def cmd_init(args, cfg):
    if args.route == "new" and not args.material:
        raise UsageError("--route new には --material (事実のメモ。本人の言葉か、確かめた資料) が必要です。")
    if args.route in ("revise", "explain") and not args.src:
        raise UsageError("--route {} には --src (直す原稿) が必要です。".format(args.route))
    for f in [args.src] + (args.material or []):
        if f and not Path(f).is_file():
            raise UsageError("ファイルがありません: {}".format(f))
    if args.src and Path(args.src).suffix.lower() in (".html", ".htm"):
        raise UsageError("HTML はそのまま工程に通せません。文言を .md に抜き出して --route explain に渡すか、jpipe certify で検査してください。")
    if not C.find_yomiyasu(cfg):
        raise UsageError("yomiyasu が見つかりません。jpipe setup を実行するか、設定の yomiyasu_dir を指定してください。")
    notes = []
    if args.route == "new" and not C.find_natural_japanese(cfg):
        raise UsageError("natural-japanese が見つかりません。jpipe setup を実行してください (設計と下書きの工程で使います)。")
    if args.draft == "external" and not C.reviewer_command(cfg):
        args.draft = "claude"
        notes.append("別のモデル (agy / gemini / codex) が見つからないので、下書きは Claude が担当します。")
    review = args.review
    if review and not C.reviewer_command(cfg):
        notes.append("別のモデルが見つからないので、判定の工程 (review) は実行時に省きます。")

    run_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    d = run_dir(run_id)
    d.mkdir(parents=True)
    st = {"id": run_id, "route": args.route, "created": C.now(), "loops": 0, "finished": False,
          "doc_type": args.doc_type or (C.guess_doc_type(args.src) if args.src else "article"),
          "reader": args.reader, "style": args.style, "purpose": args.purpose or "",
          "files": {}, "notes": notes}
    if args.src:
        shutil.copyfile(args.src, d / "00_source.md")
        st["files"]["source"] = str(d / "00_source.md")
        st["files"]["src_path"] = str(Path(args.src).resolve())
        st["files"]["src_sha256"] = C.sha256_file(args.src)
    if args.material:
        with open(d / "00_facts.md", "w", encoding="utf-8") as out:
            for m in args.material:
                out.write("<!-- 資料: {} -->\n".format(Path(m).name))
                out.write(Path(m).read_text(encoding="utf-8"))
                out.write("\n\n")
        st["files"]["facts"] = str(d / "00_facts.md")
    st["stages"] = build_plan(st, cfg, args)
    save_state(st)
    CURRENT.write_text(run_id, encoding="utf-8")
    print("工程を組みました: run {} (route {}, 文書の種類 {})".format(run_id, args.route, st["doc_type"]))
    print("工程: " + " → ".join(s["name"] for s in st["stages"]))
    for n in notes:
        print("注意: " + n)
    print()
    show_next(st, cfg)


# ---------- brief ----------

def skill_path(cfg, skill):
    if skill == "yomiyasu":
        return C.find_yomiyasu(cfg) / "SKILL.md"
    if skill == "natural-japanese":
        return C.find_natural_japanese(cfg) / "SKILL.md"
    return C.explain_skill(cfg)


def render(template, **kw):
    text = (C.TEMPLATES / template).read_text(encoding="utf-8")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(kw.get(m.group(1), "")), text)


def write_brief(st, cfg, s):
    inp = last_text_output(st, before=s)
    facts = st["files"].get("facts")
    design = next((x["output"] for x in st["stages"] if x["name"] == "design" and x["status"] == "done"), None)
    extra = []
    if s["name"] in ("draft",) and design:
        extra.append("- 設計メモ: {} (見出しと主メッセージはこれに従う)".format(design))
    if s["name"] == "fix":
        extra.append("- 指摘の一覧: {} (指摘ごとに、直すか残すかを決める)".format(s["findings"]))
    if s["name"] in ("draft", "explain", "fix", "polish"):
        extra.append("- AI っぽい表現の2つの型: {}".format(C.REFERENCES / "slop-types.md"))
    yomi_state = p(st, "yomiyasu-state")
    body = render(
        "stage-{}.md".format("polish" if s["name"] == "fix" else s["name"]),
        run=st["id"], label=LABELS[s["name"]], skill=skill_path(cfg, s["skill"]),
        skill_dir=skill_path(cfg, s["skill"]).parent,
        input=inp, facts=facts or "なし", output=s["output"], report=s["report"],
        doc_type=st["doc_type"], reader=st["reader"], style=STYLES[st["style"]],
        purpose=st["purpose"] or "(指定なし。入力から読み取る)",
        extra="\n".join(extra), yomi_state=yomi_state,
        material_opt=" --material {}".format(facts) if facts else "",
        fix_note=("\nこれは指摘への対応の工程である。入力は前の工程の最終稿で、直すのは指摘のある箇所だけにする。"
                  "指摘ごとに「直した」か「残す: 理由」を報告の「指摘への対応」に書く。\n" if s["name"] == "fix" else ""),
    )
    Path(s["brief"]).write_text(body, encoding="utf-8")
    s["input"] = inp


# ---------- next / done ----------

def show_next(st, cfg):
    s = current_stage(st)
    if s is None:
        print("すべての工程が終わりました。jpipe status --run {} で結果を確かめてください。".format(st["id"]))
        return
    i, n = stage_index(st, s)
    print("[{}/{}] {} — {}".format(i, n, s["name"], LABELS[s["name"]]))
    if s["kind"] == "agent":
        if not Path(s.get("brief", "")).is_file() or s.get("input") != last_text_output(st, before=s):
            write_brief(st, cfg, s)
            save_state(st)
        print("操作: 新しいサブエージェントにこの工程だけを任せる。")
        print('  Agent(subagent_type="jp-stage", prompt="brief: {}")'.format(s["brief"]))
        print("  (jp-stage が使えない環境では、汎用のサブエージェントに「この brief を読んで、その指示だけに従って」と渡す)")
        print("終わったら: python3 {} done --run {}".format(C.JPIPE, st["id"]))
    elif s["kind"] == "script":
        slow = s["name"] in ("review", "draft_external")
        print("操作: python3 {} run --run {}".format(C.JPIPE, st["id"]))
        if slow:
            print("  別のモデルを呼ぶので数分〜10分かかる。バックグラウンドで実行し、終わりの通知を待つ。")
    else:
        show_confirm(st)


def cmd_next(args, cfg):
    show_next(load_state(args.run), cfg)


def cmd_done(args, cfg):
    st = load_state(args.run)
    s = current_stage(st)
    if not s or s["kind"] != "agent":
        raise UsageError("今の工程はサブエージェントの工程ではありません。jpipe next で確かめてください。")
    out, rep = Path(s["output"]), Path(s["report"])
    problems = []
    if not out.is_file() or not out.read_text(encoding="utf-8").strip():
        problems.append("出力 {} がないか空です。".format(out))
    if not rep.is_file():
        problems.append("報告 {} がありません。".format(rep))
    if out.is_file() and re.match(r"\s*```", out.read_text(encoding="utf-8")):
        problems.append("出力がコードフェンスで始まっています。本文だけを書いてください。")
    if s["skill"] == "yomiyasu" and out.is_file():
        r = yomiyasu_verify(st, cfg, out, s.get("input"))
        if r != 0:
            problems.append("最終稿が yomiyasu の検査の記録と合いません。brief の検査のコマンドを、最終稿を書いたあとに実行してください。")
    if problems:
        print("この工程はまだ終わっていません:")
        for x in problems:
            print("- " + x)
        print('同じ brief でサブエージェントに続きを頼んでください: Agent(subagent_type="jp-stage", prompt="brief: {}")'.format(s["brief"]))
        return 1
    s["status"] = "done"
    s["finished"] = C.now()
    save_state(st)
    print("記録しました: {} を終えました。".format(s["name"]))
    print()
    show_next(st, cfg)
    return 0


def yomiyasu_verify(st, cfg, final, source=None):
    """最終稿が、推敲の担当が実行した検査の記録と合うか。同じ本文に別の原文の記録があっても決まるよう、原文も渡す。"""
    y = C.find_yomiyasu(cfg)
    env = dict(os.environ, YOMIYASU_STATE_DIR=p(st, "yomiyasu-state"))
    argv = ["python3", str(y / "scripts/yomiyasu_check.py"), "verify", "--final", str(final)]
    if source:
        argv += ["--source", str(source)]
    r = subprocess.run(argv,
                       capture_output=True, text=True, env=env, cwd=str(run_dir(st["id"])))
    return r.returncode


# ---------- check ----------

def run_nj(cfg, script, *argv):
    nj, uv = C.find_natural_japanese(cfg), C.find_uv(cfg)
    if not nj or not uv:
        return None, "natural-japanese か uv が見つからない"
    try:
        r = subprocess.run([uv, "run", "--quiet", str(nj / "scripts" / script)] + list(argv) + ["--json"],
                           capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or "")[-300:]
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, "JSON として読めない出力"


def do_check(st, cfg, s):
    y = C.find_yomiyasu(cfg)
    final = last_text_output(st, before=s)
    rewrite = next((x for x in reversed(st["stages"][:st["stages"].index(s)])
                    if x["kind"] == "agent" and x["status"] == "done" and x.get("produces_text")), {})
    base = rewrite.get("input") or st["files"].get("source")
    facts = st["files"].get("facts")
    findings, skipped = [], []

    argv = ["python3", str(y / "scripts/yomiyasu_check.py"), "run", "--no-record", "--json",
            "--final", final, "--doc-type", st["doc_type"], "--fail-on", "meaning-risk"]
    if base and base != facts:
        argv += ["--source", base]
    if facts:
        argv += ["--material", facts]
    if st["reader"]:
        argv += ["--reader", st["reader"]]
    env = dict(os.environ, YOMIYASU_STATE_DIR=p(st, "yomiyasu-check-state"))
    r = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(run_dir(st["id"])))
    yo = None
    try:
        yo = json.loads(r.stdout)
    except ValueError:
        skipped.append("yomiyasu_check (終了コード {}: {})".format(r.returncode, (r.stderr or "")[-200:]))
    if yo:
        for m in (yo.get("diff") or {}).get("meaning_risks") or []:
            findings.append({"from": "yomiyasu diff", "line": m.get("new_line"), "kind": "意味が変わる恐れ: {}/{}".format(m.get("kind"), m.get("subkind")),
                             "text": m.get("new"), "before": m.get("old"), "detail": m.get("detail")})
        for f in (yo.get("lint") or {}).get("findings") or []:
            if f.get("severity") in ("warn", "error"):
                findings.append({"from": "yomiyasu lint", "line": f.get("line"), "kind": f.get("rule"),
                                 "text": f.get("snippet"), "message": f.get("message")})

    genre = {"essay": "essay", "promo": "essay", "mail": "business", "proposal": "business",
             "minutes": "business", "formal": "business"}.get(st["doc_type"], "tech")
    nl, err = run_nj(cfg, "lint.py", final, "--genre", genre)
    if nl is None:
        skipped.append("natural-japanese lint ({})".format(err))
    else:
        for f in nl.get("findings", []):
            if f.get("severity") in ("warn", "critical"):
                findings.append({"from": "natural-japanese lint", "line": f.get("line"), "kind": f.get("category"),
                                 "text": f.get("excerpt"), "message": f.get("hint") or f.get("detail")})
    ca, err = run_nj(cfg, "calques.py", final)
    if ca is None:
        skipped.append("natural-japanese calques ({})".format(err))
    else:
        for f in ca:
            findings.append({"from": "natural-japanese calques", "line": f.get("line"), "kind": "読み手の分野の言葉か: " + str(f.get("word")),
                             "text": f.get("text"), "message": "読み手が自分で書くときの呼び名か、何が起きるかが分かるかを確かめる"})
    orig = facts or st["files"].get("source")
    nf, err = run_nj(cfg, "newfacts.py", orig, final)
    if nf is None:
        skipped.append("natural-japanese newfacts ({})".format(err))
    else:
        for f in nf.get("new_items", []):
            ln = (f.get("lines") or [{}])[0]
            findings.append({"from": "natural-japanese newfacts", "line": ln.get("line"), "kind": "元にない語: " + f.get("word", ""),
                             "text": ln.get("text"), "message": "資料か原文にない事実なら削るか戻す。原文から直接言える補足なら残す"})

    result = {"final": final, "base": base, "facts": facts, "findings": findings, "skipped": skipped,
              "yomiyasu_status": yo.get("status_line") if yo else None,
              "meaning_risk_count": yo.get("fail_on_meaning_risk_count") if yo else None}
    Path(s["output"]).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return findings, skipped


# ---------- review ----------

def number_lines(text):
    return "\n".join("{:>4}| {}".format(i, l) for i, l in enumerate(text.splitlines(), 1))


def parse_json_list(text):
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def norm(s):
    return re.sub(r"\s+", "", s or "")


def do_review(st, cfg, s):
    cmd = C.reviewer_command(cfg)
    final = last_text_output(st, before=s)
    if not cmd:
        return None, "別のモデル (agy / gemini / codex) が見つからない"
    text = Path(final).read_text(encoding="utf-8")
    prompt = render("review.md", slop=(C.REFERENCES / "slop-types.md").read_text(encoding="utf-8"),
                    doc_type=st["doc_type"], doc=number_lines(text))
    Path(p(st, "60_review.prompt.md")).write_text(prompt, encoding="utf-8")
    out, err = C.call_external(cmd, prompt, cfg["external_timeout"])
    if out is None:
        return None, err
    Path(p(st, "60_review.raw.txt")).write_text(out, encoding="utf-8")
    items = parse_json_list(out)
    if items is None:
        return None, "別のモデルの出力から JSON の配列を読み取れない (60_review.raw.txt)"
    body, kept, dropped = norm(text), [], 0
    for it in items:
        if not isinstance(it, dict) or not it.get("quote") or norm(it["quote"]) not in body:
            dropped += 1  # 本文にない引用は、別のモデルの読み違いとして捨てる
            continue
        kept.append({"from": "別のモデル", "line": it.get("line"), "kind": it.get("type"),
                     "text": it.get("quote"), "message": it.get("reason")})
    Path(s["output"]).write_text(json.dumps({"findings": kept, "dropped": dropped, "command": cmd[0]},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
    return kept, None


# ---------- run ----------

def write_findings_md(path, findings, title):
    lines = ["# " + title, "", "指摘は疑いである。原文・資料と照らして、直すか残すかを決める。", ""]
    for i, f in enumerate(findings, 1):
        lines.append("{}. [{}] {} (行 {})".format(i, f["from"], f.get("kind"), f.get("line") or "-"))
        if f.get("before"):
            lines.append("   元: " + str(f["before"])[:200])
        if f.get("text"):
            lines.append("   今: " + str(f["text"])[:200])
        if f.get("message"):
            lines.append("   見る点: " + str(f["message"])[:300])
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_findings(st, upto):
    found = []
    for x in st["stages"][:st["stages"].index(upto) + 1]:
        if x["kind"] == "script" and x["name"] in ("check", "review", "recheck") and x["status"] == "done":
            found += x.get("findings") or []
    return found


def maybe_insert_fix(st, s):
    """検査の最後の工程のあとで指摘があれば、修正と再検査を1回だけ差し込む。"""
    nxt = st["stages"][st["stages"].index(s) + 1]
    if nxt["kind"] != "human" or st["loops"] >= 1 or s["name"] == "recheck":
        return False
    findings = collect_findings(st, s)
    if not findings:
        return False
    fpath = p(st, "45_findings.md")
    write_findings_md(fpath, findings, "直す候補 (run {})".format(st["id"]))
    pos = st["stages"].index(nxt)
    st["stages"][pos:pos] = [
        {"name": "fix", "kind": "agent", "skill": "yomiyasu", "num": "45", "findings": fpath,
         "output": p(st, "45_fix.md"), "report": p(st, "45_fix.report.md"), "brief": p(st, "45_fix.brief.md"),
         "produces_text": True, "status": "pending"},
        {"name": "recheck", "kind": "script", "num": "55", "output": p(st, "55_recheck.json"),
         "produces_text": False, "status": "pending"},
    ]
    st["loops"] += 1
    return True


def cmd_run(args, cfg):
    st = load_state(args.run)
    s = current_stage(st)
    if not s or s["kind"] != "script":
        raise UsageError("今の工程はスクリプトの工程ではありません。jpipe next で確かめてください。")
    t0 = time.time()
    if s["name"] in ("check", "recheck"):
        findings, skipped = do_check(st, cfg, s)
        s["findings"], s["skipped"] = findings, skipped
        print("機械検査: 指摘 {} 件".format(len(findings)))
        for k in skipped:
            print("  実行できなかった検査: " + k)
    elif s["name"] == "review":
        findings, err = do_review(st, cfg, s)
        if findings is None:
            s["status"] = "skipped"
            s["reason"] = err
            st["notes"].append("別のモデルによる判定を省いた: " + err)
            print("別のモデルによる判定を省きました: " + err)
        else:
            s["findings"] = findings
            print("別のモデルの指摘: {} 件".format(len(findings)))
    elif s["name"] == "draft_external":
        cmd = C.reviewer_command(cfg)
        out, err = (None, "別のモデルが見つからない") if not cmd else C.call_external(
            cmd, render("draft-external.md", facts=Path(st["files"]["facts"]).read_text(encoding="utf-8"),
                        design=Path(next(x["output"] for x in st["stages"] if x["name"] == "design")).read_text(encoding="utf-8"),
                        slop=(C.REFERENCES / "slop-types.md").read_text(encoding="utf-8"),
                        style=STYLES[st["style"]], doc_type=st["doc_type"]), cfg["external_timeout"])
        m = re.search(r"<<<本文>>>\s*(.*?)\s*<<<ここまで>>>", out or "", re.S)
        if not m:
            # 別のモデルが使えないときは、Claude の下書きに切り替える
            reason = err or "出力に <<<本文>>> の区切りがない"
            st["notes"].append("別のモデルの下書きを使えず、Claude の下書きに切り替えた: " + reason)
            pos = st["stages"].index(s)
            s["status"] = "skipped"
            s["reason"] = reason
            st["stages"].insert(pos + 1, {"name": "draft", "kind": "agent", "skill": "natural-japanese", "num": "21",
                                          "output": p(st, "21_draft.md"), "report": p(st, "21_draft.report.md"),
                                          "brief": p(st, "21_draft.brief.md"), "produces_text": True, "status": "pending"})
            print("別のモデルの下書きを使えないので、Claude の下書きに切り替えます: " + reason)
        else:
            Path(s["output"]).write_text(m.group(1) + "\n", encoding="utf-8")
    if s["status"] == "pending":
        s["status"] = "done"
    s["seconds"] = round(time.time() - t0, 1)
    if s["status"] in ("done", "skipped") and s["name"] in ("check", "review"):
        if maybe_insert_fix(st, s):
            print("指摘があるので、修正の工程を1回だけ差し込みました ({})。".format(p(st, "45_findings.md")))
    save_state(st)
    print()
    show_next(st, cfg)
    return 0


# ---------- confirm / finish ----------

def questions_from_reports(st):
    qs = []
    for s in st["stages"]:
        rep = s.get("report")
        if s["status"] != "done" or not rep or not Path(rep).is_file():
            continue
        m = re.search(r"#+\s*書き手に確かめたい点\s*\n(.*?)(?=\n#+\s|\Z)", Path(rep).read_text(encoding="utf-8"), re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip(" -・*")
                if line and line not in ("なし", "特になし"):
                    qs.append("[{}] {}".format(s["name"], line))
    return qs


def show_confirm(st):
    final = last_text_output(st)
    remaining = []
    last_check = [x for x in st["stages"] if x["name"] in ("check", "recheck") and x["status"] == "done"]
    if last_check:
        remaining = last_check[-1].get("findings") or []
    print("最終稿: " + final)
    qs = questions_from_reports(st)
    if qs:
        print("書き手に確かめたい点 (利用者に聞いてから書き出す。答えで本文が変わるなら --route revise でもう一度通す):")
        for q in qs[:6]:
            print("  - " + q)
    if remaining:
        print("残った指摘: {} 件 (最後の検査の {})。重いものだけ利用者に伝える。".format(len(remaining), last_check[-1]["output"]))
    for n in st["notes"]:
        print("注意: " + n)
    dest = st["files"].get("src_path")
    print("操作: python3 {} finish --run {} --out {}".format(C.JPIPE, st["id"], dest or "<書き出し先>"))


def cmd_finish(args, cfg):
    st = load_state(args.run)
    s = current_stage(st)
    if s is None or s["kind"] != "human":
        raise UsageError("まだ終わっていない工程があります。jpipe next で確かめてください。")
    out = Path(args.out).expanduser()
    final = Path(last_text_output(st))
    src = st["files"].get("src_path")
    if out.exists() and not args.force:
        same_src = src and str(out.resolve()) == src and C.sha256_file(out) == st["files"].get("src_sha256")
        if not same_src:
            raise UsageError("{} はすでにあり、工程を始めたあとに変わったか、原稿とは別のファイルです。上書きするなら --force を付けてください。".format(out))
    verify = None
    polished = [x for x in st["stages"] if x.get("skill") == "yomiyasu" and x["status"] == "done"]
    if polished:
        verify = yomiyasu_verify(st, cfg, final, polished[-1].get("input"))
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(final, out)
    C.certify(out, "pipeline", st["id"])
    s["status"] = "done"
    st["finished"] = True
    st["out"] = str(out.resolve())
    save_state(st)
    report = make_report(st, verify)
    Path(p(st, "report.md")).write_text(report, encoding="utf-8")
    print(report)
    return 0


def make_report(st, verify):
    lines = ["# 工程の記録 (run {})".format(st["id"]), "",
             "書き出し先: {}".format(st.get("out")), "route: {} / 文書の種類: {}".format(st["route"], st["doc_type"]), "",
             "| 工程 | 状態 | 備考 |", "|---|---|---|"]
    for s in st["stages"]:
        note = ""
        if s.get("findings") is not None:
            note = "指摘 {} 件".format(len(s["findings"]))
        if s.get("skipped"):
            note += " / 実行できなかった検査 {} 件".format(len(s["skipped"]))
        if s.get("reason"):
            note = "省略: " + s["reason"][:80]
        lines.append("| {} | {} | {} |".format(s["name"], s["status"], note))
    lines.append("")
    if verify is not None:
        lines.append("yomiyasu の検査の記録との照合: " + ("合った" if verify == 0 else "合わない (推敲の担当が最終稿で検査を実行していない)"))
    for n in st["notes"]:
        lines.append("注意: " + n)
    return "\n".join(lines) + "\n"


# ---------- certify ----------

def cmd_certify(args, cfg):
    path = Path(args.file)
    if not path.is_file():
        raise UsageError("ファイルがありません: {}".format(path))
    if not C.find_yomiyasu(cfg):
        raise UsageError("yomiyasu が見つかりません。jpipe setup を実行してください。")
    text = C.read_doc_text(path)
    doc_type = args.doc_type or C.guess_doc_type(path)
    res = C.yomiyasu_lint(cfg, text, doc_type)
    if res is None:
        raise UsageError("yomiyasu の lint を実行できませんでした。")
    counts = res.get("counts", {})
    bad = [f for f in res.get("findings", []) if f.get("severity") in ("warn", "error")]
    ok = counts.get("error", 0) <= cfg["certify_max_error"] and counts.get("warn", 0) <= cfg["certify_max_warn"]
    if ok or args.accept:
        C.certify(path, "certify" if ok else "accepted", None if ok else args.accept)
        print("検査済みとして記録しました: {} ({}{})".format(path, "指摘なし" if ok else "理由つきで受け入れ: ",
                                                   "" if ok else args.accept))
        return 0
    print("{} に lint の指摘が {} 件あります (文書の種類 {}):".format(path, len(bad), doc_type))
    for f in bad[:20]:
        print("- 行 {} [{}] {}".format(f.get("line"), f.get("rule"), str(f.get("message"))[:160]))
        if f.get("snippet"):
            print("    " + str(f["snippet"])[:120])
    print("直してからもう一度 certify を実行する。指摘を残す理由があるなら --accept \"理由\" を付ける。")
    print("全体を仕上げ直すなら: python3 {} init --route revise --src {}".format(C.JPIPE, path))
    return 1


# ---------- status / doctor / setup ----------

def cmd_status(args, cfg):
    if args.run or CURRENT.exists():
        try:
            st = load_state(args.run)
        except UsageError:
            st = None
        if st:
            print("run {} (route {}, {})".format(st["id"], st["route"], "完了" if st["finished"] else "進行中"))
            for s in st["stages"]:
                print("  {:<15} {}".format(s["name"], s["status"]))
            if not st["finished"]:
                print()
                show_next(st, cfg)
    runs = sorted(RUNS.glob("*/state.json"), reverse=True)[:5] if RUNS.exists() else []
    if runs and not args.run:
        print("\n最近の run:")
        for r in runs:
            d = json.loads(r.read_text(encoding="utf-8"))
            print("  {} {} {}".format(d["id"], d["route"], "完了" if d["finished"] else "進行中"))
    return 0


def cmd_doctor(args, cfg):
    y, nj, uv, rv = C.find_yomiyasu(cfg), C.find_natural_japanese(cfg), C.find_uv(cfg), C.reviewer_command(cfg)
    rows = [
        ("yomiyasu (推敲・検査。必須)", str(y) if y else "見つからない → jpipe setup"),
        ("natural-japanese (設計・下書き・検査)", str(nj) if nj else "見つからない → jpipe setup (route new で必須)"),
        ("uv (natural-japanese の検査に使う)", uv or "見つからない (natural-japanese の検査を省く)"),
        ("別のモデル (判定・下書き)", " ".join(rv[:1]) if rv else "見つからない (判定の工程を省き、下書きは Claude が担当する)"),
        ("explain の手順書", str(C.explain_skill(cfg))),
        ("設定", "{} ({})".format(C.CONFIG_PATH, "あり" if C.CONFIG_PATH.exists() else "なし。既定値で動く")),
        ("hook", "chat_mode={} docs_mode={} enabled={}".format(cfg["chat_mode"], cfg["docs_mode"], cfg["enabled"])),
    ]
    for k, v in rows:
        print("{}: {}".format(k, v))
    auto = C.nj_autoloaded()
    if auto:
        print("注意: natural-japanese がスキルとして自動で読み込まれる場所にもあります ({})。".format(auto[0]))
        print("      会話のなかで自分から動き、工程の指示と食い違うことがあります。jpipe setup で取ってきた版だけを使うなら、そちらは外してください。")
    return 0 if y else 1


REPOS = {"yomiyasu": "https://github.com/nanaism/yomiyasu.git",
         "natural-japanese": "https://github.com/coji/natural-japanese.git"}


def cmd_setup(args, cfg):
    C.VENDOR.mkdir(parents=True, exist_ok=True)
    found = {"yomiyasu": C.find_yomiyasu(cfg), "natural-japanese": C.find_natural_japanese(cfg)}
    for name, url in REPOS.items():
        dest = C.VENDOR / name
        if found[name] and not str(found[name]).startswith(str(C.VENDOR)):
            print("{}: 既にある ({})".format(name, found[name]))
            continue
        if dest.exists():
            r = subprocess.run(["git", "-C", str(dest), "pull", "--ff-only", "-q"], capture_output=True, text=True)
            print("{}: 更新した ({})".format(name, dest) if r.returncode == 0 else "{}: 更新できなかった: {}".format(name, r.stderr.strip()))
        else:
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(dest)], capture_output=True, text=True)
            print("{}: 取ってきた ({})".format(name, dest) if r.returncode == 0 else "{}: 取ってこられなかった: {}".format(name, r.stderr.strip()))
    if args.link:
        link = Path.home() / ".local/bin/jpipe"
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(C.JPIPE)
        os.chmod(C.JPIPE, 0o755)
        print("jpipe を {} に置きました。".format(link))
    return cmd_doctor(args, load_cfg())


def load_cfg():
    return C.load_config()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jpipe", description="日本語の文書を工程に分けて仕上げるハーネス")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init")
    a.add_argument("--route", choices=["new", "revise", "explain"], required=True)
    a.add_argument("--src")
    a.add_argument("--material", action="append")
    a.add_argument("--doc-type", choices=C.DOC_TYPES)
    a.add_argument("--reader", choices=["expert", "informed", "general"], default="general")
    a.add_argument("--style", choices=list(STYLES), default="keep")
    a.add_argument("--purpose", help="読み手と目的を1文で")
    a.add_argument("--draft", choices=["claude", "external"], default="claude")
    a.add_argument("--explain", action="store_true", help="説明の組み立ての工程を足す")
    a.add_argument("--no-review", dest="review", action="store_false")
    for name in ["next", "done", "run", "status"]:
        sub.add_parser(name).add_argument("--run")
    f = sub.add_parser("finish")
    f.add_argument("--run")
    f.add_argument("--out", required=True)
    f.add_argument("--force", action="store_true")
    c = sub.add_parser("certify")
    c.add_argument("file")
    c.add_argument("--doc-type", choices=C.DOC_TYPES)
    c.add_argument("--accept", metavar="理由")
    sub.add_parser("doctor")
    sp = sub.add_parser("setup")
    sp.add_argument("--link", action="store_true")
    args = ap.parse_args(argv)
    cfg = C.load_config()
    handlers = {"init": cmd_init, "next": cmd_next, "done": cmd_done, "run": cmd_run, "finish": cmd_finish,
                "certify": cmd_certify, "status": cmd_status, "doctor": cmd_doctor, "setup": cmd_setup}
    try:
        return handlers[args.cmd](args, cfg) or 0
    except UsageError as e:
        print("エラー: {}".format(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
