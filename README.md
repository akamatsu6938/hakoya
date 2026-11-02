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
5. 実データの置き場所を書く：`data\local_paths.yaml` を作る（コミットされません）。
   物件の仮名ごとに glb と図面の場所、`ng_words`（施主名・住所など、出してはいけない言葉）を書く。
6. テストを走らせる（全部通れば準備完了）
   ```powershell
   $env:PYTHONUTF8 = "1"
   .\.venv\Scripts\python -m pytest
   ```
   `data\local_paths.yaml` がない PC では、実データを使うテストは飛ばされます。

## 守っていること
- 元の glb・jww には書き込みません。`data/` の中身はコミットしません。
- 施主名・住所などは、画面・出力・ログに出しません（コミット前に自動で検査します）。
