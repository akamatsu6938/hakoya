# HAKOYA（箱家）

Walk in home のデータから、1/50 の住宅模型の 3D プリント用データ（3MF）を作るソフト。
形の計算はコード、検品と直す案は Claude（Powered by Claude）。

> いまは開発中です（Built with Claude: Japan 2026 の提出作品）。仕様は [SPEC.md](SPEC.md)。

## セットアップ（Windows・PowerShell）

Python 3.11 以上と git が入っていることが前提です。

1. 入っているか確かめる
   ```powershell
   python --version
   git --version
   ```
2. このフォルダに移動して、仮想環境（このソフト専用の Python の置き場）を作る
   ```powershell
   cd C:\Users\<あなた>\hakoya
   python -m venv .venv
   ```
3. 使う部品（ライブラリ）を入れる
   ```powershell
   .\.venv\Scripts\python -m pip install -e ".[dev]"
   ```
4. コミット前の検査を有効にする（**PC ごとに1回だけ**）
   ```powershell
   git config core.hooksPath .githooks
   ```
5. 実データの置き場所を書く：画面の「設定」タブ →「物件の登録」で、glb（と図面のフォルダ）を選んで登録できます（`data\local_paths.yaml` に書き足されます。コミットされません）。手で書くときは下の例のとおりに。
   物件の仮名ごとに glb と図面の場所、`ng_words`（施主名・住所など、出してはいけない言葉）を書く。
6. テストを走らせる（全部通れば準備完了）
   ```powershell
   $env:PYTHONUTF8 = "1"
   .\.venv\Scripts\python -m pytest
   ```
   `data\local_paths.yaml` がない PC では、実データを使うテストは飛ばされます。

   `data\local_paths.yaml` の書き方の例（値は架空です）：
   ```yaml
   ng_words: ["（施主の名字）", "（住所の一部）"]   # 画面・出力・ログに出してはいけない言葉
   物件A:
     glb: C:\物件\物件A\物件A.glb
     jww_folder: C:\物件\物件A\図面
     jww: {plan: 平面図.jww, elevation: 立面図.jww, section: 断面図.jww}
   ```

## 使い方

1. `start.bat` をダブルクリックする（開発中）。ブラウザで HAKOYA の画面が開きます。exe 版は `HAKOYA.exe` をダブルクリック。
   初めてのときは「設定」タブの「物件の登録」で、仮名・glb・図面のフォルダ（なくてもよい）・出してはいけない言葉（施主名・住所）を入れて登録します。元のファイルは読むだけです。
2. 物件を選んで「読み込んで組み立てる」。glb から壁・開口・屋根・基礎を作り直し、1/50 の模型の部品（基礎・1階・屋根など）を組み立てます。
3. 3D と平面図で形を見る。「パーツを離す」で部品ごとに見られます。
4. 直す：
   - **検品**：Claude が東・西・南・北・平面・印刷の6つの面を同時に見比べ、食い違いを根拠つきで挙げます。直す案は「承認」で反映。
   - **表で直す**（AI なし）：窓・ドア・壁・階（床の高さ・天井・階高）・屋根（勾配・向き・軒先の高さ・軒の出）。
   - **囲んで直す**：平面図や 3D で部材をぐるっと囲み、一言（例「この壁厚だけおかしい。ほかとそろえて」）。
   - 直した内容は「修正の履歴」に1件ずつ残り、取り消せます。glb を入れ直しても修正は当て直されます。
5. 「書き出し」タブで 3MF（Bambu Studio 形式は外壁だけファジースキン設定済み）・STL・GLB を保存します。

## できること（2026-11 時点）
- Walk in home の glb（2024 版で確認）：平屋・2階建て・下屋、片流れ・切妻・寄棟・L字の屋根（切妻・寄棟は実物の glb でも確認。Walk in home は屋根を面ごとに別の部材で書き出すので、まとめて読みます）。
- まとめて検査：`python -m tools.batch_check`（登録した全物件を組み立て・検査・図面の照合まで通し、`data\out\_batch\<日時>\report.md` に表でまとめます。Claude は使いません）。
- glb にありえない形（同じ所に壁が2枚・はみ出す開口など）を見つけて「要確認」に出します（直しはしません）。
- 図面（.jww）：平面図を模型に位置合わせし、窓の注記（品番と上端 FL+）と模型の窓の上端を照合。表題欄は読み捨てます。
- 会社ルール：壁厚・床の高さなどの数値は `rules/*.yaml`、判断の仕方は `skills/`。承認したものだけ「学んだルール」として残ります。
- AI のつなぎ方は3通り：Claude（自分のプラン。自分用だけ）／Claude（API キー）／AI なし。**AI なしでも最後まで使えます。**

## Claude の使い方
- 数値（座標・寸法・高さ）はコードが計算します。Claude は「どの部材の・どの値を・何に合わせて直すか」と数値の出どころ（例：外壁の多数派、図面の注記）を返すだけです。
- Claude が使えるのは HAKOYA の道具（家の要約・部材の一覧・面の図・図面の画像・検査の結果・直す案・質問）だけです。
- 詳しくは [docs/claude-usage.md](docs/claude-usage.md)。

## exe を作る（配るとき）
- 自分用（AI の接続3つ）：`.\.venv\Scripts\pyinstaller packaging\hakoya_self.spec --distpath dist --workpath build --noconfirm`
- 社外用（API キーと AI なしだけ。Claude Code 本体を入れない）：
  ```powershell
  .\.venv\Scripts\pyinstaller packaging\hakoya_external.spec --distpath dist\external --workpath build\external --noconfirm
  .\.venv\Scripts\python -m tools.check_external_build dist\external\HAKOYA_external
  ```
  2つ目のコマンドで、Claude Code 本体・API キーらしい文字・data フォルダが入っていないことを確かめます。

## 公開の前に
- `.\.venv\Scripts\python -m tools.history_check` で、履歴全体（消したファイルも）に施主名・住所・API キー・メールアドレス・図面や画像がないかを調べます。言葉そのものは表示しません。
- コミットの作者のアドレスは GitHub の公開用のアドレス（noreply）です（2026-11-04 に履歴を書き換えました）。
- LICENSE は付けていません（大会の審査のための一時的な公開で、終わったら非公開に戻すため）。使ってよい条件は示していません。

## 守っていること
- 元の glb・jww には書き込みません。`data/` の中身はコミットしません。
- 施主名・住所などは、画面・出力・ログに出しません（コミット前に自動で検査します）。図面の表題欄は読み捨てます。
- API キーはコード・設定ファイル・exe に入れません。使う人が設定画面で入れ、Windows の資格情報マネージャーに保存します。
