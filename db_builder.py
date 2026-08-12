# db_builder.py
# 순수 로직 모듈 — Streamlit을 import하지 않는다. UI는 전부 페이지 쪽 담당.

import logging
import re
import tomllib
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

def list_tables(engine: Engine) -> list[str]:
    try:
        return inspect(engine).get_table_names()
    except Exception as e:
        raise DbBuilderError(f"테이블 목록 조회 실패: {e}")


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

_NL2SQL_SYSTEM = (
    "당신은 MySQL 전문가입니다. "
    "주어진 스키마로 자연어 질의에 대한 MySQL 쿼리를 반환한다. "
    "테이블명과 컬럼명은 반드시 백틱(`)으로 감싼다."
    "별칭(AS)에는 공백 대신 언더바(_)를 사용한다. "
    "별칭(AS 뒤에 오는 이름)에는 절대 공백을 사용하지 않는다. "
    "사용자 질의에 공백이 포함된 단어가 있어도, 별칭에는 반드시 언더바(_)로 변환해 적용한다. "
    "예시: 사용자가 '단말기 개수'라고 표현해도 별칭은 AS 단말기_개수 로 작성한다. "
    "파라미터 플레이스홀더(?)는 사용하지 않는다. 값은 SQL에 직접 리터럴로 작성한다. "
    "주석 없이 SQL 쿼리만 출력한다. "
    "세미콜론은 문장 끝에 한 번만 붙인다. "
    "CAST의 대상 타입은 SIGNED, UNSIGNED, DECIMAL, CHAR, DATE, DATETIME만 사용한다. BIGINT/INT로 CAST하지 않는다."
    "MySQL 8.0 문법만 사용한다. FULL OUTER JOIN은 존재하지 않으므로 사용하지 않는다. "
    "집계 함수 조건은 WHERE가 아닌 HAVING에 작성한다. "
    "[DB 스키마]에 명시된 테이블과 컬럼만 사용한다. 스키마에 없는 이름은 절대 사용하지 않는다. "
)


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


def generate_sql(user_question: str, schema_prompt: str,
                 model_name: str, endpoint: str) -> str:
    prompt = (
        f"/no_think\n\n"
        f"[DB 스키마]\n{schema_prompt}\n\n"
        f"[질의]\n{user_question}"
    )

    payload: dict = {
        "messages": [
            {"role": "system", "content": _NL2SQL_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
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
        raw = res.json()["choices"][0]["message"]["content"]
    except DbBuilderError:
        raise
    except Exception as e:
        raise DbBuilderError(f"LM Studio 통신 실패: {e}")

    sql = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
    sql = re.sub(r'```(?:sql)?', '', sql, flags=re.IGNORECASE)
    sql = sql.replace('```', '').strip()

    if not sql:
        raise DbBuilderError("LLM이 SQL을 생성하지 못했습니다.")

    sql_no_strings = _strip_string_literals(sql)
    if '?' in sql_no_strings:
        raise DbBuilderError(
            "LLM이 값을 특정하지 못해 플레이스홀더(?)를 생성했습니다. "
            "질의에 구체적인 값을 포함해 다시 시도해주세요.\n"
            "예) '설치 날짜가 ? 인 행의 설치 날짜를 NULL로 변경해줘'"
        )
    sql = _quote_unquoted_alias_with_space(sql)

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