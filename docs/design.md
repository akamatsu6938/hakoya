# HAKOYA 設計書（作りの説明）

HAKOYA（箱家）が**どういう形で作られているか**の説明です。審査の人と、HAKOYA を読んだり直したりする人に向けて書いています。
使い方は [利用マニュアル（docs/manual.md）](manual.md)、細かい仕様と確かめた事実は [SPEC.md](../SPEC.md)、技術を選んだ理由は [docs/decisions.md](decisions.md) にあります。

---

## 1. ひとことで

- Walk in home の glb（家の 3D データ）を読み、壁・窓とドア・屋根・基礎を**コードで作り直して**、1/50 の模型の部品（3MF・STL・GLB）にする Windows のソフトです。
- 中身は手元だけで動く Web アプリです（Python の FastAPI ＋ 素の HTML/JS ＋ three.js）。起動するとブラウザが開きます。サーバーは `127.0.0.1`（この PC の中）にだけ開きます。
- **形の計算はコード（模型職人の手）、見比べと直す案は Claude（模型職人の目）、決めるのは人**、と役目を分けています。

## 2. 人・コード・Claude の役目

| だれ | すること | しないこと |
|---|---|---|
| **人** | 物件の登録（仮名・出してはいけない言葉）／指摘の承認・却下・あとで／直す案の承認・却下／質問に答える／表で数値を直す／会社ルールの案の承認／分け目を動かす | — |
| **コード** | glb の読み取りと壁の作り直し／すべての数値（座標・寸法・高さ）の計算／組み立て・形の検査・分割・書き出し／直す案の数値を出どころから計算し直す／案が効くかの確かめ（効かない案は断る・形が変わらない直しは取り消す）／出してはいけない言葉の見張り | 人の承認なしに模型を直す |
| **Claude** | 6つの面の絵で模型と元の形を見比べ、指摘を根拠つきで挙げる／直す案（どの部材の・どの値を・何に合わせて）を出す／決まらないことは選択肢つきで質問する／会社ルールの案を言葉にする | 数値を作る／ファイルを直接読む・書く／HAKOYA の道具（MCP）以外を使う |

## 3. 全体の流れ

```
 Walk in home の glb（元のファイル。読むだけ）
        │  data/cache/<物件>/source.glb に写してから読む
        ▼
 adapters/  読み込み（glb_reader）→ 部材の仕分け → 壁の作り直し（walls）
            → 開口（openings）・屋根（roof）・基礎とポーチ（exterior_parts）
        │
        ▼
 building.json（中間データ：家の形。すべての値に出どころ）
        │  ＋ overrides.json（人が承認した直し。上から当て直す）
        │  ＋ 会社ルール（rules/<会社>.yaml）
        ▼
 core/      組み立て（builder / builder_multi）→ 形の検査（checks・shape_checks）
            → 印刷用の分割（print_split・body_split）→ 書き出し（export・threemf・bambu）
        │
        ▼
 out/<物件>/model/：model.3mf・model_bambu.3mf・部品ごとの STL・model.glb・checks.json

 検品・直す案（Claude）
   core/render が6つの面の絵（模型と glb）を作る
   → Claude は HAKOYA の道具（core/ai/tools.py。MCP の道具）だけで調べる
   → 指摘は findings.json、頼んだことと答えは fix_requests.json
   → 人が承認 → overrides.json → 組み直し
```

工程の受け渡しは、すべてファイル（7 章）を通します。画面・MCP・コマンドのどこから動かしても、同じファイルを読み書きします。

## 4. 大事な決まり

1. **数値はコードが計算する。** Claude は「どの部材の・どの値を・何に合わせて直すか」と、数値の出どころ（`value_source`）だけを返します。出どころは次のどれかです（`core/ai/tools.py`）。
   - `majority:<種類>`：同じ種類の壁の厚みの多数派
   - `rule:<項目>`：会社ルールの値（例：`rule:openings.door_top_fl_mm`）
   - `glb`：自動で作った元の値に戻す
   - `number`：人が一言に書いた数値（その数値が人の文にあるかを、コードが確かめる）
   - `jww:<図面>:<番号>`：図面の寸法・注記（高性能読み込みのとき）
   - `to_roof`：壁の上端を屋根まで
2. **すべての値に出どころ（`source`）か、使ったルール（`assumption`）を付ける。** glb から来た値は `glb:typeETate#27` の形です。
3. **元のファイル（glb・図面）には書き込まない。** 作業は写しで行います。
4. **自動の値（building.json）と人の直し（overrides.json）を分けて持つ。** glb を読み直しても、直しは上から当て直します。直しは1件ずつ取り消せます。
5. **Claude に渡すのは HAKOYA の道具だけ。** ファイルの読み書き・コマンドは使わせません（8 章・13 章）。
6. **出してはいけない言葉を、画面・ファイル・Claude に送る文に出さない**（12 章）。

## 5. 使っている部品

| 部品 | 使い道 |
|---|---|
| FastAPI・uvicorn | 手元のサーバー（`app/server.py`） |
| three.js（`app/static/vendor/` に同梱） | 画面の 3D 表示・押して選ぶ・囲む |
| pygltflib | glb の読み取り |
| shapely | 平面（2D）の計算（壁の帯・部屋の形・囲みの重なり） |
| manifold3d・trimesh | 閉じた立体の計算・STL と GLB の書き出し |
| Pillow | 検品の面の絵（ブラウザなしで描く） |
| ezjww・pdfplumber | 図面（jww・文字を選べる PDF）の読み取り（高性能読み込み） |
| claude-agent-sdk・anthropic | Claude の呼び出し（自分用のプラン接続・API キー接続） |
| mcp | 自分の Claude Code とつなぐ口（`app/mcp_server.py`） |
| keyring | API キーを Windows の資格情報マネージャーにしまう |

選んだ理由は [docs/decisions.md](decisions.md) にあります。

## 6. フォルダの役目と主なファイル

| フォルダ | 役目 | 主なファイル |
|---|---|---|
| `app/` | 画面とサーバー、MCP の入り口 | `server.py`（窓口）・`mcp_server.py`（MCP）・`static/index.html`・`static/app.js`・`static/pick3d.js`（3D で押す・囲む） |
| `adapters/` | glb を読んで家の形（building.json）を作る | `walkinhome.py`（全体）・`glb_reader.py`・`walls.py`（壁の作り直し）・`openings.py`・`roof.py`・`exterior_parts.py` |
| `core/` | 組み立て・検査・分割・書き出し・記録 | `builder.py`（平屋）・`builder_multi.py`（2階建て）・`checks.py`・`shape_checks.py`・`export.py`・`threemf.py`・`bambu.py`・`print_split.py`・`body_split.py`・`overrides.py`・`findings.py`・`fix_requests.py`・`answers.py`・`shape_diff.py`・`render.py`・`lasso.py`・`pick.py`・`ids.py`・`paths.py`・`settings_store.py`・`privacy.py`・`registry.py`・`rules.py`・`rule_stats.py`・`rule_check.py`・`jww.py`・`pdf_drawing.py`・`drawing_check.py` |
| `core/ai/` | Claude とのつなぎと、Claude に渡す道具 | `tools.py`（道具の中身）・`agent.py`（プランの接続・Agent SDK）・`api_runner.py`（API キーの接続）・`lasso.py`（直す案）・`rule_draft.py`（会社ルールの案）・`mcp_tools.py`（MCP の道具）・`progress.py`（検品の進み具合） |
| `rules/` | 会社ルールの数値と AI の設定 | `bkoumuten.yaml`（B工務店（架空））・`ai_models.yaml`（仕事ごとのモデル・費用の上限・料金） |
| `skills/` | アプリの Claude が読む Skill（判断の仕方） | `inspection/`（検品の手順）・`model-rules/`（模型の決まり）・`company-bkoumuten/`（会社の考え方） |
| `tools/` | 開発と確かめの道具 | `batch_check.py`（全物件をまとめて検査）・`history_check.py`（git の履歴全体の点検）・`precommit_check.py`・`check_external_build.py`・`print_estimate.py`（印刷の時間・材料の見込み）・`plan_preview.py`（平面図の絵） |
| `tests/` | pytest のテスト | `conftest.py`（実データと自社の決まりがないときに飛ばす決まり） |
| `samples/` | 同梱の見本の家（架空） | `sample_house/house.glb`・`sample_house/out/`（組み立てと検品の結果）・`sample_house/screens/`（画面の絵） |
| `packaging/` | exe の作り方の設定 | `hakoya_self.spec`・`hakoya_external.spec` |
| `data/` | 使う人のデータ（**リポジトリに入らない**） | 7 章 |

## 7. 記録の形（ファイルの中身）

物件ごとの記録は `data/out/<仮名>/` に置きます（起動のしかたで `data/` の場所が変わります。11 章）。どれも UTF-8 の JSON で、一時ファイルに書いてから入れ替えます（書きかけを読まないため）。下の例の値はすべて架空です。

| ファイル | 中身 | 書くのはだれか |
|---|---|---|
| `building.json` | 家の形（壁・開口・屋根・基礎・ポーチ・部屋・高さ）と出どころ | コード（読み込み）。前回分は `building.prev.json` に残す |
| `overrides.json` | 人が承認した直し（1件ずつ・取り消せる） | 人が承認したときに、コードが書く |
| `findings.json` | 検品の指摘・質問・まとめ | 検品（Claude かコード）。指摘の状態は人の決定で変わる |
| `fix_requests.json` | 囲んで直す・文字で直すで頼んだことと Claude の答え | 人の依頼と Claude の答え。案の状態は人の決定で変わる |
| `answers.json` | 検品の Claude の質問への人の答え | 人 |
| `model/` | `model.3mf`・`model_bambu.3mf`・部品ごとの STL・`model.glb`・`checks.json`（検査・部品・分け目） | コード（組み立て・直しのたびに作り直す） |

そのほか：`reading.json`（高性能読み込みの入れ切り）・`print_split.json`（人が動かした分け目）・`print_options.json`（外壁をざらざらにするか。下）・`inspect/`（検品の面の絵と部材の位置）・`inspect_log.jsonl`・`lasso_log.jsonl`（やりとりと費用の記録）。
起動ごとの `data/settings.json`（会社ルール・自動の検品・最近の物件。API キーは入れない）と、`data/rules/<会社>.learned.json`（承認した学んだルール）もあります。

**building.json**（`adapters/walkinhome.py`。座標は実寸 mm・平面図の向き）

```json
{
  "source": {"glb": "物件A.glb", "company_rules": "bkoumuten"},
  "levels": [{"name": "1F", "fl_mm": 600, "ceiling_mm": 2400, "source": "glb:typeEHeya#13"}],
  "walls": [{"id": "W1", "level": "1F", "kind": "exterior", "start": [0, 0], "end": [9100, 0],
             "thickness_mm": 190, "bottom_mm": 450, "height": "to_roof", "source": "glb:typeEGaih#8",
             "thickness_rule": "walls.exterior_thickness_mm"}],
  "openings": [{"id": "O1", "wall": "W1", "kind": "window", "offset_mm": 1820, "width_mm": 1690,
                "sill_fl_mm": 900, "head_fl_mm": 2000, "source": "glb:typeETate#41"}],
  "roof": {"shape": "gable", "pitch": [4, 10], "eave_mm": {"S": 600, "N": 600, "E": 0, "W": 0}, "source": "glb:typeEYane#7"},
  "foundation": {"…": "基礎の外形と高さ"},
  "assumptions": [{"field": "walls[W5]", "note": "back_to_back: glb で背中合わせの外壁の面として入っていた壁を、この内壁にまとめた"}],
  "glb_exceptions": [], "unresolved": []
}
```
- 元のファイル名は書きません（`<仮名>.glb` と書く）。書く前に出してはいけない言葉がないかを確かめます。
- 部材の番号（W・O・Y）は、読み直しても同じ部材なら同じ番号を引き継ぎます（`core/ids.py`。人の直しを当て直すため）。
- 会社ルールで決めた値には、使った項目（`thickness_rule` など）を付けます。glb から読めず決まりで埋めた所は `assumption`（全体の一覧は `assumptions`）に理由を書きます。
- `glb_exceptions`（glb の例外。背中合わせの外壁など）と `unresolved`（決めきれなかった所）は、コードの指摘の元になります。

**overrides.json**（`core/overrides.py`）

```json
[{"id": "OV3", "target": "O1", "field": "head_fl_mm", "from": 2000, "to": 2200,
  "value_source": "human:table", "reason": "立面図の窓の上端に合わせる",
  "origin": "finding:F2", "approved_by": "human", "at": "2026-11-06T10:12:00+09:00"}]
```
- `field` は直せる項目だけです（壁：厚み・上端・窓の上の帯／開口：幅・位置・下端・上端／階：床の高さ・天井の高さ／屋根：勾配・低い側・軒先の高さ・辺ごとの軒の出・軒天の高さ／`_delete`＝取り除く）。Claude の案で直せるのは、このうち壁・開口・軒の出・軒天と取り除くだけで、階と屋根の形は人が表で直します。
- 取り消すと、その直しを生んだ指摘は「未確認」に戻ります。

**findings.json**（`core/findings.py`）

```json
{"connection": "api_key", "summary": "指摘は2件です。…", "at": "…", "cost_usd": 0.4,
 "findings": [{"id": "F1", "view": "S", "kind": "opening", "severity": "中", "title": "南の窓の間の柱が細い",
   "message": "…", "targets": ["W2", "O3"], "evidence": [{"type": "glb", "ref": "glb:typeETate#41"}],
   "proposal": {"target": "W2", "field": "lintel_band_mm", "from": 0, "to": 100,
                "value_source": "rule:model.lintel_band_mm"},
   "confidence": "中", "status": "未確認"}],
 "code_findings": [], "questions": [{"id": "Q1", "question": "…", "options": ["…", "…"]}]}
```
- `status` は「未確認・承認・却下・あとで・依頼済み」。検品をやり直すと、ファイルは丸ごと書き直されます。
- `connection` は `claude_code`・`api_key`・`off`（コードの検査だけ）・`mcp`（自分の Claude Code から）。

**fix_requests.json**（`core/fix_requests.py`）

```json
{"next_id": 3, "requests": [{"id": "R2", "status": "done", "kind": "text", "text": "南の軒の出を 600 にして",
  "model_label": "Opus 5.5", "connection": "api_key", "selection": [],
  "proposals": [{"target": "Y1", "field": "eave_mm.S", "from": 0, "to": 600,
                 "value_source": "human:number:600", "state": "approved", "override_id": "OV4"}],
  "questions": [], "cost_usd": 0.05, "duration_s": 18}]}
```
- 依頼の `status`：`running`・`done`・`error`・`interrupted`（HAKOYA を止めた）。案の `state`：`pending`・`approved`・`rejected`・`no_change`（形が変わらないので取り消した）・`failed`。

**answers.json**（`core/answers.py`）：`[{"question_id": "Q1", "question": "…", "options": […], "answer": "1. …", "at": "…", "inspected_at": "…"}]`。次の検品で「前に人が答えたこと」として Claude に渡します。

**print_options.json**（`core/print_options.py`。★11/6）：`{"fuzzy_skin": true}`。印刷タブの「外壁の仕上げ」で人が決めた、外壁をざらざら（ファジースキン）にするか。
- ないとき（決めていない）は会社ルール（`model.exterior_finish` が `plaster` なら付ける）。「既定に戻す」でこの値を消します。
- 変わるのは 3MF の外壁の部品の設定（`fuzzy_skin=external` など）・書き出しの案内の文・ざらざらの層の検査・保存の名前（付けた 3MF は `…_fuzzy.3mf`）だけで、形（ざらざらの層の部品）はいつも同じに作ります。
- 書き出した結果は `model/checks.json` の `"fuzzy_skin": {"on": true, "source": "property", "company_default": false}`（`source` は `property`＝物件で決めた／`company`＝会社ルールのとおり）。`parts` の中の `fuzzy_skin` は「ざらざらの層の部品か」という形の印で、こちらとは別です。

**data/local_paths.yaml**（**リポジトリに入らない**。`.gitignore`）

```yaml
ng_words: ["（施主の名字）", "（住所の一部）"]   # 全体の出してはいけない言葉
物件A:
  glb: C:\物件\物件A\house.glb
  jww_folder: C:\物件\物件A\図面               # なくてもよい
  ng_words: ["（施主の名前）"]                  # 物件ごとの言葉
```
- 人が「新しく読み込む」で入れ、コード（`core/registry.py`）が書き足します（今の中身は書き換えない）。

## 8. 検品のしくみ

1. **材料をコードが作る**（`core/render.py`）：東・西・南・北・平面の各面について、模型の絵・同じ向きから見た glb の絵・絵の中の部材の位置（番号と範囲）。印刷の面は、コードの検査（閉じた立体・細すぎる所・造形範囲など）の結果と、各面の模型の絵を見ます。
2. **面ごとの担当（サブエージェント）6人が並行して見比べる**：S・N・E・W・plan・print。各担当は Skill（検品の手順・模型の決まり・会社の考え方）を読み、道具（`get_elements`・`render_view`・`get_drawing_region`・`run_checks`・`propose_override`・`ask_question`）で確かめます。
3. **まとめ役がまとめる**：重なりを除き、重さ（高・中・低）の順に並べ、直す案か質問を付けて、決めた形の JSON で返します。
4. **コードが仕上げる**（`core/ai/agent.py` の `finalize`）：番号を振り、直す案の数値を出どころから計算し直し、対象の番号は家にある番号だけを残します。コードの検査からの指摘（`code_findings`）と合わせて `findings.json` に書きます。
5. 画面で人が1件ずつ決めます。質問への答えは `answers.json` に残り、次の検品で渡ります。

**つなぎ方ごとの違い**（中身の道具・Skill・返す形は同じ）
- API キー（`api_runner.py`）：Messages API で6人を同時に回し、まとめ役は構造化出力で JSON を返します。Skill の文はそのまま指示として渡します。使ったトークンから費用を計算し、上限（`rules/ai_models.yaml` の `max_budget_usd`、いまは $3.00）に達したら止めます。既定のモデルは、面の担当が Sonnet 5.5、まとめ役が Opus 5.5 です。
- プラン（`agent.py`。Claude Agent SDK）：6人をサブエージェントとして定義し、使える道具は HAKOYA の道具と Agent・Skill・StructuredOutput だけ。ほかの道具は PreToolUse のフックでも止めます（二重の守り）。Skill はリポジトリの外の作業用フォルダに写して読ませます。**公開の版では出しません**（11 章）。
- AI なし（`findings.run_off`）：コードの検査からの指摘だけ。
- 自分の Claude Code（MCP）：プロンプト `/mcp__hakoya__inspect` の手順で同じ道具を使い、`report_findings` で `findings.json` に書きます。

## 9. 直す流れ

**修正タブの依頼**（囲んで直す・文字で直す）
1. 依頼を作る：`POST /api/fix/<物件>/requests` → `fix_requests.json` に `running` で保存し、裏の thread で Claude を動かします（同じ物件で同時に2つまで）。囲みの中の部材はコードが決めます（`core/lasso.py`。平面図は形の重なり、3D は見えている画素の数）。
2. Claude の案：`propose_override` で案を作ります。この時点でコードが確かめ、**効かない案は断ります**（直せない項目・すでに同じ値・窓の上の帯ができない壁・人の文にない数値・範囲外の値など）。
3. 承認：人が押すと、コードが出どころから数値を計算し直して `overrides.json` に1件足し、組み直します。組み立てられなければ、その直しを取り消して元に戻します。
4. **前後の形を比べる**（`core/shape_diff.py`）：部品ごとの体積・表面積・外形の箱・重心で比べ、変わらなければ直しを取り消して `no_change` にします（画面に「この直しでは模型が変わりませんでした（取り消しました）」）。
5. 質問には人が答え、答えを添えた新しい依頼にします。HAKOYA を止めると、動いていた依頼は次に読むとき `interrupted` になります。

**検品の指摘の承認**：直す案があれば `overrides.json` に足して組み直します。直す案がなければ、その指摘のとおりの依頼を修正タブに作り、指摘は「依頼済み」にします。

**表で直す**：人が入れた数値をそのまま1件の直しにします（出どころは `human:table`）。

**外壁の仕上げ（印刷タブ）**：チェックで `POST /api/print_options/<物件>`（`{"fuzzy_skin": true|false}`）、「既定に戻す」で `DELETE`。分け目を動かすときと同じく、検品中は断り（409）、`print_options.json` を書いてから書き出し直し、書き出せなければ前の値に戻します。今の値は `GET /api/print_options/<物件>`（`on`・`source`・`company_default`）。形は変わらないので、直しの記録（`overrides.json`）には入れません。

模型を変える操作（直しの記録・指摘の決定・組み直し）は、サーバーの中で1つずつ行います（同時に2つ書いて片方が消えないように）。

## 10. 会社ルール

- 1社分のルールは2つで1組です。**数値**は `rules/<会社>.yaml`（コードが使う：壁の厚み・高さ・模型の設定・外壁の仕上げ）、**判断の仕方**は `skills/company-<会社>/SKILL.md`（Claude が読む：家の特徴・よくある例外・迷ったときの確かめ方。数値は書かず yaml の項目名で指す）。
- 公開の版に入っているのは B工務店（架空）だけです。使う会社ルールは `settings.json` の `company` で、組み立てたときの会社は `building.json` の `source.company_rules` に残ります。
- **会社ルールの学び**：
  - コードが全物件を集計し、「会社で同じ／物件ごとに違う／件数が足りない」に分けます（`core/rule_stats.py`。Claude は使わない）。
  - Claude が集計と人の答えから案を書きます（`core/ai/rule_draft.py`）。文の中の数値は、根拠にある数値だけ（コードが確かめる）。
  - **人が承認した分だけ** `data/rules/<会社>.learned.json` に残し、元の yaml と Skill は書き換えません。学べるのは会社で変わらない値だけです。
  - 新しい物件を会社ルールと比べ、「いつもと違う」所を検査・指摘に出します（`core/rule_check.py`）。家ごとに変わる値（窓の上端・屋根の勾配・軒の出など）は比べません。

## 11. 起動のしかたと版

| 起動 | 環境変数 `HAKOYA_MODE` | データ | 口 | 違い |
|---|---|---|---|---|
| `start.bat`（自社用） | `own`（既定） | `data/` | 8765 | 全部の機能。会社ルールのタブあり |
| `start_other.bat`（他社用） | `other` | `data/other/` | 8766 | 会社ルールのタブなし。選ぶまでは B工務店（架空） |
| `start_sample.bat`（見本用） | `sample` | `data/sample/` | 8767 | 登録なし・手順の帯。初回に `samples/sample_house` を写す |

- 起動のしかたは読み込みのときに決まり（`core/paths.py`）、知らない値なら止まります（打ち間違えて別のデータを開かないため）。`HAKOYA_DATA_DIR` でデータの場所を変えられます。
- **版**：自分用（self）と社外用（external）。社外用の exe は中に `EDITION` の印を持ち、いつも他社用で動きます。社外用の exe には Claude Agent SDK と Claude Code 本体を入れず、MCP の部品（mcp）は入れます（`tools/check_external_build.py` で確かめる）。
- **プランの接続（claude_code）を出す条件**（`core/settings_store.py`）：社外用の版でない・他社用の起動でない・自社の会社ルール（`rules/jisha.yaml`）がある・Claude Agent SDK が入っている、のすべて。公開リポジトリには自社の会社ルールがないので、**どの起動でも出ません**（Anthropic の決まりで、アプリの作り手はほかの人にプランのログインを使わせてはいけないため）。サーバーも、この接続の頼みを断ります。
- exe はリポジトリに入れていません（`build/`・`dist/` は `.gitignore`）。作るときは、例えば社外用なら：
  ```powershell
  .\.venv\Scripts\pyinstaller packaging\hakoya_external.spec --distpath dist\external --workpath build\external --noconfirm
  .\.venv\Scripts\python -m tools.check_external_build dist\external\HAKOYA_external
  ```

## 12. 個人情報の守り

- **出してはいけない言葉**（`data/local_paths.yaml` の `ng_words`。全体と物件ごと）を、`core/privacy.py` の `assert_clean` で見張ります。全角・半角と空白の違いをそろえて探し、見つけたら止めます（止めたときも言葉は出さず、番号だけ）。
- 見張る所：画面に送るデータ（`app/server.py` の `_safe_json`）・`building.json` を書くとき・Claude に送る文（検品・直す案・会社ルールの案）・MCP の道具が返す文・依頼と答えの記録・画面に出すエラーの文。
- 思いがけないエラーの文は画面にも黒い画面にも出さず、エラーの種類と HAKOYA のファイルの行だけを出します（エラーの文には施主名の入った場所が入ることがあるため）。元のファイル名・場所も画面に出しません。
- **図面の表題欄を切り落とす**（`core/jww.py`・`core/pdf_drawing.py`）：用紙の下端から 45mm 以内で、幅の 8割より長い横線より下を表題欄とみなし（見つからなければ下から 30mm）、その線と文字を捨てます。出してはいけない言葉を含む文字も捨てます（jww では、縮尺1の層の文字・用紙の外の文字も）。PDF の文書の情報と注釈は使いません。Claude に送る図面の絵は、残った線から描きます。
- **API キー**は Windows の資格情報マネージャーにだけしまい、環境変数は使いません。プランの接続では、環境に `ANTHROPIC_API_KEY` があれば止めます（API の料金になるのを防ぐ）。
- **開発の見張り**：
  - git の pre-commit（`.githooks/pre-commit` → `tools/precommit_check.py`）：`data/` の中・glb・jww・3mf・stl・画像・PDF・出してはいけない言葉・API キーらしい文字が入ったコミットを止めます。
  - Claude Code のフック（`.claude/`）：元のファイルと `data/` への書き込み、`git add` で data を入れる操作を止める（`guard.py`）／core と adapters を直したら速いテストを走らせる（`fast_tests.py`）／作業の終わりに開発の記録を促す（`devlog_reminder.py`）。
  - 公開の前に、git の履歴全体を `tools/history_check.py` で調べます。

## 13. 自分の Claude Code から（MCP）

- `app/mcp_server.py`（`python -m app.mcp_server`、社外用の exe は `--mcp`）が、HAKOYA の道具を MCP の道具（`mcp__hakoya__…`）として渡します。中身は `core/ai/mcp_tools.py` で、調べる道具はアプリの Claude と同じもの（`core/ai/tools.py`）です。
- **HAKOYA は Claude を呼びません。** Claude Code は、使う人が入れてログインしたもので、その人のプランか API キーで動きます。
- 物件の名前は登録したものだけ受け付けます。返す文は出してはいけない言葉とファイルの場所らしい文字を調べてから返し、画像は中身だけを返します。
- 模型を変える4つの道具（`apply_override`・`undo_override`・`decide_finding`・`rebuild`）には、Claude Code が毎回人に許可を求める印を付けています。
- つなぎ方・守りの設定は [docs/mcp.md](mcp.md) にあります。

## 14. テスト

- pytest で動かします（`$env:PYTHONUTF8 = "1"` のあと `.\.venv\Scripts\python -m pytest -q`）。
- 実データ（`data/local_paths.yaml` とその先の glb）を使うテストには `realdata` の印があり、`data/` がない PC では飛ばします（skip）。自社の会社ルールを使うテストも、その決まりがない公開の版では飛ばします（`tests/conftest.py`）。
- 学んだルールはテストでは使いません（`HAKOYA_LEARNED_RULES=off`。人の承認で結果が変わらないように）。

## 15. 詳しい仕様（SPEC.md の節）

| 知りたいこと | SPEC.md |
|---|---|
| 守ること | §2 |
| 全体の構成・処理の流れ | §3 |
| glb の読み方・壁の作り直し方（確かめた事実） | §4-1 |
| 図面（jww）・高性能読み込みと PDF | §4-2・§4-2b |
| 会社ルール・B工務店・会社ルールの学び | §5・§5-3b・§5-4 |
| 中間データ（building・overrides・findings） | §6 |
| Claude の仕事・機能の使い分け・AI のつなぎ方と規約 | §7-2・§7-3・§7-4 |
| 組み立てと模型の決まり（部品・印刷の向き・分け目） | §8 |
| 検品と手直し（原則・検品・囲んで直す） | §9-1・§9-2・§9-3 |
| 画面と起動のしかた | §10 |
| 開発の進め方・公開の前の確認 | §11 |
| 受け入れの基準 | §12 |

公開の版の SPEC.md では、§5-1・§5-2（自社の決まり）の本文を省いています。

## 16. まだできないこと・確かめていないこと

- 確かめた glb は Walk in home 2024 版が書き出したものだけです。ほかのソフトの 3D データ（IFC など）は読みません。
- 3階建ては扱えません（読み込みで「3階建てはまだ扱えません（2階建てまで）」と止めます）。
- 斜めの壁は、面の向きだけで作るので扱えるはずですが、実物の glb ではまだ確かめていません。東西南北は glb の +Y を北とみなしています（推定）。
- 窓の上の帯の直しは平屋だけです（2階建ては区域ごとに切るため、まだ作っていません）。
- 検品の指摘の承認には、まだ「形が変わらなければ取り消す」を入れていません（修正タブの依頼の承認だけ）。指摘から頼んだ依頼を承認しても、指摘は「依頼済み」のままです。
- MCP（別のプロセス）と画面が同じ物件を同時に直すのは、サーバーの中の鍵では分けられません（使う人に「同時に直さない」と書いています）。
- Claude の検品・直す案には API キーか自分の Claude Code が要ります。プランの接続は公開の版にはありません。
- 配る exe はリポジトリに入れていません（作り方の設定は `packaging/`）。
- 実物の印刷はまだしていません。はめあいの隙間・ずれ止めの三角の大きさ・ファジースキンの強さは、試し印刷で決める仮の値です。
