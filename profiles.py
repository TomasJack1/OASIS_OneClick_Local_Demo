"""Bot profile 配置读取。

配置文件放在 config/profiles/ 下，支持两种写法：

1. OASIS 原生 user_data 格式——顶层直接是列表，每项含
   realname / username / bio / persona / age / gender / mbti / country /
   profession / interested_topics。
2. 对象格式——顶层是字典，users 字段同上，另可带 seed_posts 与
   initial_follow_edges，用于把预设帖子和初始关注网络一起配置化。

Agent 数量完全由 users 列表长度决定，运行侧不再单独设置数量。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from camel.prompts import TextPrompt

ROOT = Path(__file__).resolve().parent
PROFILES_DIR = ROOT / "config" / "profiles"

# 与 oasis.social_platform.config.user.UserInfo.to_reddit_system_message 保持一致的骨架，
# 但人设文本由本模块组装后经 self_description 单一占位符注入。
SYSTEM_PROMPT_TEMPLATE = TextPrompt("""
# OBJECTIVE
You're a Reddit user, and I'll present you with some posts. After you see the posts, choose some actions from the following functions.

# SELF-DESCRIPTION
Your actions should be consistent with your self-description and personality.
{self_description}

# RESPONSE METHOD
Please perform actions by tool calling.
""")


class ProfileConfigError(ValueError):
    """配置文件不符合约定。"""


@dataclass
class BotProfile:
    realname: str
    username: str
    bio: str
    self_description: str


@dataclass
class ProfileConfig:
    path: Path
    name: str
    description: str
    users: list[BotProfile]
    seed_posts: list[dict] = field(default_factory=list)
    initial_follow_edges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def agent_count(self) -> int:
        return len(self.users)

    def summary(self) -> str:
        parts = [f"{self.agent_count} 位 Agent"]
        if self.seed_posts:
            parts.append(f"{len(self.seed_posts)} 条预设帖子")
        if self.initial_follow_edges:
            parts.append(f"{len(self.initial_follow_edges)} 条初始关注边")
        return " · ".join(parts)


def _first(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_self_description(user: dict, realname: str, username: str) -> str:
    """把一份 profile 压成模型真正读到的自述文本。"""
    core = _first(user, "persona", "bio", "description")
    lines = [f"Your name is {realname} (@{username})."]
    if core:
        lines.append(core)
    profession = _first(user, "profession")
    if profession:
        lines.append(f"Profession: {profession}.")
    demo = (user.get("gender"), user.get("age"), user.get("mbti"), user.get("country"))
    if all(value not in (None, "") for value in demo):
        gender, age, mbti, country = demo
        lines.append(
            f"You are a {gender}, {age} years old, with an MBTI personality type "
            f"of {mbti} from {country}."
        )
    topics = user.get("interested_topics")
    if isinstance(topics, list) and topics:
        lines.append("Interested topics: " + ", ".join(str(t) for t in topics) + ".")
    return "\n".join(lines)


def _load_users(raw_users: list, where: Path) -> list[BotProfile]:
    if not isinstance(raw_users, list):
        raise ProfileConfigError(f"{where.name}: users 必须是列表。")
    users: list[BotProfile] = []
    seen: dict[str, int] = {}
    for i, user in enumerate(raw_users):
        if not isinstance(user, dict):
            raise ProfileConfigError(f"{where.name}: 第 {i} 条不是对象。")
        realname = _first(user, "realname", "name")
        username = _first(user, "username", "user_name")
        if not realname or not username:
            raise ProfileConfigError(f"{where.name}: 第 {i} 条缺少 realname 或 username。")
        key = username.lower()
        if key in seen:
            raise ProfileConfigError(
                f"{where.name}: username `{username}` 与第 {seen[key]} 条重复，"
                "会导致 OASIS 注册冲突。")
        seen[key] = i
        users.append(BotProfile(
            realname=realname, username=username,
            bio=_first(user, "bio") or _first(user, "persona"),
            self_description=_build_self_description(user, realname, username),
        ))
    if len(users) < 2:
        raise ProfileConfigError(f"{where.name}: 至少需要 2 位 Agent，当前只有 {len(users)} 条。")
    return users


def _load_seed_posts(raw: object, users: list[BotProfile], where: Path) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProfileConfigError(f"{where.name}: seed_posts 必须是列表。")
    posts = []
    for i, post in enumerate(raw):
        if not isinstance(post, dict):
            raise ProfileConfigError(f"{where.name}: seed_posts 第 {i} 项不是对象。")
        agent_id = post.get("agent_id")
        content = _first(post, "content", "text")
        if not isinstance(agent_id, int) or not 0 <= agent_id < len(users):
            raise ProfileConfigError(
                f"{where.name}: seed_posts 第 {i} 项的 agent_id={agent_id} 超出 0–{len(users) - 1}。")
        if not content:
            raise ProfileConfigError(f"{where.name}: seed_posts 第 {i} 项没有 content。")
        posts.append({"agent_id": agent_id, "content": content})
    return posts


def _load_edges(raw: object, users: list[BotProfile], where: Path) -> list[tuple[int, int]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProfileConfigError(f"{where.name}: initial_follow_edges 必须是列表。")
    edges = []
    for i, edge in enumerate(raw):
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise ProfileConfigError(f"{where.name}: initial_follow_edges 第 {i} 项应为 [关注方, 被关注方]。")
        src, dst = edge
        if not (isinstance(src, int) and isinstance(dst, int)):
            raise ProfileConfigError(f"{where.name}: initial_follow_edges 第 {i} 项必须是整数。")
        if not (0 <= src < len(users) and 0 <= dst < len(users)):
            raise ProfileConfigError(
                f"{where.name}: initial_follow_edges 第 {i} 项 {edge} 超出 0–{len(users) - 1}。")
        if src == dst:
            raise ProfileConfigError(f"{where.name}: initial_follow_edges 第 {i} 项不允许自己关注自己。")
        if (src, dst) in edges:
            continue  # 完全同向的重复边没有额外含义，忽略掉避免重复 follow
        edges.append((src, dst))
    return edges


def list_profile_configs() -> list[Path]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p for p in PROFILES_DIR.glob("*.json") if p.is_file())


def load_profile_config(path: Path) -> ProfileConfig:
    path = Path(path)
    if not path.is_file():
        raise ProfileConfigError(f"找不到 profile 配置：{path}")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileConfigError(f"{path.name} 不是可读的 JSON：{exc}") from exc

    if isinstance(obj, list):
        payload, name, description = {}, path.stem, ""
        raw_users = obj
    elif isinstance(obj, dict):
        payload, raw_users = obj, obj.get("users")
        name = str(obj.get("name") or path.stem)
        description = str(obj.get("description") or "")
    else:
        raise ProfileConfigError(f"{path.name}: 顶层必须是列表或对象。")

    users = _load_users(raw_users, path)
    return ProfileConfig(
        path=path, name=name, description=description, users=users,
        seed_posts=_load_seed_posts(payload.get("seed_posts"), users, path),
        initial_follow_edges=_load_edges(payload.get("initial_follow_edges"), users, path),
    )


def snapshot_to_dict(cfg: ProfileConfig) -> dict:
    """写进实验目录的配置快照，保证结果可追溯到当时的 roster 与拓扑。"""
    return {
        "profile_file": cfg.path.name,
        "profile_name": cfg.name,
        "description": cfg.description,
        "agent_count": cfg.agent_count,
        "users": [
            {"agent_id": i, "realname": u.realname, "username": u.username,
             "bio": u.bio, "self_description": u.self_description}
            for i, u in enumerate(cfg.users)
        ],
        "seed_posts": cfg.seed_posts,
        "initial_follow_edges": [list(e) for e in cfg.initial_follow_edges],
    }
