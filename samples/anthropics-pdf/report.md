# スキル監査レポート: pdf

## 監査対象と条件

- input: `https://github.com/anthropics/skills/tree/main/skills/pdf`
- kind: `github_tree`
- repo: `https://github.com/anthropics/skills.git`
- commit: `dbd4588f9e1033efb41dad4bef2f7947c8993d44`
- skill_subpath: `skills/pdf`
- fetched_at: `2026-10-17T19:05:01Z`
- fingerprint: `e32d8fa5f3788208a3396065683a9ae8a768d9ca6a60b9617419eaff9515f099`
- auditor.model: `claude-opus-5-5`
- auditor.runtime: `Claude Code`
- audit date: `2026-10-18`

## Summary

- **主な目的**: PDFに関するあらゆる作業（テキスト/表の抽出、結合・分割・回転、透かし、新規作成、フォーム入力、暗号化/復号、画像抽出、OCR）をエージェントに行わせるための汎用ガイド（SKILL.md:3）。
- **実際に行うこと**: 2層構成。(1) SKILL.md本文と reference.md は pypdf / pdfplumber / reportlab / qpdf / poppler / pdf-lib / pdfjs などの**コードスニペット集**で、実行可能な同梱ツールではない（SKILL.md:13-307, reference.md:1-612）。(2) forms.md は**PDFフォーム入力の強制ワークフロー**で、同梱スクリプト7本（フィールド列挙・値検証付き入力・非フィールドPDFのレイアウト抽出・bbox検査・注釈による書き込み・画像化）を手順どおりに呼ばせる（forms.md:1-294, scripts/）。
- **独自の実装や外部能力**: フォーム入力まわりの決定的な処理: AcroFormフィールドの列挙（チェックボックスのon/off値、ラジオ、選択肢を含む）と値の事前検証（scripts/extract_form_field_info.py:19-107, scripts/fill_fillable_fields.py:25-71）、非フィールドPDFからのラベル・罫線・チェックボックス座標抽出（scripts/extract_form_structure.py:20-88）、bbox重なり/高さ検査（scripts/check_bounding_boxes.py:15-56）、画像座標⇔PDF座標の変換込みのFreeText注釈書き込み（scripts/fill_pdf_form_with_annotations.py:10-90）。外部API・MCPは使わない。依存ライブラリ/CLIは同梱されず、環境側に必要。
- **期待できる付加価値**: フォーム入力では、座標系の取り違え・存在しないフィールドIDや不正なチェックボックス値の投入・文字の重なりといった典型的失敗を、手順とスクリプトで減らせると考えられる（inference、未検証）。それ以外の一般的PDF操作は、現行の有能なモデルが標準で知っている範囲のスニペットが大半で、付加価値は小さいと考えられる（inference）。ReportLabでのUnicode上付き/下付き文字が黒四角になる注意（SKILL.md:169-187）のような、小さいが具体的な落とし穴の知識もある。
- **宣伝との整合性**: スキル自体には定量的な性能主張がない。descriptionの機能列挙はおおむね本文に対応するが、「OCRで検索可能にする」は本文ではOCRでテキストを取り出すだけで、検索可能PDF（テキスト層付き）を作る手順がない（SKILL.md:3 と SKILL.md:233-250）。reference.md の「pdf-libはフォーム構造をより良く保つ」はフォーム手順（pypdfのみ）と食い違い、根拠もない。リポジトリREADMEの「本番で使われている」は検証不能で、同READMEに挙動は異なりうるという免責がある。
- **重要な疑問点**:
  - フォーム入力ワークフローは、素のエージェントや最小指示と比べて実際に入力精度（正しいフィールド・正しい位置）を上げるか（未検証）。
  - descriptionが「PDFが話題に出たら必ず」と非常に広いため、単にPDFを読むだけの依頼でも約300行の本文がロードされる。コンテキスト消費に見合うか（未検証）。
  - 依存（pypdf, pdfplumber, reportlab, pdf2image+poppler, pytesseract+tesseract, qpdf, ImageMagick, Pillow）がない環境で、スキルがどの程度破綻・回復するか（未確認）。

**起動条件**: frontmatterの description のみが選択条件。「PDFファイルで何かをしたいときは常に」「.pdfに言及したり作成を求めたら使え」と非常に広い（SKILL.md:3）。`disable-model-invocation` や `paths` 等の制限はない（SKILL.md:1-5）。PDF関連の依頼ではほぼ確実に選ばれる設計だが、PDFを読むだけの軽い依頼でも本文（314行）が読み込まれる可能性がある（inference）。forms.md・reference.md は本文から名前で参照されるのみで、必要時に読む段階的読み込み（SKILL.md:11, SKILL.md:309-314）。なお本文の参照は `REFERENCE.md` / `FORMS.md`（大文字）だが実ファイルは `reference.md` / `forms.md`（小文字）で、大文字小文字を区別するファイルシステムでは参照名と実ファイル名が一致しない。 `SKILL.md:1-5`, `SKILL.md:11`, `SKILL.md:307-314`

## Implementation

| 機能 | 実際の処理 | 実装場所 | モデルだけで代替できるか |
|---|---|---|---|
| F1 一般的PDF操作のスニペット集（pypdf / pdfplumber / reportlab / CLI） | 読み込み・テキスト抽出・結合・分割・メタデータ・回転・表抽出（pandas/Excel出力）・reportlabでの作成・pdftotext/qpdf/pdftk・透かし・画像抽出・暗号化のコード例を並べる。手順の強制や分岐はなく、参考例の提示のみ。透かしは既存の watermark.pdf を前提とし、その作り方はない（SKILL.md:256-257）。 | `SKILL.md:13-167`, `SKILL.md:189-294` | モデル標準能力で代替可能と考えられる (推論による仮説 / 確信度: 中) |
| F2 上級リファレンス（pypdfium2 / pdf-lib / pdfjs / qpdf・poppler上級 / トラブルシュート） | 追加ライブラリと上級CLIオプションの例。図抽出の例は途中で終わっている（reference.md:453-460「implementation depends on specific needs」）。一部の記述は疑わしい: `qpdf --fix-qdf` を破損PDFの修復として挙げているが、fix-qdf は qpdf のオプションではなくQDF形式用の別コマンドと考えられる（reference.md:323-325）。`pdftotext -bbox-layout` を「プレーンテキスト抽出で最速」としているが、このオプションは座標付きXHTMLを出すもの（reference.md:536）。いずれも監査者の知識による inference で、実行確認はしていない。 | `reference.md:5-341`, `reference.md:343-601` | モデル標準能力で代替可能と考えられる (推論による仮説 / 確信度: 中) |
| F3 ReportLabの上付き/下付き文字の落とし穴 | Unicodeの上付き/下付き文字は標準フォントにグリフがなく黒四角になるので使うな、Paragraphの `<sub>`/`<super>` タグを使え、と指示する。 | `SKILL.md:169-187` | 短い追加指示で代替可能と考えられる (推論による仮説 / 確信度: 中) |
| F4 フォーム入力の分岐と強制手順 | 「必ず順番に、コードを書く前に」と強制し（forms.md:1）、まずフィールド有無を判定して「Fillable / Non-fillable」に分岐（forms.md:3-4）。非フィールド側はさらに構造抽出（A）／画像目視（B）／併用（Hybrid）に分岐し、bbox検査→入力→画像化して目視確認、を指示（forms.md:78-294）。ラベルのグループ化・入力欄の位置決め・画像上のフィールドの意味解釈はモデルの判断に委ねられている（forms.md:55, forms.md:100-132, forms.md:180-222）。 | `forms.md:1-4`, `forms.md:78-132`, `forms.md:172-294` | 独自の知識・手順・実装が価値を持つ (推論による仮説 / 確信度: 中) |
| F5 AcroFormフィールドの列挙と値検証付き入力 | pypdfでフィールドを列挙し、型（text/checkbox/radio_group/choice）、ページ、Rect、チェックボックスのon/off値、ラジオの選択肢値、選択肢一覧をJSON化（scripts/extract_form_field_info.py:19-115）。入力時は field_id・ページ・チェックボックス/ラジオ/選択肢の値を検証し、誤りがあれば終了（scripts/fill_fillable_fields.py:25-43, scripts/fill_fillable_fields.py:55-71）、その後 NeedAppearances を立てて書き出す（scripts/fill_fillable_fields.py:45-52）。選択肢の /Opt 形式に対する pypdf の挙動をモンキーパッチで補正（scripts/fill_fillable_fields.py:74-87）。判定用 scripts/check_fillable_fields.py:7-11 は get_fields() の有無を表示するだけ。制約: /Kids を持つ非ボタンのフィールド（複数ウィジェットのテキスト欄など）は列挙から黙って除外される（scripts/extract_form_field_info.py:53-57）。テキスト欄の値は検証されない。警告メッセージに `${field_id}` というJS風の書式ミスがある（scripts/extract_form_field_info.py:32）。 | `scripts/extract_form_field_info.py:9-122`, `scripts/fill_fillable_fields.py:11-98`, `scripts/check_fillable_fields.py:1-11`, `forms.md:6-76` | 独自の知識・手順・実装が価値を持つ (実装確認済み / 確信度: 高) |
| F6 非フィールドPDFへの注釈による書き込み（構造抽出・bbox検査・座標変換） | pdfplumberで単語座標、ページ幅の50%超の水平線、5〜15ptのほぼ正方形の矩形（チェックボックス候補）、罫線間の行境界を抽出（scripts/extract_form_structure.py:20-88）。fields.json のラベル/入力bboxの重なりと、入力欄の高さ<フォントサイズを検出（scripts/check_bounding_boxes.py:15-56）。pdf_width があればPDF上端原点座標、image_width なら画像座標とみなして pypdf の下端原点座標に変換し、FreeText注釈として書き込む（scripts/fill_pdf_form_with_annotations.py:10-90）。ページ画像化は200dpiでレンダリング後、最大1000pxに縮小（scripts/convert_pdf_to_images.py:9-24）。制約（実装確認）: 座標変換は mediabox の高さのみを使い、mediabox の原点オフセットやページ回転を考慮しない（scripts/fill_pdf_form_with_annotations.py:23-30, scripts/fill_pdf_form_with_annotations.py:43-46）。書き込みは平坦化されない注釈で、表示はビューア依存と考えられる（inference）。高さ検査は画像座標の場合ピクセルとptを比較することになる（scripts/check_bounding_boxes.py:43-49、影響は inference）。forms.md の画像座標の例は 1700x2200 だが、変換スクリプトは最大1000pxで出力する（forms.md:228-231 と scripts/convert_pdf_to_images.py:9-18）。 | `scripts/extract_form_structure.py:20-115`, `scripts/check_bounding_boxes.py:15-65`, `scripts/fill_pdf_form_with_annotations.py:10-107`, `scripts/convert_pdf_to_images.py:9-33` | 独自の知識・手順・実装が価値を持つ (実装確認済み / 確信度: 高) |
| F7 スキャンPDFのOCR | pdf2imageで画像化し pytesseract.image_to_string でページごとのテキストを得る例のみ（SKILL.md:233-250, reference.md:589-601）。テキスト層を持つ検索可能PDFを作る手順はない。tesseract本体・popplerが必要なことは書かれていない（`pip install` のコメントのみ、SKILL.md:235）。 | `SKILL.md:233-250`, `reference.md:589-601` | モデル標準能力で代替可能と考えられる (推論による仮説 / 確信度: 中) |

### 機能の詳細

#### F1 一般的PDF操作のスニペット集（pypdf / pdfplumber / reportlab / CLI）
- 分類: 知識
- 外部能力: pypdf, pdfplumber, pandas, reportlab, poppler-utils, qpdf, pdftk（任意）
- 期待される効果: 使うライブラリを毎回同じ選択に寄せ、APIの書き方の誤りを減らす。
- 理由: 内容はいずれのライブラリでも公式ドキュメントの基本例に相当する一般知識で、固有情報や決定的なコードはない。現行の汎用モデルは指示なしでも同等のコードを書くと考えられる（実験なしのため inference）。ツール選択を固定する効果はありうる。

#### F2 上級リファレンス（pypdfium2 / pdf-lib / pdfjs / qpdf・poppler上級 / トラブルシュート）
- 分類: 知識
- 外部能力: pypdfium2, pdf-lib（Node.js）, pdfjs-dist, qpdf, poppler-utils, numpy, pytesseract
- 期待される効果: 上級操作の際の参照資料。
- 理由: 一般知識の範囲で、不完全な例や疑わしいCLI記述を含む。本文から「必要なら見よ」と名前で参照されるだけで、読まれるかはモデル次第。

#### F3 ReportLabの上付き/下付き文字の落とし穴
- 分類: 知識, 指示
- 外部能力: reportlab
- 期待される効果: 化学式・数式を含むPDF生成時の文字化け（黒四角）を防ぐ。
- 理由: 内容自体は1〜2行で伝えられるが、利用者が事前に知らなければ書けない具体的な失敗知識で、最小指示案には含まれない。小さいが実用的な固有の注意と考えられる（効果は未検証）。

#### F4 フォーム入力の分岐と強制手順
- 分類: 手順, 指示
- 外部能力: ImageMagick（magick/convert）
- 期待される効果: コードをいきなり書かずにフィールド情報を確認させ、座標系の取り違えや検証の省略を減らす。
- 理由: 確認工程（bbox検査・出力の画像化による目視確認）の強制は、素のモデルが自発的には省きがちな工程と考えられる。ただし最初の判定コマンドは `python scripts/check_fillable_fields`（拡張子なし）で、実ファイル `scripts/check_fillable_fields.py` と一致せずそのままでは失敗する（forms.md:4）。モデルが自力で補正する可能性は高いが未確認。

#### F5 AcroFormフィールドの列挙と値検証付き入力
- 分類: ツール, アルゴリズム
- 外部能力: pypdf
- 期待される効果: 存在しないフィールドIDや不正なチェックボックス値（/On か /Yes か等）を投入して無言で失敗する事故を防ぎ、入力結果を再現的にする。
- 理由: 検証ロジックと /Opt 補正はコードとして実在する（implementation_confirmed）。モデルもその場で同様のコードを書けるが、チェックボックス状態値の抽出や値検証まで毎回やるとは限らない（inference）。

#### F6 非フィールドPDFへの注釈による書き込み（構造抽出・bbox検査・座標変換）
- 分類: ツール, アルゴリズム
- 外部能力: pdfplumber, pypdf, pdf2image + poppler
- 期待される効果: 目視推定だけに頼らずPDF内の実座標を使い、座標系変換をコードで行うことで、文字の位置ずれ・重なりを減らす。
- 理由: 座標系の取り違え（上端原点/下端原点、画像/PDF）はモデルが誤りやすい処理で、それを決定的なコードで吸収している（実装は確認済み、効果は未検証）。ただし入力欄の位置の決定自体はモデルの判断に残る。

#### F7 スキャンPDFのOCR
- 分類: 知識
- 外部能力: pytesseract + tesseract, pdf2image + poppler
- 期待される効果: スキャンPDFからテキストを取り出す。
- 理由: 最も一般的なOCRの定型例で、固有の工夫はない。

## Claims

| READMEの主張 | 実装 | 効果 | 宣伝との整合 | 独自価値 | 確信度 |
|---|---|---|---|---|---|
| C1: “reading or extracting text/tables from PDFs, combining or merging ... filling PDF forms, encrypting/decrypting PDFs, extracting images” `SKILL.md:3` | 確認済み | 未検証 | 整合 | モデル標準能力で代替可能と考えられる<br>(推論による仮説) | 中 |
| C2: “OCR on scanned PDFs to make them searchable” `SKILL.md:3` | 一部のみ | 未検証 | 誇張の疑い | モデル標準能力で代替可能と考えられる<br>(推論による仮説) | 中 |
| C3: “This script will verify that the field IDs and values you provide are valid” `forms.md:76` | 確認済み | 未検証 | 整合 | 独自の知識・手順・実装が価値を持つ<br>(推論による仮説) | 高 |
| C4: “First try to extract coordinates from the PDF structure (more accurate), then fall back to visual estimation” `forms.md:79` | 確認済み | 未検証 | 整合 | 独自の知識・手順・実装が価値を持つ<br>(推論による仮説) | 中 |
| C5: “This checks for intersecting bounding boxes and entry boxes that are too small for the font size.” `forms.md:163-168` | 確認済み | 未検証 | 整合 | 独自の知識・手順・実装が価値を持つ<br>(推論による仮説) | 高 |
| C6: “pdf-lib maintains form structure better than most alternatives” `reference.md:545` | 欠落 | 未検証 | 誇張の疑い | 現時点では判断不能<br>(未確認) | 中 |
| C7: “the document creation & editing skills that power Claude's document capabilities under the hood ... actively used in a production AI application” `@repo/README.md:20` | 該当なし | 評価不能 | 評価不能 | 現時点では判断不能<br>(未確認) | 低 |

**判定の集計**

| 観点 | 判定 | 件数 |
|---|---|---|
| 実装 | 確認済み | 4 |
| 実装 | 一部のみ | 1 |
| 実装 | 欠落 | 1 |
| 実装 | 該当なし | 1 |
| 効果 | 未検証 | 6 |
| 効果 | 評価不能 | 1 |
| 宣伝との整合 | 整合 | 4 |
| 宣伝との整合 | 誇張の疑い | 2 |
| 宣伝との整合 | 評価不能 | 1 |
| 独自価値 | モデル標準能力で代替可能と考えられる | 2 |
| 独自価値 | 独自の知識・手順・実装が価値を持つ | 3 |
| 独自価値 | 現時点では判断不能 | 2 |
| 確信度 | 高 | 2 |
| 確信度 | 中 | 4 |
| 確信度 | 低 | 1 |

#### C1
- 主張されている効果: 列挙されたPDF操作全般に対応する。
- 仕組み: 本文のコード例と、フォーム入力用の手順・スクリプト。
- 実装上の根拠: フォーム入力以外はコード例の提示のみで、実行可能なツールは同梱されていない。透かしは既存の透かしPDFを前提とする（SKILL.md:256-257）。列挙された操作それぞれに対応する例は存在する。 `SKILL.md:30-119`, `SKILL.md:121-187`, `SKILL.md:252-294`, `forms.md:1-294`
- 実験上の根拠: 実験未実施
- notes: 対応範囲の主張としては控えめで実装に見合う。ただしフォーム入力を除けば、スキルがあることでモデル標準以上にできるようになる根拠は示されていない。依存ライブラリ・CLIの導入前提は説明されていない。

#### C2
- 主張されている効果: スキャンPDFをOCRして検索可能にする。
- 仕組み: pytesseract によるOCR例。
- 実装上の根拠: 例はOCR結果を文字列として出力するだけで、テキスト層を埋め込んだ検索可能PDFを作る手順（例: hOCR/PDF出力や ocrmypdf）はない。 `SKILL.md:233-250`
- 実験上の根拠: 実験未実施
- notes: 「searchable」の部分に対応する実装・手順がない。モデルが自力で補う可能性はあるが、それはスキルの寄与ではない。

#### C3
- 主張されている効果: 入力前にフィールドIDと値の妥当性を検証する。
- 仕組み: fill_fillable_fields.py が extract_form_field_info の結果と突き合わせる。
- 実装上の根拠: field_id の存在、ページ番号、チェックボックス/ラジオ/選択肢の値を検証する。テキスト欄の値は検証対象外。/Kids を持つ非ボタンのフィールドは列挙されないため、正しいIDでも「invalid」と判定されうる（scripts/extract_form_field_info.py:53-57）。 `scripts/fill_fillable_fields.py:25-43`, `scripts/fill_fillable_fields.py:55-71`
- 実験上の根拠: 実験未実施
- notes: 機能の記述として実装どおり。

#### C4
- 主張されている効果: 構造抽出による座標は目視推定より正確。
- 仕組み: pdfplumber による単語・罫線・矩形の座標抽出と、上端原点→下端原点の変換。
- 実装上の根拠: 抽出と変換は実装されている。入力欄の位置はラベル座標からモデルが計算する（forms.md:122-132）。 `scripts/extract_form_structure.py:20-88`, `scripts/fill_pdf_form_with_annotations.py:23-30`, `forms.md:96-168`
- 実験上の根拠: 実験未実施
- notes: テキスト層のあるPDFでは実座標を使う方が精度が高いという主張は技術的に妥当と考えられるが、測定データはない。mediabox の原点オフセットや回転ページでは位置ずれしうる（scripts/fill_pdf_form_with_annotations.py:43-46）。

#### C5
- 主張されている効果: bboxの重なりと高さ不足を検出する。
- 仕組み: check_bounding_boxes.py の総当たり交差判定と高さ比較。
- 実装上の根拠: 記述どおり。同一ページ内のラベル/入力の全ペアを比較し、エラーは20件で打ち切る。 `scripts/check_bounding_boxes.py:20-52`
- 実験上の根拠: 実験未実施
- notes: 画像座標の場合、高さ（px）とフォントサイズ（pt）を直接比較している。

#### C6
- 主張されている効果: フォーム入力では pdf-lib の方が構造を保つ。
- 仕組み: 説明なし
- 実装上の根拠: スキルのフォーム手順はすべて pypdf で、pdf-lib によるフォーム入力の例・スクリプトはない。Quick Reference は「pdf-lib or pypdf (see FORMS.md)」としているが、FORMS.md に pdf-lib は登場しない。 `forms.md:74-75`, `SKILL.md:307`
- 実験上の根拠: 実験未実施
- notes: 比較の根拠がなく、スキル自身の推奨手順（pypdf）とも食い違うため、モデルの方針を揺らす可能性がある（inference）。

#### C7
- 主張されている効果: 本番のClaudeの文書機能で使われている実績のあるスキルである。
- 仕組み: 説明なし
- 実装上の根拠: リポジトリ内で検証できる事柄ではない。同READMEは「Claudeで得られる実装・挙動はここに示すものと異なりうる」と免責している（@repo/README.md:24）。
- 実験上の根拠: 実験未実施
- notes: 作者・採用実績は品質の根拠に数えない。本番利用されていても、この版と本番版の同一性も、効果も確認できない。

## Necessity

**素のモデルでできそうな部分**

- pypdf/pdfplumber/reportlab/qpdf/pdftotext による結合・分割・回転・抽出・作成・暗号化の基本例（SKILL.md:13-167, SKILL.md:189-294）
- reference.md の上級ライブラリ例の大半（reference.md:5-601）
- pytesseract によるOCRの定型例（SKILL.md:233-250）

**数行の指示で代替できそうな部分**

- ツール選択の方針（表は pdfplumber、作成は reportlab 等。SKILL.md:296-307）
- ReportLabでUnicode上付き/下付き文字を使わず <sub>/<super> を使う注意（SKILL.md:169-187）

**スキル固有の価値**

- AcroFormフィールド列挙（チェックボックス状態値・ラジオ・選択肢を含む）と値検証付き入力（scripts/extract_form_field_info.py, scripts/fill_fillable_fields.py）
- 非フィールドPDFの構造抽出・bbox検査・座標系変換込みの注釈書き込み（scripts/extract_form_structure.py, scripts/check_bounding_boxes.py, scripts/fill_pdf_form_with_annotations.py）
- フォーム入力の分岐と検証工程（bbox検査・出力の画像化による目視確認）の強制（forms.md:1-294）

**不要または冗長と思われる構成**

- どこからも参照されない scripts/create_validation_image.py（エージェントが使う導線がない）
- SKILL.md と reference.md の重複（結合・分割・OCR・暗号化の例が両方にある）
- pdftk・pdfjs-dist（ブラウザ用）・pdf-lib の例は、Python中心のワークフローでは使われにくい

**確証がない部分**

- フォーム入力ワークフローの実際の入力精度向上
- `qpdf --fix-qdf` や `pdftotext -bbox-layout`（最速との記述）の正しさ（reference.md:325, reference.md:536。監査者の知識では誤りの疑い、実行未確認）
- 依存が欠けた環境での挙動

### 最小指示案（B群）

```text
このPDFファイルについて作業してほしい。必要に応じてテキストや表の抽出、結合・分割・ページ回転、透かし追加、新規PDF作成、フォーム入力、暗号化/復号、画像抽出、スキャンPDFのOCRを行って、結果をファイルとして出力して。Pythonなど使えるツールは自由に使ってよい。
```

**最小指示で再現できないもの**

- フォーム入力用の同梱スクリプト群（フィールド列挙・値検証・構造抽出・bbox検査・座標変換付き注釈書き込み）
- フォーム入力時の分岐手順と、bbox検査・出力画像化による確認工程の強制
- ReportLabでUnicode上付き/下付き文字が黒四角になるという具体的な落とし穴
- ライブラリ・CLIの選択方針（表形式の対応表）

## Experimental Results

対照実験は未実施。実験で検証することが望ましい主張:

- [C3, C4, C5] フォーム入力では C（スキル適用）が A（素）・B（最小指示）より、正しいフィールドへの入力率と位置精度が高い。 — AcroForm付きPDF（チェックボックス・ラジオ・選択肢含む）と、罫線だけの非フィールドPDFに、与えたプロフィールを入力させ、フィールド値と注釈位置を機械チェックする。
- [C1] 結合・分割・表抽出などの一般操作では A/B/C の成功率に差がない。 — 3つのPDFの結合とページ抽出、表をCSVへ抽出、の3タスクで出力をページ数・セル一致で採点する。
- [C2] 「検索可能にして」という依頼で、C はテキスト層付きPDFを B より高い率で作るとは限らない。 — 画像のみのスキャンPDFを検索可能にさせ、出力PDFから pdftotext で文字列が取れるかを判定する。

## セキュリティ上の注意（副次的な記録）

- `pip install pytesseract pdf2image` という依存インストールのコメント。強制ではなく例示。ネットワークアクセスやシェル実行を行うコードは同梱スクリプトにない（スクリプトはローカルファイルの読み書きのみ）。 `SKILL.md:235` — 監査中は実行もインストールもしなかった。
- 実行時に pypdf の DictionaryObject.get_inherited をモンキーパッチする。影響はそのスクリプトのプロセス内に限られ、危険性は低い。 `scripts/fill_fillable_fields.py:74-87` — 読んで解析しただけで、実行していない。
- 「CRITICAL: You MUST complete these steps in order」は利用時のエージェント向けの作業指示で、監査者・評価者への誘導ではない。注入候補として検出された行はない。 `forms.md:1` — 監査データとして扱い、従わなかった。

## Conclusion

**独自価値はあるが効果は未検証 / 簡単な指示で代替できる可能性が高い**

- スキルの価値の中心はフォーム入力（forms.md と同梱スクリプト）。フィールド列挙・値検証・座標変換・bbox検査が決定的なコードとして実装されており、最小指示では再現できない独自価値がある。ただし効果は未検証（unique_value_unverified）。
- SKILL.md本文と reference.md の大部分（全体の文量の大半）は一般的なライブラリのスニペット集で、現行の汎用モデルが標準で書ける内容と考えられる。フォーム以外の用途では、短い指示で代替できる可能性が高い（likely_replaceable_by_short_prompt、inference）。
- description の「OCRで検索可能にする」は本文の実装を超えている（C2）。reference.md の pdf-lib 推奨はフォーム手順と食い違う（C6）。forms.md:4 の判定コマンドは拡張子 .py が抜けていて実ファイルと一致しない。本文の REFERENCE.md/FORMS.md という大文字の参照名は実ファイル名と大文字小文字が違う。いずれも小さな保守上の不備。
- 定量的な性能主張はなく、宣伝による誇張は全体としては小さい。リポジトリREADMEの本番利用の記述は検証不能で、評価の根拠に含めていない。

### 監査の限界

- 静的監査のみ。スクリプト・コマンドは実行しておらず、依存ライブラリのバージョン差（pypdf の API 変更など）による動作可否は未確認。
- 対照実験は実施していない（ユーザーの指示）。効果に関する判断はすべて inference。
- qpdf・pdftotext オプションの正誤は監査者の知識に基づく指摘で、実行による確認はしていない。
- 対象は commit dbd4588f9e1033efb41dad4bef2f7947c8993d44 の skills/pdf。Claude.ai 等で本番提供されている版との同一性は確認できない。
