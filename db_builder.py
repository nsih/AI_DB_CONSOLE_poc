# db_builder.py
# 순수 로직 모듈 — Streamlit을 import하지 않는다. UI는 전부 페이지 쪽 담당.

import logging
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import requests
import sqlparse
from sqlalchemy import create_engine, inspect, text
from sqlalchemy import BigInteger, Integer, Float, Text, String, Date, DateTime
from sqlalchemy.dialects.mysql import DOUBLE as MYSQL_DOUBLE, TINYINT as MYSQL_TINYINT
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# 예외

class DbBuilderError(Exception):
    pass


# 설정 로드

def _load_secrets() -> dict:
    path = Path(__file__).parent / ".streamlit" / "secrets.toml"
    with open(path, "rb") as f:
        return tomllib.load(f)


# 연결

def get_engine() -> Engine:
    """SQLAlchemy 엔진 생성. 캐싱은 호출측(utils.load_engine)이 담당.

    패스워드는 URL에 넣지 않고 connect_args로 넘긴다 (특수문자 파싱 오류 회피)."""
    secrets = _load_secrets()
    host     = secrets.get("DB_HOST", "127.0.0.1")
    port     = int(secrets.get("DB_PORT", 3306))
    name     = secrets.get("DB_NAME", "csu_db")
    user     = secrets.get("DB_USER", "csu_admin")
    password = secrets.get("DB_PASSWORD", "")

    try:
        engine = create_engine(
            f"mysql+pymysql://{user}@{host}:{port}/{name}",
            connect_args={"password": password},
            pool_pre_ping=True,
            # 서버 max_connections=20 중 최대 10개만 사용 (4개로는 동시 3명에서 대기 발생).
            pool_size=5,
            max_overflow=5,
            pool_timeout=10,
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("DB 연결 성공")
        return engine
    except Exception as e:
        raise DbBuilderError(f"DB 연결 실패: {e}")


# Introspection

def list_views(engine: Engine) -> list[str]:
    """뷰 이름 목록. 조회에 실패하면 빈 리스트 (뷰가 없는 것과 같이 취급)."""
    try:
        return inspect(engine).get_view_names()
    except Exception as e:
        logger.warning(f"뷰 목록 조회 실패: {e}")
        return []


def list_tables(engine: Engine) -> list[str]:
    """테이블 + 뷰. get_table_names()는 뷰를 빼므로 따로 붙인다.

    뷰도 SELECT 대상이라 사이드바 목록·LLM 스키마·이름 충돌 검사에서 모두
    테이블과 같이 보여야 한다. 쓰기 대상이 아닌 것은 호출측이 판단한다."""
    try:
        tables = inspect(engine).get_table_names()
    except Exception as e:
        raise DbBuilderError(f"테이블 목록 조회 실패: {e}")
    views = set(list_views(engine))
    return sorted(set(tables) | views)


def get_schema(engine: Engine, table: str) -> dict:
    try:
        insp = inspect(engine)
        return {
            "columns":      insp.get_columns(table),
            "pk":           insp.get_pk_constraint(table),
            "foreign_keys": insp.get_foreign_keys(table),
        }
    except Exception as e:
        raise DbBuilderError(f"스키마 조회 실패 ({table}): {e}")


_SAMPLE_MAX_LEN = 40


def format_sample_value(value) -> str:
    """샘플 값을 프롬프트 한 줄에 안전하게 넣을 형태로 만든다.

    개행은 '-- ' 주석을 끊고, '|'는 컬럼 구분자와 충돌하며, 긴 값은 셀에 섞인
    지시문이 그대로 전달되는 통로가 된다."""
    if value is None:
        return "NULL"
    text_value = re.sub(r'\s+', ' ', str(value)).replace('|', '/').strip()
    if len(text_value) > _SAMPLE_MAX_LEN:
        text_value = text_value[:_SAMPLE_MAX_LEN] + "…"
    return text_value


def list_invisible_columns(engine: Engine, table: str) -> set[str]:
    """테이블의 INVISIBLE 컬럼 이름 집합.

    get_columns()가 invisible 여부를 노출하지 않아 information_schema를 직접 본다."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                "AND EXTRA LIKE '%INVISIBLE%'"
            ), {"t": table}).fetchall()
        return {r[0] for r in rows}
    except Exception as e:
        logger.warning(f"invisible 컬럼 조회 실패 ({table}): {e}")
        return set()


# 테이블 행 수를 `-- 행 수: N` 주석으로 실어 1:N 관계를 알려주는 방안을 시험했다가
# 되돌렸다. 이 모델은 행 수를 보면 오히려 조인 조건을 뒤집어 이미 한 자리인 쪽에
# SUBSTRING을 걸었다 (`LEFT(b.호관,1) = r.사용호실`). 질의 하나·temperature 0.1에서
# 잰 것이라 표본은 약하지만, 도움이 된 사례는 한 건도 없었다.
def get_schema_prompt(engine: Engine,
                      tables: list[str] | None = None,
                      sample_rows: int = 3) -> str:
    if tables is None:
        tables = list_tables(engine)

    parts: list[str] = []
    for table in tables:
        schema = get_schema(engine, table)
        # INVISIBLE 컬럼은 SELECT *에 안 나오므로 LLM에게도 숨긴다
        # (알리면 조회되지 않는 컬럼을 참조하는 SQL을 만든다).
        hidden = list_invisible_columns(engine, table)

        col_defs = []
        pk_cols = set(schema["pk"].get("constrained_columns", []))
        for col in schema["columns"]:
            if col["name"] in hidden:
                continue
            nullable = "" if col["nullable"] else " NOT NULL"
            pk_mark  = " PRIMARY KEY" if col["name"] in pk_cols else ""
            col_defs.append(f"  {col['name']} {col['type']}{nullable}{pk_mark}")

        for fk in schema["foreign_keys"]:
            ref_table  = fk["referred_table"]
            local_cols = ", ".join(fk["constrained_columns"])
            ref_cols   = ", ".join(fk["referred_columns"])
            col_defs.append(f"  FOREIGN KEY ({local_cols}) REFERENCES {ref_table}({ref_cols})")

        create_stmt = f"CREATE TABLE {table} (\n" + ",\n".join(col_defs) + "\n);"
        parts.append(create_stmt)

        if sample_rows > 0:
            try:
                with engine.connect() as conn:
                    result = conn.execute(
                        text(f"SELECT * FROM `{table}` LIMIT :n"),
                        {"n": sample_rows}
                    )
                    # 헤더는 조회 결과에서 가져온다 (스키마 컬럼 목록을 쓰면
                    # INVISIBLE 컬럼 때문에 값과 개수가 어긋난다).
                    headers = list(result.keys())
                    rows    = result.fetchall()
                if rows:
                    sample_lines = ["-- 샘플 데이터 (값의 형식 참고용):"]
                    sample_lines.append("-- " + " | ".join(
                        format_sample_value(h) for h in headers))
                    for row in rows:
                        sample_lines.append("-- " + " | ".join(
                            format_sample_value(v) for v in row))
                    parts.append("\n".join(sample_lines))
            except Exception as e:
                logger.warning(f"샘플 행 조회 실패 ({table}): {e}")

        parts.append("")

    return "\n".join(parts).strip()


# SQL 가드

_DANGEROUS_PATTERNS = re.compile(
    r'\b(DROP\s+DATABASE|DROP\s+SCHEMA|TRUNCATE|'
    r'DROP\s+USER|CREATE\s+USER|ALTER\s+USER|GRANT|REVOKE|SHUTDOWN|'
    r'LOAD\s+DATA|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b',
    re.IGNORECASE | re.MULTILINE)

_SHOW_RE = re.compile(r'^\s*(SHOW|DESCRIBE|DESC|EXPLAIN)\b', re.IGNORECASE)
_CTE_RE = re.compile(r'^\s*WITH\b', re.IGNORECASE)

def _strip_parens(sql: str) -> str:
    """괄호 내용을 반복 제거해 최상위 토큰만 남긴다 (CTE 본문·서브쿼리 제거)."""
    prev = None
    while prev != sql:
        prev = sql
        sql = re.sub(r'\([^()]*\)', ' ', sql)
    return sql

def _strip_string_literals(sql: str) -> str:
    """문자열 리터럴 내용을 지운다. 값 안의 키워드('TRUNCATE 완료' 등) 오탐 방지."""
    return re.sub(r"'[^']*'", "''", sql)


def _mask_string_literals(sql: str) -> str:
    """문자열 리터럴을 같은 길이의 공백으로 덮는다. 원문과 인덱스가 일치해
    '위치'를 찾는 용도로 쓸 수 있다."""
    return re.sub(r"'[^']*'",
                  lambda m: "'" + " " * (len(m.group(0)) - 2) + "'",
                  sql)

def classify_sql(sql: str) -> str:
    """첫 구문 verb 판별 → 'select' | 'ddl' | 'dml' | 'unknown'."""
    sql = sql.strip()
    if not sql:
        return "unknown"

    if _SHOW_RE.match(sql):
        return "select"
    
    if _CTE_RE.match(sql):
        top = _strip_parens(sql)
        m = re.search(r'\b(SELECT|UPDATE|DELETE|INSERT)\b', top, re.IGNORECASE)
        if m:
            verb = m.group(1).upper()
            return "select" if verb == "SELECT" else "dml"
        return "unknown"

    try:
        parsed = sqlparse.parse(sql)
        if not parsed:
            return "unknown"
        stmt  = parsed[0]
        stype = stmt.get_type()
        if stype == "SELECT":
            return "select"
        if stype in ("CREATE", "ALTER", "DROP", "RENAME"):
            return "ddl"
        if stype in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            return "dml"
        return "unknown"
    except Exception:
        return "unknown"


def guard_sql(sql: str, allow_write: bool) -> None:
    sql = sql.strip()
    if not sql:
        raise DbBuilderError("SQL이 비어 있습니다.")

    stmts = [s for s in sqlparse.split(sql) if s.strip()]
    if len(stmts) > 1:
        raise DbBuilderError("복수 SQL 문장은 허용되지 않습니다. 한 번에 하나씩 실행하세요.")

    if _DANGEROUS_PATTERNS.search(_strip_string_literals(sql)):
        raise DbBuilderError("허용되지 않는 구문이 포함되어 있습니다 (DROP DATABASE / TRUNCATE 등).")

    kind = classify_sql(sql)
    if allow_write:
        if kind not in ("dml", "ddl"):
            raise DbBuilderError("쓰기 경로에서는 DML(INSERT/UPDATE/DELETE) 또는 DDL만 허용됩니다.")
    else:
        if kind != "select":
            raise DbBuilderError("조회 경로에서는 SELECT / SHOW / DESCRIBE / EXPLAIN만 허용됩니다.")

# SQL 정적 검증 (LLM 산출물 되먹임용)
#
# guard_sql이 "실행해도 되는가"를 본다면, 여기는 "실행하면 성공하는가"를 본다.
# 실패 사유를 사람이 읽을 수 있는 문장으로 돌려주고, 그 문장을 그대로 LLM에게
# 되먹여 재생성시킨다.

_AGGREGATE_FUNCS = (
    "AVG|BIT_AND|BIT_OR|BIT_XOR|COUNT|GROUP_CONCAT|JSON_ARRAYAGG|JSON_OBJECTAGG|"
    "MAX|MIN|STD|STDDEV|STDDEV_POP|STDDEV_SAMP|SUM|VAR_POP|VAR_SAMP|VARIANCE"
)
_AGGREGATE_CALL_RE = re.compile(rf'\b({_AGGREGATE_FUNCS})\s*\(', re.IGNORECASE)

# 서브쿼리를 걷어낸 뒤 WHERE 절 본문만 잘라낸다.
_WHERE_CLAUSE_RE = re.compile(
    r'\bWHERE\b(.*?)(?=\b(?:GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|WINDOW|UNION|INTO)\b|$)',
    re.IGNORECASE | re.DOTALL)

_FULL_JOIN_RE     = re.compile(r'\bFULL\s+(?:OUTER\s+)?JOIN\b', re.IGNORECASE)
_SUBQUERY_HEAD_RE = re.compile(r'\s*SELECT\b', re.IGNORECASE)


def _strip_subqueries(sql: str) -> str:
    """서브쿼리 `( SELECT ... )`만 통째로 제거하고 나머지 표현식은 그대로 둔다.

    _strip_parens는 `ABS(SUM(x))`까지 지워 함수 호출 흔적을 잃는다. 여기서는
    괄호 구조를 보존하므로 중첩된 집계 함수도 찾을 수 있고, 동시에 서브쿼리
    안의 WHERE·집계는 시야에서 사라져 오탐하지 않는다."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql[i] != '(':
            out.append(sql[i])
            i += 1
            continue

        depth, j = 0, i
        while j < n:
            if sql[j] == '(':
                depth += 1
            elif sql[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1

        closed = j < n
        inner  = sql[i + 1:j] if closed else sql[i + 1:]
        if _SUBQUERY_HEAD_RE.match(inner):
            out.append(' ')
        else:
            out.append('(' + _strip_subqueries(inner) + (')' if closed else ''))
        i = j + 1

    return ''.join(out)


def validate_sql(sql: str) -> list[str]:
    """DB 없이 잡아낼 수 있는 오류 목록. 문제가 없으면 빈 리스트.

    반환 문장은 사용자 화면과 LLM 재생성 프롬프트에 그대로 쓰인다."""
    findings: list[str] = []
    masked = _strip_string_literals(sql)

    if '?' in masked:
        findings.append(
            "파라미터 플레이스홀더(?)가 있습니다. 값을 SQL에 직접 써야 합니다.")

    # 서브쿼리를 걷어내면 남는 WHERE는 전부 최상위 WHERE다.
    top_level = _strip_subqueries(masked)

    for m in _WHERE_CLAUSE_RE.finditer(top_level):
        agg = _AGGREGATE_CALL_RE.search(m.group(1))
        if agg:
            findings.append(
                f"WHERE 절에 집계 함수 {agg.group(1).upper()}()가 있습니다. "
                "집계 결과로 거르는 조건은 WHERE가 아니라 GROUP BY 뒤의 HAVING에 써야 합니다.")
            break

    if _FULL_JOIN_RE.search(masked):
        findings.append(
            "MySQL에는 FULL OUTER JOIN이 없습니다. "
            "LEFT JOIN과 RIGHT JOIN을 UNION으로 합치거나 LEFT JOIN만 사용하세요.")

    return findings


def _db_error_message(exc: Exception) -> str:
    """SQLAlchemy 예외에서 MySQL이 실제로 돌려준 문장만 뽑는다."""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None)
    if args and len(args) >= 2:
        return f"[{args[0]}] {args[1]}"
    return str(orig or exc).split("\n")[0]


def explain_sql(engine: Engine, sql: str) -> str | None:
    """EXPLAIN으로 실행 없이 검증. 오류 메시지를 돌려주고, 문제없으면 None.

    존재하지 않는 컬럼·테이블, 문법 오류 등 정적 검사로는 잡히지 않는 것을
    MySQL 본인에게 물어본다. EXPLAIN은 DML도 실행하지 않는다."""
    if classify_sql(sql) not in ("select", "dml"):
        return None
    body = sql.strip().rstrip(";")
    if _SHOW_RE.match(body):   # SHOW / DESCRIBE / EXPLAIN은 EXPLAIN 대상이 아니다
        return None
    if _DANGEROUS_PATTERNS.search(_strip_string_literals(body)):
        return None
    try:
        with engine.connect() as conn:
            conn.execute(text(f"EXPLAIN {body}"))
        return None
    except Exception as e:
        return _db_error_message(e)


def check_sql(engine: Engine, sql: str, report_skip: bool = True) -> list[str]:
    """정적 검사 + EXPLAIN 시험. LLM 생성물과 손으로 고친 SQL 모두에 쓴다.

    report_skip: DB에 물어보지 못했을 때 그 사실을 결과에 넣을지.
                 화면에는 알려야 검증된 SQL로 오해하지 않는다. 반대로 LLM
                 재생성에는 되먹이지 않는다 — SQL이 틀린 게 아니라 확인을
                 못 한 것이라, 멀쩡한 쿼리를 고치게 만들고 재시도만 태운다."""
    findings = validate_sql(sql)
    try:
        db_error = explain_sql(engine, sql)
    except Exception as e:          # 연결 실패 등 — 검증 실패로 흐름을 막지 않는다
        logger.warning(f"EXPLAIN 검증 생략: {e}")
        if report_skip:
            findings.append(
                "DB에 연결하지 못해 실행 가능 여부를 확인하지 못했습니다 "
                f"— {_db_error_message(e)}. 정적 검사만 통과한 상태입니다.")
        return findings
    if db_error:
        findings.append(f"MySQL이 거부한 쿼리입니다 — {db_error}")
    return findings

def add_limit(sql: str, limit: int = 20000) -> str:
    """SELECT에 LIMIT이 없으면 강제 주입. SHOW / DESCRIBE / EXPLAIN은 스킵."""
    if classify_sql(sql) != "select":
        return sql
    if _SHOW_RE.match(sql.strip()):
        return sql
    # 서브쿼리·문자열 안의 LIMIT에 오탐하지 않도록 최상위 레벨만 검사한다.
    top_level = _strip_parens(_strip_string_literals(sql))
    if re.search(r'\bLIMIT\b', top_level, re.IGNORECASE):
        return sql
    sql_stripped = sql.rstrip().rstrip(";")
    return f"{sql_stripped} LIMIT {limit}"


# 실행

def run_select(engine: Engine, sql: str, limit: int = 20000) -> pd.DataFrame:
    guard_sql(sql, allow_write=False)
    safe_sql = add_limit(sql, limit)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(safe_sql))
            rows = result.fetchall()
            cols = list(result.keys())
        return pd.DataFrame(rows, columns=cols)
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"SELECT 실행 실패: {e}")


def run_write(engine: Engine, sql: str, commit: bool = False) -> dict:
    guard_sql(sql, allow_write=True)

    kind = classify_sql(sql)

    if kind == "ddl" and not commit:
        return {"rowcount": -1, "committed": False,
                "message": "DDL은 미리보기가 지원되지 않습니다. SQL을 확인 후 실행하세요."}

    try:
        if not commit:
            # rollback 경로 — 커밋하지 않고 종료
            with engine.connect() as conn:
                result   = conn.execute(text(sql))
                rowcount = result.rowcount if result.rowcount is not None else -1
            return {"rowcount": rowcount, "committed": False}

        with engine.begin() as conn:
            result   = conn.execute(text(sql))
            rowcount = result.rowcount if result.rowcount is not None else -1
            return {"rowcount": rowcount, "committed": True}
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"쓰기 실행 실패: {e}")

def run_write_batch(engine: Engine, items: list[dict]) -> dict:
    if not items:
        raise DbBuilderError("실행할 SQL이 없습니다.")

    for item in items:
        sql = item.get("exec_sql", "")
        guard_sql(sql, allow_write=True)
        if classify_sql(sql) != "dml":
            raise DbBuilderError(
                "배치 실행은 DML(INSERT/UPDATE/DELETE)만 지원합니다."
            )

    total = 0
    try:
        with engine.begin() as conn:
            for item in items:
                result = conn.execute(
                    text(item["exec_sql"]),
                    item.get("params") or {},
                )
                if result.rowcount is not None and result.rowcount > 0:
                    total += result.rowcount
        return {"rowcount": total, "committed": True}
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"배치 실행 실패 — 전체 롤백되었습니다: {e}")

# LLM 호출 (NL2SQL)

# 규칙은 번호를 매긴 짧은 문장으로 둔다. 한 문단으로 이어 쓰면 소형 모델이
# 뒤쪽 규칙을 무시한다. 자주 틀리는 규칙(HAVING·GROUP BY·조인 집계)은 예시를 함께 준다.
_NL2SQL_SYSTEM = """당신은 MySQL 8.0 전문가다. 주어진 스키마로 자연어 질의에 답하는 MySQL 쿼리를 만든다.

규칙:
1. 주석·설명 없이 SQL 쿼리만 출력한다. 세미콜론은 문장 끝에 한 번만 붙인다.
2. [DB 스키마]에 있는 테이블과 컬럼만 쓴다. 없는 이름은 절대 만들어내지 않는다.
3. 테이블명과 컬럼명은 백틱(`)으로 감싼다.
4. 별칭에는 공백을 쓰지 않는다. 질의에 '단말기 개수'라고 적혀 있어도 별칭은 `AS 단말기_개수`로 쓴다.
5. 집계 함수(SUM/COUNT/AVG/MIN/MAX)를 쓴 조건은 WHERE에 넣을 수 없다. 반드시 GROUP BY 뒤의 HAVING에 넣는다.
   - 틀린 예: SELECT a, SUM(b) FROM t WHERE SUM(b) > 10 GROUP BY a
   - 맞는 예: SELECT a, SUM(b) AS 합계 FROM t GROUP BY a HAVING 합계 > 10
6. 값은 SQL에 리터럴로 직접 쓴다. 플레이스홀더(?)를 쓰지 않는다.
7. CAST 대상 타입은 SIGNED, UNSIGNED, DECIMAL, CHAR, DATE, DATETIME만 쓴다. BIGINT/INT로 CAST하지 않는다.
8. FULL OUTER JOIN은 MySQL에 없다. LEFT JOIN을 쓴다.
9. 두 테이블을 조인할 때는 값의 형태가 실제로 맞물리는 컬럼끼리 연결한다. 샘플 데이터를 보고 판단한다.
10. GROUP BY를 쓰면 SELECT 목록의 모든 컬럼은 GROUP BY 절에 있거나 집계 함수 안에 있어야 한다.
    조인한 상대 테이블의 컬럼도 예외가 아니다.
   - 틀린 예: SELECT a.k, SUM(a.v), b.w FROM a JOIN b ON a.k = b.k GROUP BY a.k
   - 맞는 예: SELECT a.k, SUM(a.v), SUM(b.w) FROM a JOIN b ON a.k = b.k GROUP BY a.k
11. 한 행에 여러 행이 딸린 관계(1:N)를 조인한 뒤 SUM을 쓰면 행이 복제되어 합계가 부풀어 오른다.
    N쪽 테이블을 먼저 GROUP BY로 집계한 뒤, 그 결과와 조인한다.
   - 틀린 예: SELECT a.k, SUM(a.v), SUM(b.w) FROM a JOIN b ON a.k = b.k GROUP BY a.k
   - 맞는 예: SELECT a.k, a.v, COALESCE(t.합, 0)
              FROM a LEFT JOIN (SELECT k, SUM(w) AS 합 FROM b GROUP BY k) t ON a.k = t.k"""

_REPAIR_INSTRUCTION = """방금 쿼리는 실행할 수 없다.

[오류]
{errors}

오류를 고친 MySQL 쿼리 전체를 다시 출력한다. 설명은 쓰지 않는다."""


_ALIAS_STOP_WORDS = {"FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT"}
_ALIAS_TOKEN_RE   = re.compile(r'^[\w가-힣]+$')

def _quote_unquoted_alias_with_space(sql: str) -> str:
    pattern = re.compile(r'\b(AS)\s+([^,;]+?)(?=[,;]|$)', re.IGNORECASE)

    def _repl(m: re.Match) -> str:
        as_kw = m.group(1)
        rest  = m.group(2)
        tokens = rest.split()

        collected: list[str] = []
        for tok in tokens:
            if tok.upper() in _ALIAS_STOP_WORDS:
                break
            # 괄호·연산자 포함 토큰 → CAST(... AS TYPE) 같은 표현식이므로 원문 유지
            if not _ALIAS_TOKEN_RE.match(tok):
                return m.group(0)
            collected.append(tok)

        if len(collected) < 2:
            return m.group(0)

        alias     = " ".join(collected)
        remainder = rest[len(alias):]
        return f"{as_kw} `{alias}`{remainder}"

    return pattern.sub(_repl, sql)


def _chat(messages: list[dict], model_name: str, endpoint: str) -> str:
    payload: dict = {
        "messages":    messages,
        "stream":      False,
        "temperature": 0.1,
        "max_tokens":  512,
    }
    if model_name:
        payload["model"] = model_name

    try:
        res = requests.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=(10, 120),
        )
        if res.status_code != 200:
            raise DbBuilderError(f"LM Studio 응답 오류: {res.status_code} - {res.text}")
        return res.json()["choices"][0]["message"]["content"]
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"LM Studio 통신 실패: {e}")


def _extract_sql(raw: str) -> str:
    """모델 응답에서 SQL만 남긴다 (think 블록·코드펜스 제거)."""
    sql = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
    sql = re.sub(r'```(?:sql)?', '', sql, flags=re.IGNORECASE)
    sql = sql.replace('```', '').strip()

    if not sql:
        raise DbBuilderError("LLM이 SQL을 생성하지 못했습니다.")

    return _quote_unquoted_alias_with_space(sql)


def generate_sql(user_question: str, schema_prompt: str,
                 model_name: str, endpoint: str,
                 validate: Callable[[str], list[str]] | None = None,
                 max_repair: int = 2) -> str:
    """자연어 → SQL. 검증에 걸리면 오류를 되먹여 재생성을 시도한다.

    validate: SQL을 받아 오류 문장 목록을 돌려주는 함수 (보통 check_sql 부분적용).
              정적 검사(validate_sql)는 validate와 무관하게 항상 수행한다.
    max_repair: 재생성 시도 횟수. 0이면 되먹임 없이 첫 결과를 그대로 돌려준다.

    끝까지 오류가 남아도 SQL은 돌려준다 — 사용자가 화면에서 직접 고칠 수 있고,
    실행 전 check_sql이 같은 오류를 다시 경고한다."""
    messages = [
        {"role": "system", "content": _NL2SQL_SYSTEM},
        {"role": "user",   "content": (f"/no_think\n\n"
                                       f"[DB 스키마]\n{schema_prompt}\n\n"
                                       f"[질의]\n{user_question}")},
    ]

    sql = _extract_sql(_chat(messages, model_name, endpoint))

    for attempt in range(max_repair):
        # validate가 check_sql이면 정적 검사 결과가 겹친다 — 순서 유지하며 중복 제거.
        errors = list(dict.fromkeys(
            validate_sql(sql) + (validate(sql) if validate else [])))
        if not errors:
            return sql

        logger.info(f"생성 SQL 검증 실패 (재시도 {attempt + 1}/{max_repair}): {errors}")
        messages += [
            {"role": "assistant", "content": sql},
            {"role": "user",
             "content": _REPAIR_INSTRUCTION.format(
                 errors="\n".join(f"- {e}" for e in errors))},
        ]
        sql = _extract_sql(_chat(messages, model_name, endpoint))

    # 플레이스홀더는 재생성으로도 안 고쳐지면 질의 자체가 값을 안 담고 있는 것이다.
    if '?' in _strip_string_literals(sql):
        raise DbBuilderError(
            "LLM이 값을 특정하지 못해 플레이스홀더(?)를 생성했습니다. "
            "질의에 구체적인 값을 포함해 다시 시도해주세요.\n"
            "예) '설치 날짜가 ? 인 행의 설치 날짜를 NULL로 변경해줘'"
        )

    return sql


# PDF 표 → 적재

def parse_markdown_tables(md_text: str) -> list[pd.DataFrame]:
    results: list[pd.DataFrame] = []

    table_pattern = re.compile(
        r'(\|[^\n]+\|\n\|[-:| ]+\|\n(?:\|[^\n]+\|\n?)+)',
        re.MULTILINE,
    )
    matches = table_pattern.findall(md_text)

    def _clean_cell(s: str) -> str:
        s = re.sub(r'\*{1,3}', '', s)
        s = re.sub(r'_{1,3}', '', s)
        return s.strip()

    for match in matches:
        try:
            lines = [l.strip() for l in match.strip().split('\n') if l.strip()]
            if len(lines) < 2:
                continue

            headers    = [_clean_cell(h) for h in lines[0].split('|') if h.strip()]
            data_lines = lines[2:]

            rows = []
            for line in data_lines:
                cells = [_clean_cell(c) for c in line.split('|') if c != '']
                if len(cells) < len(headers):
                    cells += [''] * (len(headers) - len(cells))
                elif len(cells) > len(headers):
                    cells = cells[:len(headers)]
                rows.append(cells)

            if not rows:
                continue

            df = pd.DataFrame(rows, columns=headers)
            results.append(df)
        except Exception as e:
            logger.warning(f"표 파싱 실패 (스킵): {e}")
            continue

    logger.info(f"parse_markdown_tables: {len(results)}개 표 추출")
    return results


_DTYPE_MAP: dict[str, str] = {
    "int64":   "INT",
    "float64": "DOUBLE",
    "float32": "FLOAT",
    "bool":    "TINYINT(1)",
    "object":  "TEXT",
    "string":  "TEXT",
}


_LEADING_ZERO_RE = re.compile(r'^0\d+$')


def infer_column_types(df: pd.DataFrame) -> dict[str, str]:
    result: dict[str, str] = {}
    for col in df.columns:
        series   = df[col]
        non_null = series.notna().sum()

        # 앞자리 0이 있는 값(우편번호·사번)은 숫자 변환 시 손상되므로(06236 → 6236) 텍스트 유지
        has_leading_zero = (
            non_null > 0
            and series.dropna().astype(str).str.match(_LEADING_ZERO_RE).any()
        )

        if not has_leading_zero:
            converted = pd.to_numeric(series, errors='coerce')
            # 비어있지 않은 값 전부가 숫자로 변환된 경우만 숫자형
            if non_null > 0 and converted.notna().sum() == non_null:
                if (converted.dropna() % 1 == 0).all():
                    result[col] = "BIGINT"
                else:
                    result[col] = "DOUBLE"
                continue

        dtype_str = str(series.dtype)
        result[col] = _DTYPE_MAP.get(dtype_str, "TEXT")
    return result


# UI 타입 문자열 → SQLAlchemy 타입. file_table.py의 _SQL_TYPE_OPTIONS와 1:1 대응.
_SQL_TYPE_TO_SA = {
    "TEXT":         Text(),
    "BIGINT":       BigInteger(),
    "INT":          Integer(),
    "DOUBLE":       MYSQL_DOUBLE(),
    "FLOAT":        Float(),
    "TINYINT(1)":   MYSQL_TINYINT(display_width=1),
    "DATE":         Date(),
    "DATETIME":     DateTime(),
    "VARCHAR(255)": String(255),
}


def _build_sa_dtype(df: pd.DataFrame,
                    col_types: dict[str, str] | None) -> dict | None:
    """UI 타입 문자열 dict → to_sql용 SQLAlchemy 타입 dict.

    df에 없는 컬럼은 스킵, 모르는 타입은 Text로 폴백,
    결과가 비면 None(= to_sql 기본 추론)을 돌려준다."""
    if not col_types:
        return None

    dtype: dict = {}
    for col, type_str in col_types.items():
        if col not in df.columns:
            logger.warning(f"col_types의 컬럼 '{col}'이 DataFrame에 없음 — 스킵")
            continue
        dtype[col] = _SQL_TYPE_TO_SA.get(type_str, Text())

    return dtype or None


def _normalize_empty_strings(df: pd.DataFrame) -> pd.DataFrame:
    """빈 문자열만 NULL로 치환한다.

    "nan"/"None"까지 치환하면 같은 값을 가진 실제 데이터가 손상된다."""
    return df.replace({"": None})


# 행 식별용 PK 자동 부여
#
# to_sql은 PK 없는 테이블을 만드는데, PK가 없으면 인라인 편집 UPDATE가 행을
# 특정할 수 없다. INVISIBLE이라 SELECT * 결과에는 나타나지 않는다.

ROW_ID_COLUMN = "my_row_id"

_ADD_PK_DDL = (
    "ALTER TABLE `{table}` ADD COLUMN `{col}` BIGINT UNSIGNED "
    "AUTO_INCREMENT PRIMARY KEY INVISIBLE FIRST"
)

_SAFE_IDENT_RE = re.compile(r'^[\w가-힣]+$')


def has_primary_key(engine: Engine, table: str) -> bool:
    try:
        pk = inspect(engine).get_pk_constraint(table)
        return bool(pk.get("constrained_columns"))
    except Exception as e:
        logger.warning(f"PK 조회 실패 ({table}): {e}")
        return False


def build_add_pk_sql(table: str) -> str:
    """INVISIBLE auto-increment PK 추가 DDL."""
    if not _SAFE_IDENT_RE.match(table):
        raise DbBuilderError(f"테이블명에 사용할 수 없는 문자가 있습니다: {table}")
    return _ADD_PK_DDL.format(table=table, col=ROW_ID_COLUMN)


def build_create_view_sql(view: str, select_sql: str,
                          or_replace: bool = False) -> str:
    """SELECT를 뷰로 굳히는 DDL.

    새 테이블 저장이 '지금 이 순간의 사본'이라면 뷰는 '질의 자체'다. 원본이
    바뀌면 뷰 조회 결과도 따라 바뀌므로, 조인·집계 결과를 계속 들여다볼 때 쓴다.
    대신 뷰에는 기본키가 없어 인라인 편집은 여전히 막힌다."""
    if not _SAFE_IDENT_RE.match(view):
        raise DbBuilderError(f"뷰 이름에 사용할 수 없는 문자가 있습니다: {view}")
    if len(view) > 64:                      # MySQL 식별자 상한
        raise DbBuilderError("뷰 이름은 64자를 넘을 수 없습니다.")

    body = select_sql.strip().rstrip(";").strip()
    if not body:
        raise DbBuilderError("뷰로 만들 SELECT가 비어 있습니다.")
    if classify_sql(body) != "select":
        raise DbBuilderError("SELECT 결과만 뷰로 만들 수 있습니다.")
    # SHOW/DESCRIBE/EXPLAIN은 classify_sql이 select로 묶지만 뷰가 될 수 없다.
    if _SHOW_RE.match(body):
        raise DbBuilderError("SHOW / DESCRIBE / EXPLAIN은 뷰로 만들 수 없습니다.")
    if _DANGEROUS_PATTERNS.search(_strip_string_literals(body)):
        raise DbBuilderError("뷰 정의에 허용되지 않는 구문이 있습니다.")

    head = "CREATE OR REPLACE VIEW" if or_replace else "CREATE VIEW"
    return f"{head} `{view}` AS\n{body}"


def create_view(engine: Engine, view: str, select_sql: str,
                or_replace: bool = False) -> str:
    """뷰를 만들고 실행한 DDL을 돌려준다."""
    ddl = build_create_view_sql(view, select_sql, or_replace)
    try:
        with engine.begin() as conn:
            conn.execute(text(ddl))
    except Exception as e:
        raise DbBuilderError(f"뷰 생성 실패 ({view}): {_db_error_message(e)}")
    logger.info(f"뷰 생성: {view}")
    return ddl

def ensure_row_id_pk(engine: Engine, table: str) -> bool:
    """PK가 없으면 INVISIBLE PK를 추가한다. 실제 추가했으면 True.

    적재는 이미 끝난 상태이므로 실패해도 예외를 올리지 않는다."""
    if has_primary_key(engine, table):
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text(build_add_pk_sql(table)))
        logger.info(f"{table}: {ROW_ID_COLUMN} PK 추가됨")
        return True
    except Exception as e:
        logger.warning(f"{table}: PK 추가 실패 — {e}")
        return False


def load_dataframe(engine: Engine, df: pd.DataFrame,
                   table: str, if_exists: str = "fail",
                   col_types: dict[str, str] | None = None) -> int:
    """DataFrame을 MySQL 테이블로 적재.

    col_types({컬럼명: 타입문자열})는 신규 생성·replace 시에만 반영된다
    (append는 기존 스키마가 우선)."""
    if df.empty:
        raise DbBuilderError("적재할 데이터가 없습니다 (DataFrame이 비어 있음).")

    df = _normalize_empty_strings(df)

    dtype = _build_sa_dtype(df, col_types)

    try:
        written = df.to_sql(
            name=table,
            con=engine,
            if_exists=if_exists,
            index=False,
            chunksize=500,
            dtype=dtype,
        )
        count = written if written is not None else len(df)
        logger.info(f"load_dataframe 완료: {table} {count}행"
                    + (f" (타입 지정 {len(dtype)}개 컬럼)" if dtype else ""))
    except Exception as e:
        raise DbBuilderError(f"테이블 적재 실패 ({table}): {e}")

    # 테이블을 새로 만든 경로에서만 PK를 부여한다 (append는 스키마를 건드리지 않는다).
    if if_exists in ("fail", "replace"):
        ensure_row_id_pk(engine, table)

    return count


# 편집 가능 여부 판정 / 키 컬럼 확보

# 조인·집계·중복제거·서브쿼리 결과는 원본 행과 1:1 대응이 안 되므로 편집 불가.
_NON_EDITABLE_RE = re.compile(
    r'\b(JOIN|GROUP\s+BY|UNION|DISTINCT)\b'
    r'|\(\s*SELECT\b'
    r'|\bFROM\s+`?\w+`?\s*,',
    re.IGNORECASE)

_FROM_RE = re.compile(r'\bFROM\b', re.IGNORECASE)


def is_single_table_select(sql: str) -> bool:
    """단일 테이블 단순 조회인지 판정."""
    return not _NON_EDITABLE_RE.search(_strip_string_literals(sql))


def extract_select_table(sql: str) -> str | None:
    """SELECT의 FROM 뒤 테이블명을 뽑는다."""
    m = re.search(r'\bFROM\s+`?(\w+)`?', _strip_string_literals(sql), re.IGNORECASE)
    return m.group(1) if m else None


def extract_target_table(sql: str) -> str | None:
    """임의 구문에서 대상 테이블명을 뽑는다 (실행 후 자동 조회용).

    DROP TABLE은 대상이 사라지므로 None."""
    masked = _strip_string_literals(sql)
    if re.search(r'\bDROP\s+TABLE\b', masked, re.IGNORECASE):
        return None
    m = re.search(r'\b(?:INTO|TABLE|FROM|UPDATE)\s+`?(\w+)`?', masked, re.IGNORECASE)
    return m.group(1) if m else None


def get_primary_key_columns(engine: Engine, table: str) -> list[str]:
    try:
        pk = inspect(engine).get_pk_constraint(table)
        return list(pk.get("constrained_columns") or [])
    except Exception as e:
        logger.warning(f"PK 컬럼 조회 실패 ({table}): {e}")
        return []


def inject_key_columns(sql: str, columns: list[str]) -> str:
    """SELECT 컬럼 목록 끝(FROM 앞)에 키 컬럼을 덧붙인다.

    INVISIBLE PK는 명시해야 조회된다. MySQL이 `SELECT col, *`를 허용하지 않아
    앞이 아닌 FROM 바로 앞에 넣는다."""
    if not columns:
        return sql
    m = _FROM_RE.search(_mask_string_literals(sql))
    if not m:
        raise DbBuilderError("FROM 절을 찾을 수 없어 키 컬럼을 추가할 수 없습니다.")
    added = ", " + ", ".join(f"`{c}`" for c in columns) + " "
    return sql[:m.start()] + added + sql[m.start():]


# 인라인 편집 → UPDATE 생성

def build_update_sqls(original: pd.DataFrame,
                      edited: pd.DataFrame,
                      table: str,
                      pk_values: pd.DataFrame) -> list[dict]:
    """변경된 셀에 대한 UPDATE 문 생성. WHERE는 기본키로만 구성한다.

    pk_values: original과 같은 인덱스, 컬럼이 PK 컬럼명인 DataFrame."""
    if list(original.columns) != list(edited.columns):
        raise DbBuilderError("원본과 편집본의 컬럼 구조가 다릅니다.")

    if original.shape[0] != edited.shape[0]:
        raise DbBuilderError("행 수가 다릅니다. 행 추가/삭제는 지원하지 않습니다.")

    if pk_values is None or len(pk_values.columns) == 0:
        raise DbBuilderError("행을 특정할 기본키가 없어 변경을 적용할 수 없습니다.")

    if len(pk_values) != len(original):
        raise DbBuilderError("기본키 정보가 조회 결과와 일치하지 않습니다.")

    pk_cols = list(pk_values.columns)
    cols    = list(original.columns)
    results = []

    for idx in original.index:
        orig_row = original.loc[idx]
        edit_row = edited.loc[idx]

        if (orig_row.astype(str) == edit_row.astype(str)).all():
            continue

        params: dict = {}
        p_seq = 0

        def _bind(val) -> str:
            nonlocal p_seq
            key = f"p{p_seq}"
            p_seq += 1
            params[key] = str(val)
            return f":{key}"

        def _display(val) -> str:
            escaped = str(val).replace("'", "''")
            return f"'{escaped}'"

        set_disp, set_exec = [], []
        for col in cols:
            if str(orig_row[col]) != str(edit_row[col]):
                val = edit_row[col]
                if pd.isna(val):
                    set_disp.append(f"`{col}` = NULL")
                    set_exec.append(f"`{col}` = NULL")
                else:
                    set_disp.append(f"`{col}` = {_display(val)}")
                    set_exec.append(f"`{col}` = {_bind(val)}")

        where_disp, where_exec = [], []
        for col in pk_cols:
            val = pk_values.loc[idx, col]
            if pd.isna(val):
                raise DbBuilderError(f"기본키 `{col}` 값이 비어 있어 행을 특정할 수 없습니다.")
            where_disp.append(f"`{col}` = {_display(val)}")
            where_exec.append(f"`{col}` = {_bind(val)}")

        display_sql = (f"UPDATE `{table}` "
                       f"SET {', '.join(set_disp)} "
                       f"WHERE {' AND '.join(where_disp)}")
        exec_sql    = (f"UPDATE `{table}` "
                       f"SET {', '.join(set_exec)} "
                       f"WHERE {' AND '.join(where_exec)}")

        results.append({
            "sql":      display_sql,
            "exec_sql": exec_sql,
            "params":   params,
            "warning":  None,
        })

    return results


# DDL 정적 검사

# ADD/DROP 뒤에 오지만 컬럼명이 아닌 키워드 (`DROP PRIMARY KEY` 오탐 방지).
_NON_COLUMN_KEYWORDS = {
    "INDEX", "KEY", "PRIMARY", "UNIQUE", "CONSTRAINT",
    "FOREIGN", "FULLTEXT", "SPATIAL", "CHECK",
}


def preview_ddl(engine: Engine, sql: str) -> dict:
    guard_sql(sql, allow_write=True)
    if classify_sql(sql) != "ddl":
        raise DbBuilderError("DDL이 아닙니다.")

    findings: list[dict] = []
    result = {"type": "알 수 없음", "table": None, "findings": findings}

    existing_tables = list_tables(engine)

    m = re.match(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?', sql, re.IGNORECASE)
    if m:
        table          = m.group(1)
        result["type"]  = "CREATE TABLE"
        result["table"] = table
        if table in existing_tables:
            if re.search(r'IF\s+NOT\s+EXISTS', sql, re.IGNORECASE):
                findings.append({"level": "warning",
                                  "msg": f"테이블 {table} 이미 존재 — IF NOT EXISTS로 인해 스킵됩니다"})
            else:
                findings.append({"level": "error",
                                  "msg": f"테이블 {table} 이미 존재 — 실행 시 오류 발생"})
        else:
            findings.append({"level": "info", "msg": f"테이블 {table} 신규 생성"})
        return result

    m = re.match(r'DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?`?(\w+)`?', sql, re.IGNORECASE)
    if m:
        table          = m.group(1)
        result["type"]  = "DROP TABLE"
        result["table"] = table
        if table not in existing_tables:
            findings.append({"level": "error",
                              "msg": f"테이블 {table} 존재하지 않음 — 실행 시 오류 발생"})
        else:
            findings.append({"level": "warning",
                              "msg": f"테이블 {table} 및 모든 데이터 영구 삭제"})
        return result

    m = re.match(r'ALTER\s+TABLE\s+`?(\w+)`?', sql, re.IGNORECASE)
    if m:
        table          = m.group(1)
        result["type"]  = "ALTER TABLE"
        result["table"] = table

        if table not in existing_tables:
            findings.append({"level": "error",
                              "msg": f"테이블 {table} 존재하지 않음 — 실행 시 오류 발생"})
            return result

        findings.append({"level": "info", "msg": f"대상 테이블 {table} 존재함"})

        try:
            existing_cols = {c["name"] for c in inspect(engine).get_columns(table)}
        except Exception:
            existing_cols = set()

        for m_add in re.finditer(r'\bADD\s+(?:COLUMN\s+)?`?(\w+)`?', sql, re.IGNORECASE):
            col = m_add.group(1)
            if col.upper() in _NON_COLUMN_KEYWORDS:
                findings.append({"level": "info",
                                  "msg": f"ADD {col.upper()} — 컬럼이 아닌 인덱스/제약 추가"})
            elif col in existing_cols:
                findings.append({"level": "error",
                                  "msg": f"ADD COLUMN {col} — 이미 존재하는 컬럼"})
            else:
                findings.append({"level": "info",
                                  "msg": f"ADD COLUMN {col} — 신규 추가"})

        for m_drop in re.finditer(r'\bDROP\s+(?:COLUMN\s+)?`?(\w+)`?', sql, re.IGNORECASE):
            col = m_drop.group(1)
            if col.upper() in _NON_COLUMN_KEYWORDS:
                findings.append({"level": "warning",
                                  "msg": f"DROP {col.upper()} — 컬럼이 아닌 인덱스/제약 삭제"})
            elif col not in existing_cols:
                findings.append({"level": "error",
                                  "msg": f"DROP COLUMN {col} — 존재하지 않는 컬럼"})
            else:
                findings.append({"level": "warning",
                                  "msg": f"DROP COLUMN {col} — 삭제 후 복구 불가"})

        for col_m in re.finditer(
            r'RENAME\s+COLUMN\s+`?(\w+)`?\s+TO\s+`?(\w+)`?', sql, re.IGNORECASE
        ):
            old, new = col_m.group(1), col_m.group(2)
            if old not in existing_cols:
                findings.append({"level": "error",
                                  "msg": f"RENAME COLUMN {old} — 존재하지 않는 컬럼"})
            else:
                findings.append({"level": "info",
                                  "msg": f"RENAME COLUMN {old} → {new}"})

        return result

    findings.append({"level": "info", "msg": "세부 분석이 지원되지 않는 DDL 구문입니다."})
    return result