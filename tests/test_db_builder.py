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


# ---------------------------------------------------------------------------
# build_update_sqls
# ---------------------------------------------------------------------------

class TestBuildUpdateSqls:

    def test_변경_없으면_빈_리스트(self):
        df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        assert db.build_update_sqls(df, df.copy(), "t") == []

    def test_한_셀_변경(self):
        orig = pd.DataFrame({"id": [1], "name": ["a"]})
        edit = pd.DataFrame({"id": [1], "name": ["b"]})
        result = db.build_update_sqls(orig, edit, "t")
        assert len(result) == 1
        item = result[0]
        assert "`name` = " in item["sql"]
        assert "`id` = " in item["sql"]
        assert item["warning"] is None
        # exec_sql/params는 바인드 파라미터를 쓴다 (998e351: 이스케이프 취약점 제거)
        assert ":p0" in item["exec_sql"]
        assert "b" in item["params"].values()

    def test_null로_변경(self):
        orig = pd.DataFrame({"id": [1], "name": ["a"]})
        edit = pd.DataFrame({"id": [1], "name": [None]})
        result = db.build_update_sqls(orig, edit, "t")
        assert "`name` = NULL" in result[0]["sql"]

    def test_컬럼_구조_다르면_에러(self):
        orig = pd.DataFrame({"id": [1], "name": ["a"]})
        edit = pd.DataFrame({"id": [1], "other": ["a"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t")

    def test_행_수_다르면_에러(self):
        orig = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        edit = pd.DataFrame({"id": [1], "name": ["a"]})
        with pytest.raises(db.DbBuilderError):
            db.build_update_sqls(orig, edit, "t")

    def test_중복_행_경고(self):
        # 원본에 동일한 행이 2개 있는 상태에서 그중 하나를 수정
        orig = pd.DataFrame({"id": [1, 1], "name": ["a", "a"]})
        edit = pd.DataFrame({"id": [1, 1], "name": ["a", "b"]})
        result = db.build_update_sqls(orig, edit, "t")
        assert len(result) == 1
        assert result[0]["warning"] is not None


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
