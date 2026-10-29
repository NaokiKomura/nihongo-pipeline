"""natural-japanese の lint.py と同じ形の JSON を返す代わり (テスト用)。"""
import json, sys
print(json.dumps({"file": sys.argv[1], "stats": {}, "findings": []}))
