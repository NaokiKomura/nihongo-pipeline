"""natural-japanese の newfacts.py の代わり (テスト用)。元の文書にない数字だけを返す。"""
import json, re, sys
orig, new = (open(p, encoding="utf-8").read() for p in sys.argv[1:3])
items = sorted(set(re.findall(r"\d+", new)) - set(re.findall(r"\d+", orig)))
print(json.dumps({"new_items": [{"word": w, "lines": []} for w in items]}, ensure_ascii=False))
