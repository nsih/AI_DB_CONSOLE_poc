# 실제 DB와 LM Studio를 둘 다 붙여 돌리는 회귀 테스트.
#
# test_db_builder.py와 달리 외부 의존이 있어 기본 실행에서는 빠진다.
#   pytest            → 제외 (pytest.ini의 addopts)
#   pytest -m live    → 이 파일만
#
# 검사 대상은 SQL 문자열이 아니라 결과 숫자다. 같은 정답을 모델이 서브쿼리로도
# GROUP BY로도 쓰는데 둘 다 맞는 답이므로, 모양을 고정하면 오탐만 늘어난다.

from decimal import Decimal

import pytest
import requests
from sqlalchemy import text

import db_builder as db

pytestmark = pytest.mark.live


# 1:N 팬아웃 함정.
#
# BUILDING_IP_COUNTS(8행)의 값은 그대로 보존하고 ROOM_IP_COUNTS(392행)만
# 합산해야 한다. 조인부터 하고 양쪽에 SUM을 걸면 건물 값이 그 호관의 호실_수만큼
# 부풀어 오른다 (185 → 11,840 등). 원 질의와 달리 "호관별로 한 행씩"으로 grain을
# 못 박았다 — 호관별/호실별을 오가면 팬아웃과 무관하게 판정이 흔들린다.
NL_QUESTION = (
    "호관별로 한 행씩 만들어줘.\n"
    "BUILDING_IP_COUNTS에 적힌 그 호관의 단말기 수와, ROOM_IP_COUNTS에서 "
    "그 호관에 속한 호실들의 단말기 개수를 합한 값을 나란히 놓아서 "
    "두 값이 일치하는지 볼 수 있게 해줘.\n"
    "사용호실의 첫 글자 숫자가 호관 번호다.\n"
    "호관 오름차순 정렬"
)
TABLES = ["BUILDING_IP_COUNTS", "ROOM_IP_COUNTS"]


@pytest.fixture(scope="module")
def engine():
    try:
        return db.get_engine()
    except db.DbBuilderError as e:
        pytest.skip(f"DB에 연결할 수 없다: {e}")


@pytest.fixture(scope="module")
def worker():
    """LM Studio 접속 정보. 서버가 안 떠 있으면 실패가 아니라 skip."""
    secrets = db._load_secrets()
    host = secrets.get("AI_WORKER_IP")
    port = secrets.get("AI_WORKER_PORT")
    if not host or not port:
        pytest.skip("secrets.toml에 AI_WORKER_IP / AI_WORKER_PORT가 없다")

    base = f"http://{host}:{port}"
    try:
        requests.get(f"{base}/v1/models", timeout=5).raise_for_status()
    except Exception as e:
        pytest.skip(f"LM Studio에 연결할 수 없다 ({base}): {e}")

    return f"{base}/v1/chat/completions", secrets.get("AI_MODEL_NAME", "")


@pytest.fixture(scope="module")
def truth(engine):
    """호관 → 단말기 수.

    이 검사는 두 표의 합계가 원래 서로 같다는 성질에 기댄다. 데이터가 어긋나
    있으면 모델 잘못이 아니므로 실패가 아니라 skip으로 빠진다."""
    with engine.connect() as conn:
        building = conn.execute(text(
            "SELECT `호관`, CAST(`IP가_부여된_단말기_수` AS SIGNED) "
            "FROM BUILDING_IP_COUNTS")).all()
        room = conn.execute(text(
            "SELECT SUBSTRING(`사용호실`, 1, 1), SUM(`단말기_개수`) "
            "FROM ROOM_IP_COUNTS GROUP BY 1")).all()

    by_building = {str(k): int(v) for k, v in building if v is not None}
    by_room     = {str(k): int(v) for k, v in room if v is not None}
    if not by_building:
        pytest.skip("BUILDING_IP_COUNTS가 비어 있다")
    if by_building != by_room:
        pytest.skip("두 표의 단말기 수가 서로 맞지 않아 이 검사를 쓸 수 없다 "
                    f"(건물 {by_building} / 호실 {by_room})")
    return by_building


def _as_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, Decimal, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _dump(columns, rows, limit: int = 10) -> str:
    lines = [" | ".join(map(str, columns))]
    lines += [" | ".join(str(v) for v in row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"... 총 {len(rows)}행")
    return "\n".join(lines)


def test_1대N_조인에서_합계가_부풀지_않는다(engine, worker, truth):
    endpoint, model_name = worker
    sql = db.generate_sql(
        NL_QUESTION,
        db.get_schema_prompt(engine, tables=TABLES),
        model_name, endpoint,
        validate=lambda s: db.check_sql(engine, s, report_skip=False),
    )

    with engine.connect() as conn:
        result  = conn.execute(text(db.add_limit(sql.rstrip(";"), 5000)))
        columns = list(result.keys())
        rows    = result.fetchall()

    report = f"\n--- 생성된 SQL ---\n{sql}\n--- 결과 ---\n{_dump(columns, rows)}"

    assert "호관" in columns, f"호관 컬럼이 없다{report}"
    key = columns.index("호관")

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(row)

    assert set(grouped) == set(truth), (
        f"호관 집합이 다르다 (기대 {sorted(truth)}, 실제 {sorted(grouped)}). "
        f"조인 조건이 틀리면 매칭이 사라진다{report}")
    assert all(len(v) == 1 for v in grouped.values()), (
        f"호관별로 한 행이어야 한다{report}")

    # 건물 쪽 값과 호실 합산이 '나란히' 맞아야 하므로 정답 컬럼이 둘 이상이어야 한다.
    # 팬아웃이 나면 건물 쪽만 호실_수 배로 부풀어 하나만 남는다.
    matched = [c for i, c in enumerate(columns)
               if i != key and all(_as_int(rs[0][i]) == truth[b]
                                   for b, rs in grouped.items())]
    assert len(matched) >= 2, (
        f"두 값이 모두 정답이어야 하는데 재현한 컬럼은 {matched}뿐이다. "
        f"기대값 {dict(sorted(truth.items()))}{report}")
