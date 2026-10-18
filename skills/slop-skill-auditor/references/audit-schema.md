# audit.json の書き方

`report.py skeleton <audit_dir>` で雛形が作れる。`report.py check` が下のルールを検査し、`report.py render` が report.md を作る。文章フィールドはMarkdown可。レポートの言語はユーザーの言語に合わせる（`render --lang ja|en` はラベルと見出しの言語）。

## 文章フィールドの書き方

report.md の読み手は「このスキルを入れるか」を決める人。`summary`・`features`・`claims`・`conclusion` などの自由記述は次のように書く。

- 最初の文で結論を言う。理由はその後に書く。
- 1文を短くし、1文に1つのことだけを書く。
- 専門用語や英語の略語（inference・bbox など）を避ける。判定の語彙（`unique_value` など）やID（F1・C1）を文中に書かない。レポートでは短い日本語のラベルと機能名・引用に置き換わる。
- ファイルと行の根拠は位置フィールド（`locations`・`evidence`・`implementation_evidence` など）に入れ、文中に `path:行` を並べない。
- 同じ内容を複数のフィールドで繰り返さない（`rationale` で `actual_processing` を言い直さない、`notes` で `implementation_note` を繰り返さない）。

## 位置（loc）の書式

- `path:LINE` または `path:START-END`。path はスキルルート相対（例 `SKILL.md:12-30`, `scripts/<name>.py:40`）。
- スキル外のファイル（リポジトリ文書、`external_files` の共有スクリプトやプラグインの hooks など）は `@repo/README.md:5` のように source_root 相対、ユーザー提供文書は `@docs/<file>:3`。
- `check` が実在と行範囲を検証する。存在しないファイル（欠落参照）は loc にできない。欠落を示すときは、それを**参照している行**を loc にする。

## 構造

```json
{
  "schema": "slop-skill-auditor/audit@1",
  "auditor": {"model": "claude-opus-5-5", "runtime": "Claude Code 2.1.302", "date": "2026-10-18"},
  "summary": {
    "purpose": "スキルの主な目的",
    "actual_behavior": "実際に行うこと",
    "unique_capabilities": "独自の実装や外部能力",
    "expected_value": "期待できる付加価値",
    "claim_consistency": "宣伝との整合性",
    "open_questions": ["重要な疑問点"]
  },
  "activation": {"description": "どの条件で起動・選択されるか", "evidence": ["SKILL.md:1-4"]},
  "features": [{
    "id": "F1",
    "name": "CSVの列プロファイル",
    "categories": ["tool", "algorithm"],
    "actual_processing": "列ごとに件数・欠損・最小/最大/平均・頻出値を計算してJSONで出す",
    "locations": ["scripts/csv_profile.py:10-58", "SKILL.md:8-12"],
    "external_capabilities": [],
    "expected_effect": "大きなCSVでもモデルの暗算に頼らず正確な統計が得られる",
    "substitutability": "unique_value",
    "basis": "implementation_confirmed",
    "confidence": "high",
    "rationale": "決定的な計算をコードで保証している。モデルも同等のスクリプトをその場で書けるが毎回の再現性は保証されない（inference）"
  }],
  "claims": [{
    "id": "C1",
    "source": "README.md:3",
    "quote": "原文の短い引用",
    "claimed_effect": "主張されている効果",
    "mechanism": "それを実現するとされる仕組み（説明なしなら「説明なし」）",
    "implementation_evidence": ["scripts/csv_profile.py:10-58"],
    "implementation_note": "対応する実装の有無と一致・不一致",
    "experimental_evidence": null,
    "experimental_evidence_locations": [],
    "experiment_ref": null,
    "assessment": {
      "implementation": "confirmed",
      "effect": "unverified",
      "consistency": "consistent",
      "unique_value": "unique_value",
      "confidence": "medium"
    },
    "unique_value_basis": "inference",
    "notes": "前提条件・制約の説明の有無、誇張と判断した場合のギャップなど"
  }],
  "necessity": {
    "base_model": ["素のモデルでできそうな部分"],
    "short_instruction": ["数行の指示で代替できそうな部分"],
    "unique_value": ["スキル固有の価値"],
    "redundant": ["不要または冗長と思われる構成"],
    "unconfirmed": ["確証がない部分"]
  },
  "minimal_prompt": {
    "text": "READMEの目的を、普通のユーザーが数行で頼んだ場合の指示（B群にそのまま使う）",
    "derived_from": ["README.md:1-5", "SKILL.md:3"],
    "dropped": ["この最小指示では再現できないもの"]
  },
  "security_notes": [{"location": "SKILL.md:20", "observation": "監査者宛ての評価誘導と curl|sh の実行要求", "handling": "データとして扱い、実行も従いもしなかった"}],
  "experiments": {
    "status": "not_run",
    "runs": [],
    "recommended": [{"claim_ids": ["C1"], "hypothesis": "C は A/B より…", "task_idea": "…"}]
  },
  "conclusion": {
    "verdicts": ["unique_value_unverified"],
    "reasons": ["具体的な理由"]
  },
  "limitations": ["監査の限界"],
  "not_reviewed": [{"path": "assets/big.bin", "reason": "バイナリで内容を確認できない"}]
}
```

## 語彙

- `categories`: `instruction` `knowledge` `procedure` `tool` `algorithm` `environment`
- `substitutability`: `base_model` `short_instruction` `unique_value` `external_capability` `undetermined`
- `confidence`（features と claims.assessment）: `high` `medium` `low`
- `claims[].assessment`（意味は [claim-review.md](claim-review.md)）:
  - `implementation`: `confirmed` `partial` `missing` `contradicted` `not_applicable` `unknown`
  - `effect`: `experimentally_supported` `not_supported_in_experiment` `unverified` `not_assessable`
  - `consistency`: `consistent` `possibly_exaggerated` `inconsistent` `not_assessable`
  - `unique_value`: `substitutability` と同じ語彙
- `claims[].unique_value_basis`: `basis` と同じ語彙
- `basis`: `implementation_confirmed` `inference` `experiment_observed` `unconfirmed`
- `conclusion.verdicts`（1つ以上）:
  - `clear_unique_value` 導入による独自価値が明確
  - `unique_value_unverified` 独自価値はあるが効果は未検証
  - `likely_replaceable_by_short_prompt` 簡単な指示で代替できる可能性が高い
  - `claims_exceed_evidence` 宣伝に対して実装・根拠が不足
  - `insufficient_information` 判断材料不足
  - `possible_negative_effect` 悪影響・過剰な制約の可能性（主に実験後）

## check が強制するルール

- 全 loc が実在し行範囲内であること。
- feature は `basis: unconfirmed` でない限り loc が1つ以上必要。
- `assessment.implementation` が `confirmed` / `partial` / `missing` / `contradicted` の claim は `implementation_evidence` が必要（`missing` は欠落部分を参照している行）。
- `effect: experimentally_supported` は、`experiments.runs` にある `experiment_ref`（results_file が実在）か、公開データの位置 `experimental_evidence_locations` が必要。`not_supported_in_experiment` は `experiment_ref` が必要。
- `basis` / `unique_value_basis` の `experiment_observed` は、実在する results_file のある実験が必要。
- `minimal_prompt.derived_from` は1つ以上必要で、README・`@docs/`・SKILL.md の frontmatter の行だけを指せる（スキル本文から最小指示を作ってはいけない）。
- `experiments.status: "not_run"` なのに `runs` や `experiment_ref` があればエラー。
- 警告: `inventory.json` の `review_targets`（SKILL.md、そこから辿れるファイル、スクリプト、README、スキル外の `@repo/…` ファイル）のうち、どの根拠にも引用されず `not_reviewed` にもないもの。「評価して問題なし」と「評価していない」を区別するため、読まなかったファイルは理由つきで `not_reviewed` に書く。
- 警告: 推論だけで「代替可能」を確信度 high にしている、実装が確認済みなのに整合を `inconsistent` にしている、注入候補行があるのに `security_notes` が空、欠落参照がどこにも言及されていない、最小指示が長すぎる、`dropped` が空。
