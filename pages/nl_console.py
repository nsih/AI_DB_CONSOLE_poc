import streamlit as st
import re
import db_builder
from utils import (load_engine, auto_select, extract_target_table,
                   reset_nl_state, reset_all, if_exists_selector,
                   invalidate_tables, warn_if_not_editable)

engine = load_engine()

AI_WORKER_IP   = st.secrets["AI_WORKER_IP"]
AI_WORKER_PORT = st.secrets.get("AI_WORKER_PORT", 1234)
AI_MODEL_NAME  = st.secrets.get("AI_MODEL_NAME", "")
AI_ENDPOINT    = f"http://{AI_WORKER_IP}:{AI_WORKER_PORT}/v1/chat/completions"

st.title("NL 2 SQL Console")
st.caption("자연어로 질의하면 쿼리를 생성합니다. **생성된 쿼리를 확인 후 실행해주세요.**")


def _reset_for_new_sql() -> None:
    """이전 조회 결과·확인 게이트 상태를 전부 비운다."""
    gen = st.session_state.get("nl_sql_gen", 0) + 1
    reset_nl_state()
    st.session_state["nl_sql_gen"] = gen


def _start_new_sql(sql: str) -> None:
    """확보된 SQL을 편집·실행 단계로 넘긴다."""
    st.session_state["nl_sql"]  = sql
    st.session_state["nl_kind"] = db_builder.classify_sql(sql)


MODE_NL     = "자연어 질의"
MODE_DIRECT = "SQL 직접 입력"

SAVE_TABLE   = "새 테이블"
SAVE_VIEW    = "뷰"
SELECT_LIMIT = 20000

# 입력 방식 선택은 폼 밖에 둔다 — 폼 안 위젯은 제출 전까지 rerun하지 않아
# 안에 넣으면 방식을 바꿔도 입력창이 그대로 남는다.
mode = st.selectbox("입력 방식", [MODE_NL, MODE_DIRECT])

with st.form("nl_form"):
    if mode == MODE_NL:
        user_input = st.text_area(
            MODE_NL, height=80,
            placeholder="예) '~'테이블에서 '~'가 '~'인 테이터 전부 삭제해줘")
    else:
        # LLM만 건너뛰고 가드·확인 게이트는 자연어 경로와 동일하다.
        user_input = st.text_area(
            MODE_DIRECT, height=80,
            placeholder="예) ALTER TABLE `table_name` ADD COLUMN ...")
    submitted = st.form_submit_button(
        "SQL 생성" if mode == MODE_NL else "이 SQL 사용", type="primary")

if submitted and user_input.strip():
    _reset_for_new_sql()
    if mode == MODE_DIRECT:
        _start_new_sql(user_input.strip())
    else:
        with st.spinner("스키마 로딩 및 SQL 생성 중..."):
            try:
                schema_prompt = db_builder.get_schema_prompt(engine)
                sql = db_builder.generate_sql(
                    user_question=user_input,
                    schema_prompt=schema_prompt,
                    model_name=AI_MODEL_NAME,
                    endpoint=AI_ENDPOINT,
                    # 실행 불가능한 SQL은 사용자에게 보이기 전에 오류를 되먹여 다시 생성한다.
                    # 검증을 못 한 경우(report_skip)는 SQL 잘못이 아니므로 되먹이지 않는다.
                    validate=lambda s: db_builder.check_sql(engine, s, report_skip=False),
                )
                _start_new_sql(sql)
            except db_builder.DbBuilderError as e:
                st.error(f"SQL 생성 실패: {e}")

# 완료 화면 — 결과 표시 후 여기서 종료
if st.session_state.get("nl_done"):
    rc = st.session_state.get("nl_last_rowcount")
    st.success(f"작업 완료 (영향 행: {rc}행)" if rc is not None and rc >= 0 else "작업 완료")

    target = st.session_state.get("nl_post_update_target")
    if target:
        try:
            df_after = db_builder.run_select(
                engine, f"SELECT * FROM `{target}`", limit=50
            )
            st.markdown(f"#### `{target}` 현재 상태 상위 50행")
            st.dataframe(df_after, use_container_width=True)
            st.caption(f"{len(df_after)}행 조회됨")
        except db_builder.DbBuilderError as e:
            st.warning(f"자동 조회 실패: {e}")

    if st.button("다음 작업 실행", type="primary"):
        reset_all()
        st.rerun()
    st.stop()

# 생성 SQL 표시 + 실행
if "nl_sql" not in st.session_state:
    st.stop()

sql  = st.session_state["nl_sql"]
kind = st.session_state["nl_kind"]
gen  = st.session_state.get("nl_sql_gen", 0)

st.markdown("#### 생성된 SQL")
edited_sql = st.text_area("SQL (직접 수정 가능)", value=sql, height=200,
                          key=f"nl_sql_editor_{gen}")

# 수정된 SQL 세션 반영
st.session_state["nl_sql"]  = edited_sql
st.session_state["nl_kind"] = db_builder.classify_sql(edited_sql)
kind = st.session_state["nl_kind"]

st.caption(f"구문 분류: **{kind.upper()}**")

# 실행 전 검증 — 손으로 고친 SQL도 매 rerun마다 다시 본다.
for problem in db_builder.check_sql(engine, edited_sql):
    st.warning(problem)

st.markdown("---")

# SELECT 경로
if kind == "select":
    saved = st.session_state.pop("nl_saved_table", None)
    if saved:
        if saved["kind"] == SAVE_VIEW:
            st.success(f"`{saved['name']}` 뷰 생성 완료 — 조회할 때마다 최신 결과가 나옵니다.")
            st.caption("뷰에는 기본키가 없어 인라인 편집은 지원되지 않습니다. (조회는 가능)")
        else:
            st.success(f"`{saved['name']}` 테이블에 {saved['rows']}행 저장 완료")
            warn_if_not_editable(engine, saved["name"])

    if st.button("▶ 조회 실행", type="primary"):
        for k in ("nl_df", "nl_df_orig", "nl_target_table", "nl_pk_values",
                "nl_update_sqls", "nl_update_pending", "nl_save_as",
                "nl_truncated"):
            st.session_state.pop(k, None)
        st.session_state["nl_edit_gen"] = st.session_state.get("nl_edit_gen", 0) + 1
        try:
            sql_body = edited_sql.rstrip().rstrip(";")

            # 편집 조건: 단일 테이블 단순 조회 + 기본키 확보 (행 1:1 대응)
            target  = (db_builder.extract_select_table(sql_body)
                       if db_builder.is_single_table_select(sql_body) else None)
            pk_cols = db_builder.get_primary_key_columns(engine, target) if target else []

            df = None
            if target and pk_cols:
                try:
                    # INVISIBLE 기본키는 명시해야 조회된다
                    keyed = db_builder.inject_key_columns(sql_body, pk_cols)
                    full  = db_builder.run_select(engine, keyed, limit=SELECT_LIMIT)
                    full  = full.loc[:, ~full.columns.duplicated()]
                    if all(c in full.columns for c in pk_cols):
                        st.session_state["nl_pk_values"]   = full[pk_cols]
                        st.session_state["nl_target_table"] = target
                        df = full.drop(columns=pk_cols)   # 키는 화면에 노출하지 않음
                except db_builder.DbBuilderError:
                    df = None   # 키 확보 실패 → 읽기 전용으로 폴백

            if df is None:
                df = db_builder.run_select(engine, edited_sql, limit=SELECT_LIMIT)
                st.session_state["nl_target_table"] = None

            # attrs는 하위 연산에서 유실될 수 있으니 조회 직후에 뽑아 둔다.
            st.session_state["nl_truncated"] = bool(df.attrs.get("truncated"))
            st.session_state["nl_df"]      = df
            st.session_state["nl_df_orig"] = df.copy()
        except db_builder.DbBuilderError as e:
            st.error(f"조회 실패: {e}")

    if "nl_df" not in st.session_state:
        st.stop()

    df_orig      = st.session_state["nl_df_orig"]
    target_table = st.session_state.get("nl_target_table")
    edit_gen     = st.session_state.get("nl_edit_gen", 0)

    st.success(f"{len(df_orig)}행 조회됨")
    if st.session_state.get("nl_truncated"):
        st.warning(
            f"결과가 상한 {SELECT_LIMIT:,}행에서 잘렸습니다. 뒤쪽 행은 화면에도, "
            "표 내보내기에도 포함되지 않습니다. 전체가 필요하면 조건을 좁히거나 "
            "SQL에 직접 LIMIT / OFFSET을 지정하세요.")

    if target_table:
        st.caption("수정하려면 셀을 수정하고 **변경 반영** 버튼을 누르세요.")
        edited_df = st.data_editor(
            df_orig, use_container_width=True,
            key=f"nl_editor_{edit_gen}"
        )
    else:
        st.caption("조인/집계 결과이거나 기본키가 없어 편집이 지원되지 않습니다. "
                   "(기본키가 없는 테이블은 수정할 행을 특정할 수 없습니다) — "
                   "**저장**으로 새 테이블에 복사하면 기본키가 붙어 편집할 수 있고, "
                   "뷰로 남기면 원본을 따라 계속 갱신됩니다.")
        edited_df = df_orig
        st.dataframe(df_orig, use_container_width=True)

    # 저장은 편집 가능 여부와 무관하다 — 오히려 조인·집계 결과처럼 편집이 막힌
    # 쪽이 따로 남길 값이 크다. '변경 반영'만 편집 가능할 때 붙는다.
    btn_cols = st.columns(2 if target_table else 1)
    if target_table:
        with btn_cols[0]:
            if st.button("변경 반영", use_container_width=True):
                st.session_state.pop("nl_save_as", None)
                try:
                    update_sqls = db_builder.build_update_sqls(
                        df_orig, edited_df, target_table,
                        st.session_state.get("nl_pk_values"),
                    )
                    if not update_sqls:
                        st.info("변경된 셀이 없습니다.")
                    else:
                        st.session_state["nl_update_sqls"] = update_sqls
                        st.rerun()
                except db_builder.DbBuilderError as e:
                    st.error(f"변경 감지 실패: {e}")
    with btn_cols[-1]:
        if st.button("저장", use_container_width=True):
            st.session_state.pop("nl_update_sqls", None)
            st.session_state["nl_save_as"] = True
            st.rerun()

    # UPDATE 승인 게이트
    if "nl_update_sqls" in st.session_state:
        update_sqls = st.session_state["nl_update_sqls"]
        st.markdown(f"#### 변경 {len(update_sqls)}건 — 생성된 UPDATE SQL")
        for item in update_sqls:
            if item["warning"]:
                st.warning(f"{item['warning']}")
            st.code(item["sql"], language="sql")

        st.caption("전체 변경이 단일 트랜잭션으로 실행됩니다. 하나라도 실패하면 전체 롤백됩니다.")
        st.markdown("---")
        if "nl_update_pending" not in st.session_state:
            if st.button("전체 실행 확정", type="primary"):
                st.session_state["nl_update_pending"] = True
                st.rerun()
        else:
            st.error("정말 실행하시겠습니까? 되돌릴 수 없습니다.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("예, 실행", type="primary", use_container_width=True):
                    try:
                        result = db_builder.run_write_batch(engine, update_sqls)
                        st.session_state.pop("nl_update_sqls", None)
                        st.session_state.pop("nl_update_pending", None)
                        st.session_state["nl_done"] = True
                        st.session_state["nl_post_update_target"] = target_table
                        st.session_state["nl_last_rowcount"] = result["rowcount"]
                        st.rerun()
                    except db_builder.DbBuilderError as e:
                        st.error(f"실행 실패 (전체 롤백됨): {e}")
                        st.session_state.pop("nl_update_pending", None)
            with c2:
                if st.button("취소", use_container_width=True):
                    st.session_state.pop("nl_update_sqls", None)
                    st.session_state.pop("nl_update_pending", None)
                    st.rerun()

    # 저장 게이트
    if st.session_state.get("nl_save_as"):
        st.markdown("#### 저장")
        save_kind = st.radio(
            "저장 형태", [SAVE_TABLE, SAVE_VIEW],
            captions=[
                f"지금 화면의 데이터를 그대로 복사합니다 (최대 {SELECT_LIMIT:,}행). "
                "기본키가 자동으로 붙어 저장 후에는 편집할 수 있습니다.",
                "데이터가 아니라 쿼리를 저장합니다. 원본이 바뀌면 결과도 따라 "
                "바뀌지만, 기본키가 없어 편집은 계속 막힙니다.",
            ],
            horizontal=True, key="nl_save_as_kind")

        new_name = st.text_input(
            "새 테이블명" if save_kind == SAVE_TABLE else "새 뷰 이름",
            placeholder="예) ip_table_backup" if save_kind == SAVE_TABLE
                        else "예) v_호관별_단말기",
            key="nl_save_as_name"
        )

        if save_kind == SAVE_TABLE:
            if_exists  = if_exists_selector(engine, new_name,
                                            key="nl_save_as_ifexists")
            or_replace = False
        else:
            if_exists  = None
            or_replace = st.checkbox(
                "같은 이름의 뷰가 있으면 교체 (CREATE OR REPLACE VIEW)",
                key="nl_save_as_replace")
            st.caption("저장되는 쿼리는 아래 편집창의 SQL 그대로입니다.")

        s1, s2 = st.columns(2)
        with s1:
            if st.button("저장 실행", type="primary",
                         disabled=not (new_name or "").strip(),
                         use_container_width=True):
                try:
                    if save_kind == SAVE_TABLE:
                        cnt = db_builder.load_dataframe(
                            engine, edited_df, new_name, if_exists=if_exists)
                        saved = {"name": new_name, "rows": cnt, "kind": SAVE_TABLE}
                    else:
                        db_builder.create_view(engine, new_name, edited_sql,
                                               or_replace=or_replace)
                        saved = {"name": new_name, "rows": None, "kind": SAVE_VIEW}
                    invalidate_tables()
                    # rerun으로 화면이 지워지므로 결과는 세션에 넘긴다
                    st.session_state["nl_saved_table"] = saved
                    st.session_state.pop("nl_save_as", None)
                    st.rerun()
                except db_builder.DbBuilderError as e:
                    st.error(f"저장 실패: {e}")
        with s2:
            if st.button("취소", use_container_width=True, key="nl_save_as_cancel"):
                st.session_state.pop("nl_save_as", None)
                st.rerun()

# DDL / DML 경로
elif kind in ("ddl", "dml"):
    st.warning("쓰기 작업입니다. SQL을 꼼꼼히 확인하세요.")

    if kind == "ddl":
        if re.search(
            r'\bDROP\s+TABLE\b', edited_sql, re.IGNORECASE
        ):
            st.error("테이블 전체 삭제입니다. 테이블과 데이터가 영구 삭제됩니다.")

    col1, col2 = st.columns(2)

    with col1:
        if kind == "dml":
            if st.button("미리보기 (rollback)", use_container_width=True):
                try:
                    result = db_builder.run_write(engine, edited_sql, commit=False)
                    msg = result.get("message", "")
                    if msg:
                        st.info(msg)
                    else:
                        st.info(f"예상 영향 행 수: {result['rowcount']}행 (미커밋)")
                    auto_select(engine, edited_sql)
                except db_builder.DbBuilderError as e:
                    st.error(f"미리보기 실패: {e}")
        else:
            if st.button("사전 검사", use_container_width=True):
                try:
                    preview = db_builder.preview_ddl(engine, edited_sql)
                    st.session_state["nl_ddl_preview"] = preview
                except db_builder.DbBuilderError as e:
                    st.error(f"검사 실패: {e}")

            if "nl_ddl_preview" in st.session_state:
                preview = st.session_state["nl_ddl_preview"]
                st.caption(f"구문 유형: {preview['type']}")
                if preview.get("table"):
                    st.caption(f"대상 테이블: {preview['table']}")
                for f in preview.get("findings", []):
                    if f["level"] == "error":
                        st.error(f["msg"])
                    elif f["level"] == "warning":
                        st.warning(f["msg"])
                    else:
                        st.caption(f["msg"])

    with col2:
        if "nl_pending_commit" not in st.session_state:
            if st.button("실행 확정", type="primary", use_container_width=True):
                st.session_state["nl_pending_commit"] = True
                st.session_state.pop("nl_ddl_preview", None)
                st.rerun()
        else:
            st.error("정말 실행하시겠습니까? 되돌릴 수 없습니다.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("예, 실행", type="primary", use_container_width=True):
                    try:
                        result = db_builder.run_write(engine, edited_sql, commit=True)
                        if kind == "ddl":
                            invalidate_tables()   # CREATE/DROP으로 목록이 바뀜
                        st.session_state.pop("nl_pending_commit", None)
                        st.session_state["nl_done"] = True
                        st.session_state["nl_post_update_target"] = \
                            extract_target_table(edited_sql)
                        st.session_state["nl_last_rowcount"] = result["rowcount"]
                        st.rerun()
                    except db_builder.DbBuilderError as e:
                        st.error(f"실행 실패: {e}")
                        st.session_state.pop("nl_pending_commit", None)
            with c2:
                if st.button("취소", use_container_width=True):
                    st.session_state.pop("nl_pending_commit", None)
                    st.session_state.pop("nl_ddl_preview", None)
                    st.rerun()

else:
    st.error("분류할 수 없는 SQL입니다. 직접 수정 후 재시도하세요.")