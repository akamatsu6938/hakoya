# 技術の選定と決めたこと

新しい順に上へ足していく。

## 2026-11-02 glb の読み取りと平面の計算に使うライブラリ
- **決めたこと**：glb の読み取りは **pygltflib**、座標の計算は **numpy**、平面（2D）の計算は **shapely 2**、設定ファイルは **PyYAML**、テストは **pytest**。
- **理由**
  - pygltflib：ノード名（`typeE○○`）・行列・マテリアル名（部位のコード）・Walk in home の付加情報（`WIH_version_info`）を、glb の形のまま取り出せる。部材ごとの「出どころ」（`glb:typeETate#27`）を正確に残すのに向いている。
  - shapely 2：SPEC §4-1 の壁の作り直し（帯にする・合わせる・膨らませて縮める）が、そのまま関数の呼び出しで書ける。斜めの壁も同じ書き方で扱える。
- **選ばなかったもの**：trimesh（glb の読み取りに使うと、部材をまとめたり名前を付け替えたりするため、出どころを追いにくい）。3D の立体づくり（11/4〜）では改めて比べる。
- **確かめたこと**：どれも Python 3.14（Windows）用の完成品（wheel）があり、入る（2026-11-02、pip で確認）。

## 2026-11-02 開発の見張り（フック・pre-commit）
- **決めたこと**：Claude Code のフックは Python で書き、`.venv` の Python で動かす。git の pre-commit は `.githooks/` に置き、`git config core.hooksPath .githooks` で有効にする。
- **理由**：PowerShell と Git Bash の両方から同じように動き、テスト（pytest）で「本当に止まるか」を確かめられる。`.git/hooks` はリポジトリに入らないため、`.githooks/` に置いて2台の PC で共有する。
- **止める範囲**：見張り（書き込む前）は「glb・jww への書き込み」「data/ の中への書き込み（local_paths.yaml だけは例外）」「git add -f と data/・glb・jww・3mf の追加」。pre-commit は「data/・図面・模型・画像ファイル」「ng_words の言葉」「API キーらしき文字列」。
- **出してはいけない言葉**：`data/local_paths.yaml` に `ng_words:` の一覧を足した（SPEC にない項目）。検査で見つけても、言葉そのものは画面に出さない。

## 2026-11-02 ソフトの名前
- **HAKOYA（箱家）**。フォルダ・リポジトリ名は `hakoya`。
- 理由：「箱」＋「家」で家の模型だとすぐ分かり、短く言いやすい（作者が「箱庭」の方向から選んだ）。
