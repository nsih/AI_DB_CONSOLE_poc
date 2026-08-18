# DB/Streamlit/LLM 없이 동작하는 순수 로직 함수 테스트.
# db_builder.py가 Streamlit을 import하지 않도록 설계된 덕분에
# 여기 있는 함수들은 mock/fixture 없이 바로 테스트 가능하다.

import pandas as pd
import pytest

import db_builder as db


# ---------------------------------------------------------------------------
# classify_sql
# ---------------------------------------------------------------------------

class TestClassifySql:

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM t",
        "select * from t",
        "SHOW TABLES",
        "DESCRIBE t",
        "DESC t",
        "EXPLAIN SELECT * FROM t",
    ])
    def test_select_계열(self, sql):
        assert db.classify_sql(sql) == "select"

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t (a) VALUES (1)",
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "REPLACE INTO t (a) VALUES (1)",
    ])
    def test_dml_계열(self, sql):
        assert db.classify_sql(sql) == "dml"

    @pytest.mark.parametrize("sql", [
        "CREATE TABLE t (id INT)",
        "ALTER TABLE t ADD COLUMN x INT",
        "DROP TABLE t",
    ])
    def test_ddl_계열(self, sql):
        assert db.classify_sql(sql) == "ddl"

    @pytest.mark.parametrize("sql,expected", [
        # 820e5cf: CTE 본문이 SELECT면 select
        ("WITH cte AS (SELECT id FROM u) SELECT * FROM cte", "select"),
        # 820e5cf: CTE 본문이 UPDATE/DELETE면 dml
        ("WITH cte AS (SELECT id FROM u) UPDATE t SET x=1 WHERE id IN (SELECT id FROM cte)", "dml"),
        ("WITH cte AS (SELECT id FROM u) DELETE FROM t WHERE id IN (SELECT id FROM cte)", "dml"),
    ])
    def test_cte_구문(self, sql, expected):
        assert db.classify_sql(sql) == expected

    @pytest.mark.parametrize("sql", ["", "   "])
    def test_빈_문자열은_unknown(self, sql):
        assert db.classify_sql(sql) == "unknown"


# ---------------------------------------------------------------------------
# guard_sql
# ---------------------------------------------------------------------------

class TestGuardSql:

    def test_빈_sql_차단(self):
        with pytest.raises(db.DbBuilderError):
            db.guard_sql("", allow_write=True)

    def test_복수_문장_차단(self):
        with pytest.raises(db.DbBuilderError):
            db.guard_sql("SELECT 1; SELECT 2", allow_write=True)

    @pytest.mark.parametrize("sql", [
        "DROP DATABASE csu_db",
        "DROP SCHEMA csu_db",
        "TRUNCATE TABLE t",
        "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    ])
    def test_위험_구문_차단(self, sql):
        with pytest.raises(db.DbBuilderError):
            db.guard_sql(sql, allow_write=True)

    def test_조회_경로에서_select는_통과(self):
        db.guard_sql("SELECT * FROM t", allow_write=False)  # 예외 없이 통과

    @pytest.mark.parametrize("sql", [
        "DELETE FROM t",
        "UPDATE t SET x = 1",
        "CREATE TABLE t (id INT)",
    ])
    def test_조회_경로에서_쓰기는_차단(self, sql):
        with pytest.raises(db.DbBuilderError):
            db.guard_sql(sql, allow_write=False)

    @pytest.mark.parametrize("sql", [
        "UPDATE t SET x = 1",
        "DELETE FROM t",
        "INSERT INTO t (a) VALUES (1)",
        "CREATE TABLE t (id INT)",
        "ALTER TABLE t ADD COLUMN x INT",
    ])
    def test_쓰기_경로에서_dml_ddl은_통과(self, sql):
        db.guard_sql(sql, allow_write=True)  # 예외 없이 통과

    def test_쓰기_경로에서_select는_차단(self):
        # run_write는 DML/DDL 전용 경로 — SELECT가 흘러들면 막는다
        with pytest.raises(db.DbBuilderError):
            db.guard_sql("SELECT * FROM t", allow_write=True)

    @pytest.mark.parametrize("sql", [
        "SET GLOBAL general_log = ON",
        "CALL some_proc()",
        "RENAME TABLE a TO b",
    ])
    def test_쓰기_경로에서_분류_불가_구문_차단(self, sql):
        # classify_sql이 unknown으로 분류하는 구문은 화이트리스트 밖이므로 차단
        with pytest.raises(db.DbBuilderError):
            db.guard_sql(sql, allow_write=True)

    @pytest.mark.parametrize("sql", [
        "CREATE USER 'x'@'%' IDENTIFIED BY 'p'",
        "ALTER USER 'csu_admin'@'%' IDENTIFIED BY 'p'",
    ])
    def test_계정_생성_변경_차단(self, sql):
        with pytest.raises(db.DbBuilderError):
            db.guard_sql(sql, allow_write=True)

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM t WHERE msg = 'TRUNCATE 완료'",
        "SELECT * FROM t WHERE name = 'GRANT'",
    ])
    def test_문자열_리터럴_안의_위험단어는_오탐하지_않음(self, sql):
        db.guard_sql(sql, allow_write=False)  # 예외 없이 통과


# ---------------------------------------------------------------------------
# add_limit
# ---------------------------------------------------------------------------

class TestAddLimit:

    def test_limit_없으면_추가(self):
        result = db.add_limit("SELECT * FROM t", 100)
        assert "LIMIT 100" in result

    def test_이미_limit_있으면_유지(self):
        sql = "SELECT * FROM t LIMIT 5"
        assert db.add_limit(sql, 100) == sql

    def test_show는_limit_추가_안함(self):
        sql = "SHOW TABLES"
        assert db.add_limit(sql, 100) == sql

    def test_dml은_변경하지_않음(self):
        sql = "UPDATE t SET x = 1"
        assert db.add_limit(sql, 100) == sql

    def test_서브쿼리_안의_limit은_무시하고_바깥에_추가(self):
        # 서브쿼리 안에만 LIMIT이 있으면 바깥 SELECT에는 상한이 없는 상태다
        sql = "SELECT * FROM t WHERE id IN (SELECT id FROM u LIMIT 5)"
        result = db.add_limit(sql, 100)
        assert result.rstrip(";").endswith("LIMIT 100")

    def test_문자열_리터럴_안의_limit_단어는_기존_limit으로_오인하지_않음(self):
        sql = "SELECT * FROM t WHERE note = 'no limit here'"
        result = db.add_limit(sql, 100)
        assert "LIMIT 100" in result


class TestLimitApplies:
    """상한이 개입하는 상황인지 — run_select의 '잘림' 판정 근거."""

    def test_평범한_select는_개입한다(self):
        assert db.limit_applies("SELECT * FROM t") is True

    def test_직접_쓴_limit이_있으면_개입하지_않는다(self):
        # 사용자가 LIMIT 5를 썼으면 5행에서 끊겨도 '잘린' 것이 아니다.
        assert db.limit_applies("SELECT * FROM t LIMIT 5") is False

    def test_show는_개입하지_않는다(self):
        assert db.limit_applies("SHOW TABLES") is False

    def test_dml은_개입하지_않는다(self):
        assert db.limit_applies("UPDATE t SET x = 1") is False

    def test_서브쿼리_속_limit은_최상위로_치지_않는다(self):
        sql = "SELECT * FROM t WHERE id IN (SELECT id FROM u LIMIT 5)"
        assert db.limit_applies(sql) is True


class TestRunSelectTruncation:
    """run_select가 상한 초과를 어떻게 알리는지. 엔진은 대역으로 세운다."""

    class _FakeResult:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
        def keys(self): return ["a"]

    class _FakeConn:
        def __init__(self, rows, seen): self._rows, self._seen = rows, seen
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, stmt, *a, **k):
            self._seen.append(str(stmt))
            return TestRunSelectTruncation._FakeResult(self._rows)

    class _FakeEngine:
        def __init__(self, rows, seen): self._rows, self._seen = rows, seen
        def connect(self): return TestRunSelectTruncation._FakeConn(self._rows, self._seen)

    def _run(self, n_rows, limit=3, sql="SELECT a FROM t"):
        seen: list[str] = []
        engine = self._FakeEngine([(i,) for i in range(n_rows)], seen)
        return db.run_select(engine, sql, limit=limit), seen

    def test_상한_미만이면_잘리지_않았다(self):
        df, _ = self._run(2)
        assert len(df) == 2
        assert df.attrs["truncated"] is False

    def test_정확히_상한이면_잘리지_않았다(self):
        # 상한과 같은 행 수가 '원래 그만큼'인지 구분되어야 한다.
        df, _ = self._run(3)
        assert len(df) == 3
        assert df.attrs["truncated"] is False

    def test_상한을_넘으면_잘랐다고_알린다(self):
        df, _ = self._run(4)
        assert len(df) == 3          # 여분의 1행은 화면에 내보내지 않는다
        assert df.attrs["truncated"] is True

    def test_판정용으로_한_행을_더_요청한다(self):
        _, seen = self._run(4, limit=3)
        assert "LIMIT 4" in seen[0]

    def test_직접_쓴_limit이면_상한을_건드리지_않는다(self):
        df, seen = self._run(9, limit=3, sql="SELECT a FROM t LIMIT 9")
        assert "LIMIT 4" not in seen[0]
        assert df.attrs["truncated"] is False
        assert len(df) == 9

    def test_적용된_상한도_함께_알린다(self):
        df, _ = self._run(1, limit=3)
        assert df.attrs["limit"] == 3


# ---------------------------------------------------------------------------
# build_update_sqls
# ---------------------------------------------------------------------------

def _pk(*ids) -> pd.DataFrame:
    """my_row_id 기본키 프레임 생성 헬퍼."""
    return pd.DataFrame({"my_row_id": list(ids)})


class TestBuildUpdateSqls:

    def test_변경_없으면_빈_리스트(self):
        df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        assert db.build_update_sqls(df, df.copy(), "t", _pk(1, 2)) == []

    def test_한_셀_변경(self):
        orig = pd.DataFrame({"id": [1], "name": ["a"]})
        edit = pd.DataFrame({"id": [1], "name": ["b"]})
        result = db.build_update_sqls(orig, edit, "t", _pk(77))
        assert len(result) == 1
        item = result[0]
        assert "`name` = " in item["sql"]
        # exec_sql/params는 바인드 파라미터를 쓴다 (998e351: 이스케이프 취약점 제거)
        assert ":p0" in item["exec_sql"]
        assert "b" in item["params"].values()

    def test_where는_기본키만_사용한다(self):
        # 조회 컬럼이 테이블의 일부여도 다른 행이 함께 갱신되면 안 된다
        orig = pd.DataFrame({"name": ["a"], "phone": ["010"]})
        edit = pd.DataFrame({"name": ["b"], "phone": ["010"]})
        item = db.build_update_sqls(orig, edit, "t", _pk(42))[0]
        where = item["sql"].split("WHERE", 1)[1]
        assert "`my_row_id` = '42'" in where
        assert "`name`"  not in where
        assert "`phone`" not in where

    def test_중복_행이어도_각각_구분된다(self):
        # 값이 완전히 같은 두 행 — PK로 구분되므로 하나만 갱신되어야 한다
        orig = pd.DataFrame({"name": ["a", "a"]})
        edit = pd.DataFrame({"name": ["a", "b"]})
        result = db.build_update_sqls(orig, edit, "t", _pk(1, 2))
        assert len(result) == 1
        assert "`my_row_id` = '2'" in result[0]["sql"]
        assert result[0]["warning"] is None

    def test_null로_변경(self):
        orig = pd.DataFrame({"id": [1], "name": ["a"]})
        edit = pd.DataFrame({"id": [1], "name": [None]})
        result = db.build_update_sqls(orig, edit, "t", _pk(1))
        assert "`name` = NULL" in result[0]["sql"]

    def test_컬럼_구조_다르면_에러(self):
        orig = pd.DataFrame({"id": [1], "name": ["a"]})
        edit = pd.DataFrame({"id": [1], "other": ["a"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t", _pk(1))

    def test_행_수_다르면_에러(self):
        orig = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        edit = pd.DataFrame({"id": [1], "name": ["a"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t", _pk(1, 2))

    def test_기본키_없으면_에러(self):
        orig = pd.DataFrame({"name": ["a"]})
        edit = pd.DataFrame({"name": ["b"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t", None)
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t", pd.DataFrame())

    def test_기본키_행수_불일치시_에러(self):
        orig = pd.DataFrame({"name": ["a", "b"]})
        edit = pd.DataFrame({"name": ["x", "b"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t", _pk(1))

    def test_기본키_값이_null이면_에러(self):
        orig = pd.DataFrame({"name": ["a"]})
        edit = pd.DataFrame({"name": ["b"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t", _pk(None))

    def test_복합키_지원(self):
        orig = pd.DataFrame({"val": ["a"]})
        edit = pd.DataFrame({"val": ["b"]})
        pk   = pd.DataFrame({"k1": [1], "k2": ["x"]})
        item = db.build_update_sqls(orig, edit, "t", pk)[0]
        assert "`k1` = '1'"  in item["sql"]
        assert "`k2` = 'x'"  in item["sql"]


# ---------------------------------------------------------------------------
# is_single_table_select / extract_select_table / inject_key_columns
# ---------------------------------------------------------------------------

class TestEditableQueryDetection:

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM users",
        "SELECT name, phone FROM users WHERE id = 1",
        "SELECT * FROM users ORDER BY name LIMIT 10",
    ])
    def test_단일_테이블_조회는_편집_가능(self, sql):
        assert db.is_single_table_select(sql) is True

    @pytest.mark.parametrize("sql", [
        "SELECT * FROM a JOIN b ON a.id = b.id",
        "SELECT dept, COUNT(*) FROM users GROUP BY dept",
        "SELECT DISTINCT name FROM users",
        "SELECT * FROM a UNION SELECT * FROM b",
        "SELECT * FROM users WHERE id IN (SELECT id FROM x)",
        # 콤마 조인 — 정규식 방식에서 놓치던 케이스
        "SELECT * FROM a, b WHERE a.id = b.id",
        "SELECT * FROM `a`, `b`",
    ])
    def test_조인_집계_서브쿼리는_편집_불가(self, sql):
        assert db.is_single_table_select(sql) is False

    def test_문자열_리터럴_안의_join은_오탐하지_않음(self):
        assert db.is_single_table_select(
            "SELECT * FROM users WHERE note = 'JOIN 완료'") is True

    @pytest.mark.parametrize("sql,expected", [
        ("SELECT * FROM users", "users"),
        ("SELECT * FROM `users` WHERE x = 1", "users"),
        ("select a from 직원", "직원"),
    ])
    def test_테이블명_추출(self, sql, expected):
        assert db.extract_select_table(sql) == expected

    def test_from_없으면_none(self):
        assert db.extract_select_table("SELECT 1") is None


class TestFormatSampleValue:

    def test_개행은_공백으로(self):
        # 개행이 남으면 '-- ' 주석 접두사가 끊겨 값의 나머지가 맨 텍스트가 된다
        assert "\n" not in db.format_sample_value("1차 완료\n2차 예정")
        assert db.format_sample_value("1차 완료\n2차 예정") == "1차 완료 2차 예정"

    def test_탭과_연속공백도_정리(self):
        assert db.format_sample_value("a\t\t  b") == "a b"

    def test_구분자_충돌_방지(self):
        # '|'가 남으면 샘플 표의 열이 밀린다
        assert "|" not in db.format_sample_value("a|b")

    def test_긴_값은_잘린다(self):
        out = db.format_sample_value("가" * 200)
        assert len(out) <= db._SAMPLE_MAX_LEN + 1
        assert out.endswith("…")

    def test_짧은_값은_그대로(self):
        assert db.format_sample_value("2025-05-14") == "2025-05-14"

    def test_none은_null_표기(self):
        assert db.format_sample_value(None) == "NULL"

    def test_숫자도_처리(self):
        assert db.format_sample_value(123) == "123"


class TestExtractTargetTable:

    @pytest.mark.parametrize("sql,expected", [
        ("SELECT * FROM users", "users"),
        ("INSERT INTO users (a) VALUES (1)", "users"),
        ("UPDATE users SET x = 1", "users"),
        ("CREATE TABLE users (id INT)", "users"),
        ("ALTER TABLE `users` ADD COLUMN x INT", "users"),
    ])
    def test_구문별_대상_테이블(self, sql, expected):
        assert db.extract_target_table(sql) == expected

    def test_drop_table은_none(self):
        # 테이블이 사라지므로 실행 후 조회할 대상이 없다
        assert db.extract_target_table("DROP TABLE users") is None

    def test_문자열_리터럴_안의_from은_오탐하지_않음(self):
        assert db.extract_target_table(
            "UPDATE logs SET msg = 'FROM ghost' WHERE id = 1") == "logs"


class TestInjectKeyColumns:

    def test_star_조회에_키_추가(self):
        # MySQL은 `SELECT col, *`를 거부하므로 FROM 앞에 붙어야 한다
        result = db.inject_key_columns("SELECT * FROM `t`", ["my_row_id"])
        assert result == "SELECT * , `my_row_id` FROM `t`"

    def test_컬럼_지정_조회에_키_추가(self):
        result = db.inject_key_columns("SELECT `a`, `b` FROM `t`", ["my_row_id"])
        assert "`my_row_id` FROM" in result

    def test_where_order_by_유지(self):
        result = db.inject_key_columns(
            "SELECT * FROM `t` WHERE `x` = 1 ORDER BY `y`", ["my_row_id"])
        assert result.endswith("WHERE `x` = 1 ORDER BY `y`")
        assert "`my_row_id` FROM" in result

    def test_복합키(self):
        result = db.inject_key_columns("SELECT * FROM `t`", ["k1", "k2"])
        assert "`k1`, `k2` FROM" in result

    def test_빈_목록이면_원문_유지(self):
        sql = "SELECT * FROM `t`"
        assert db.inject_key_columns(sql, []) == sql

    def test_문자열_리터럴_안의_from은_건드리지_않음(self):
        result = db.inject_key_columns(
            "SELECT * FROM `t` WHERE note = 'FROM here'", ["my_row_id"])
        # 리터럴 안의 FROM이 아니라 실제 FROM 앞에 삽입되어야 한다
        assert result.index("`my_row_id`") < result.index("`t`")
        assert "'FROM here'" in result

    def test_from_없으면_에러(self):
        with pytest.raises(db.DbBuilderError):
            db.inject_key_columns("SELECT 1", ["my_row_id"])


# ---------------------------------------------------------------------------
# infer_column_types
# ---------------------------------------------------------------------------

class TestInferColumnTypes:

    def test_정수_컬럼은_bigint(self):
        df = pd.DataFrame({"n": ["1", "2", "3"]})
        assert db.infer_column_types(df)["n"] == "BIGINT"

    def test_소수_컬럼은_double(self):
        df = pd.DataFrame({"n": ["1.5", "2.0", "3.25"]})
        assert db.infer_column_types(df)["n"] == "DOUBLE"

    def test_텍스트_컬럼은_text(self):
        df = pd.DataFrame({"s": ["a", "b", "c"]})
        assert db.infer_column_types(df)["s"] == "TEXT"

    def test_혼합_컬럼은_text(self):
        df = pd.DataFrame({"mix": ["1", "abc", "3"]})
        assert db.infer_column_types(df)["mix"] == "TEXT"

    def test_앞자리_0은_텍스트로_유지(self):
        # 우편번호처럼 앞자리 0이 있는 값은 BIGINT로 변환하면
        # 06236 -> 6236 처럼 원본이 손상된다.
        df = pd.DataFrame({"우편번호": ["06236", "01011", "00007"]})
        assert db.infer_column_types(df)["우편번호"] == "TEXT"

    def test_0_단독값은_숫자로_유지(self):
        # "0"은 앞자리 0 패딩이 아니라 그냥 0이므로 숫자형 유지
        df = pd.DataFrame({"n": ["0", "5", "10"]})
        assert db.infer_column_types(df)["n"] == "BIGINT"

    def test_앞자리_0_소수는_숫자로_유지(self):
        # "0.5" 는 소수점이 있어 앞자리 0 패딩 패턴과 다르다
        df = pd.DataFrame({"n": ["0.5", "0.25"]})
        assert db.infer_column_types(df)["n"] == "DOUBLE"


# ---------------------------------------------------------------------------
# parse_markdown_tables
# ---------------------------------------------------------------------------

class TestParseMarkdownTables:

    def test_단일_표_파싱(self):
        md = (
            "| 이름 | 나이 |\n"
            "|---|---|\n"
            "| 홍길동 | 30 |\n"
            "| 김철수 | 25 |\n"
        )
        tables = db.parse_markdown_tables(md)
        assert len(tables) == 1
        assert list(tables[0].columns) == ["이름", "나이"]
        assert len(tables[0]) == 2

    def test_굵게_기울임_마크업_제거(self):
        md = (
            "| 이름 | 나이 |\n"
            "|---|---|\n"
            "| **홍길동** | _30_ |\n"
        )
        tables = db.parse_markdown_tables(md)
        assert tables[0].iloc[0]["이름"] == "홍길동"
        assert tables[0].iloc[0]["나이"] == "30"

    def test_표가_없으면_빈_리스트(self):
        assert db.parse_markdown_tables("그냥 텍스트입니다.") == []

    def test_다중_표_파싱(self):
        md = (
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
            "\n텍스트\n\n"
            "| c | d |\n|---|---|\n| 3 | 4 |\n"
        )
        tables = db.parse_markdown_tables(md)
        assert len(tables) == 2


# ---------------------------------------------------------------------------
# _quote_unquoted_alias_with_space
# ---------------------------------------------------------------------------

class TestQuoteUnquotedAlias:

    def test_공백_포함_별칭에_백틱_적용(self):
        sql = "SELECT COUNT(*) AS 단말기 개수 FROM t"
        result = db._quote_unquoted_alias_with_space(sql)
        assert "AS `단말기 개수`" in result

    def test_공백_없는_별칭은_그대로(self):
        sql = "SELECT COUNT(*) AS cnt FROM t"
        assert db._quote_unquoted_alias_with_space(sql) == sql

    def test_cast_표현식_내부_as는_건드리지_않음(self):
        sql = "SELECT CAST(x AS SIGNED) FROM t"
        assert db._quote_unquoted_alias_with_space(sql) == sql


# ---------------------------------------------------------------------------
# build_add_pk_sql
# ---------------------------------------------------------------------------

class TestBuildAddPkSql:

    def test_기본_형태(self):
        sql = db.build_add_pk_sql("users")
        assert "ALTER TABLE `users`" in sql
        assert "`my_row_id`" in sql
        # INVISIBLE이어야 SELECT *에 안 나와 기존 화면이 유지된다
        assert "INVISIBLE" in sql
        assert "AUTO_INCREMENT PRIMARY KEY" in sql

    def test_한글_테이블명_허용(self):
        sql = db.build_add_pk_sql("직원")
        assert "ALTER TABLE `직원`" in sql

    @pytest.mark.parametrize("bad", [
        "users`; DROP TABLE x; --",
        "users x",
        "users;",
        "",
    ])
    def test_위험한_테이블명_차단(self, bad):
        with pytest.raises(db.DbBuilderError):
            db.build_add_pk_sql(bad)

    def test_생성된_ddl은_가드를_통과한다(self):
        # 사용자가 NL 콘솔에 그대로 붙여넣어 실행할 수 있어야 한다
        sql = db.build_add_pk_sql("users")
        assert db.classify_sql(sql) == "ddl"
        db.guard_sql(sql, allow_write=True)  # 예외 없이 통과


# ---------------------------------------------------------------------------
# _normalize_empty_strings
# ---------------------------------------------------------------------------

class TestNormalizeEmptyStrings:

    def test_빈_문자열만_null로(self):
        df = pd.DataFrame({"a": ["", "x"]})
        result = db._normalize_empty_strings(df)
        assert result["a"].iloc[0] is None
        assert result["a"].iloc[1] == "x"

    def test_none_문자열_값은_보존(self):
        # "None"/"nan" 이 실제 데이터 값인 경우 (예: 상태값) 훼손하면 안 된다
        df = pd.DataFrame({"status": ["None", "nan", "완료"]})
        result = db._normalize_empty_strings(df)
        assert result["status"].tolist() == ["None", "nan", "완료"]


# ---------------------------------------------------------------------------
# _build_sa_dtype
# ---------------------------------------------------------------------------

class TestBuildSaDtype:

    def test_col_types_none이면_none(self):
        df = pd.DataFrame({"a": [1]})
        assert db._build_sa_dtype(df, None) is None

    def test_유효한_타입_매핑(self):
        df = pd.DataFrame({"a": [1], "b": ["x"]})
        result = db._build_sa_dtype(df, {"a": "BIGINT", "b": "TEXT"})
        assert set(result.keys()) == {"a", "b"}

    def test_df에_없는_컬럼은_스킵(self):
        df = pd.DataFrame({"a": [1]})
        result = db._build_sa_dtype(df, {"a": "BIGINT", "ghost": "TEXT"})
        assert set(result.keys()) == {"a"}

    def test_알수없는_타입문자열은_text로_폴백(self):
        df = pd.DataFrame({"a": [1]})
        result = db._build_sa_dtype(df, {"a": "NOT_A_REAL_TYPE"})
        assert type(result["a"]).__name__ == "Text"

    def test_유효_항목_없으면_none(self):
        df = pd.DataFrame({"a": [1]})
        assert db._build_sa_dtype(df, {"ghost": "TEXT"}) is None


# ---------------------------------------------------------------------------
# validate_sql — DB 없이 잡히는 오류
# ---------------------------------------------------------------------------

class TestValidateSql:

    # 관측된 실패: 집계 조건을 WHERE에 써서 MySQL 1111로 거부됨
    def test_where의_집계함수를_잡는다(self):
        sql = ("SELECT 호관, SUM(호실_수) FROM t "
               "WHERE SUM(호실_수) != 1 GROUP BY 호관")
        findings = db.validate_sql(sql)
        assert len(findings) == 1
        assert "HAVING" in findings[0]

    def test_중첩_함수_안의_집계도_잡는다(self):
        sql = "SELECT a FROM t WHERE ABS(SUM(CAST(b AS SIGNED))) > 1 GROUP BY a"
        assert any("HAVING" in f for f in db.validate_sql(sql))

    def test_having의_집계는_정상(self):
        sql = "SELECT 호관, SUM(호실_수) AS s FROM t GROUP BY 호관 HAVING s > 10"
        assert db.validate_sql(sql) == []

    def test_where의_서브쿼리_집계는_정상(self):
        # 스칼라 서브쿼리 안의 집계는 합법 — 오탐하면 안 된다
        sql = "SELECT * FROM t WHERE x > (SELECT AVG(x) FROM t)"
        assert db.validate_sql(sql) == []

    def test_where의_비집계_함수는_정상(self):
        sql = "SELECT * FROM t WHERE LEFT(사용호실, 1) = '1'"
        assert db.validate_sql(sql) == []

    def test_문자열_리터럴_안의_집계표기는_오탐하지_않음(self):
        assert db.validate_sql("SELECT * FROM t WHERE name = 'SUM(x)'") == []

    def test_where_없는_구문은_정상(self):
        assert db.validate_sql("SELECT SUM(a) FROM t GROUP BY b") == []

    def test_full_outer_join을_잡는다(self):
        findings = db.validate_sql("SELECT * FROM a FULL OUTER JOIN b ON a.x = b.x")
        assert any("FULL OUTER JOIN" in f for f in findings)

    def test_플레이스홀더를_잡는다(self):
        findings = db.validate_sql("UPDATE t SET a = NULL WHERE ip = ?")
        assert any("?" in f for f in findings)

    def test_정상_쿼리는_빈_리스트(self):
        sql = ("SELECT b.호관, SUM(r.단말기_개수) AS 합계 FROM b "
               "LEFT JOIN r ON LEFT(r.사용호실, 1) = b.호관 "
               "GROUP BY b.호관 HAVING 합계 > 0")
        assert db.validate_sql(sql) == []


# ---------------------------------------------------------------------------
# build_create_view_sql
# ---------------------------------------------------------------------------

class TestBuildCreateViewSql:

    SELECT = "SELECT a, SUM(b) AS 합계 FROM t GROUP BY a"

    def test_기본_형태(self):
        sql = db.build_create_view_sql("v_요약", self.SELECT)
        assert sql.startswith("CREATE VIEW `v_요약` AS")
        assert self.SELECT in sql

    def test_or_replace(self):
        sql = db.build_create_view_sql("v_요약", self.SELECT, or_replace=True)
        assert sql.startswith("CREATE OR REPLACE VIEW `v_요약` AS")

    def test_끝의_세미콜론은_떼어낸다(self):
        # CREATE VIEW ... AS SELECT ...; 안에 세미콜론이 남으면 문법 오류다.
        sql = db.build_create_view_sql("v", self.SELECT + ";  ")
        assert ";" not in sql

    def test_이름에_백틱이_있으면_거부(self):
        with pytest.raises(db.DbBuilderError, match="사용할 수 없는 문자"):
            db.build_create_view_sql("v`; DROP TABLE t; --", self.SELECT)

    def test_이름에_공백이_있으면_거부(self):
        with pytest.raises(db.DbBuilderError, match="사용할 수 없는 문자"):
            db.build_create_view_sql("내 뷰", self.SELECT)

    def test_64자를_넘으면_거부(self):
        with pytest.raises(db.DbBuilderError, match="64자"):
            db.build_create_view_sql("v" * 65, self.SELECT)

    def test_빈_SELECT는_거부(self):
        with pytest.raises(db.DbBuilderError, match="비어 있습니다"):
            db.build_create_view_sql("v", "   ;  ")

    def test_SELECT가_아니면_거부(self):
        with pytest.raises(db.DbBuilderError, match="SELECT 결과만"):
            db.build_create_view_sql("v", "UPDATE t SET a = 1")

    def test_SHOW는_거부(self):
        # classify_sql은 SHOW를 select로 묶지만 뷰가 될 수는 없다.
        with pytest.raises(db.DbBuilderError, match="SHOW"):
            db.build_create_view_sql("v", "SHOW TABLES")

    def test_위험_구문이_섞이면_거부(self):
        with pytest.raises(db.DbBuilderError, match="허용되지 않는"):
            db.build_create_view_sql("v", "SELECT * FROM t INTO OUTFILE '/tmp/x'")

    def test_문자열_리터럴_속_키워드는_통과(self):
        sql = db.build_create_view_sql("v", "SELECT * FROM t WHERE msg = 'TRUNCATE 완료'")
        assert "CREATE VIEW" in sql


# ---------------------------------------------------------------------------
# check_sql — EXPLAIN을 못 돌렸을 때의 처리
# ---------------------------------------------------------------------------

class TestCheckSql:

    @staticmethod
    def _explain_raises(monkeypatch):
        def _boom(engine, sql):
            raise RuntimeError("Can't connect to MySQL server")
        monkeypatch.setattr(db, "explain_sql", _boom)

    def test_연결_실패를_화면용_결과에_알린다(self, monkeypatch):
        self._explain_raises(monkeypatch)
        findings = db.check_sql(None, "SELECT a FROM t")
        assert any("확인하지 못했습니다" in f for f in findings)

    def test_연결_실패를_LLM에는_되먹이지_않는다(self, monkeypatch):
        self._explain_raises(monkeypatch)
        assert db.check_sql(None, "SELECT a FROM t", report_skip=False) == []

    def test_report_skip이_꺼져도_정적_검사는_남는다(self, monkeypatch):
        self._explain_raises(monkeypatch)
        findings = db.check_sql(None, "SELECT a FROM t WHERE SUM(b) > 1",
                                report_skip=False)
        assert any("HAVING" in f for f in findings)

    def test_EXPLAIN이_통과하면_알림이_없다(self, monkeypatch):
        monkeypatch.setattr(db, "explain_sql", lambda engine, sql: None)
        assert db.check_sql(None, "SELECT a FROM t") == []

    def test_MySQL_거부_사유를_전달한다(self, monkeypatch):
        monkeypatch.setattr(db, "explain_sql",
                            lambda engine, sql: "[1055] not in GROUP BY clause")
        findings = db.check_sql(None, "SELECT a FROM t")
        assert any("1055" in f for f in findings)


class TestStripSubqueries:

    def test_일반_함수_호출은_보존(self):
        sql = "ABS(SUM(CAST(x AS SIGNED)))"
        assert db._strip_subqueries(sql) == sql

    def test_서브쿼리는_통째로_사라진다(self):
        assert db._strip_subqueries("x > (SELECT AVG(y) FROM t)").strip() == "x >"

    def test_중첩_서브쿼리도_사라진다(self):
        sql = "x IN (SELECT a FROM t WHERE b IN (SELECT c FROM u))"
        assert "SELECT" not in db._strip_subqueries(sql)

    def test_함수_안의_서브쿼리만_지운다(self):
        result = db._strip_subqueries("COALESCE((SELECT MAX(a) FROM t), 0)")
        assert "SELECT" not in result and "COALESCE" in result

    def test_괄호_없으면_그대로(self):
        assert db._strip_subqueries("a = b") == "a = b"


# ---------------------------------------------------------------------------
# generate_sql — 검증 실패 시 오류 되먹임 재생성
# ---------------------------------------------------------------------------

class TestGenerateSqlRepair:

    @staticmethod
    def _fake_chat(responses: list[str], calls: list):
        """_chat 대역 — 호출될 때마다 responses를 순서대로 돌려준다."""
        def _chat(messages, model_name, endpoint):
            calls.append(messages)
            return responses[len(calls) - 1]
        return _chat

    def _run(self, monkeypatch, responses, **kwargs):
        calls: list = []
        monkeypatch.setattr(db, "_chat", self._fake_chat(responses, calls))
        sql = db.generate_sql("질의", "스키마", "model", "http://x", **kwargs)
        return sql, calls

    def test_첫_결과가_정상이면_한_번만_호출(self, monkeypatch):
        sql, calls = self._run(monkeypatch, ["SELECT a FROM t"])
        assert sql == "SELECT a FROM t"
        assert len(calls) == 1

    def test_검증_실패시_오류를_되먹여_재생성(self, monkeypatch):
        bad  = "SELECT a, SUM(b) FROM t WHERE SUM(b) > 1 GROUP BY a"
        good = "SELECT a, SUM(b) AS s FROM t GROUP BY a HAVING s > 1"
        sql, calls = self._run(monkeypatch, [bad, good])

        assert sql == good
        assert len(calls) == 2
        # 2회차 대화에 실패한 SQL과 오류 문장이 들어간다
        followup = calls[1]
        assert followup[-2] == {"role": "assistant", "content": bad}
        assert "HAVING" in followup[-1]["content"]

    def test_외부_validate_결과도_되먹인다(self, monkeypatch):
        sql, calls = self._run(
            monkeypatch, ["SELECT 없는컬럼 FROM t", "SELECT a FROM t"],
            validate=lambda s: (["Unknown column '없는컬럼'"]
                                if "없는컬럼" in s else []),
        )
        assert sql == "SELECT a FROM t"
        assert "없는컬럼" in calls[1][-1]["content"]

    def test_max_repair_0이면_되먹이지_않는다(self, monkeypatch):
        bad = "SELECT a FROM t WHERE SUM(b) > 1"
        sql, calls = self._run(monkeypatch, [bad], max_repair=0)
        assert sql == bad
        assert len(calls) == 1

    def test_끝까지_실패해도_sql은_돌려준다(self, monkeypatch):
        bad = "SELECT a FROM t WHERE SUM(b) > 1"
        sql, calls = self._run(monkeypatch, [bad] * 3)
        assert sql == bad
        assert len(calls) == 3      # 최초 1회 + max_repair 기본 2회

    def test_두_번째_재시도에서_고쳐도_받는다(self, monkeypatch):
        bad  = "SELECT a FROM t WHERE SUM(b) > 1"
        good = "SELECT a, SUM(b) AS s FROM t GROUP BY a HAVING s > 1"
        sql, calls = self._run(monkeypatch, [bad, bad, good])
        assert sql == good
        assert len(calls) == 3

    def test_플레이스홀더가_남으면_에러(self, monkeypatch):
        with pytest.raises(db.DbBuilderError, match="플레이스홀더"):
            self._run(monkeypatch, ["SELECT * FROM t WHERE ip = ?"] * 3)

    def test_think_블록과_코드펜스_제거(self, monkeypatch):
        raw = "<think>고민중</think>\n```sql\nSELECT a FROM t\n```"
        sql, _ = self._run(monkeypatch, [raw])
        assert sql == "SELECT a FROM t"

    def test_빈_응답은_에러(self, monkeypatch):
        with pytest.raises(db.DbBuilderError, match="생성하지 못했"):
            self._run(monkeypatch, ["<think>음</think>"])

    def test_중복_오류는_한_번만_되먹인다(self, monkeypatch):
        # validate가 check_sql처럼 정적 검사를 다시 수행해도 문장이 겹치지 않아야 한다
        bad = "SELECT a, SUM(b) FROM t WHERE SUM(b) > 1 GROUP BY a"
        _, calls = self._run(monkeypatch, [bad, "SELECT a FROM t"],
                             validate=db.validate_sql)
        assert calls[1][-1]["content"].count("HAVING") == 1
