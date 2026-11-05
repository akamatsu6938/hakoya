# 2026-11-05 テストの失敗の表示に、物件のファイル名（施主名入り）が出た

## 何が起きたか
- 全体のテストで `tests/test_two_story_builder.py` が失敗した（物件B の glb が、local_paths.yaml に書いた場所になかった）。
- pytest は、失敗した関数に渡した値をそのまま表示する。`glb_working_copy(files)` の `files`（`PropertyFiles`）の表示に、
  glb の場所（元のファイル名＝施主名入り）が入っていて、Claude Code の画面（会話の記録）に出た。
- 施主名は、決まり（CLAUDE.md の 2）で「ログ・画面・出力に出さない」もの。どこにも書き写していない。

## 原因
- `PropertyFiles` は dataclass のままで、表示（repr）に場所がそのまま入っていた。
- pytest の既定の表示（長い表示）は、失敗した所の関数の引数の値を出す。今までは失敗が起きなかったので気づかなかった。

## 直したこと
- `PropertyFiles` の表示を「あり／なし」と種類の数だけにした（core/paths.py）。
- pytest の表示を短くした（pyproject.toml の `addopts = "--tb=short"`。引数の値を出さない）。

## 次から
- 場所や名前を持つものを作るときは、表示（repr）に場所を出さない。
- 実データを使うテストが失敗したら、表示を読む前に、名前が出ていないか気をつける（出たら、書き写さずに 作者に知らせる）。
