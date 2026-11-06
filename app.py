"""OASIS-Vis: historical playback and live OASIS/Qwen control.

Use with live_control.py, live_worker.py, dashboard_view.py, replay_view.py.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from replay_view import render_dynamic_replay
from dashboard_view import render_dashboard, render_agent_analytics, render_experiment_compare
from live_control import render_live_console
from experiment_paths import DB_NAME, EXPERIMENTS_DIR, experiment_dir, list_experiments

st.set_page_config(page_title="OASIS 多智能体仿真可视化", page_icon="🌐", layout="wide")

ROOT = Path(__file__).resolve().parent
DEFAULT_EXPERIMENT = "reddit_simulation"
TABLE_NAMES = ("user", "post", "comment", "follow", "trace")
ACTIONS = {
    "sign_up": "注册账号",
    "create_post": "发布帖子",
    "create_comment": "发表评论",
    "like_post": "点赞帖子",
    "dislike_post": "点踩帖子",
    "like_comment": "点赞评论",
    "dislike_comment": "点踩评论",
    "follow": "关注用户",
    "unfollow": "取消关注",
    "repost": "转发帖子",
    "quote_post": "引用帖子",
    "search_posts": "搜索帖子",
    "search_user": "搜索用户",
    "refresh": "刷新信息流",
    "do_nothing": "不执行动作",
}


def read_database(db_path: Path) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Return official OASIS tables, absent tables become empty DataFrames."""
    db_path = db_path.resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到数据库：{db_path}")
    # mode=ro avoids mutating or accidentally creating the database.
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        out: dict[str, pd.DataFrame] = {}
        for table in TABLE_NAMES:
            # Table names are hardcoded/whitelisted, never user input.
            out[table] = (
                pd.read_sql_query('SELECT rowid AS _trace_rowid, * FROM "trace" ORDER BY rowid' if table == "trace" else f'SELECT * FROM "{table}"', conn)
                if table in existing
                else pd.DataFrame()
            )
        return out, sorted(existing)
    finally:
        conn.close()


@st.cache_data(show_spinner="正在读取 OASIS 数据库……")
def cached_read(path: str, modified_ns: int, size: int):
    return read_database(Path(path))


def experiment_brief(exp_dir: Path) -> str:
    """汇总实验目录里记录的配置来源；历史记录缺文件时返回空串。"""
    for name in ("experiment.json", "profile_snapshot.json"):
        path = exp_dir / name
        if not path.is_file():
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        bits = []
        if obj.get("profile_file"):
            bits.append(f"人设配置：{obj['profile_file']}")
        count = obj.get("agents_requested") or obj.get("agent_count")
        if count:
            bits.append(f"{count} 位 Agent")
        if obj.get("model"):
            bits.append(str(obj["model"]))
        return " · ".join(bits)
    return ""


def user_label(user_id, user_df: pd.DataFrame) -> str:
    if user_df.empty or "user_id" not in user_df.columns:
        return f"用户 {user_id}"
    matches = user_df[user_df["user_id"].astype(str) == str(user_id)]
    if matches.empty:
        return f"用户 {user_id}"
    row = matches.iloc[0]
    for key in ("name", "user_name", "agent_id"):
        if key in matches.columns and pd.notna(row[key]) and str(row[key]).strip():
            return f"{row[key]} (ID: {user_id})"
    return f"用户 {user_id}"


def display_df(df: pd.DataFrame, *, height: int = 300):
    if df.empty:
        st.info("该数据表目前没有记录。")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True, height=height)


def network_figure(users: pd.DataFrame, follows: pd.DataFrame):
    graph = nx.DiGraph()
    if "user_id" in users.columns:
        for user_id in users["user_id"].dropna():
            graph.add_node(str(user_id))
    if {"follower_id", "followee_id"}.issubset(follows.columns):
        for _, edge in follows.iterrows():
            if pd.notna(edge["follower_id"]) and pd.notna(edge["followee_id"]):
                graph.add_edge(str(edge["follower_id"]), str(edge["followee_id"]))
    if not graph.nodes:
        return None, graph
    pos = nx.spring_layout(graph, seed=42)
    edge_x, edge_y = [], []
    for src, dst in graph.edges:
        edge_x += [pos[src][0], pos[dst][0], None]
        edge_y += [pos[src][1], pos[dst][1], None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=1.6, color="rgba(130,145,166,0.48)"),
        hoverinfo="skip", showlegend=False,
    ))
    ids = list(graph.nodes)
    degrees = [graph.in_degree(u) for u in ids]
    labels = [user_label(u, users) for u in ids]
    hover = [
        f"{label}<br>关注者：{graph.in_degree(uid)}<br>关注中：{graph.out_degree(uid)}"
        for uid, label in zip(ids, labels)
    ]
    fig.add_trace(go.Scatter(
        x=[pos[u][0] for u in ids],
        y=[pos[u][1] for u in ids],
        mode="markers+text",
        text=[x.split(" (ID:")[0] for x in labels],
        textposition="top center",
        hovertext=hover,
        hoverinfo="text",
        marker=dict(
            size=[max(16, min(35, 17 + d * 2.5)) for d in degrees],
            color=degrees,
            colorscale="Blues",
            showscale=len(ids) > 4,
            colorbar=dict(title="关注者数"),
            line=dict(width=1, color="white"),
        ),
        showlegend=False,
    ))
    fig.update_layout(
        height=480, margin=dict(l=5, r=5, t=10, b=5),
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        dragmode="pan",
    )
    return fig, graph


def safe_json_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if not isinstance(value, str):
        return str(value)
    try:
        result = json.loads(value)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return value



def render_social_feed(users: pd.DataFrame, posts: pd.DataFrame, comments: pd.DataFrame) -> None:
    """在帖子下展示其关联评论，仅使用数据库真实记录，不生成虚构回复层级。"""
    st.subheader("Agent 社交内容流")
    st.caption("帖子与评论均来自当前 OASIS SQLite。评论按 post_id 关联，未记录评论父子关系时不推断谁回复谁。")

    if posts.empty:
        st.info("本次实验还没有帖子。")
        return
    if not {"post_id", "user_id", "content"}.issubset(posts.columns):
        st.warning("post 表缺少 post_id / user_id / content 字段，无法构建内容流。")
        return

    filter_col, search_col = st.columns([1, 2])
    author_ids = posts["user_id"].dropna().drop_duplicates().tolist()
    with filter_col:
        selected_author = st.selectbox(
            "筛选帖子作者",
            options=["全部"] + [str(uid) for uid in author_ids],
            format_func=lambda uid: "全部作者" if uid == "全部" else user_label(uid, users),
            key="feed_author_filter",
        )
    with search_col:
        keyword = st.text_input("搜索帖子内容", placeholder="输入关键词（留空显示全部）", key="feed_keyword").strip()

    view_posts = posts.copy()
    if selected_author != "全部":
        view_posts = view_posts[view_posts["user_id"].astype(str) == selected_author]
    if keyword:
        view_posts = view_posts[
            view_posts["content"].fillna("").astype(str).str.contains(keyword, case=False, regex=False)
        ]
    if "created_at" in view_posts.columns:
        view_posts = view_posts.sort_values("created_at", ascending=False, kind="stable")

    st.caption(f"当前匹配 {len(view_posts)} 篇帖子；原始数据库共 {len(posts)} 篇帖子、{len(comments)} 条评论。")
    if view_posts.empty:
        st.info("当前筛选条件下没有帖子。")
        return

    has_comment_links = not comments.empty and {"post_id", "user_id", "content"}.issubset(comments.columns)
    for _, post in view_posts.iterrows():
        post_id = post["post_id"]
        matching_comments = (
            comments[comments["post_id"].astype(str) == str(post_id)].copy()
            if has_comment_links else pd.DataFrame()
        )
        if not matching_comments.empty and "created_at" in matching_comments.columns:
            matching_comments = matching_comments.sort_values("created_at", kind="stable")

        with st.container(border=True):
            st.markdown(f"#### 帖子 #{post_id}")
            st.write(f"**作者：** {user_label(post['user_id'], users)}")
            if "created_at" in posts.columns:
                st.caption(f"发布时间：{post.get('created_at', '未知')}")
            content = post.get("content")
            st.write(str(content) if pd.notna(content) and str(content).strip() else "（正文为空）")

            badges = []
            for key, label in (("num_likes", "点赞"), ("num_shares", "转发")):
                if key in posts.columns and pd.notna(post.get(key)):
                    badges.append(f"{label}：{post[key]}")
            badges.append(f"关联评论：{len(matching_comments)}")
            st.caption(" · ".join(badges))
            st.divider()
            st.markdown(f"**评论区（{len(matching_comments)} 条）**")
            if matching_comments.empty:
                st.caption("当前帖子没有评论。")
            else:
                for _, comment in matching_comments.iterrows():
                    st.write(f"**{user_label(comment['user_id'], users)}**")
                    comment_content = comment.get("content")
                    st.write(str(comment_content) if pd.notna(comment_content) else "（空评论）")
                    if "created_at" in matching_comments.columns:
                        st.caption(f"评论时间：{comment.get('created_at', '未知')}")
                    if len(matching_comments) > 1:
                        st.markdown("---")


st.title("OASIS 多智能体社交网络仿真平台")
st.caption("历史实验回放 + 实时 Agent 模拟控制 · 使用本机 OASIS 引擎和内置演示 API 配置")

AVAILABLE_EXPERIMENTS = list_experiments()
if not AVAILABLE_EXPERIMENTS:
    st.error("experiments/ 下还没有任何实验。每场实验是一个文件夹，至少包含一个 experiment.db：")
    st.code(str(EXPERIMENTS_DIR / "实验名称" / DB_NAME))
    st.stop()

AVAILABLE_NAMES = [p.name for p in AVAILABLE_EXPERIMENTS]
with st.sidebar:
    chosen_name = st.selectbox(
        "选择实验",
        options=AVAILABLE_NAMES,
        index=(AVAILABLE_NAMES.index(DEFAULT_EXPERIMENT)
               if DEFAULT_EXPERIMENT in AVAILABLE_NAMES else 0),
    )
EXP_DIR = experiment_dir(chosen_name)
DB_PATH = EXP_DIR / DB_NAME
stat = DB_PATH.stat()
try:
    db, table_list = cached_read(str(DB_PATH), stat.st_mtime_ns, stat.st_size)
except Exception as ex:
    st.error("数据库读取失败，请确认 OASIS 已运行结束，并检查数据库是否完整。")
    st.exception(ex)
    st.stop()

users, posts = db["user"], db["post"]
comments, follows, traces = db["comment"], db["follow"], db["trace"]

with st.sidebar:
    st.header("实验数据")
    st.success("SQLite 已连接")
    st.caption(f"实验目录：{EXP_DIR.name}")
    brief = experiment_brief(EXP_DIR)
    if brief:
        st.caption(brief)
    st.caption(f"数据库：{DB_PATH.name}（{stat.st_size / 1024:.1f} KB）")
    st.caption(f"找到 {len(table_list)} 张数据表")
    st.caption("实时运行请切换到「实时模拟（Live）」选项卡；历史数据可以离线回放。")
    if st.button("重新读取数据库"):
        st.cache_data.clear()
        st.rerun()

st.caption("顶部指标是**整场实验最终总量**；「动态实验回放」选项卡内显示当前事件进度对应的累计指标。")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Agent 用户", len(users))
m2.metric("帖子", len(posts))
m3.metric("评论", len(comments))
m4.metric("关注关系", len(follows))
m5.metric("行为记录", len(traces))

summary, content_tab, event_tab, network_tab, details_tab, replay_tab, compare_tab, live_tab = st.tabs(
    ["实验概览", "帖子与评论", "行为时间轴", "社交关系图", "Agent 详情与数据检查", "动态实验回放", "实验对比", "实时模拟（Live）"]
)

with summary:
    render_dashboard(users, posts, comments, follows, traces, DB_PATH)

with content_tab:
    # Step 5：直接在本文件内渲染，无需额外创建 social_feed.py。
    render_social_feed(users, posts, comments)

    # 保留原始表格作为科研数据核对区，默认折叠。
    with st.expander("查看原始帖子与评论表（数据核对）", expanded=False):
        st.markdown("**原始帖子表**")
        if not posts.empty:
            show_posts = posts.copy()
            if "user_id" in show_posts.columns:
                show_posts.insert(0, "作者", show_posts["user_id"].map(lambda u: user_label(u, users)))
            display_df(show_posts, height=260)
        else:
            st.info("本次实验没有帖子。")
        st.markdown("**原始评论表**")
        if not comments.empty:
            show_comments = comments.copy()
            if "user_id" in show_comments.columns:
                show_comments.insert(0, "评论用户", show_comments["user_id"].map(lambda u: user_label(u, users)))
            display_df(show_comments, height=240)
        else:
            st.info("本次实验没有评论。")

with event_tab:
    st.subheader("按事件序号回放")
    st.caption("这里按 trace 记录顺序回放；事件序号不等于 OASIS 的 env.step 轮次。")
    if traces.empty:
        st.info("trace 表为空，暂时无法回放动作。")
    else:
        event_df = traces.copy()
        if "created_at" in event_df.columns:
            event_df = event_df.sort_values("created_at", kind="stable").reset_index(drop=True)
        else:
            event_df = event_df.reset_index(drop=True)
        event_df.insert(0, "事件序号", range(1, len(event_df) + 1))
        current = st.slider("显示前 N 条行为", min_value=0, max_value=len(event_df), value=len(event_df), step=1)
        current_events = event_df.head(current).copy()
        st.metric("当前已展示行为", len(current_events))
        if "action" in current_events.columns:
            current_events["动作说明"] = current_events["action"].astype(str).map(
                lambda a: ACTIONS.get(a.lower(), a)
            )
        display_df(current_events.iloc[::-1], height=340)
        if "action" in current_events.columns and not current_events.empty:
            counts = current_events["action"].astype(str).value_counts()
            st.bar_chart(pd.DataFrame({"动作": counts.index, "次数": counts.values}).set_index("动作"))
        if "info" in current_events.columns and not current_events.empty:
            st.markdown("**检查最新事件的参数**")
            st.code(safe_json_text(current_events.iloc[-1]["info"]), language="json")

with network_tab:
    st.subheader("用户关注关系图")
    st.caption("有向边 A → B 表示 A 关注 B；这里显示 follow 表中的最终状态，并非历史快照。")
    fig, graph = network_figure(users, follows)
    if fig is None:
        st.info("没有可绘制的用户节点。")
    else:
        st.plotly_chart(fig, use_container_width=True)
        st.caption(f"网络节点：{graph.number_of_nodes()} · 关注边：{graph.number_of_edges()}")
        if not graph.edges:
            st.warning("本次 Quick Start 未产生关注关系，所以只有节点、没有连线。这不是程序错误。")
        display_df(follows, height=180)

with details_tab:
    render_agent_analytics(users, posts, comments, follows, traces)

    st.divider()
    st.subheader("数据库结构检查")
    st.caption("如果页面数据和预期不符，可在此核对数据库的真实表名和字段。")
    st.write("全部数据表：", ", ".join(table_list) if table_list else "（无）")
    for table in TABLE_NAMES:
        with st.expander(f"{table} — {len(db[table])} 条"):
            st.code(", ".join(map(str, db[table].columns)) or "表不存在或无字段")
            display_df(db[table].head(15), height=220)


with replay_tab:
    render_dynamic_replay(users, posts, comments, follows, traces, DB_PATH)

with compare_tab:
    render_experiment_compare(DB_PATH, cached_read)


with live_tab:
    render_live_console()
