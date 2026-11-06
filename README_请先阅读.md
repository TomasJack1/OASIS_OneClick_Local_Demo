# OASIS 一键本地运行 Demo — Windows

**目标：** 解压 ZIP 后双击 `start.cmd`，首次自动安装 Python 3.11 / 项目依赖，然后在浏览器打开 Streamlit。后续直接双击启动。无须手动填写 API Key，也无须访问密码。

## 使用方法

1. 把 ZIP 解压到一个普通目录（不要在压缩包预览窗口内运行）。
2. Windows 64 位系统，保持网络连接，双击 `start.cmd`。
3. **首次运行会自动安装大量依赖，包括 OASIS、CAMEL 和 PyTorch。需要能访问 GitHub 和 Python 包下载源；可能占用数 GB 空间。** 安装完成后在浏览器访问 `http://127.0.0.1:8501`。
4. 进入「实时模拟（Live）」，选择一份 **Bot profile 配置**、填写**实验名称**、设置轮次（1–5）并可启用预设初始关注网络，点击开始。**Agent 数量不再手动设置，完全由所选配置里的 users 条数决定。** 程序逐 Agent 执行并每约 2 秒刷新。每次最多 40 次 Agent 决策（因此 10 位 Agent 最多 4 轮、36 位只能 1 轮），模型调用可能远多于决策次数。
5. 历史实验回放、内容流、关系网络、Agent 画像和实验对比均在同一网页中。**每场实验是 `experiments/` 下的一个文件夹**，内含 `experiment.db`（OASIS 数据）、`experiment.json`（阶段与配置元数据）、`profile_snapshot.json`（本次实际使用的人设快照）、`status.json`、`cancel.flag`、`run.log`。

## 人设配置（bot profile）

配置放在 `config/profiles/*.json`，支持两种写法：

- **对象写法**（推荐，本项目的校园议题就是这种）：顶层含 `users`，并可加 `seed_posts`（开局由研究者人工发布的帖子）与 `initial_follow_edges`（第一次 LLM 决策前人工注入的关注边，格式 `[关注方序号, 被关注方序号]`，序号从 0 起）。
- **OASIS 原生写法**：顶层直接是列表，字段为 `realname / username / bio / persona / age / gender / mbti / country / profession / interested_topics`，与 OASIS 官方 `user_data` 文件通用；此时没有预设帖子和初始网络。

`persona` 优先于 `bio` 进入模型的 system prompt；`bio` 写入数据库的用户简介字段。新增一份 JSON 就会自动出现在网页下拉框里，无须改代码。

### seed_posts 与 initial_follow_edges 的写法

两个字段都只在**对象写法**里可用，且都发生在第一次 LLM 决策之前，属于「研究者人工注入」，**不消耗 API 调用，也不计入 40 次决策预算**。

`seed_posts`：每项 `{"agent_id": 序号, "content": "正文"}`。序号从 0 计，对应 `users` 数组的位置。同一个 `agent_id` 可以出现多次，即同一人开局发多篇帖子。

```json
"seed_posts": [
  { "agent_id": 0, "content": "议题一……" },
  { "agent_id": 0, "content": "同一位 Agent 的第二篇帖子" },
  { "agent_id": 2, "content": "议题二……" }
]
```

`initial_follow_edges`：每项是一个两元素数组 `[关注方序号, 被关注方序号]`，读作「前者关注后者」，与 OASIS 的 follow 动作方向一致。

```json
"initial_follow_edges": [
  [1, 0], [2, 0],      // 1 号和 2 号都关注 0 号
  [0, 3],              // 与上一条无关：方向独立
  [3, 1], [3, 2]       // 3 号同时关注 1 号和 2 号（同一人可有多条出边）
]
```

规则与限制：

- 序号越界、自己关注自己、`agent_id` 越界都会**在启动前直接报错**，不会跑到一半才失败。
- `[1,0]` 与 `[0,1]` 是两条不同的边（单向）；完全同向的重复边会被自动忽略。
- 网页上勾选「预设初始关注网络」时注入这些边；取消勾选则开局网络为空，关系完全由 Agent 自主形成。配置里没有写该字段时，复选框会自动置灰。
- 这些边在回放与关系图里显示为**橙色**（人工预设），Agent 自己发的关注是蓝色。

**注意**：Agent 数量 = `users` 条数。想要 7 位就准备一份只有 7 条的配置，代码不再复制人设。
6. 关闭网页的同时也要关闭 CMD 窗口才会停止 Streamlit；**已经启动的后台实验进程不会仅因为关闭浏览器而停止**，要在「实时模拟」页面点击请求停止，并等待当前行动结束。

## 数据说明

`experiments/` 下附带四组既有结果：两人 Quick Start、五人 `reddit_simulation`、七人 `reddit_live_20261105_234553_819bb0`、十人 `reddit_multi_10_20261105_123401`。每组就是一个文件夹，发包时保留整个文件夹即可；不想公开的实验，把对应文件夹移走。

历史数据里的人设文本来自当时的代码版本，与现在的配置文件不完全一致；十人那组用的就是较详细的一版，已原样收录进 `config/profiles/campus_ai_integrity.json`。

## API 费用和凭据风险（必须阅读）

这个包按项目提供者要求，**包含明文 API 凭据**。打开 ZIP 的任何人都能够从配置文件复制凭据并绕过网页的 Agent/轮次限制，直接调用该 Qwen 服务。接收者不必配置 API，但这意味着 API 帐户完全由项目提供者承担费用和风险。

- 只分享给明确可信的人员，不要上传 GitHub、公共云盘或论坛。
- 每人都是本机独立调用同一个模型网关账户。项目作者的电脑即使关闭，接收者已正确安装后仍可运行。
- **展示完立即禁用/轮换 API Key**；由于凭据先前在聊天中被披露，不能认为它仅在接收人之间可见。
- 强烈建议在网关或账户侧限制总预算、并发和允许调用的模型（如平台提供这些控制）。
- 这个 Demo 无账户、无访问密码，不应直接将 Streamlit 端口暴露到互联网。仅绑定本机 `127.0.0.1`。

## 出错处理

- 如果首次安装失败，把 `start.cmd` 窗口里最后的错误信息发给项目维护者。不要发送整个 `.env`。
- 如果 Python/PyPI/GitHub 网络下载被拦截，纯 ZIP 无法保证在所有 Windows 电脑“完全无依赖”运行。需要由项目作者提前在目标 Windows 机器上打包为可执行程序，或提供内部依赖镜像。
- 如果 API 返回 401/403/429，检查模型服务是否仍有效及账户额度。包内模型为 `qwen3.8-27b`，地址与凭据都来自 `.env`（当前指向北邮 `myai.bupt.edu.cn` 网关）。
- 如果需要展示已有历史实验，请保留整个实验文件夹——`experiment.db` 与 `experiment.json` 必须来自同一次运行，否则阶段跳转会失效。

## 文件说明

- `app.py`：OASIS 历史回放 + 实时模拟网页
- `dashboard_view.py`：统计总览、Agent 画像和实验对比
- `replay_view.py`：关系图及按事件回放
- `live_control.py`：实时监控、启动和停止
- `live_worker.py`：后台 OASIS 模拟与 Qwen 调用（不再内置人设，全部来自配置）
- `profiles.py`：读取 `config/profiles/` 下的人设配置，组装进入 system prompt 的自述文本
- `experiment_paths.py`：`experiments/` 目录布局与实验名称校验
- `config/profiles/`：bot profile 配置（校园详细版、校园精简版、OASIS 原生 36 人示例）
- `experiments/`：每场实验一个文件夹，包含数据、元数据、状态与日志
- `.env`：API Key / 模型地址（**仅在获得授权的展示人员之间分享**）
- `bootstrap.ps1`：自动准备运行环境
- `start.cmd`：唯一的启动入口
