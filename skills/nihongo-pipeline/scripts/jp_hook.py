#!/usr/bin/env python3
"""nihongo-pipeline の hook。Claude Code の3つの時点で呼ばれる。

  jp_hook.py prompt     UserPromptSubmit: 依頼の種類を見分けて、返答の決まりか工程の案内を注入する
  jp_hook.py post-tool  PostToolUse (Write|Edit|MultiEdit): 書いた日本語の文書を記録する
  jp_hook.py stop       Stop: 検査していない文書があれば一度だけ止める。返答を lint にかけ、癖を次の返答に渡す

hook の失敗で会話を止めないよう、例外はすべて記録して終了コード0で抜ける。
依頼に #nopipe を含めると、そのターンは何もしない。
"""
import fnmatch
import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jpcore as C  # noqa: E402

SESSIONS = C.STATE_HOME / "sessions"
JPIPE = "python3 {}".format(C.JPIPE)

DELIVERABLE = re.compile(
    r"(資料|スライド|プレゼン|登壇|ペラ|レポート|報告書|報告|記事|ブログ|note|提案書|企画書|議事録|メール|文面|文書|ドキュメント|"
    r"README|手順書|マニュアル|ガイド|原稿|紹介文|告知|お知らせ|リリースノート|プレスリリース|申請書|仕様書|設計書|要約|まとめ)")
ACTION = re.compile(r"(書いて|書く|作って|作る|作成|まとめて|起こして|直して|推敲|書き直|整えて|下書き|仕上げ|リライト|翻訳|訳して|生成)")
REVISE = re.compile(r"(推敲|読みやすく|AIっぽ|AI臭|自然な日本語|日本語を直|文章を直|添削|校正)")

DOC_RULE = (
    "[nihongo-pipeline] この依頼は、読み手に渡す日本語の文章を作る依頼に見える。本文はファイルに書き、スキル nihongo-pipeline の工程で仕上げる。"
    "新しく書くなら `{j} init --route new --material <事実のメモ>`、直すなら `{j} init --route revise --src <原稿>`、"
    "スライドや箇条書きの文言なら `{j} init --route explain --src <文言の .md>` から始め、`{j} next` の指示に従う。"
    "工程に通すほどでない短い文書は、書いたあと `{j} certify <ファイル>` を通す。"
    "このターンで書いた文書が検査を通っていないと、返答を終える前に止める。"
)
REVISE_RULE = (
    "[nihongo-pipeline] 日本語の推敲の依頼に見える。原稿をファイルにして `{j} init --route revise --src <原稿>` から始め、`{j} next` の指示に従う。"
    "yomiyasu の名前を出して頼まれたときは、yomiyasu スキルを直接使ってよい。"
)


def load_session(sid):
    return C._load_json(SESSIONS / "{}.json".format(sid), {"pending": {}, "feedback": [], "optout": False})


def save_session(sid, ss):
    C._save_json(SESSIONS / "{}.json".format(sid), ss)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


# ---------- prompt ----------

def on_prompt(data, cfg):
    sid = data.get("session_id") or "default"
    prompt = data.get("prompt") or ""
    ss = load_session(sid)
    ss["optout"] = "#nopipe" in prompt
    if ss["optout"]:
        save_session(sid, ss)
        return
    if not C.is_japanese(prompt, 0.1):
        save_session(sid, ss)
        return
    parts = []
    if REVISE.search(prompt):
        parts.append(REVISE_RULE.format(j=JPIPE))
    elif DELIVERABLE.search(prompt) and ACTION.search(prompt):
        parts.append(DOC_RULE.format(j=JPIPE))
    if cfg["chat_mode"] != "off":
        parts.append((C.REFERENCES / "chat-rules.md").read_text(encoding="utf-8").strip())
    if ss.get("feedback"):
        parts.append("[nihongo-pipeline] 前の返答を lint にかけたところ、次の癖が出ていた。この返答では避ける: " + " / ".join(ss["feedback"]))
        ss["feedback"] = []
    save_session(sid, ss)
    if parts:
        emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(parts)}})


# ---------- post-tool ----------

def eligible_path(path, cfg):
    p = str(Path(path).expanduser().resolve())
    if Path(p).suffix.lower() not in cfg["doc_exts"]:
        return False
    if p.startswith(str(C.STATE_HOME)):
        return False
    return not any(fnmatch.fnmatch(p, g.replace("**/", "*/").replace("/**", "/*")) for g in cfg["exclude_globs"])


def eligible_content(path, cfg):
    try:
        text = C.read_doc_text(path)
    except OSError:
        return False
    return len(text) >= cfg["docs_min_chars"] and C.is_japanese(text)


def on_post_tool(data, cfg):
    if data.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        return
    path = (data.get("tool_input") or {}).get("file_path")
    if not path or not eligible_path(path, cfg):
        return
    sid = data.get("session_id") or "default"
    ss = load_session(sid)
    ss.setdefault("pending", {})[str(Path(path).resolve())] = C.now()
    save_session(sid, ss)


# ---------- stop ----------

def last_assistant_text(data):
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        return msg
    path = data.get("transcript_path")
    if not path or not Path(path).is_file():
        return ""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    texts = []
    for e in reversed(entries):
        m = e.get("message") or {}
        content = m.get("content")
        if e.get("type") == "user":
            is_tool_result = isinstance(content, list) and all(
                isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
            if not is_tool_result and not e.get("isMeta"):
                break
        elif e.get("type") == "assistant" and isinstance(content, list):
            for c in reversed(content):
                if isinstance(c, dict) and c.get("type") == "text":
                    texts.append(c.get("text", ""))
    return "\n\n".join(reversed(texts))


def summarize(findings, limit=5):
    seen, out = set(), []
    for f in findings:
        rule = f.get("rule")
        if rule in seen:
            continue
        seen.add(rule)
        msg = re.sub(r"\s+", " ", str(f.get("message", "")))
        out.append("{}: {}".format(rule, msg[:90]))
        if len(out) >= limit:
            break
    return out


def on_stop(data, cfg):
    if data.get("stop_hook_active"):
        return
    sid = data.get("session_id") or "default"
    ss = load_session(sid)
    pending = list((ss.get("pending") or {}).keys())
    ss["pending"] = {}  # 止めるのは一度だけ。次のターンに持ち越さない
    if ss.get("optout"):
        save_session(sid, ss)
        return

    if cfg["docs_mode"] != "off":
        unchecked = [p for p in pending if Path(p).is_file() and eligible_content(p, cfg) and not C.certified(p)]
        if unchecked:
            listing = "\n".join("- " + p for p in unchecked[:10])
            reason = (
                "[nihongo-pipeline] このターンで書いた次の日本語の文書が、検査を通っていない。\n{}\n"
                "返答を終える前に、文書ごとに `{j} certify <ファイル>` を実行する。指摘が出たら直してもう一度実行する。"
                "しっかり仕上げるなら `{j} init --route revise --src <ファイル>` で工程に通す。"
                "中間ファイルなど検査が要らないものは、`{j} certify <ファイル> --accept \"理由\"` で理由を残す。"
            ).format(listing, j=JPIPE)
            save_session(sid, ss)
            if cfg["docs_mode"] == "block":
                emit({"decision": "block", "reason": reason})
            else:
                emit({"systemMessage": "nihongo-pipeline: 検査を通っていない文書があります\n" + listing})
            return

    if cfg["chat_mode"] in ("feedback", "block"):
        text = last_assistant_text(data)
        if len(text) >= cfg["chat_min_chars"] and C.is_japanese(text):
            res = C.yomiyasu_lint(cfg, text, "mail", timeout=10)
            bad = [f for f in (res or {}).get("findings", []) if f.get("severity") in ("warn", "error")]
            if bad and cfg["chat_mode"] == "block" and len(bad) >= cfg["chat_block_warn"]:
                save_session(sid, ss)
                emit({"decision": "block", "reason":
                      "[nihongo-pipeline] 返答を lint にかけたところ、次の指摘が {} 件出た。返答を直し、直した全文だけをもう一度出す。前置きや謝罪は書かない。\n- {}".format(
                          len(bad), "\n- ".join(summarize(bad, 8)))})
                return
            if bad:
                ss["feedback"] = summarize(bad)
    save_session(sid, ss)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        cfg = C.load_config()
        if not cfg["enabled"]:
            return 0
        data = json.loads(sys.stdin.read() or "{}")
        {"prompt": on_prompt, "post-tool": on_post_tool, "stop": on_stop}[mode](data, cfg)
    except Exception:  # hook の失敗で会話を止めない
        try:
            log = C.STATE_HOME / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write("{} {}\n{}\n".format(C.now(), mode, traceback.format_exc()))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
