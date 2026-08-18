from datetime import datetime

import streamlit as st
import db_builder

# SQL 파싱은 테스트 가능하도록 db_builder에 두고 여기서는 재노출만 한다.
extract_target_table = db_builder.extract_target_table


@st.cache_resource
def load_engine():
    return db_builder.get_engine()


@st.cache_data(ttl=60, show_spinner=False)
def list_tables_cached(_engine) -> list[str]:
    """테이블 + 뷰 목록. 매 rerun마다 DB를 때리지 않도록 캐싱하며,
    목록이 바뀌면 invalidate_tables()로 비운다."""
    return db_builder.list_tables(_engine)


@st.cache_data(ttl=60, show_spinner=False)
def list_views_cached(_engine) -> list[str]:
    """뷰 목록만. 사이드바에서 테이블과 뷰를 갈라 보여줄 때 쓴다."""
    return db_builder.list_views(_engine)


def split_tables_views(engine) -> tuple[list[str], list[str]]:
    """(순수 테이블, 뷰). list_tables_cached는 둘을 합쳐 돌려주므로 여기서 가른다."""
    views = list_views_cached(engine)
    view_set = set(views)
    return [t for t in list_tables_cached(engine) if t not in view_set], list(views)


def invalidate_tables() -> None:
    """적재·DDL 실행 후 테이블/뷰 목록 캐시를 비운다."""
    list_tables_cached.clear()
    list_views_cached.clear()


def if_exists_selector(engine, table_name: str, key: str | None = None) -> str:
    """테이블이 이미 있으면 처리 방식 선택을 띄우고 선택값을, 없으면 'fail'을 돌려준다."""
    if not (table_name or "").strip():
        return "fail"
    if table_name not in list_tables_cached(engine):
        return "fail"
    st.warning(f"`{table_name}` 테이블이 이미 존재합니다.")
    return st.radio("처리 방식", ["fail", "replace", "append"],
                    captions=["중단", "덮어쓰기", "이어붙이기"],
                    horizontal=True, key=key)


def warn_if_not_editable(engine, table: str) -> None:
    """적재 후 기본키가 없으면 알린다.

    자동 부여가 실패했거나 무(無)키 테이블에 append한 경우로, 인라인 편집이 막힌다."""
    if db_builder.has_primary_key(engine, table):
        return
    st.warning(f"`{table}` 테이블에 기본키가 없어 인라인 편집이 지원되지 않습니다. (조회는 가능)")
    try:
        add_pk_sql = db_builder.build_add_pk_sql(table)
    except db_builder.DbBuilderError:
        return
    st.caption("편집이 필요하면 NL 콘솔의 'SQL 직접 입력'에 아래를 실행하세요.")
    st.code(add_pk_sql, language="sql")


def auto_select(engine, sql: str) -> None:
    table = extract_target_table(sql)
    if not table:
        return

    try:
        df = db_builder.run_select(engine, f"SELECT * FROM `{table}`", limit=50)
        st.markdown(f"#### 📋 `{table}` 현재 상태 (최대 50행)")
        st.dataframe(df, use_container_width=True)
        st.caption(f"{len(df)}행 조회됨")
    except db_builder.DbBuilderError as e:
        st.warning(f"자동 조회 실패: {e}")


HISTORY_MAX = 20


def push_history(history: list[dict], sql: str,
                 question: str | None = None,
                 cap: int = HISTORY_MAX) -> list[dict]:
    """이력 맨 앞에 항목을 넣은 새 리스트를 돌려준다 (원본은 건드리지 않는다).

    같은 SQL이 이미 있으면 옛 자리에서 빼고 앞으로 올린다 — 같은 쿼리를 반복해서
    돌리는 일이 흔해서, 중복을 그대로 쌓으면 이력이 금세 한 쿼리로 채워진다.

    이력은 reset_nl_state()의 삭제 목록에 없다. '다음 작업 실행'으로 화면을 비워도
    남아야 의미가 있다."""
    sql = (sql or "").strip()
    if not sql:
        return list(history)

    entry = {
        "sql":      sql,
        "question": (question or "").strip() or None,
        "ts":       datetime.now().strftime("%H:%M:%S"),
    }
    kept = [h for h in history if h.get("sql") != sql]
    return [entry, *kept][:cap]


def reset_nl_state() -> None:
    for k in ("nl_sql", "nl_df", "nl_df_orig", "nl_kind", "nl_pending_commit",
              "nl_target_table", "nl_pk_values", "nl_update_sqls",
              "nl_update_pending", "nl_edit_gen", "nl_sql_gen", "nl_save_as",
              "nl_done", "nl_ddl_preview", "nl_post_update_target",
              "nl_saved_table", "nl_truncated"):
        st.session_state.pop(k, None)


def reset_pdf_state() -> None:
    for k in ("pdf_tables", "pdf_md", "pdf_step", "pdf_table_idx",
              "pdf_col_types", "pdf_table_name", "pending_load", "pdf_merge_mode"):
        st.session_state.pop(k, None)


def reset_all() -> None:
    reset_nl_state()
    reset_pdf_state()
    st.session_state.pop("quick_view_table", None)