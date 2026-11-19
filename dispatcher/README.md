# v2-dispatch：常驻调度器

让服务器上的 Agent（Codex、Claude Code 等）持续干活的常驻进程。思路借鉴 [dsh-superagent](https://github.com/deepseek-ai/dsh-superagent-prototype)：一个 router 负责拆任务、派人、验收，worker 各自在独立 worktree 里干活，只在需要人的时候打扰人。这里 router 是 Hark（或任何会读写卡片的人/程序），v2-dispatch 负责其余的常驻部分：

- 队列：卡片 front matter 写 `dispatch: "queued"` 即排队，`priority` 越小越先（默认 50），同优先级先来先派。
- 并发槽位：同时最多 `slots` 个，空闲内存低于 `min_free_mem_gb` 时暂缓。
- 独立工作区：按卡片的 `repo`、`base`、`branch` 自动建 git worktree，软链 `node_modules`。
- 提问回流：worker 用 `board-ask` 提一个具体问题后结束本轮；人用 `board-dispatch answer` 回答后，调度器在**同一会话**里唤醒它（Codex `exec resume`，Claude `--resume`）。
- 重启接管：Agent 在独立进程组里跑，调度器重启不会杀掉它们，新进程会接管并在结束后收尾。
- 全程走现有的 `agent-run` / `board-report`，看板照常显示开始、进度、完成。

## 卡片字段

```yaml
---
title: "做什么"
status: "open"            # 看板状态，由 board-report 维护
dispatch: "queued"        # 调度状态，由 v2-dispatch 维护
executor: "codex-astra"   # 配置里 executors 的键
repo: "/srv/v0-ai"        # 可选：自动建 worktree
base: "h/trunk"           # 可选：新分支的起点
branch: "h/some-feature"  # 可选：默认 d/<卡号>
priority: "50"            # 可选
timeout: "21600"          # 可选，秒
---
任务正文：目标、范围、验收标准。调度器会自动附上调度协议。
```

调度状态：`queued` 排队 → `running` 在跑 → `asking` 等人回答 → `answered` 已回答待续跑 → `done` 待验收 / `failed` 失败；`held` 暂停；`closed` 已验收或放弃。

停止一轮：在卡片 front matter 里写 `stop: "<时间>"`（看板的「停止」按钮写的就是它）。dispatcher 先记下这一轮的整棵进程树，给进程组发 TERM，`stop_grace_seconds`（默认 10）秒后还活着的（包括 Agent 另起会话的子进程）一律 KILL，然后去掉 `stop`。这一轮被打断（失败，或还有留言没送达）时卡置为 `held` 并写上 `stopped`；它自己正常跑完仍是 `done`，停下前提了问题仍是 `asking`。还没开跑的卡（`queued`、`answered`）不启动，直接置为 `held` 加 `stopped`。带 `stopped` 的 `held` 卡收到留言会去掉这个标记回到 `answered`：跑过的接着原会话，没跑过的按第一轮给完整任务；手工 `held` 的不会被留言唤醒。

## 命令

```sh
board-dispatch status                       # 需要人的（提问、失败、待验收）、在跑的、排队的
board-dispatch queue  CARD [--executor X]   # 排队 / 失败后重排
board-dispatch answer CARD Q1 "回答"         # 回答后自动续跑
board-dispatch hold   CARD                  # 暂停
board-dispatch close  CARD                  # 验收通过或放弃
board-ask CARD "问题" --option 甲 --option 乙  # worker 用
```

## 安装

```sh
git clone https://github.com/joyboy123456/v2-board.git /opt/v2-board
sh /opt/v2-board/dispatcher/install.sh       # 首次会生成 /etc/v2-dispatch.json，按需修改执行器
journalctl -u v2-dispatch -f
```

更新：`git -C /opt/v2-board pull && systemctl restart v2-dispatch`（在跑的任务不受影响）。

测试：`python3 -m unittest tests.test_dispatcher`
