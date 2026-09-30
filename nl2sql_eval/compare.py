# 실행 결과 비교 — 생성 SQL이 '맞았는가'를 판정한다.
#
# SQL 문자열이 아니라 실행 결과를 비교한다 (Spider 벤치마크의 execution accuracy).
# 같은 답을 서브쿼리로도 GROUP BY로도 쓸 수 있으므로 모양을 비교하면 오탐만 는다.
#
# 관대하게 보는 것:
#   - 컬럼 순서와 컬럼 이름 (별칭은 모델 마음이다)
#   - 정답에 없는 컬럼이 더 붙은 것 ("호관 목록"에 개수를 곁들여도 답은 맞다)
#   - 값의 표기 차이: '185' 와 185 와 Decimal('185.00'), 소수 셋째 자리 이하
# 엄격하게 보는 것:
#   - 행 수와 각 행의 값 (중복 포함 — DISTINCT를 빠뜨리면 틀린다)
#   - 순서를 요구한 문항의 행 순서

from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import product
import math
import re

_NUMERIC_RE = re.compile(r'^[+-]?\d+(\.\d+)?$')
_MAX_MAPPINGS = 20000   # 컬럼 대응 조합 탐색 상한 (넓은 SELECT * 대비)


def normalize(value):
    """비교용 정규화. 숫자는 소수 둘째 자리 반올림, 숫자 모양 문자열도 숫자로."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float, Decimal)):
        return _num(value)
    text = str(value).strip()
    if _NUMERIC_RE.match(text):
        return _num(text)
    return text


def _num(value):
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return str(value)
    return int(d) if d == d.to_integral_value() else float(d)


def compare_results(gold_rows, pred_rows, order_matters: bool = False) -> tuple[bool, str]:
    """(일치 여부, 사유). 사유는 실패 원인을 사람이 읽을 수 있게 적는다."""
    gold = [[normalize(v) for v in r] for r in gold_rows]
    pred = [[normalize(v) for v in r] for r in pred_rows]

    if len(gold) != len(pred):
        return False, f"행 수 다름 (정답 {len(gold)}, 생성 {len(pred)})"
    if not gold:
        return True, "둘 다 빈 결과"

    m, n = len(gold[0]), len(pred[0])
    if n < m:
        return False, f"컬럼 부족 (정답 {m}개, 생성 {n}개)"

    def column(rows, j):
        vals = [r[j] for r in rows]
        return vals if order_matters else Counter(vals)

    # 정답 컬럼마다, 값 모음이 같은 생성 컬럼만 후보로 남긴다 (필요조건)
    candidates = []
    for j in range(m):
        target = column(gold, j)
        cands = [k for k in range(n) if column(pred, k) == target]
        if not cands:
            return False, f"정답 {j + 1}번째 컬럼과 값이 같은 컬럼이 없음"
        candidates.append(cands)

    gold_rows_norm = [tuple(r) for r in gold]
    tried = 0
    for mapping in product(*candidates):
        if len(set(mapping)) != m:
            continue
        tried += 1
        if tried > _MAX_MAPPINGS:
            break
        projected = [tuple(r[k] for k in mapping) for r in pred]
        if order_matters:
            if projected == gold_rows_norm:
                return True, "일치"
        elif Counter(projected) == Counter(gold_rows_norm):
            return True, "일치"

    if order_matters:
        return False, "값은 같지만 행 순서 또는 행 단위 조합이 다름"
    return False, "컬럼별 값은 같지만 행 단위 조합이 다름"
