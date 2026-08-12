import streamlit as st
import db_builder

# SQL 파싱은 테스트 가능하도록 db_builder에 두고 여기서는 재노출만 한다.
extract_target_table = db_builder.extract_target_table


@st.cache_resource
def load_engine():
    return db_builder.get_engine()


@st.cache_data(ttl=60, show_spinner=False)
def list_tables_cached(_engine) -> list[str]:
    """테이블 목록. 매 rerun마다 DB를 때리지 않도록 캐싱하며,
    목록이 바뀌면 invalidate_tables()로 비운다."""
    return db_builder.list_tables(_engine)


def invalidate_tables() -> None:
    """적재·DDL 실행 후 테이블 목록 캐시를 비운다."""
    list_tables_cached.clear()


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


def reset_nl_state() -> None:
    for k in ("nl_sql", "nl_df", "nl_df_orig", "nl_kind", "nl_pending_commit",
              "nl_target_table", "nl_pk_values", "nl_update_sqls",
              "nl_update_pending", "nl_edit_gen", "nl_sql_gen", "nl_save_as",
              "nl_done", "nl_ddl_preview", "nl_post_update_target",
              "nl_saved_table"):
        st.session_state.pop(k, None)


def reset_pdf_state() -> None:
    for k in ("pdf_tables", "pdf_md", "pdf_step", "pdf_table_idx",
              "pdf_col_types", "pdf_table_name", "pending_load", "pdf_merge_mode"):
        st.session_state.pop(k, None)


def reset_all() -> None:
    reset_nl_state()
    reset_pdf_state()
    st.session_state.pop("quick_view_table", None)