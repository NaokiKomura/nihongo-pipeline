"""jpipe と jp_hook が共有する部品。標準ライブラリだけで動く (Python 3.9 以上)。

置き場所の探索、設定、検査済みの記録、yomiyasu の lint の呼び出しをまとめる。
"""
import glob
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"
REFERENCES = SKILL_DIR / "references"
JPIPE = SKILL_DIR / "scripts" / "jpipe.py"
VERSION = "0.1.0"

HOME = Path.home()
STATE_HOME = Path(os.environ.get("NIHONGO_PIPELINE_HOME") or HOME / ".local/state/nihongo-pipeline")
CONFIG_PATH = Path(os.environ.get("NIHONGO_PIPELINE_CONFIG") or HOME / ".config/nihongo-pipeline/config.json")
VENDOR = STATE_HOME / "vendor"

DEFAULTS = {
    # off にすると hook はすべて何もしない
    "enabled": True,
    # 返答の hook: rules (規則を注入するだけ) / feedback (lint の結果を次の返答に渡す) / block (指摘が多ければ出し直させる)
    "chat_mode": "feedback",
    "chat_min_chars": 300,
    "chat_block_warn": 4,
    # 文書の hook: block (検査を通すまで終わらせない) / remind (注意を出すだけ) / off
    "docs_mode": "block",
    "docs_min_chars": 200,
    "doc_exts": [".md", ".markdown", ".txt", ".html", ".htm"],
    "exclude_globs": [
        "**/.claude/**", "**/.git/**", "**/node_modules/**", "**/.venv/**",
        "**/SKILL.md", "**/CLAUDE.md", "**/AGENTS.md", "**/GEMINI.md",
        "**/memory/**", "**/CHANGELOG*", "**/LICENSE*",
    ],
    # certify で通す lint の上限
    "certify_max_error": 0,
    "certify_max_warn": 0,
    # 別のモデル。reviewer を off にすると使わない。reviewer_command が null なら agy → gemini → codex の順に探す。
    # {prompt} を依頼文に置き換える。見つからないときや失敗したときは、その工程を省いて進む
    "reviewer": "auto",
    "reviewer_command": None,
    "agy_model": "gemini-4-argon-high",
    "external_timeout": 1800,
    # 部品の置き場所。null なら探す
    "yomiyasu_dir": None,
    "natural_japanese_dir": None,
    # explain 工程の手順書。null なら同梱の references/explain-first.md
    "explain_skill": None,
    "uv": None,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if os.environ.get("NIHONGO_PIPELINE") == "off":
        cfg["enabled"] = False
    return cfg


# ---------- 部品の置き場所 ----------

def _first_dir(cands, marker):
    for c in cands:
        if not c:
            continue
        for p in sorted(glob.glob(os.path.expanduser(str(c))), reverse=True):
            if (Path(p) / marker).is_file():
                return Path(p).resolve()
    return None


def find_yomiyasu(cfg):
    return _first_dir([
        os.environ.get("NIHONGO_YOMIYASU_DIR"), cfg.get("yomiyasu_dir"),
        "~/.claude/skills/yomiyasu",
        "~/.claude/plugins/cache/*/yomiyasu/*/skills/yomiyasu",
        "~/.claude/plugins/marketplaces/*/skills/yomiyasu",
        str(VENDOR / "yomiyasu/skills/yomiyasu"),
        "~/.agents/skills/yomiyasu", "~/.codex/skills/yomiyasu",
    ], "scripts/yomiyasu_check.py")


def find_natural_japanese(cfg):
    return _first_dir([
        os.environ.get("NIHONGO_NJ_DIR"), cfg.get("natural_japanese_dir"),
        str(VENDOR / "natural-japanese/skills/natural-japanese"),
        "~/.claude/skills/natural-japanese",
        "~/.claude/plugins/cache/*/natural-japanese/*/skills/natural-japanese",
        "~/.claude/plugins/marketplaces/*/skills/natural-japanese",
        "~/.agents/skills/natural-japanese",
    ], "scripts/lint.py")


def nj_autoloaded():
    """natural-japanese がスキルとして自動で読み込まれる場所にあるか (指示が食い違う原因になる)。"""
    hits = []
    for pat in ["~/.claude/skills/natural-japanese", "~/.claude/plugins/cache/*/natural-japanese"]:
        hits += glob.glob(os.path.expanduser(pat))
    return hits


def find_uv(cfg):
    for c in [os.environ.get("NIHONGO_UV"), cfg.get("uv"), shutil.which("uv"), str(HOME / ".local/bin/uv")]:
        if c and Path(c).is_file():
            return c
    return None


def explain_skill(cfg):
    p = cfg.get("explain_skill")
    if p and Path(os.path.expanduser(p)).is_file():
        return Path(os.path.expanduser(p)).resolve()
    return REFERENCES / "explain-first.md"


def reviewer_command(cfg):
    """別のモデルを呼ぶコマンド。使えなければ None (呼ぶ側は工程を省いて進む)。"""
    env = os.environ.get("NIHONGO_REVIEWER")
    if env == "off" or cfg.get("reviewer") == "off":
        return None
    if env:
        return [env, "{prompt}"]
    if cfg.get("reviewer_command"):
        cmd = list(cfg["reviewer_command"])
        exe = shutil.which(cmd[0]) or (cmd[0] if Path(os.path.expanduser(cmd[0])).is_file() else None)
        return [exe] + cmd[1:] if exe else None
    if shutil.which("agy") or (HOME / ".local/bin/agy").is_file():
        exe = shutil.which("agy") or str(HOME / ".local/bin/agy")
        return [exe, "-p", "{prompt}", "--model", cfg["agy_model"], "--print-timeout", "0"]
    if shutil.which("gemini"):
        return [shutil.which("gemini"), "-p", "{prompt}"]
    if shutil.which("codex"):
        return [shutil.which("codex"), "exec", "{prompt}"]
    return None


def call_external(cmd, prompt, timeout):
    """別のモデルに依頼文を渡し、標準出力を返す。失敗したら (None, 理由)。"""
    argv = [prompt if a == "{prompt}" else a for a in cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "{}: {}".format(type(e).__name__, e)
    if r.returncode != 0:
        return None, "終了コード {}: {}".format(r.returncode, (r.stderr or "")[-500:])
    return r.stdout, None


# ---------- テキスト ----------

KANA = re.compile(r"[぀-ヿ]")
JA = re.compile(r"[぀-ヿ㐀-鿿]")


def is_japanese(text, min_ratio=0.15):
    body = re.sub(r"```.*?```", "", text or "", flags=re.S)
    letters = [c for c in body if not c.isspace()]
    if not letters or not KANA.search(body):
        return False
    return sum(1 for c in letters if JA.match(c)) / len(letters) >= min_ratio


def html_to_text(src):
    s = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1>", "", src)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|aside|figcaption|td|th)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    lines = [html.unescape(l).strip() for l in s.splitlines()]
    return "\n".join(l for l in lines if l)


def read_doc_text(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if Path(path).suffix.lower() in (".html", ".htm"):
        return html_to_text(text)
    return text


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------- 検査済みの記録 ----------

CERT_PATH = STATE_HOME / "certified.json"


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, str(path))


def certify(path, how, detail=None):
    path = str(Path(path).resolve())
    db = _load_json(CERT_PATH, {})
    db[path] = {"sha256": sha256_file(path), "how": how, "detail": detail, "at": now()}
    _save_json(CERT_PATH, db)


def certified(path):
    path = str(Path(path).resolve())
    rec = _load_json(CERT_PATH, {}).get(path)
    try:
        return bool(rec) and rec["sha256"] == sha256_file(path)
    except OSError:
        return False


# ---------- yomiyasu の lint ----------

DOC_TYPES = ["pr", "article", "incident", "proposal", "minutes", "manual", "slides",
             "essay", "formal", "promo", "mail", "paper", "script"]


def yomiyasu_lint(cfg, text, doc_type=None, timeout=30):
    """text を yomiyasu_lint.py にかけ、JSON を返す。使えなければ None。"""
    y = find_yomiyasu(cfg)
    if not y:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp = f.name
    try:
        argv = ["python3", str(y / "scripts/yomiyasu_lint.py"), tmp, "--json"]
        if doc_type:
            argv += ["--doc-type", doc_type]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        os.unlink(tmp)


def guess_doc_type(path):
    name = Path(path).name.lower()
    if Path(path).suffix.lower() in (".html", ".htm") or "slide" in name:
        return "slides"
    for key, t in [("readme", "manual"), ("minutes", "minutes"), ("議事録", "minutes"),
                   ("report", "proposal"), ("proposal", "proposal"), ("提案", "proposal"),
                   ("pr_", "pr"), ("mail", "mail"), ("paper", "paper")]:
        if key in name:
            return t
    return "article"
