import streamlit as st
import db_builder
from utils import load_engine, split_tables_views

st.set_page_config(page_title="CSU DB Console", layout="wide")

try:
    engine = load_engine()
except db_builder.DbBuilderError as e:
    st.error(f"DB 연결 실패: {e}")
    st.stop()

def _close_preview() -> None:
    """미리보기를 닫고 드롭다운 선택도 비운다.

    반드시 on_click 콜백에서 해야 한다 — 스크립트 본문에서 위젯 키를 건드리면
    '위젯 생성 후 수정' 예외가 난다. 드롭다운을 비우지 않으면 같은 항목을 다시
    골라도 값이 안 바뀌어 on_change가 안 걸리고, 미리보기가 열리지 않는다."""
    st.session_state.pop("quick_view_table", None)
    st.session_state["sb_pick_table"] = None
    st.session_state["sb_pick_view"]  = None


def _pick_object(key: str) -> None:
    """드롭다운에서 고른 대상을 본문 미리보기로 넘긴다.

    on_change로 걸어야 '고른 순간'에만 반응한다. 매 rerun마다 값을 읽어
    넘기면 닫기 버튼을 눌러도 선택이 남아 있어 곧바로 다시 열린다."""
    picked = st.session_state.get(key)
    if picked:
        st.session_state["quick_view_table"] = picked


# 사이드바 — 테이블 / 뷰 목록
with st.sidebar:
    st.markdown("---")
    try:
        tables, views = split_tables_views(engine)
        st.session_state["sb_view_names"] = views   # 미리보기 제목에서 뷰/테이블 구분용

        st.selectbox("테이블 목록", tables, index=None,
                     placeholder="테이블 없음" if not tables else "테이블 선택",
                     disabled=not tables,
                     key="sb_pick_table",
                     on_change=_pick_object, args=("sb_pick_table",))

        st.selectbox("뷰 목록", views, index=None,
                     placeholder="뷰 없음" if not views else "뷰 선택",
                     disabled=not views,
                     key="sb_pick_view",
                     on_change=_pick_object, args=("sb_pick_view",))
    except Exception as e:
        st.caption(f"목록 조회 실패: {e}")


selected = st.session_state.get("quick_view_table")
if selected:
    kind_label = "View" if selected in st.session_state.get("sb_view_names", []) else "Table"
    with st.container(border=True):
        col1, col2 = st.columns([6, 1])
        with col1:
            st.markdown(f"#### `{selected}` \n {kind_label} preview (최대 100행)")
        with col2:
            st.button("닫기", use_container_width=True, key="close_quick_view",
                      on_click=_close_preview)
        try:
            df = db_builder.run_select(engine, f"SELECT * FROM `{selected}`", limit=100)
            st.dataframe(df, use_container_width=True)
            st.caption(f"{len(df)}행 조회됨")
        except db_builder.DbBuilderError as e:
            st.error(f"조회 실패: {e}")

    st.markdown("---")

nl_page   = st.Page("pages/nl_console.py", title="NL 2 SQL Console")
file_page = st.Page("pages/file_table.py", title="파일 → Table")

pg = st.navigation([nl_page, file_page])
pg.run()