"""Streamlit live control panel for locally installed OASIS and Qwen."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import psutil
import streamlit as st
from dotenv import load_dotenv

import experiment_paths as layout
from profiles import PROFILES_DIR, ProfileConfigError, list_profile_configs, load_profile_config
from replay_view import _events, _graph_figure, _replay_state, load_metadata, ACTION_LABELS

ROOT = Path(__file__).resolve().parent
EXPERIMENTS_DIR = layout.EXPERIMENTS_DIR
ACTIVE_FILE = layout.ACTIVE_FILE
WORKER_FILE = ROOT / "live_worker.py"
MIN_AGENTS = 2
MAX_ROUNDS = 5
MAX_DECISIONS = 40

load_dotenv(ROOT / ".env", override=True)


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_atomic_json = layout.atomic_json


def get_active() -> dict:
    """Return active experiment if process still exists; avoid accidental duplicate starts."""
    lock = _read_json(ACTIVE_FILE)
    if not lock:
        return {}
    name = str(lock.get("name", ""))
    status = _read_json(layout.experiment_dir(name) / layout.STATUS_NAME) if name else {}
    if status.get("state") in ("completed", "cancelled", "error"):
        try:
            ACTIVE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {}
    pid = lock.get("pid")
    if pid is not None and psutil.pid_exists(int(pid)):
        return lock
    # Avoid removing a lock while Popen is being initialized in another session.
    created = float(lock.get("created_epoch", 0))
    if time.time() - created > 15:
        try:
            ACTIVE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {}
    return lock


def _oasis_python() -> Path:
    # All dependencies (CAMEL, OASIS, Streamlit) live in the shared local .venv.
    name = "python.exe" if os.name == "nt" else "python"
    return ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / name


def start_run(name_raw: object, rounds: int, seed_network: bool, profile_path: object) -> str:
    """Atomic single-run lock, then launch with *no secret on the command line*."""
    config = load_profile_config(Path(profile_path))
    name = layout.normalize_name(name_raw)
    agents = config.agent_count
    if agents < MIN_AGENTS:
        raise ValueError(f"{config.path.name} 只有 {agents} 位 Agent，至少需要 {MIN_AGENTS} 位。")
    if not 1 <= rounds <= MAX_ROUNDS:
        raise ValueError(f"轮次需为 1–{MAX_ROUNDS}。")
    if agents * rounds > MAX_DECISIONS:
        raise ValueError(
            f"{agents} 位 Agent × {rounds} 轮 = {agents * rounds} 次决策，超过每次上限 "
            f"{MAX_DECISIONS}；这份配置最多可跑 {MAX_DECISIONS // agents} 轮。")
    if seed_network and not config.initial_follow_edges:
        raise ValueError(f"{config.path.name} 未定义 initial_follow_edges，无法启用预设初始网络。")
    if (not os.getenv("DASHSCOPE_API_KEY") or not os.getenv("DASHSCOPE_BASE_URL")
            or os.getenv("DASHSCOPE_API_KEY", "").startswith("REPLACE_")):
        raise RuntimeError("没有读取到演示 API 配置，请检查程序包内的 .env 文件。")
    python_exe = _oasis_python()
    if not python_exe.is_file():
        raise FileNotFoundError(f"找不到 OASIS Python：{python_exe}")
    if not WORKER_FILE.is_file():
        raise FileNotFoundError(f"找不到运行脚本：{WORKER_FILE}")
    EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    if get_active():
        raise RuntimeError("已有实验运行中。请等待完成或请求停止，不要重复调用付费 API。")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    exp_dir = layout.experiment_dir(name)
    exp_dir.mkdir(parents=True, exist_ok=True)
    db_path = exp_dir / layout.DB_NAME
    status_file = exp_dir / layout.STATUS_NAME
    cancel_file = exp_dir / layout.CANCEL_NAME
    log_path = exp_dir / layout.LOG_NAME

    lock = {"run_id": run_id, "name": name, "pid": None, "created_epoch": time.time()}
    try:
        fd = os.open(str(ACTIVE_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError("其他浏览器会话刚启动了一个实验；一次只能运行一组。")
    with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
        json.dump(lock, lock_file)

    queued = {
        "run_id": run_id, "name": name, "state": "starting", "phase": "正在创建 OASIS 进程",
        "profile": config.path.name, "agents": agents, "rounds": rounds,
        "seed_network": seed_network,
        "completed_decisions": 0, "total_decisions": agents * rounds,
        "db": str(db_path),
    }
    _atomic_json(status_file, queued)
    cmd = [
        str(python_exe), "-u", str(WORKER_FILE),
        "--name", name, "--profile", str(config.path), "--rounds", str(rounds),
        "--experiment-dir", str(exp_dir), "--status", str(status_file),
        "--cancel", str(cancel_file), "--active-lock", str(ACTIVE_FILE),
        "--run-id", run_id,
    ]
    if seed_network:
        cmd.append("--seed-network")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                cmd, cwd=str(exp_dir), env=os.environ.copy(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True, creationflags=flags,
            )
        lock["pid"] = proc.pid
        _atomic_json(ACTIVE_FILE, lock)
        return name
    except Exception:
        ACTIVE_FILE.unlink(missing_ok=True)
        status_file.unlink(missing_ok=True)
        try:
            exp_dir.rmdir()  # 只在目录仍为空时成功，失败说明已有产物
        except OSError:
            pass
        raise


def request_cancel(name: str) -> None:
    """在实验目录里落下停止标记；只允许操作正在运行的那场实验。"""
    active = get_active()
    if not active or active.get("name") != name:
        raise ValueError("当前实验已经结束或不是活动实验。")
    cancel_file = layout.experiment_dir(name) / layout.CANCEL_NAME
    cancel_file.parent.mkdir(parents=True, exist_ok=True)
    cancel_file.write_text("cancel", encoding="utf-8")


def _safe_tables(db_path: Path) -> dict[str, pd.DataFrame]:
    empty = {name: pd.DataFrame() for name in ("user", "post", "comment", "follow", "trace")}
    if not db_path.is_file():
        return empty
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in empty:
                if name in tables:
                    sql = ('SELECT rowid AS _trace_rowid, * FROM "trace" ORDER BY rowid'
                           if name == "trace" else f'SELECT * FROM "{name}"')
                    empty[name] = pd.read_sql_query(sql, conn)
    except (sqlite3.Error, OSError):
        # Writer may briefly hold a lock: retry on next fragment refresh.
        pass
    return empty


def _authors(users: pd.DataFrame) -> dict[str, str]:
    if users.empty:
        return {}
    result = {}
    for _, row in users.iterrows():
        result[str(row.get("user_id"))] = str(row.get("name") or row.get("user_name") or row.get("user_id"))
    return result


def _stream_content(events: pd.DataFrame, tables: dict) -> None:
    st.markdown("#### Agent 发布的内容（最近 15 条）")
    if events.empty:
        st.info("正在等待 Agent 发帖或发表评论……")
        return
    posts, comments = tables["post"], tables["comment"]
    authors = _authors(tables["user"])
    post_lookup = ({str(r["post_id"]): r.to_dict() for _, r in posts.iterrows()}
                   if not posts.empty and "post_id" in posts else {})
    comment_lookup = ({str(r["comment_id"]): r.to_dict() for _, r in comments.iterrows()}
                      if not comments.empty and "comment_id" in comments else {})
    relevant = events[events["action"].isin(["create_post", "create_comment"])].tail(15).iloc[::-1]
    if relevant.empty:
        st.info("暂无发帖或评论。")
        return
    for _, event in relevant.iterrows():
        payload = event["payload"] or {}
        action = event["action"]
        if action == "create_post":
            linked = post_lookup.get(str(payload.get("post_id")), {})
        else:
            linked = comment_lookup.get(str(payload.get("comment_id")), {})
        content = payload.get("content") or linked.get("content")
        with st.container(border=True):
            label = "发布帖子" if action == "create_post" else "发表评论"
            agent = authors.get(str(event["user_id"]), f'Agent {event["user_id"]}')
            st.markdown(f"**{agent} · {label}**")
            st.write(str(content) if content else "（记录中没有可展示的正文）")
            st.caption(f'事件 #{event["event_no"]} · {event.get("created_at", "")}')


def _safe_progress(status: dict):
    total = int(status.get("total_decisions", 0))
    done = int(status.get("completed_decisions", 0))
    if total:
        st.progress(min(1.0, done / total), text=f"LLM 决策已完成 {done}/{total}")
    st.caption(f'状态：{status.get("state", "未知")} · 当前阶段：{status.get("phase", "未开始")}')
    if status.get("actor"):
        st.caption(f'当前 Agent：{status["actor"]}')


@st.fragment(run_every="2s")
def live_monitor() -> None:
    dirs = ([p for p in EXPERIMENTS_DIR.iterdir()
             if p.is_dir() and (p / layout.STATUS_NAME).is_file()]
            if EXPERIMENTS_DIR.is_dir() else [])
    dirs.sort(key=lambda p: (p / layout.STATUS_NAME).stat().st_mtime, reverse=True)
    if not dirs:
        st.info("暂无网页发起的实验。请在上方设置实验名称、人设配置和轮次后点击开始。")
        return
    active = get_active()
    options = [p.name for p in dirs]
    selected = st.selectbox(
        "监控哪场实验", options,
        index=(options.index(active["name"]) if active.get("name") in options else 0),
        key="live_run_select",
    )
    status = _read_json(layout.experiment_dir(selected) / layout.STATUS_NAME)
    st.markdown(f"**实验：** `{selected}` ｜ **运行编号：** `{status.get('run_id', '未知')}`")
    _safe_progress(status)
    if status.get("state") == "error":
        st.error("运行失败：" + str(status.get("error", "请查看本机日志")))
    elif status.get("state") == "cancelled":
        st.warning("已按请求停止。已产生的数据仍保留，可用于历史回放。")
    elif status.get("state") == "completed":
        st.success("本次 OASIS 实验执行完成。可以在左侧选择该实验查看全部回放页面。")

    if active.get("name") == selected:
        if st.button("请求停止（当前正在执行的 API 调用可能继续完成）", key=f"cancel_{selected}"):
            request_cancel(selected)
            st.info("已发送停止请求；程序会在当前 Agent 动作结束后停止。")

    db_path = layout.experiment_dir(selected) / layout.DB_NAME
    if not db_path.is_file():
        st.info("正在等待 OASIS 创建 SQLite 数据库……")
        return
    tables = _safe_tables(db_path)
    events = _events(tables["trace"])
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("已注册 Agent", len(tables["user"]))
    c2.metric("已发布帖子", len(tables["post"]))
    c3.metric("已发表评论", len(tables["comment"]))
    c4.metric("当前关注边", len(tables["follow"]))
    c5.metric("累计事件", len(events))

    left, right = st.columns([1.25, 1], gap="large")
    with left:
        st.markdown("#### 关系网络实时状态")
        relation = st.radio(
            "关系类型", ["全部关系", "关注关系", "评论互动"],
            horizontal=True, key="live_relation_choice",
        )
        if tables["user"].empty:
            st.info("正在等待 Agent 注册……")
        else:
            meta, _ = load_metadata(db_path, len(events))
            state = _replay_state(events, len(events), tables["post"], tables["comment"], tables["follow"], meta)
            fig = _graph_figure(tables["user"], state, events, tables["post"], tables["comment"], tables["follow"], relation, meta)
            if fig is not None:
                fig.update_layout(height=430)
                st.plotly_chart(fig, use_container_width=True, key="live_network_fig")
            if state.get("unknown_follows"):
                st.caption("提示：个别历史关注动作无法由当前记录完整还原，详见原有动态回放页。")
    with right:
        _stream_content(events, tables)
    st.markdown("#### 最近 Agent 行动")
    if events.empty:
        st.info("尚无事件记录。")
    else:
        names = _authors(tables["user"])
        recent = events.tail(20).iloc[::-1].copy()
        recent["Agent"] = recent["user_id"].map(lambda x: names.get(str(x), f"Agent {x}"))
        recent["行为"] = recent["action"].map(lambda x: ACTION_LABELS.get(x, x))
        st.dataframe(recent[["event_no", "Agent", "行为", "created_at"]].rename(
            columns={"event_no": "事件序号", "created_at": "记录时间"}),
            use_container_width=True, hide_index=True, height=300,
        )
        activity = events["action"].value_counts().rename_axis("动作").reset_index(name="次数")
        st.bar_chart(activity.set_index("动作"))


def render_live_console() -> None:
    st.subheader("实时 OASIS 模拟控制台")
    st.caption("qwen3.8-27b · 本机独立执行 · 每约 2 秒读取已提交的 OASIS 行动")
    st.warning("模型调用使用演示包内配置的 API 账户计费。请勿重复启动或在公开环境传播本程序包。")
    active = get_active()
    if active:
        st.warning(f'实验 `{active.get("name")}` 正在运行；为了控制费用，一次只允许一组实验。')

    configs = list_profile_configs()
    if not configs:
        st.error("没有找到任何 bot profile 配置，请把 JSON 放到下面这个目录后刷新页面：")
        st.code(str(PROFILES_DIR))
        return
    names = [p.stem for p in configs]

    # 选择器放在表单外：表单内的控件要到提交才生效，而这里需要即时预览配置内容。
    profile_name = st.selectbox("Bot profile 配置", names, key="live_profile")
    try:
        config = load_profile_config(PROFILES_DIR / f"{profile_name}.json")
    except ProfileConfigError as exc:
        st.error(f"profile 配置有问题：{exc}")
        return
    agents = config.agent_count
    max_rounds = max(1, MAX_DECISIONS // agents)

    if st.session_state.get("_profile_for_name") != profile_name:
        st.session_state["_profile_for_name"] = profile_name
        st.session_state["live_exp_name"] = f"{profile_name}_{datetime.now().strftime('%Y%m%d_%H%M')}"

    with st.form("live_new_run", clear_on_submit=False):
        c1, c2 = st.columns([2, 1])
        with c1:
            st.text_input("实验名称", key="live_exp_name",
                          help="将作为 experiments/ 下的文件夹名。同名实验不会被覆盖。")
        with c2:
            rounds = st.number_input("LLM 决策轮次", min_value=1, max_value=MAX_ROUNDS,
                                     value=2, step=1, key="live_rounds")
        seed = st.checkbox(
            "预设初始关注网络", value=True, disabled=not config.initial_follow_edges,
            help="在第一次 LLM 决策前，由研究者手动注入配置里的 initial_follow_edges。不消耗 API 调用。")
        st.caption(f"人设配置：{config.summary()}")
        st.caption(f"预计 Agent 决策：{agents * int(rounds)}（每次决策可能包含多次实际 API 请求）；"
                   f"这份配置在当前 {MAX_DECISIONS} 次预算下最多可跑 {max_rounds} 轮。")
        if agents < MIN_AGENTS:
            st.error(f"至少需要 {MIN_AGENTS} 位 Agent。")
            valid = False
        else:
            valid = agents * int(rounds) <= MAX_DECISIONS
        if not valid:
            st.warning(f"每次最多 {MAX_DECISIONS} 次 Agent 决策。")
        if not config.seed_posts:
            st.info("该配置没有 seed_posts，实验开局不会有人工预设帖子。")
        if not config.initial_follow_edges:
            st.info("该配置没有 initial_follow_edges，开局关注网络为空，关系完全由 Agent 自主形成。")
        started = st.form_submit_button("开始模拟（会产生 API 费用）", disabled=bool(active) or not valid)
        if started:
            try:
                new_name = start_run(st.session_state.get("live_exp_name", ""), int(rounds),
                                     bool(seed) and bool(config.initial_follow_edges), config.path)
                st.success(f"实验已启动：{new_name}")
                st.rerun()
            except Exception as exc:
                st.error(f"启动失败：{exc}")
    st.divider()
    live_monitor()
