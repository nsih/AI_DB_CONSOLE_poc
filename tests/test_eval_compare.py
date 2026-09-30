from decimal import Decimal

from nl2sql_eval.compare import compare_results, normalize


class TestNormalize:

    def test_숫자_표기_통일(self):
        assert normalize("185") == normalize(185) == normalize(Decimal("185.00"))

    def test_소수_둘째자리_반올림(self):
        assert normalize(Decimal("2.9668")) == normalize(2.97)

    def test_none과_nan(self):
        assert normalize(None) is None
        assert normalize(float("nan")) is None

    def test_일반_문자열은_그대로(self):
        assert normalize(" 서버 ") == "서버"


class TestCompareResults:

    def test_완전_일치(self):
        assert compare_results([(1, "a")], [(1, "a")])[0]

    def test_컬럼_순서_무시(self):
        assert compare_results([(1, "a")], [("a", 1)])[0]

    def test_추가_컬럼_허용(self):
        # "호관 목록" 질문에 개수를 곁들여도 정답
        assert compare_results([("3",), ("1",)], [("3", 482), ("1", 185)])[0]

    def test_행_순서_무시가_기본(self):
        assert compare_results([(1,), (2,)], [(2,), (1,)])[0]

    def test_순서_요구시_엄격(self):
        ok, reason = compare_results([(1,), (2,)], [(2,), (1,)], order_matters=True)
        assert not ok

    def test_텍스트_숫자_정렬_함정은_틀림(self):
        # CAST 없이 문자열로 정렬하면 순서가 달라진다
        gold = [("3",), ("1",), ("9",)]
        pred = [("1",), ("3",), ("9",)]
        assert not compare_results(gold, pred, order_matters=True)[0]

    def test_행_수_다르면_틀림(self):
        ok, reason = compare_results([(1,)], [(1,), (1,)])
        assert not ok and "행 수" in reason

    def test_distinct_누락은_틀림(self):
        assert not compare_results([("HP",), ("IBM",)], [("HP",), ("HP",)])[0]

    def test_컬럼_부족은_틀림(self):
        ok, reason = compare_results([(1, 2)], [(1,)])
        assert not ok and "컬럼 부족" in reason

    def test_컬럼별로는_같아도_행_조합이_다르면_틀림(self):
        gold = [(1, "a"), (2, "b")]
        pred = [(1, "b"), (2, "a")]
        assert not compare_results(gold, pred)[0]

    def test_같은_값의_컬럼이_둘이어도_대응을_찾는다(self):
        # 건물값과 호실합계가 같은 경우 — 어느 쪽에 대응시켜도 되어야 한다
        gold = [("1", 185, 185), ("2", 80, 80)]
        pred = [(185, "1", 185), (80, "2", 80)]
        assert compare_results(gold, pred)[0]

    def test_빈_결과끼리는_일치(self):
        assert compare_results([], [])[0]

    def test_문자열_숫자와_숫자는_같다(self):
        assert compare_results([("185",)], [(185,)])[0]
