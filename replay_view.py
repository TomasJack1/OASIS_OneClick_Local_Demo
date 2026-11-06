"""Event-sourced, read-only replay of OASIS 0.2.x SQLite trace records.

Important: follow trace `info` normally contains `follow_id`, not followee_id.
Historic follows removed by unfollow may not be recoverable from the final
`follow` table. The UI reports incomplete reconstruction instead of guessing.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import math

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

COLORS = {"manual": "#D97706", "agent": "#2563EB", "unknown": "#6B7280", "comment": "#0F9D8A"}
ACTION_LABELS = {
    "sign_up": "注册账号", "create_post": "发布帖子", "create_comment": "发表评论",
    "follow": "关注", "unfollow": "取消关注", "refresh": "刷新信息流",
    "like_post": "点赞", "like_comment": "点赞评论", "do_nothing": "无操作",
    "dislike_post": "点踩", "search_posts": "搜索帖子", "search_user": "搜索用户",
}


def _id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def _payload(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        x = json.loads(value)
        return x if isinstance(x, dict) else {}
    except (ValueError, TypeError):
        return {}


def _index_table(frame: pd.DataFrame, id_field: str) -> dict:
    if frame.empty or id_field not in frame:
        return {}
    return {_id(row[id_field]): row.to_dict() for _, row in frame.iterrows()}


def _events(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["event_no", "user_id", "action", "info", "payload", "created_at"])
    e = frame.copy()
    if "_trace_rowid" in e:
        e = e.sort_values("_trace_rowid", kind="stable")
    e = e.reset_index(drop=True)
    e["event_no"] = range(1, len(e) + 1)
    for field in ("action", "user_id", "info", "created_at"):
        if field not in e:
            e[field] = None
    e["action"] = e["action"].fillna("").astype(str).str.lower()
    e["payload"] = e["info"].map(_payload)
    return e


def load_metadata(db_path: Path, count: int) -> tuple[dict, str]:
    path = db_path.with_suffix(".json")
    if not path.is_file():
        return {}, "未找到同名 .json 实验配置；仍可按事件顺序回放，但无法精确标注 LLM 轮次。"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return {}, "实验配置不是 JSON 对象。"
        stages = obj.get("stages", [])
        if not isinstance(stages, list):
            return {}, "实验配置中的 stages 格式不正确。"
        for s in stages:
            bef, aft = int(s["trace_count_before"]), int(s["trace_count_after"])
            if not (0 <= bef <= aft <= count):
                return {}, "实验配置中的 trace 行数与数据库不匹配，已禁用阶段跳转。"
        if stages and int(stages[-1]["trace_count_after"]) != count:
            return {}, "实验配置的最终 trace 数量与数据库不同，已禁用阶段跳转。"
        return obj, ""
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {}, f"读取阶段配置失败：{exc}"


def _stage_name(event_no: int, metadata: dict) -> str:
    stages = metadata.get("stages", [])
    if not stages:
        return "未记录阶段"
    if event_no <= int(stages[0]["trace_count_before"]):
        return "注册初始化"
    for s in stages:
        if int(s["trace_count_before"]) < event_no <= int(s["trace_count_after"]):
            return str(s.get("phase", "未命名阶段"))
    return "阶段未知"


def _replay_state(events: pd.DataFrame, cursor: int, posts: pd.DataFrame,
                  comments: pd.DataFrame, follows: pd.DataFrame, metadata: dict) -> dict:
    post_by_id = _index_table(posts, "post_id")
    comment_by_id = _index_table(comments, "comment_id")
    follow_by_id = _index_table(follows, "follow_id")
    all_final = {(_id(r["follower_id"]), _id(r["followee_id"]))
                 for _, r in follows.iterrows()} if not follows.empty else set()
    active = {}  # (src,dst) -> manual / agent / unknown
    interaction = Counter()
    exposed_posts: set[str] = set()
    exposed_comments: set[str] = set()
    unknown_follows = 0
    incomplete_comment = 0
    unknown_content = 0
    status_rows = []
    for _, row in events.iloc[:cursor].iterrows():
        action, info = str(row["action"]), row["payload"]
        source = _id(row["user_id"])
        stage = _stage_name(int(row["event_no"]), metadata)
        if action == "create_post":
            pid = _id(info.get("post_id"))
            if pid in post_by_id:
                exposed_posts.add(pid)
            else:
                unknown_content += 1
        elif action == "create_comment":
            cid = _id(info.get("comment_id"))
            if cid in comment_by_id:
                exposed_comments.add(cid)
                post_id = _id(comment_by_id[cid].get("post_id"))
            else:
                post_id = _id(info.get("post_id"))
                unknown_content += 1
            if post_id in post_by_id:
                target = _id(post_by_id[post_id].get("user_id"))
                if target and target != source:
                    interaction[(source, target)] += 1
            else:
                incomplete_comment += 1
        elif action == "follow":
            target = _id(info.get("followee_id"))
            fid = _id(info.get("follow_id"))
            if not target and fid in follow_by_id:
                target = _id(follow_by_id[fid].get("followee_id"))
            if target:
                provenance = "manual" if stage == "manual_seed_follows" else ("agent" if stage.startswith("llm_round_") else "unknown")
                active[(source, target)] = provenance
            else:
                unknown_follows += 1
        elif action == "unfollow":
            target = _id(info.get("followee_id"))
            if target:
                active.pop((source, target), None)
            else:
                unknown_follows += 1
        status_rows.append({
            "事件": int(row["event_no"]), "动作": ACTION_LABELS.get(action, action),
            "Agent ID": source, "阶段": stage,
            "时间": str(row["created_at"]), "动作详情": json.dumps(info, ensure_ascii=False),
        })
    if cursor == len(events):
        recovered = set(active)
        # Show discrepancy, never backfill at intermediate cursor from final state.
        missing_final = len(all_final - recovered)
    else:
        missing_final = 0
    return dict(active=active, interaction=interaction, exposed_posts=exposed_posts,
                exposed_comments=exposed_comments, unknown_follows=unknown_follows,
                incomplete_comment=incomplete_comment, unknown_content=unknown_content,
                missing_final=missing_final, status_rows=status_rows)


def _graph_figure(users: pd.DataFrame, state: dict, full_events: pd.DataFrame,
                  posts: pd.DataFrame, comments: pd.DataFrame, follows: pd.DataFrame,
                  mode: str, metadata: dict):
    ids = list(dict.fromkeys(_id(x) for x in users["user_id"].tolist())) if "user_id" in users else []
    if not ids:
        return None
    names = {_id(r["user_id"]): str(r.get("name") or r.get("user_name") or r["user_id"])
             for _, r in users.iterrows()}
    # Layout uses complete graph, and remains identical at all replay cursors.
    full = _replay_state(full_events, len(full_events), posts, comments, follows, metadata)
    layout_graph = nx.Graph()
    layout_graph.add_nodes_from(ids)
    layout_graph.add_edges_from(full["active"].keys())
    layout_graph.add_edges_from(full["interaction"].keys())
    # Fallback: seed layout with final known edges for older DBs that lack traces.
    if not follows.empty:
        layout_graph.add_edges_from(
            (_id(r["follower_id"]), _id(r["followee_id"])) for _, r in follows.iterrows()
        )
    positions = nx.spring_layout(layout_graph, seed=42, k=1.6 / max(1, math.sqrt(len(ids))))
    fig = go.Figure()
    drawn = []
    if mode in ("关注关系", "全部关系"):
        for (src, dst), typ in state["active"].items():
            drawn.append((src, dst, COLORS[typ], "关注（预设）" if typ == "manual" else "关注（自主/未知）", 1))
    if mode in ("评论互动", "全部关系"):
        for (src, dst), count in state["interaction"].items():
            drawn.append((src, dst, COLORS["comment"], "评论互动", count))

    nodes_degrees = Counter()
    used_labels = set()
    for src, dst, color, label, weight in drawn:
        if src not in positions or dst not in positions or src == dst:
            continue
        nodes_degrees[dst] += weight
        nodes_degrees[src] += weight
        sx, sy = positions[src]
        tx, ty = positions[dst]
        dx, dy = tx - sx, ty - sy
        mag = math.hypot(dx, dy) or 1
        # Stop before entering target node (data coordinates).
        ratio = min(.10 / mag, .28)
        tx2, ty2 = tx - dx * ratio, ty - dy * ratio
        fig.add_trace(go.Scatter(
            x=[sx, tx2], y=[sy, ty2], mode="lines",
            line=dict(color=color, width=min(5, 1.5 + math.log1p(weight))),
            showlegend=label not in used_labels,
            name=label, hoverinfo="skip",
        ))
        used_labels.add(label)
        # An arrowhead gives a clear source -> destination direction.
        fig.add_annotation(
            x=tx2, y=ty2, ax=sx, ay=sy, axref="x", ayref="y",
            showarrow=True, arrowhead=2, arrowsize=1.0,
            arrowwidth=1.7, arrowcolor=color, opacity=0.85,
        )
        fig.add_trace(go.Scatter(
            x=[(sx + tx2)/2], y=[(sy + ty2)/2], mode="markers",
            marker=dict(size=14, opacity=0.0), showlegend=False,
            hovertext=[f"{names.get(src, src)} → {names.get(dst, dst)}<br>{label}<br>次数：{weight}"],
            hoverinfo="text",
        ))
    fig.add_trace(go.Scatter(
        x=[positions[u][0] for u in ids], y=[positions[u][1] for u in ids],
        text=[names.get(u, u) for u in ids], mode="markers+text", textposition="top center",
        marker=dict(color="#334155", size=[min(34, 19 + 2*nodes_degrees[u]) for u in ids],
                    line=dict(color="white", width=1.7)),
        hovertext=[f"{names.get(u,u)}<br>User ID：{u}<br>当前已展示关系次数：{nodes_degrees[u]}" for u in ids],
        hoverinfo="text", name="Agent", showlegend=False,
    ))
    fig.update_layout(
        height=530, margin=dict(l=12, r=12, t=5, b=12),
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        hovermode="closest", legend=dict(orientation="h", y=1.05),
        dragmode="pan", uirevision="fixed-graph-layout",
    )
    return fig


def render_dynamic_replay(users: pd.DataFrame, posts: pd.DataFrame,
                          comments: pd.DataFrame, follows: pd.DataFrame,
                          traces: pd.DataFrame, database_path: Path):
    st.subheader("动态网络与 Agent 行为联动回放")
    st.caption("逐条回放 SQLite trace 事件；关注箭头表示 A 关注 B，评论箭头表示 A 评论了 B 的帖子。事件顺序不等于现实时间，也不等于 LLM 推理顺序。")
    events = _events(traces)
    if events.empty or users.empty:
        st.info("本次实验没有足够的 Agent 或 trace 记录。")
        return
    metadata, metadata_warning = load_metadata(database_path, len(events))
    if metadata_warning:
        st.info(metadata_warning)
    if metadata:
        st.caption(f"实验配置：{metadata.get('model','未知模型')} · {metadata.get('platform','')} · {metadata.get('agents_requested','?')} Agents · {metadata.get('llm_rounds','?')} 轮 LLM 决策 · 预设关注关系：{'是' if metadata.get('seed_network') else '否'}")
    total = len(events)
    run_key = str(database_path.resolve())
    if st.session_state.get("_replay_db") != run_key:
        st.session_state["_replay_db"] = run_key
        st.session_state["replay_cursor"] = total
        st.session_state["phase_jump"] = "请选择阶段"  # different experiments may have different stages
    if st.session_state.get("replay_cursor", 0) > total:
        st.session_state["replay_cursor"] = total

    stages = metadata.get("stages", [])
    if stages:
        jump_values = {"请选择阶段": None, "注册初始化结束": int(stages[0]["trace_count_before"])}
        for s in stages:
            jump_values[f"{s.get('phase','阶段')}（前 {s['trace_count_after']} 条）"] = int(s["trace_count_after"])
        def jump_to_phase():
            val = jump_values[st.session_state["phase_jump"]]
            if val is not None:
                st.session_state["replay_cursor"] = val
        st.selectbox("快捷跳转实验阶段", list(jump_values), key="phase_jump", on_change=jump_to_phase)

    st.slider("回放至第 N 条事件", min_value=0, max_value=total, key="replay_cursor")
    cursor = int(st.session_state["replay_cursor"])
    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 4])
    with ctrl1:
        st.button("上一条", disabled=cursor <= 0, on_click=lambda: st.session_state.update(replay_cursor=max(0, cursor-1)))
    with ctrl2:
        st.button("下一条", disabled=cursor >= total, on_click=lambda: st.session_state.update(replay_cursor=min(total, cursor+1)))
    with ctrl3:
        st.caption(f"事件 {cursor}/{total}  ·  当前阶段：{_stage_name(cursor, metadata) if cursor else '尚未发生事件'}")

    state = _replay_state(events, cursor, posts, comments, follows, metadata)
    s = state
    metrics = st.columns(5)
    for col, label, value in zip(
        metrics,
        ("已执行行为", "已发布帖子", "已发表评论", "当前关注边", "跨用户评论次数"),
        (cursor, len(s["exposed_posts"]), len(s["exposed_comments"]),
         len(s["active"]), sum(s["interaction"].values())),
    ):
        col.metric(label, value)
    if s["unknown_follows"]:
        st.warning(f"有 {s['unknown_follows']} 条关注/取消关注动作无法从日志恢复对应目标，当前网络可能不完整。OASIS 的 follow trace 可能仅保存 follow_id，删除后的历史边无法通过最终 follow 表反查。")
    if s["incomplete_comment"] or s["unknown_content"]:
        st.caption(f"存在无法反查内容 ID 的动作：内容 {s['unknown_content']}，评论关联 {s['incomplete_comment']}。对应内容未被推断或补造。")
    if cursor == total and s["missing_final"]:
        st.warning(f"当前回放的关注关系比终态 follow 表少 {s['missing_final']} 条；应检查 trace 是否完整。")
    left, right = st.columns([1.65, 1])
    with left:
        mode = st.radio("网络关系类型", ["全部关系", "关注关系", "评论互动"], horizontal=True)
        fig = _graph_figure(users, state, events, posts, comments, follows, mode, metadata)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
        st.caption("蓝色：LLM 行为产生的关注边；橙色：研究人员预设的关注边；绿色：评论互动边。灰色表示无法确定来源。若无同名 JSON 阶段文件，关系来源可能不能精确区分。")
    with right:
        st.markdown("**最近发生的行为**")
        if state["status_rows"]:
            recent = pd.DataFrame(state["status_rows"]).tail(12).iloc[::-1]
            st.dataframe(recent[["事件","Agent ID","动作","阶段","时间"]], hide_index=True, use_container_width=True, height=440)
            last = state["status_rows"][-1]
            with st.expander("查看当前事件的原始参数"):
                st.json(_payload(last["动作详情"]))
        else:
            st.info("尚未执行任何事件。")
    st.markdown("**截至当前事件的帖子与评论**")
    post_map = _index_table(posts, "post_id")
    comment_map = _index_table(comments, "comment_id")
    names = {_id(r["user_id"]): str(r.get("name") or r["user_id"]) for _, r in users.iterrows()}
    if not s["exposed_posts"]:
        st.caption("此时尚未产生帖子。")
    else:
        post_ids = sorted(s["exposed_posts"], key=lambda x: int(x) if x.isdecimal() else x)
        for pid in post_ids[-5:]:
            p = post_map[pid]
            post_comments = [c for cid, c in comment_map.items()
                             if cid in s["exposed_comments"] and _id(c.get("post_id")) == pid]
            with st.expander(f"{names.get(_id(p.get('user_id')), '未知 Agent')} · 帖子 #{pid} · {len(post_comments)} 条评论", expanded=(pid == post_ids[-1])):
                st.write(str(p.get("content", "")))
                for c in sorted(post_comments, key=lambda item: str(item.get("created_at", ""))):
                    st.caption(f"{names.get(_id(c.get('user_id')), '未知 Agent')}：{c.get('content','')}")
    st.markdown("**行为随回放进度的累计变化**")
    count_rows = []
    p_count, c_count, f_count = 0, 0, 0
    for _, e in events.iloc[:cursor].iterrows():
        a = e["action"]
        p_count += int(a == "create_post")
        c_count += int(a == "create_comment")
        f_count += int(a == "follow") - int(a == "unfollow")
        count_rows.append({"事件":int(e["event_no"]), "发帖动作":p_count, "评论动作":c_count, "关注净动作":f_count})
    if count_rows:
        st.line_chart(pd.DataFrame(count_rows).set_index("事件"))
        st.download_button("导出当前事件记录 CSV", data=pd.DataFrame(s["status_rows"]).to_csv(index=False).encode("utf-8-sig"),
                           file_name="oasis_replayed_events.csv", mime="text/csv")
    st.caption("注意：回放指标根据已记录的动作重建；点赞总数等终态字段不用于推断历史数值。初始人工注入内容应与 LLM 自主行动区分。")
