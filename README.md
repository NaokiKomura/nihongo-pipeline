# nihongo-pipeline

AI エージェントが書く日本語を、工程に分けて仕上げる Claude Code プラグインです。設計・下書き・説明の組み立て・推敲・機械検査・別のモデルによる判定は、工程ごとに別のサブエージェントが担当する形にしました。hook を入れると、日本語の返答には決まりが注入され、返答の本文は lint にかけられます。ターン中に書いた日本語の文書は、検査を通すまで返答を終えられません。

推敲には [yomiyasu](https://github.com/nanaism/yomiyasu)、設計と下書きには [natural-japanese](https://github.com/coji/natural-japanese) を使います。このプラグインが受け持つのは、2つをどの順に、どの文脈で使うかと、工程を通ったかの検査です。

## なぜ工程に分けるか

日本語の文章術のスキルは、それぞれ直す範囲が違います。yomiyasu は原文の構成を残して文の骨格と比喩を直し、natural-japanese は段落の役割から組み直します。2つを1つの文脈に読み込むと、指示が食い違うことがあります (yomiyasu の README にも書かれています)。どちらの SKILL.md も数万字あり、まとめて読ませると読み落としも増えます。

そこで、工程ごとに担当のスキルを1つに決め、工程ごとに新しいサブエージェントに任せます。構成 → 説明 → 文 と、粗いものから細かいものへ進める順にしました。本文を書き換えるのは Claude だけです。別のモデル (Gemini など) には、問題のある箇所を挙げさせるだけにしています。2つのモデルに交互に書き換えさせると、往復のたびに文が変わり、前後を比べる検査が意味をなさなくなるためです。

## 工程

```
事実のメモ (利用者の言葉と、確かめた資料)
  → design   natural-japanese: 読み手・主メッセージ・見出し        (new のみ)
  → draft    natural-japanese で Claude が書く / 別のモデルが書く    (new のみ)
  → explain  口頭で説明してから短くする                              (explain、または --explain)
  → polish   yomiyasu: 文の骨格・比喩・装飾。検査の記録を残す
  → check    yomiyasu の lint と diff、natural-japanese の lint・calques・newfacts
  → review   別のモデルが「意味破綻型」「過剰圧縮型」の箇所を挙げる
  → (指摘があれば fix と recheck を1回だけ)
  → confirm  書き手に確かめたい点を利用者に聞き、書き出す
```

工程の順序、受け渡すファイル、通過の条件、差し戻しの回数は `jpipe` (標準ライブラリだけの Python スクリプト) が決めます。エージェントは `jpipe next` が表示する操作を1つずつ実行します。たとえば yomiyasu の工程は、最終稿が yomiyasu の検査の記録と合わなければ、次の工程に進めません。

## 入れ方

```
/plugin marketplace add NaokiKomura/nihongo-pipeline
/plugin install nihongo-pipeline@nihongo-pipeline
```

入れたあと、部品をそろえます。yomiyasu をまだ入れていなければ、`setup` が取得します。

```
python3 ~/.claude/plugins/cache/nihongo-pipeline/nihongo-pipeline/*/skills/nihongo-pipeline/scripts/jpipe.py setup --link
jpipe doctor
```

`--link` を付けると `~/.local/bin/jpipe` を置きます。natural-japanese は、スキルとして自動で読み込まれる場所には置かず、`setup` が取得したものを工程の中でだけ使うことを勧めます。会話のなかで自分から動き、工程の指示と食い違うことがあるためです (`doctor` が知らせます)。

必要なもの: Python 3.9 以上、git。natural-japanese の検査には [uv](https://docs.astral.sh/uv/) を使います。別のモデルとして Antigravity CLI (`agy`)、Gemini CLI (`gemini`)、Codex CLI (`codex`) のどれかがあれば使います。

## 使い方

エージェントに日本語の文書を頼むと、スキルと hook が工程に案内します。手で動かすときは次のとおりです。

| 依頼 | コマンド |
|---|---|
| 新しく書く | `jpipe init --route new --material facts.md --doc-type proposal --style desu` |
| 原稿を直す | `jpipe init --route revise --src draft.md` |
| スライドや箇条書きの文言 | `jpipe init --route explain --src slide-text.md --doc-type slides` |
| 書いたファイルを検査だけする | `jpipe certify file.md` |
| 今どの工程か | `jpipe status` |

## hook の動き

| 時点 | すること |
|---|---|
| 依頼を受けたとき | 日本語の依頼に、返答の決まり (約200字) を注入する。文書・レポート・スライドなどを作る依頼なら、工程の始め方も注入する |
| ファイルを書いたとき | 日本語の .md・.txt・.html を記録する。SKILL.md・CLAUDE.md・`.claude/` の下などは除く |
| 返答を終えるとき | 記録した文書が検査を通っていなければ、一度だけ止めて `certify` か工程に案内する。返答の本文は yomiyasu の lint にかけ、出た癖を次の返答の決まりに足す |

依頼に `#nopipe` と書いたターンでは、hook は何もしません。環境変数 `NIHONGO_PIPELINE=off` で全体を止められます。

設定は `~/.config/nihongo-pipeline/config.json` に書きます。主な項目は次のとおりです。

| 項目 | 既定 | 意味 |
|---|---|---|
| `chat_mode` | `feedback` | `rules` (決まりの注入だけ)、`feedback` (lint の結果を次の返答に渡す)、`block` (指摘が `chat_block_warn` 件以上なら出し直させる)、`off` |
| `docs_mode` | `block` | `block` (検査を通すまで終わらせない)、`remind` (知らせるだけ)、`off` |
| `certify_max_warn` | `0` | `certify` で通す warn の件数の上限 |
| `reviewer` | `auto` | `off` で別のモデルを使わない |
| `reviewer_command` | `null` | 別のモデルを呼ぶコマンド。例: `["gemini", "-p", "{prompt}"]` |
| `explain_skill` | `null` | explain の工程で使う手順書。元の explain-before-shorten を使うならそのパス |

## 別のモデルがない環境

agy・gemini・codex のどれもない環境、または呼び出しに失敗したとき (認証切れ、時間切れ、JSON として読めない出力) は、判定の工程を「省いた」と記録して先に進みます。`--draft external` を指定していた場合は、下書きを Claude が担当します。省いた理由と切り替えた理由は最後に出る工程の記録に残るので、利用者への報告から漏れません。別のモデルの指摘のうち、引用した語句が本文にないものは、読み違いとみなして捨てています。

## 限界

- 検査が拾うのは決まった形だけです。指摘が0件でも、意味が保たれたことや読みやすいことの証明にはなりません。
- 表示し終えた返答に lint をかけるので、`chat_mode` が `block` でなければ、結果を使えるのは次の返答からです。
- 依頼の種類は語句で見分けています。文書の依頼を見逃したときも、書いたファイルは返答を終えるときに検査されます。
- 工程を全部通すと、短い文書で数分、別のモデルの判定を含めると10分を超えることがあります。

## テスト

```
NIHONGO_YOMIYASU_DIR=/path/to/yomiyasu/skills/yomiyasu python3 -m unittest discover tests
```

natural-japanese と別のモデルは、テストでは `tests/stubs` の代わりのスクリプトを使います。

## ライセンス

MIT。使う部品と参考にした文書は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) にあります。
