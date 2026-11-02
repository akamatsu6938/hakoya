# PowerShell でファイルを書き換えて、pyproject.toml を壊した（2026-11-02）

## 起きたこと
- PowerShell 5.1 の `(Get-Content f -Raw) -replace ... | Set-Content -Encoding utf8 f` で pyproject.toml に1行足した。
- ① `Set-Content -Encoding utf8` が先頭に BOM を付けた → pytest が設定を読めなくなった。
- ② `Get-Content` が UTF-8（BOM なし）のファイルを Shift_JIS として読み、日本語の説明文が文字化けしたまま書き戻された。
- 壊れたままコミットしてしまった（テストの結果の行が出ていないのを見落とした）。

## 直し方
- 一つ前のコミットの pyproject.toml に戻し、Python（`read_text/write_text(encoding="utf-8")`）で1行足し直した。

## 次から
- 日本語を含むファイルの書き換えは、PowerShell の Get-Content / Set-Content を使わない。Edit ツールか Python で行う。
- テストを走らせたら、「○ passed」の行が出たことを必ず確かめてからコミットする。出ていなければ、出力をファイルに書いて中身を見る。
