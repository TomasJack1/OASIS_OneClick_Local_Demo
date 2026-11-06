"""Run OASIS actions in a background subprocess using the local package env."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from camel.models import ModelFactory
from camel.types import ModelPlatformType
import oasis
from oasis import ActionType, AgentGraph, LLMAction, ManualAction, SocialAgent, UserInfo

import experiment_paths as layout
from profiles import SYSTEM_PROMPT_TEMPLATE, load_profile_config, snapshot_to_dict

MODEL_ID = "qwen3.8-27b"
MIN_AGENTS = 2
MAX_ROUNDS = 5
MAX_DECISIONS = 40


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def sql_counts(db_path: Path) -> dict[str, int]:
    out = {k: 0 for k in ("user", "post", "comment", "follow", "trace")}
    if not db_path.exists():
        return out
    try:
        with sqlite3.connect(str(db_path), timeout=4) as conn:
            conn.execute("PRAGMA busy_timeout=4000")
            existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in out:
                if name in existing:
                    out[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    except sqlite3.Error:
        # OASIS may temporarily hold a write transaction. The next poll will retry.
        pass
    return out


def user_id_mapping(db_path: Path) -> dict[int, int]:
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        rows = conn.execute('SELECT agent_id, user_id FROM "user"').fetchall()
    return {int(a): int(u) for a, u in rows}


def args_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="实验名称，同时作为实验目录名")
    parser.add_argument("--profile", type=Path, required=True, help="config/profiles 下的人设配置")
    parser.add_argument("--rounds", type=int, required=True)
    parser.add_argument("--seed-network", action="store_true")
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--cancel", type=Path, required=True)
    parser.add_argument("--active-lock", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    args.config = load_profile_config(args.profile)
    args.db = args.experiment_dir / layout.DB_NAME
    args.agents = args.config.agent_count
    if not (args.agents >= MIN_AGENTS and 1 <= args.rounds <= MAX_ROUNDS
            and args.agents * args.rounds <= MAX_DECISIONS):
        parser.error(
            f"Supported: >= {MIN_AGENTS} profiles, 1-{MAX_ROUNDS} rounds, "
            f"<= {MAX_DECISIONS} LLM decisions per run "
            f"(profiles={args.agents}, rounds={args.rounds})")
    if args.seed_network and not args.config.initial_follow_edges:
        parser.error("所选 profile 配置没有 initial_follow_edges，无法启用预设初始网络。")
    return args


async def simulate(a, progress, meta, meta_path: Path):
    key = os.getenv("DASHSCOPE_API_KEY")
    base = os.getenv("DASHSCOPE_BASE_URL")
    if not key or not base:
        raise RuntimeError("没有找到演示 API 设置，检查程序包的 .env。")

    model = ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
        model_type=MODEL_ID,
        api_key=key,
        url=base,
        model_config_dict={"extra_body": {"enable_thinking": True}, "stream": False},
        timeout=120.0,
        max_retries=2,
    )
    actions = [
        ActionType.CREATE_POST, ActionType.CREATE_COMMENT, ActionType.LIKE_POST,
        ActionType.FOLLOW, ActionType.REFRESH, ActionType.DO_NOTHING,
    ]
    agent_graph = AgentGraph()
    config = a.config
    for agent_id, profile in enumerate(config.users):
        agent_graph.add_agent(SocialAgent(
            agent_id=agent_id,
            user_info=UserInfo(
                user_name=profile.username, name=profile.realname,
                description=profile.bio,
                # OASIS 默认链路只会把名字写进 system prompt；这里用自定义模板
                # 让人设真正生效，profile 字典只暴露模板需要的这一个键。
                profile={"self_description": profile.self_description},
                recsys_type="reddit",
            ),
            user_info_template=SYSTEM_PROMPT_TEMPLATE,
            agent_graph=agent_graph, model=model, available_actions=actions,
        ))
    env = oasis.make(
        agent_graph=agent_graph, platform=oasis.DefaultPlatformType.REDDIT,
        database_path=str(a.db), semaphore=1,
    )

    def flush(phase: str, actor: str = "", completed: int | None = None):
        counts = sql_counts(a.db)
        progress.update(phase=phase, actor=actor, counts=counts, last_update_utc=now())
        if completed is not None:
            progress["completed_decisions"] = completed
        meta["counts"] = counts
        atomic_json(meta_path, meta)
        atomic_json(a.status, progress)
        print(f"[PROGRESS] {phase} {actor} {counts}", flush=True)

    async def phase_step(label: str, selected: dict, actor: str = ""):
        begin = sql_counts(a.db)["trace"]
        started = now()
        flush(label, actor)
        await env.step(selected)
        after = sql_counts(a.db)["trace"]
        meta["stages"].append({
            "phase": label, "actor": actor,
            "trace_count_before": begin, "trace_count_after": after,
            "new_trace_rows": max(0, after - begin),
            "wall_start_utc": started, "wall_end_utc": now(),
        })
        flush(label, actor)

    progress.update(state="running", phase="环境初始化", actor="", started_utc=now())
    atomic_json(a.status, progress)
    await env.reset()
    flush("注册 Agent")
    completed = 0
    try:
        if config.seed_posts:
            initial: dict = {}
            for post in config.seed_posts:
                agent = env.agent_graph.get_agent(post["agent_id"])
                initial.setdefault(agent, []).append(ManualAction(
                    action_type=ActionType.CREATE_POST,
                    action_args={"content": post["content"]},
                ))
            await phase_step("manual_seed_posts", initial, "研究者预设帖子")

        if a.seed_network:
            mapping = user_id_mapping(a.db)
            seed_actions: dict = {}
            for src, dst in config.initial_follow_edges:
                seed_actions.setdefault(env.agent_graph.get_agent(src), []).append(ManualAction(
                    action_type=ActionType.FOLLOW,
                    action_args={"followee_id": mapping[dst]},
                ))
                meta["seeded_follow_edges"].append({
                    "source_agent_id": src, "target_agent_id": dst,
                    "target_user_id": mapping[dst],
                    "origin": "manual_experimental_initialization",
                })
            if seed_actions:
                await phase_step("manual_seed_follows", seed_actions, "研究者预设关系")

        for r in range(1, a.rounds + 1):
            for idx, profile in enumerate(config.users):
                if a.cancel.exists():
                    progress.update(state="cancelled", phase="用户请求停止", ended_utc=now())
                    flush("已停止", profile.realname, completed)
                    return
                actor = f"{profile.realname} · Agent {idx}"
                label = f"llm_round_{r}"
                await phase_step(label, {env.agent_graph.get_agent(idx): LLMAction()}, actor)
                completed += 1
                progress["round"] = r
                flush(label, actor, completed)
        progress.update(state="completed", phase="全部执行完成", ended_utc=now())
        flush("全部执行完成", completed=completed)
    finally:
        await env.close()
        flush(progress.get("phase", "完成"), completed=completed)


async def run(a):
    a.experiment_dir.mkdir(parents=True, exist_ok=True)
    a.db = a.db.resolve()
    progress = {
        "run_id": a.run_id, "name": a.name, "state": "starting", "pid": os.getpid(),
        "experiment": str(a.experiment_dir), "profile": a.config.path.name,
        "agents": a.agents, "rounds": a.rounds, "seed_network": a.seed_network,
        "model": MODEL_ID, "db": str(a.db), "phase": "启动中", "actor": "",
        "completed_decisions": 0, "total_decisions": a.agents * a.rounds,
        "started_utc": now(), "last_update_utc": now(),
    }
    meta_path = a.db.with_suffix(".json")
    meta = {
        "database": a.db.name, "experiment_name": a.name,
        "model": MODEL_ID, "platform": "reddit",
        "profile_file": a.config.path.name,
        "profile_sha256": hashlib.sha256(a.config.path.read_bytes()).hexdigest(),
        "agents_requested": a.agents, "llm_rounds": a.rounds,
        "manual_seed_posts": len(a.config.seed_posts),
        "seed_network": a.seed_network, "seeded_follow_edges": [],
        "stages": [], "counts": {},
        "execution_note": "Live interactive mode runs one LLM Agent per env.step sequentially. This differs from parallel rounds.",
    }
    try:
        atomic_json(a.experiment_dir / layout.SNAPSHOT_NAME, snapshot_to_dict(a.config))
        atomic_json(a.status, progress)
        await simulate(a, progress, meta, meta_path)
    except Exception as e:
        secret = os.getenv("DASHSCOPE_API_KEY", "")
        details = traceback.format_exc().replace(secret, "[REDACTED]") if secret else traceback.format_exc()
        print(details, file=sys.stderr, flush=True)
        error_text = str(e).replace(secret, "[REDACTED]") if secret else str(e)
        progress.update(
            state="error", phase="执行失败", error=f"{type(e).__name__}: {error_text[:500]}",
            last_update_utc=now(), ended_utc=now(),
        )
        atomic_json(a.status, progress)
        meta["counts"] = sql_counts(a.db)
        atomic_json(meta_path, meta)
    finally:
        try:
            if a.active_lock.exists():
                lock = json.loads(a.active_lock.read_text(encoding="utf-8"))
                if lock.get("run_id") == a.run_id:
                    a.active_lock.unlink()
        except (OSError, ValueError, TypeError):
            pass


if __name__ == "__main__":
    asyncio.run(run(args_parser()))
