# NL2SQL 정확도 평가.
#
#   python -m nl2sql_eval.run_eval                 # 재생성 0회 vs 2회 비교
#   python -m nl2sql_eval.run_eval --repair 2      # 한 설정만
#   python -m nl2sql_eval.run_eval --only C20,C27  # 일부 문항만
#   python -m nl2sql_eval.run_eval --runs 3        # 문항당 3회 반복 (편차 측정)
#
# 실제 DB와 LM Studio가 필요하다. 생성 SQL은 run_select로만 실행하므로
# 모델이 쓰기 쿼리를 내놓아도 가드에서 막혀 DB는 바뀌지 않는다.
#
# 결과는 nl2sql_eval/results/ 에 JSON(전체 기록)과 Markdown(요약 보고서)으로 남는다.
# 보고서에는 SQL과 판정 사유만 적고 조회된 데이터 값은 적지 않는다.

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import db_builder as db
from nl2sql_eval.compare import compare_results

HERE        = Path(__file__).parent
ROW_LIMIT   = 5000


def _rows(engine, sql: str) -> list[tuple]:
    df = db.run_select(engine, sql, limit=ROW_LIMIT)
    return list(df.itertuples(index=False, name=None))


def _endpoint() -> tuple[str, str]:
    s = db._load_secrets()
    return (f"http://{s['AI_WORKER_IP']}:{s.get('AI_WORKER_PORT', 1234)}/v1/chat/completions",
            s.get("AI_MODEL_NAME", ""))


class _CallCounter:
    """generate_sql 내부의 LLM 호출 횟수를 센다 (재생성 발생 여부 측정용)."""
    def __init__(self):
        self.calls = 0
        self._orig = db._chat

    def __enter__(self):
        def counted(*a, **kw):
            self.calls += 1
            return self._orig(*a, **kw)
        db._chat = counted
        return self

    def __exit__(self, *exc):
        db._chat = self._orig


def evaluate_case(engine, case, gold_rows, schema_prompt, endpoint, model, repair,
                  think: bool = False, vocabulary: set[str] | None = None) -> dict:
    rec = {"id": case["id"], "category": case["category"], "repair": repair,
           "sql": None, "ok": False, "stage": None, "reason": None,
           "latency": None, "llm_calls": 0}
    t0 = time.perf_counter()
    try:
        with _CallCounter() as cc:
            sql = db.generate_sql(
                case["question"], schema_prompt, model, endpoint,
                validate=lambda s: db.check_sql(engine, s, report_skip=False) + (
                    db.find_missing_question_values(case["question"], s, vocabulary)
                    if vocabulary is not None else []),
                max_repair=repair, think=think)
        rec["sql"] = sql
    except db.DbBuilderError as e:
        rec.update(stage="생성 실패", reason=str(e)[:300])
        return rec
    finally:
        rec["latency"] = round(time.perf_counter() - t0, 2)
        rec["llm_calls"] = cc.calls if "cc" in locals() else 0

    try:
        pred_rows = _rows(engine, sql)
    except db.DbBuilderError as e:
        rec.update(stage="실행 실패", reason=str(e)[:300])
        return rec

    ok, reason = compare_results(gold_rows, pred_rows, case.get("order_matters", False))
    rec.update(ok=ok, stage="정답" if ok else "오답", reason=reason)
    return rec


def summarize(records: list[dict]) -> dict:
    """반복 실행 전체 요약. 정확도·실행 가능률은 실행(run)별로 낸 뒤 평균±표준편차."""
    runs = sorted({r["run"] for r in records})
    acc, exe = [], []
    for k in runs:
        rs = [r for r in records if r["run"] == k]
        acc.append(sum(r["ok"] for r in rs) / len(rs))
        exe.append(sum(r["stage"] in ("정답", "오답") for r in rs) / len(rs))

    per_case = defaultdict(list)
    for r in records:
        per_case[r["id"]].append(r["ok"])

    sd = (lambda xs: statistics.stdev(xs) if len(xs) > 1 else 0.0)
    return {
        "문항 수":         len(per_case),
        "반복 횟수":       len(runs),
        "정확도":          (statistics.mean(acc), sd(acc)),
        "실행 가능률":     (statistics.mean(exe), sd(exe)),
        "항상 정답 문항":  sum(all(v) for v in per_case.values()),
        "결과가 흔들린 문항": sum(0 < sum(v) < len(v) for v in per_case.values()),
        "중앙 지연(초)":   statistics.median(r["latency"] for r in records),
        "평균 LLM 호출":   statistics.mean(r["llm_calls"] for r in records),
        "재생성 발생 비율": sum(r["llm_calls"] > 1 for r in records) / len(records),
    }


def _fmt(key, v) -> str:
    if isinstance(v, tuple):
        return f"{v[0]:.1%} ± {v[1]:.1%}"
    if key.endswith("비율"):
        return f"{v:.1%}"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def write_report(out_dir: Path, meta: dict, cases, results: dict[int, list[dict]]) -> Path:
    stamp = meta["started"].replace(":", "").replace("-", "").replace(" ", "_")
    (out_dir / f"{stamp}.json").write_text(
        json.dumps({"meta": meta, "results": {str(k): v for k, v in results.items()}},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    repairs = sorted(results)
    L = [f"# NL2SQL 평가 보고서 ({meta['started']})", "",
         f"- 모델: `{meta['model']}` (사고 모드 {'켬' if meta.get('think') else '끔'}, 값 누락 검사 {'켬' if meta.get('value_check') else '끔'}, 샘플링 {meta.get('sampling', {'temperature': 0.1})}) / 문항 {len(cases)}개 / 스키마 테이블·뷰 {meta['tables']}개",
         "- 채점: 실행 결과 일치 (컬럼 순서·별칭·추가 컬럼 무시, 값 표기 정규화, 순서 요구 문항은 순서까지)",
         "", "## 요약", "",
         "| 지표 | " + " | ".join(f"재생성 {r}회" for r in repairs) + " |",
         "|---|" + "---|" * len(repairs)]
    sums = {r: summarize(results[r]) for r in repairs}
    for key in next(iter(sums.values())):
        L.append(f"| {key} | " + " | ".join(_fmt(key, sums[r][key]) for r in repairs) + " |")

    L += ["", "## 카테고리별 정확도", "",
          "| 카테고리 | 문항 | " + " | ".join(f"재생성 {r}회" for r in repairs) + " |",
          "|---|---|" + "---|" * len(repairs)]
    cats = defaultdict(list)
    for c in cases:
        cats[c["category"]].append(c["id"])
    for cat, ids in cats.items():
        cells = []
        for r in repairs:
            recs = [x for x in results[r] if x["id"] in ids]
            cells.append(f"{sum(x['ok'] for x in recs) / len(recs):.0%}")
        L.append(f"| {cat} | {len(ids)} | " + " | ".join(cells) + " |")

    L += ["", "## 문항별", "",
          "| ID | 카테고리 | " + " | ".join(f"재생성 {r}회" for r in repairs) + " |",
          "|---|---|" + "---|" * len(repairs)]
    for c in cases:
        cells = []
        for r in repairs:
            xs = [x for x in results[r] if x["id"] == c["id"]]
            ok = sum(x["ok"] for x in xs)
            mark = "✅" if ok == len(xs) else "❌" if ok == 0 else "⚠️"
            calls = statistics.mean(x["llm_calls"] for x in xs)
            cells.append(f"{mark} {ok}/{len(xs)} (호출 {calls:.1f}회)")
        L.append(f"| {c['id']} | {c['category']} | " + " | ".join(cells) + " |")

    L += ["", "## 실패 상세", ""]
    L.append("같은 문항에서 서로 다른 오답 SQL은 모두 적는다 (중복 제거).")
    for r in repairs:
        fails = [x for x in results[r] if not x["ok"]]
        L.append(f"\n### 재생성 {r}회 — 실패 {len(fails)}건")
        seen = set()
        for x in fails:
            key = (x["id"], (x["sql"] or "").strip())
            if key in seen:
                continue
            seen.add(key)
            n = sum(1 for y in fails if (y["id"], (y["sql"] or "").strip()) == key)
            q = next(c for c in cases if c["id"] == x["id"])
            L += ["", f"**{x['id']}** ({x['category']}, {n}회) — {x['stage']}: {x['reason']}",
                  f"> {q['question'].splitlines()[0]}", "",
                  "```sql", (x["sql"] or "(없음)").strip(), "```"]
        L.append("")

    path = out_dir / f"{stamp}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repair", default="0,2", help="재생성 횟수 설정들, 쉼표 구분")
    ap.add_argument("--only", default="", help="평가할 문항 ID, 쉼표 구분")
    ap.add_argument("--runs", type=int, default=1,
                    help="문항당 반복 횟수. 모델 출력이 매번 달라서 1회 결과로는 설정 비교가 어렵다")
    ap.add_argument("--model", default="", help="LM Studio 모델명. 비우면 secrets.toml 설정")
    ap.add_argument("--think", action=argparse.BooleanOptionalAction, default=None,
                    help="사고 모드. 지정하지 않으면 앱과 같이 secrets.toml의 AI_THINK (기본 켬)")
    ap.add_argument("--value-check", action="store_true",
                    help="질문 값 누락 검사를 재생성 되먹임에 포함")
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()

    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    repairs = [int(x) for x in args.repair.split(",")]

    engine = db.get_engine()
    endpoint, model = _endpoint()
    model = args.model or model
    if args.think is None:
        args.think = bool(db._load_secrets().get("AI_THINK", True))
    schema_prompt = db.get_schema_prompt(engine)   # 앱과 동일하게 전체 스키마
    vocabulary = db.build_value_vocabulary(engine) if args.value_check else None

    # 정답 SQL부터 전부 실행해 둔다 — 정답이 깨져 있으면 평가가 무의미하다.
    gold = {}
    for c in cases:
        try:
            gold[c["id"]] = _rows(engine, c["gold"])
        except db.DbBuilderError as e:
            print(f"[정답 오류] {c['id']}: {e}", file=sys.stderr)
            return 2

    meta = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": model, "think": args.think,
            "sampling": db.THINK_SAMPLING if args.think else {"temperature": 0.1},
            "value_check": args.value_check, "tables": len(db.list_tables(engine)),
            "repairs": repairs, "runs": args.runs, "row_limit": ROW_LIMIT}
    results: dict[int, list[dict]] = {}
    for r in repairs:
        results[r] = []
        for k in range(1, args.runs + 1):
            for i, c in enumerate(cases, 1):
                rec = evaluate_case(engine, c, gold[c["id"]], schema_prompt, endpoint, model, r,
                                    think=args.think, vocabulary=vocabulary)
                rec["run"] = k
                results[r].append(rec)
                print(f"[재생성 {r} / 반복 {k}] {i:2d}/{len(cases)} {c['id']} "
                      f"{'O' if rec['ok'] else 'X'} {rec['stage']} "
                      f"({rec['llm_calls']}회, {rec['latency']}s)", flush=True)
        sm = summarize(results[r])
        print(f"== 재생성 {r}회: 정확도 {_fmt('정확도', sm['정확도'])}, "
              f"실행 가능률 {_fmt('실행 가능률', sm['실행 가능률'])}", flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print("보고서:", write_report(out, meta, cases, results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
