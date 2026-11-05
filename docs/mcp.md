# HAKOYA を自分の Claude Code から使う（MCP）

HAKOYA（他社利用用）を使う会社の方が、**自分の Claude Code** を HAKOYA につないで、物件の確認・検品・直す・書き出しまでを
Claude と話しながら進めるための説明です。

## 1. これは何か

HAKOYA の中の道具（家の要約・面の図・コードの検査・直す案・指摘の送付・修正と取り消し・書き出し）を、
Claude Code から使えるようにする「つなぎ口」（MCP サーバー）です。Claude Code の中では `mcp__hakoya__…` という名前の道具になります。

できること：
- 物件の一覧を見る、家の要約・部材の一覧・6つの面の図（南・北・東・西・平面と、glb の元の形）を見る、コードの検査を読む
- 検品する（手順はプロンプト `/mcp__hakoya__inspect`）。指摘は HAKOYA の画面の「検品」タブに出ます
- 直す案を試す・指摘を承認／却下／あとでにする・修正を記録する・取り消す（**どれも人の許可が要ります**）
- 組み立て直す・書き出したファイルを確かめる・HAKOYA の画面でその物件を開くアドレスを受け取る

## 2. 規約の上での位置づけ

- **HAKOYA は Claude を呼びません。** 道具を渡すだけです。
- Claude を動かすのは、あなたの会社の人が自分で入れてログインした **Anthropic の Claude Code** で、
  **あなたの会社の Claude のプラン（または API キー）の利用枠**を使います。
- HAKOYA は Claude のログイン情報・API キーに触れません。集めません。作者の Claude のプランや認証を通すこともありません。
- Claude Code を使うときは、あなたの会社と Anthropic の利用規約・設定に従ってください。

## 3. 送られるもの・残るもの（必ず読んでください）

- 道具を使うと、**模型と glb（元の形）の面の図、部材の一覧と数値、図面の画像（表題欄を切り落としたもの）と寸法・注記**が、
  あなたの会社の Claude Code を通して Claude（Anthropic）に送られます。
- 面の図などの画像は、Claude Code の記録のフォルダ（`%USERPROFILE%\.claude\projects\…\tool-results`）にも残ります。
  お客様の家の形が写っているので、その PC の外に出さないでください。
- HAKOYA は、道具の結果を返す前に「出してはいけない言葉」（物件の登録で入れた施主名・住所など。`data\local_paths.yaml` の `ng_words`）が
  入っていないかを調べ、入っていれば結果を渡しません。**物件を登録するときに、施主名・住所を必ず入れてください。**
- 結果にファイルの場所（フォルダの名前）は入れません。画像は中身だけを渡します。

## 4. つなぎ方

### 4-1. 作業用のフォルダを、HAKOYA のフォルダとは別に作る

Claude Code は、起動したフォルダのファイルを読み書きする道具（Read・Edit・Bash など）も持っています。
HAKOYA のデータのフォルダにはお客様の glb・図面が入っているので、**HAKOYA のフォルダの中や、その上のフォルダで Claude Code を起動しないでください。**
HAKOYA 専用の作業用フォルダを作り、そこで起動します。

```powershell
mkdir C:\HAKOYA_claude
cd C:\HAKOYA_claude
```

以下では、HAKOYA を `C:\HAKOYA`（exe 版は `C:\HAKOYA_external`）に置いたとして書きます。自分の置き場所に読み替えてください。

### 4-2. HAKOYA をつなぐ

**Python 版**（HAKOYA のフォルダに `.venv` がある場合）：

```powershell
cd C:\HAKOYA_claude
claude mcp add --scope project hakoya -e HAKOYA_MODE=other -e PYTHONUTF8=1 -e PYTHONPATH=C:\HAKOYA -- "C:\HAKOYA\.venv\Scripts\python.exe" -m app.mcp_server
```

**exe 版**（社外用の HAKOYA_external）：

```powershell
cd C:\HAKOYA_claude
claude mcp add --scope project hakoya -e HAKOYA_MODE=other -- "C:\HAKOYA_external\HAKOYA_external.exe" --mcp
```

- `HAKOYA_MODE=other`：他社利用用の HAKOYA につなぎます（画面の口は 8766）。
- `PYTHONPATH`：Python 版だけ。HAKOYA のフォルダを書きます（Claude Code がどのフォルダでサーバーを起動しても動くように）。
- データを別の場所に置いているときは `-e HAKOYA_DATA_DIR=<データのフォルダ>` を足します。
- `--scope project` にすると、作業用フォルダに `.mcp.json` ができます（次の 4-3）。初めて使うとき、Claude Code が「このフォルダの MCP サーバーを使うか」を聞くので、許可してください。

### 4-3. `.mcp.json` の例（手で書くとき）

Python 版：

```json
{
  "mcpServers": {
    "hakoya": {
      "command": "C:\\HAKOYA\\.venv\\Scripts\\python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": {"HAKOYA_MODE": "other", "PYTHONUTF8": "1", "PYTHONPATH": "C:\\HAKOYA"}
    }
  }
}
```

exe 版：

```json
{
  "mcpServers": {
    "hakoya": {
      "command": "C:\\HAKOYA_external\\HAKOYA_external.exe",
      "args": ["--mcp"],
      "env": {"HAKOYA_MODE": "other"}
    }
  }
}
```

### 4-4. 守りの設定（作業用フォルダの `.claude\settings.json`）

HAKOYA の道具だけを使わせ、ファイルを直接読む道具・コマンドを止めます（**おすすめ**）。

```json
{
  "permissions": {
    "allow": ["mcp__hakoya__*"],
    "deny": [
      "Read(//c/HAKOYA/data/**)", "Edit(//c/HAKOYA/data/**)", "Write(//c/HAKOYA/data/**)",
      "Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep",
      "Bash", "PowerShell", "WebFetch", "WebSearch"
    ]
  }
}
```

- `allow` の `mcp__hakoya__*`：HAKOYA の道具を、毎回たずねずに使えるようにします（起動のときの `claude --allowedTools "mcp__hakoya__*"` と同じ）。
- **模型を変える4つの道具**（`apply_override`・`undo_override`・`decide_finding`・`rebuild`）は、HAKOYA の側で「毎回たずねる」印を付けています。
  `allow` に入っていても、**毎回 Claude Code が許可を求めます**（「今後たずねない」は出ません）。たずねない動き方（dontAsk）では、この4つは断られます。
- `deny`：Claude Code の決まりは「禁止 → たずねる → 許可」の順に見るので、`allow` より強く効きます。
  - `Read(//c/HAKOYA/data/**)` などは、HAKOYA のデータのフォルダ（`C:\HAKOYA\data`）を指します。Windows では `C:\` を `//c/` と書きます。
    exe 版は `//c/HAKOYA_external/data/**` にします。Read の禁止は Grep・Glob にも効きます。
  - Bash・PowerShell は、場所を名指ししない読み方（フォルダごとの検索・スクリプト）までは止めきれないので、丸ごと止めます。
  - ほかの仕事にも同じフォルダの Claude Code を使う場合は、`Read`・`Edit`・`Write`・`Glob`・`Grep`（場所の付かないもの）を外しても構いません。
    そのときもデータのフォルダの3行と、Bash・PowerShell は残してください。
- 設定したら、Claude Code の中で `/permissions` を開き、決まりが入っているか確かめてください。

### 4-5. つながったか確かめる

```powershell
cd C:\HAKOYA_claude
claude mcp list
```

`hakoya` が「Connected」（つながった）になれば準備完了です。Claude Code の中では `/mcp` で道具の一覧が見えます。
つながらないとき：HAKOYA の置き場所（`command`・`PYTHONPATH`）が合っているか、`.venv` があるかを確かめてください。

## 5. 使い方の例

HAKOYA の画面（`http://127.0.0.1:8766/`）は開いておくと便利です（開いていなくても道具は動きます）。

```text
あなた> /mcp__hakoya__inspect 物件A

Claude  get_building・run_checks・get_code_findings で家の要約とコードの検査を読み、
        6つの面（南・北・東・西・平面・印刷）ごとに render_view で模型の図と glb の図を見比べ、
        get_elements で部材の番号を確かめる。直す案は propose_override で試す（数値はコードが計算）。
        最後に report_findings で指摘 4 件を HAKOYA に送る。
        → HAKOYA の画面の「検品」タブに 4 件が出る（画面を読み込み直す）

あなた> F2 の直す案を承認して

Claude  decide_finding（F2・承認）
        → Claude Code が「この道具を使ってよいか」をたずねる → あなたが「はい」
        → 修正 OV3 として記録し、模型を組み直す
Claude  open_in_hakoya → http://127.0.0.1:8766/?alias=%E7%89%A9%E4%BB%B6A
        → 画面を読み込み直すと、直した模型と指摘の状態（承認）が出る

あなた> やっぱり取り消して

Claude  undo_override（OV3）→ 許可 → 元の模型に戻り、F2 は「未確認」に戻る
```

数値を自分で決めたいとき（例：「袖壁を 900 にしたい」）は、その数値を文に書いてください。
Claude は、あなたが書いた数値だけを使えます（HAKOYA が、その数値があなたの文にあるかを確かめます）。

## 6. 道具の一覧

| 道具 | すること | 模型を変えるか |
|---|---|---|
| list_properties | 登録してある物件の一覧（組み立て済みか・部材の数・指摘の数） | 変えない |
| get_building | 家の要約（階・外形・壁と開口の数・屋根・会社ルール） | 変えない |
| get_elements | 部材の一覧（面を渡すと、その図に写る部材と図の上の範囲） | 変えない |
| render_view | 面の図（模型か glb。部材を赤い枠で強調できる） | 変えない |
| get_drawing_region | 図面の画像と寸法・注記（高性能読み込みの物件だけ） | 変えない |
| run_checks | コードの検査の結果 | 変えない |
| get_code_findings | コードの検査から作った指摘 | 変えない |
| get_findings | いま HAKOYA の検品タブに出ている指摘と状態 | 変えない |
| list_overrides | 人が承認した修正の一覧 | 変えない |
| propose_override | 直す案を試す（保存しない。数値はコードが計算） | 変えない |
| ask_question | 人に聞く（質問は検品タブにも出る） | 変えない |
| report_findings | 検品の結果を HAKOYA に送る（前の指摘の一覧は置き換わる） | 変えない（指摘の一覧だけ） |
| apply_override | 直す案を修正として記録し、組み直す | **変える・毎回許可** |
| undo_override | 修正を取り消して、組み直す | **変える・毎回許可** |
| decide_finding | 指摘を承認・却下・あとでにする（承認は修正になる） | **変える・毎回許可** |
| rebuild | glb を読み直して組み立て直す（人の修正は当て直す） | **変える・毎回許可** |
| export | 書き出したファイルの一覧（古ければ書き出し直す） | 変えない（書き出し直すだけ） |
| open_in_hakoya | HAKOYA の画面でその物件を開くアドレス | 変えない |

## 7. 気をつけること

- **同じ物件を、HAKOYA の画面と Claude Code で同時に直さないでください。** 別々に動いているので、後から書いた方が残ります。
  画面で検品している間は、Claude Code から直さないでください。
- 書き出したファイル（3MF など）は、HAKOYA の画面の「書き出し」タブから保存します（Claude Code にはファイルの場所を渡しません）。
- エラーのときは、日本語の説明とエラーの種類だけが返ります。分からないときは、HAKOYA の画面で同じ操作を試すか、作者に知らせてください。
- 会社ルール（壁の厚みなど）は、HAKOYA の画面の「設定」で選んだものを使います。
