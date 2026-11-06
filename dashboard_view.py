"""Step 9: Overview, agent analysis and experiment comparison for OASIS-Vis.

This module only reads already-loaded OASIS simulation data. It never calls an LLM.
Designed for app_step9.py and replay_view.py shipped together in the ZIP.
"""
from __future__ import annotations

from pathlib import Path
import math
import pandas as pd
import streamlit as st

from experiment_paths import DB_NAME, experiment_dir, list_experiments
from replay_view import (
    _events, _graph_figure, _replay_state, load_metadata, ACTION_LABELS,
)


def uid(value) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        f = float(value)
        if math.isfinite(f) and f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return str(value)


def name_map(users: pd.DataFrame) -> dict[str, str]:
    if users.empty or "user_id" not in users:
        return {}
    return {uid(r["user_id"]): str(r.get("name") or r.get("user_name") or r["user_id"])
            for _, r in users.iterrows()}


def comment_edges(posts: pd.DataFrame, comments: pd.DataFrame) -> pd.DataFrame:
    """Each cross-user comment is mapped to the author of its linked post."""
    required_post = {"post_id", "user_id"}
    required_comment = {"comment_id", "post_id", "user_id"}
    columns = ["comment_id", "commenter", "post_author", "post_id", "created_at"]
    if posts.empty or comments.empty or not required_post.issubset(posts.columns) or not required_comment.issubset(comments.columns):
        return pd.DataFrame(columns=columns)
    targets = posts[["post_id", "user_id"]].copy()
    targets["post_id_key"] = targets["post_id"].map(uid)
    targets["post_author"] = targets["user_id"].map(uid)
    targets = targets[["post_id_key", "post_author"]].drop_duplicates("post_id_key")
    data = comments.copy()
    data["post_id_key"] = data["post_id"].map(uid)
    data["commenter"] = data["user_id"].map(uid)
    data = data.merge(targets, on="post_id_key", how="left")
    data = data[data["post_author"].notna() & (data["commenter"] != data["post_author"])].copy()
    if "created_at" not in data:
        data["created_at"] = ""
    return data[["comment_id", "commenter", "post_author", "post_id", "created_at"]]


def _action_stats(traces: pd.DataFrame) -> pd.DataFrame:
    events = _events(traces)
    if events.empty:
        return pd.DataFrame(columns=["事件", "帖子", "评论", "关注动作"])
    post = comment = follow = 0
    rows = []
    for _, r in events.iterrows():
        action = r["action"]
        post += int(action == "create_post")
        comment += int(action == "create_comment")
        follow += int(action == "follow")
        rows.append({"事件": int(r["event_no"]), "帖子": post, "评论": comment, "关注动作": follow})
    return pd.DataFrame(rows)


def render_dashboard(users, posts, comments, follows, traces, database_path: Path) -> None:
    """A compact, visible-at-a-glance research demo overview."""
    st.subheader("模拟实验总览")
    events = _events(traces)
    metadata, warning = load_metadata(database_path, len(events))
    if metadata:
        st.caption(
            f"模型：{metadata.get('model','未知')} ｜ 平台：{metadata.get('platform','未知')}"
            f" ｜ Agent：{metadata.get('agents_requested','?')}"
            f" ｜ LLM 决策轮：{metadata.get('llm_rounds','?')}"
            f" ｜ 预设初始关注：{'是' if metadata.get('seed_network') else '否'}"
        )
    else:
        st.caption("当前实验没有可用的阶段配置，仍可分析 SQLite 中的实际记录。")
    st.caption("本页统计为**实验终态**；动态过程请打开「动态实验回放」。")
    edges = comment_edges(posts, comments)
    cross_participants = edges["commenter"].nunique() if not edges.empty else 0
    commenters = comments["user_id"].nunique() if not comments.empty and "user_id" in comments else 0
    x1, x2, x3, x4 = st.columns(4)
    x1.metric("跨用户评论", len(edges))
    x2.metric("发表评论的 Agent", int(commenters))
    x3.metric("参与跨用户评论的 Agent", int(cross_participants))
    x4.metric("记录的关注关系", len(follows))
    st.divider()
    left, right = st.columns([1.65, 1], gap="large")
    with left:
        st.markdown("**社交关系总览（关注 + 评论互动）**")
        if events.empty or users.empty:
            st.info("当前实验没有足够的数据生成互动网络。")
        else:
            state = _replay_state(events, len(events), posts, comments, follows, metadata)
            fig = _graph_figure(users, state, events, posts, comments, follows, "全部关系", metadata)
            if fig is not None:
                fig.update_layout(height=440)
                st.plotly_chart(fig, use_container_width=True)
            if state["unknown_follows"] or state["missing_final"]:
                st.warning("部分关注关系无法仅靠 trace 完整恢复，详情请查看「动态实验回放」中的提示。")
        st.caption("橙色：人工预设关注；蓝色：后续关注；绿色：跨用户评论。来源无法确定时应查看阶段记录。")
    with right:
        st.markdown("**最新 Agent 动作**")
        if events.empty:
            st.info("当前没有行为日志。")
        else:
            names = name_map(users)
            recent = events.tail(10).iloc[::-1].copy()
            recent["Agent"] = recent["user_id"].map(lambda v: names.get(uid(v), f"Agent {uid(v)}"))
            recent["动作"] = recent["action"].map(lambda x: ACTION_LABELS.get(x, x))
            st.dataframe(recent[["event_no", "Agent", "动作"]].rename(columns={"event_no": "事件"}),
                         hide_index=True, use_container_width=True, height=440)
    st.divider()
    st.markdown("**累计社交行为变化**")
    trend = _action_stats(traces)
    if not trend.empty:
        st.line_chart(trend.set_index("事件")[["帖子", "评论", "关注动作"]])
        st.caption("横轴是 trace 事件序号；「关注动作」是累计 follow 操作数，不等于当前有效关注边数。")
    else:
        st.info("当前没有行为变化数据。")
    if not posts.empty and "post_id" in posts and "user_id" in posts:
        st.markdown("**讨论参与情况（按帖子）**")
        names = name_map(users)
        counts = (comments["post_id"].map(uid).value_counts().to_dict()
                  if not comments.empty and "post_id" in comments else {})
        rows = []
        for _, p in posts.iterrows():
            pid = uid(p["post_id"])
            rows.append({"帖子": pid, "作者": names.get(uid(p["user_id"]), uid(p["user_id"])),
                         "评论数": int(counts.get(pid, 0)),
                         "内容摘要": str(p.get("content") or "")[:90]})
        st.dataframe(pd.DataFrame(rows).sort_values("评论数", ascending=False),
                     hide_index=True, use_container_width=True)
    if metadata.get("stages"):
        with st.expander("实验阶段及新增行为记录（研究核验）"):
            frames = [{"阶段": s.get("phase", ""),
                       "开始事件数": s.get("trace_count_before", ""),
                       "结束事件数": s.get("trace_count_after", ""),
                       "新增行为记录": s.get("new_trace_rows", "")}
                      for s in metadata["stages"]]
            st.dataframe(pd.DataFrame(frames), hide_index=True, use_container_width=True)
    elif warning:
        st.caption(warning)


def render_agent_analytics(users, posts, comments, follows, traces) -> None:
    """Agent overview and both directions of cross-user interaction."""
    st.subheader("Agent 行为画像")
    if users.empty or "user_id" not in users:
        st.info("user 表没有有效 Agent。")
        return
    names = name_map(users)
    user_ids = list(dict.fromkeys(users["user_id"].map(uid)))
    selected = st.selectbox("选择 Agent", user_ids, key="step9_agent_id",
                            format_func=lambda u: f"{names.get(u, 'Agent')} (ID: {u})")
    profile = users[users["user_id"].map(uid) == selected].iloc[0]
    bio = profile.get("bio") if "bio" in profile else ""
    st.markdown(f"**{names.get(selected, selected)}**")
    st.caption(f"User ID: {selected} ｜ 用户名：{profile.get('user_name', '未记录')}")
    if pd.notna(bio) and str(bio).strip():
        st.write(str(bio))
    post_ids = (posts["user_id"].map(uid) == selected) if "user_id" in posts else pd.Series(False, index=posts.index)
    comment_ids = (comments["user_id"].map(uid) == selected) if "user_id" in comments else pd.Series(False, index=comments.index)
    if {"follower_id", "followee_id"}.issubset(follows.columns):
        following = int((follows["follower_id"].map(uid) == selected).sum())
        followers = int((follows["followee_id"].map(uid) == selected).sum())
    else:
        following = followers = 0
    m1,m2,m3,m4 = st.columns(4)
    m1.metric("发帖数", int(post_ids.sum()))
    m2.metric("评论数", int(comment_ids.sum()))
    m3.metric("关注中", following)
    m4.metric("被关注", followers)
    edges = comment_edges(posts, comments)
    c1,c2 = st.columns(2)
    with c1:
        st.markdown("**该 Agent 评论过哪些其他用户**")
        outgoing = edges[edges["commenter"] == selected] if not edges.empty else pd.DataFrame()
        if outgoing.empty:
            st.info("没有记录到跨用户评论。")
        else:
            out = outgoing.groupby("post_author").size().reset_index(name="评论次数")
            out["对象"] = out["post_author"].map(lambda i: names.get(i, i))
            st.dataframe(out[["对象", "评论次数"]].sort_values("评论次数", ascending=False),
                         hide_index=True, use_container_width=True)
    with c2:
        st.markdown("**哪些其他用户评论过该 Agent 的帖子**")
        incoming = edges[edges["post_author"] == selected] if not edges.empty else pd.DataFrame()
        if incoming.empty:
            st.info("没有记录到其他用户评论其帖子。")
        else:
            inc = incoming.groupby("commenter").size().reset_index(name="评论次数")
            inc["对象"] = inc["commenter"].map(lambda i: names.get(i, i))
            st.dataframe(inc[["对象", "评论次数"]].sort_values("评论次数", ascending=False),
                         hide_index=True, use_container_width=True)
    st.markdown("**Agent 行为记录（最近 30 条）**")
    events = _events(traces)
    if events.empty:
        st.info("无行动记录。")
    else:
        actions = events[events["user_id"].map(uid) == selected].tail(30).iloc[::-1].copy()
        actions["动作"] = actions["action"].map(lambda x: ACTION_LABELS.get(x, x))
        if actions.empty:
            st.info("未找到该 Agent 的行为记录。")
        else:
            st.dataframe(actions[["event_no", "created_at", "动作"]].rename(
                columns={"event_no":"事件", "created_at":"记录时间"}),
                hide_index=True, use_container_width=True)
    with st.expander("该 Agent 的发帖和评论原文"):
        own_posts = posts.loc[post_ids].copy()
        own_comments = comments.loc[comment_ids].copy()
        st.markdown("**帖子**")
        if own_posts.empty:
            st.caption("无")
        else:
            st.dataframe(own_posts[[x for x in ("post_id", "created_at", "content") if x in own_posts]],
                         hide_index=True, use_container_width=True)
        st.markdown("**评论**")
        if own_comments.empty:
            st.caption("无")
        else:
            st.dataframe(own_comments[[x for x in ("comment_id", "post_id", "created_at", "content") if x in own_comments]],
                         hide_index=True, use_container_width=True)
    st.caption("这里描述的是可观测平台行为，不代表用户心理状态、情绪或真实信念。")


def _run_summary(db: dict[str, pd.DataFrame]) -> dict:
    users, posts, comments, follows, traces = (db[x] for x in ("user", "post", "comment", "follow", "trace"))
    n = len(users)
    return {"Agent 数": n, "帖子数": len(posts), "评论数": len(comments),
            "关注边数": len(follows), "行为数": len(traces),
            "每 Agent 评论": round(len(comments) / n, 2) if n else 0,
            "跨用户评论": len(comment_edges(posts, comments))}


def render_experiment_compare(current_path: Path, cached_read) -> None:
    """Descriptive two-run comparison; explicitly not a controlled causal study."""
    st.subheader("实验结果对比")
    options = [p for p in list_experiments() if (p / DB_NAME).resolve() != current_path.resolve()]
    if not options:
        st.info(f"experiments/ 下需要至少两场含 {DB_NAME} 的实验才能对比。")
        return
    compare_name = st.selectbox("选择要对比的另一场实验", [p.name for p in options], key="step9_compare_db")
    other = experiment_dir(compare_name) / DB_NAME
    stat = other.stat()
    try:
        other_db, _ = cached_read(str(other), stat.st_mtime_ns, stat.st_size)
    except Exception as exc:
        st.error(f"无法读取对比数据库：{exc}")
        return
    # Main app can change the selected DB; always read it by the same cache function.
    s = current_path.stat()
    current_db, _ = cached_read(str(current_path), s.st_mtime_ns, s.st_size)
    cur_row = _run_summary(current_db)
    other_row = _run_summary(other_db)
    chart = pd.DataFrame([{"实验": current_path.stem, **cur_row},
                          {"实验": other.stem, **other_row}])
    st.dataframe(chart, hide_index=True, use_container_width=True)
    a,b = st.columns(2)
    with a:
        st.markdown("**整体活动量**")
        st.bar_chart(chart.set_index("实验")[["帖子数", "评论数", "关注边数"]])
    with b:
        st.markdown("**按 Agent 数量归一化的评论数**")
        st.bar_chart(chart.set_index("实验")[["每 Agent 评论"]])
    st.info("这是两次不同规模/参数实验的**描述性比较**。不能直接把差异解释为 Agent 数量或初始网络的独立因果影响；若要做研究结论，需要控制其他变量并重复多次运行。")
