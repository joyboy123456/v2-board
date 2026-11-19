# V2 依赖地图看板

一个只读的多 Agent 开发进度看板：把一个目录里的 Markdown 任务卡（front matter 写 status/title/agent/depends）和对应 git 分支，画成「谁在干活、进度、待验收、卡住原因、必须拍板的事」一屏可看的界面，并展示卡片之间的依赖与合并路线。

- 前端：Vite + React + TypeScript + lucide-react，亮色（默认）与暗色两套主题，顶栏切换
- 后端：Python FastAPI，只读 Markdown 与 git，SSE 推送实时更新
- 附带 `agent-run` / `board-report` 两个脚本，让 Codex、Claude Code 等 Agent 自己把开始、进度、结束汇报到看板
- `dispatcher/`：常驻调度器 v2-dispatch，排队、并发槽位、提问回流与同会话续跑，见 dispatcher/README.md

快速开始：

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
V2_BOARD_DIR=/path/to/cards V2_REPO_DIR=/path/to/repo ./start.sh 3160
```

许可证：MIT

---

# V2 开发看板

面向多 Agent 的产出看板。默认打开「Agent 产出」，任务地图和原有任务卡详情保留。中文界面，时间统一 Asia/Shanghai。Agent 的 CLI 可用性与工作量分别显示；一次执行一个独立 Run，成功退出不会被当成卡片已验收。

## 架构与数据

- `backend/app.py`：FastAPI 只读接口、SSE、0.5 秒数据目录轮询；任务卡快照每 3 秒更新。`board_parse.py` 是仓库内独立解析器，不依赖 `/root/v2-jobs`。
- `backend/signals.py`：宿主、Git 钩子、后端共用的标准库协议。JSONL 用 `flock` 分配 seq 并追加，Run 用独立锁和原子替换；UTF-8 尾部、半行恢复均有测试。
- `scripts/agent-run`：包装 CLI，原始输出 tee 到调用者和 `<id>.log`，旁路解析 Codex JSONL / Claude stream-json，其他 CLI 按行；工具事件立即写，文本/思考合并至多等待 500ms。采集出错保留 CLI 返回码。
- `scripts/board-report`：保持任务卡 / progress 汇报，追加 BOARD 自动汇报与机器事件；支持显式进度。
- `frontend/src/Agents.tsx`：身份卡片、产出汇总、运行时间线、原生 dialog 抽屉、实时 transcript。SSE 和 REST 按 seq 合并，补齐从连续序列的末尾开始，避免收到后续消息时跳过空缺。

数据目录 `V2_BOARD_DIR`（兼容 `BOARD_ROOT`，默认 `/srv/v2-wiring-board`）：

```text
**/*.md                          任务卡、*-progress.md、BOARD.md
runs/<run_id>.json               一次执行的状态和产出；原子写
runs/<run_id>.json.lock          不删除的并发写锁
runs/<run_id>.messages.jsonl     每个 run 独立、从 1 递增的 seq
runs/<run_id>.log                原始 CLI 日志（不直接作为静态资源公开）
events/events.jsonl             全局递增 seq、唯一 id、追加事件
agents.json                     安装时探测的宿主 CLI 可执行性
```

Run 保存 card、agent、model、reasoning、worktree、branch、base/head commit、pid、log/final 路径、开始/结束/last_seen、退出码、耗时、提交数、净改动文件数与 final 前 600 字。状态为 queued/running/completed/failed/cancelled/timeout；另有 `lost` 表示心跳失联，不代表执行已经失败。最终返回码会覆盖 lost。终止信号记录 cancelled；124 记录 timeout。没有调度器，queued 是对外部排队记录的展示能力。

完成卡数只算 **accepted** 的关联任务卡；待验收另算。成功率以有最终状态的 run 为分母。提交总数是各 run 相对 base 的提交数之和，文件数是各次执行净改动的文件数之和；并行共享分支可能重复计数，建议每个 run 使用独立 worktree。今日提交按钩子事件中的 SHA 去重；今日待验收/已验收卡按真实状态变化去重。

启动时从 BOARD「自动汇报（Agent 钩子）」和各 progress 的带时间开始/结束汇报回填；历史 ID 确定且幂等，标 `backfilled`。只知道结束的记录标 `started_unknown`，未知提交数/文件数不猜测、记 0；未配对开始标 lost。已有原生 run 的开始/结束汇报不会再回填一份。

## 宿主安装

在本仓库任意 checkout 中执行（需要写 `/usr/local` 和业务仓库 hooks 的权限）：

```sh
nice -n 19 sh scripts/install-host.sh
nice -n 19 sh scripts/install-hooks.sh /srv/v0-ai
nice -n 19 sh scripts/prepare-data.sh /srv/v2-wiring-board
```

宿主对外只有 `/usr/local/bin/agent-run`、`board-report` 两个入口，共用的标准库模块安装到 `/usr/local/lib/v2-board/signals.py`。来源全部在本仓库，更新时重跑安装即可；首次替换前备份 `.bak-20261117`，重复安装不覆盖备份。`prepare-data.sh` 需要系统 `acl` 包，以 UID 10001 的命名 ACL 授权容器读写数据，不改变任务文件的属主；新目录继承 ACL。

Git 共享钩子为 `/srv/v0-ai/.git/hooks/post-commit` 和 `post-merge`，所有 worktree 共用。安装器通过 git common-dir 定位，幂等；已有钩子备份 `.bak-20261117` 并链式调用。钩子只启动后台采集及原钩子，立即返回 0；各后台任务有 150ms 截止时间，出错静默。原钩子若超时也会被终止，这是保证不阻塞 Git 的取舍。采集脚本和协议快照存在 `.git/hooks/.v2-board/`。未修改业务代码或 Git 的分支/提交。设置过 `core.hooksPath` 时安装器会明确报错，避免安装一个无效钩子。

```sh
nice -n 19 agent-run QPUB 'Codex gpt-6.1-sol high' /path/to/worktree FINAL=/tmp/final.md -- codex exec -m gpt-6.1-sol -o /tmp/final.md '实现任务'
nice -n 19 agent-run SOME-CARD 'Claude Code opus' /path/to/worktree -- claude -p '实现任务'
# 在 agent-run 的子进程中调用，BOARD_RUN_ID 由包装层传入：
nice -n 19 board-report QPUB --progress '已接通接口，正在整理交付' --step 2 --total 3
```

直接调用的 `codex exec` 自动增加 `--json`；直接调用的 `claude -p` 自动增加 `--output-format stream-json --verbose`。若调用者包了 `timeout` / shell 等，请自行把结构化参数传给里面的 CLI，包装层仍会解析最终输出。`BOARD_AGENT`、`BOARD_RUN_ID`、`BOARD_ROOT` 会传入 CLI。可以在 prompt 末尾要求 agent 调用 `board-report --progress`；没有显式进度时最新 agent_message 自动作为当前动作，无需人工汇报。

旧版本已在运行的 agent-run 不会被热替换；它们仍按旧逻辑报告结束。新启动的执行自动进入实时采集。

## 心跳与远端推送

选择 **宿主心跳文件**，容器不配置 `pid: host`。包装层每 15 秒原子更新 `last_seen_at`；容器后台每 15 秒检查，超过 180 秒标 lost。宿主直接运行预览时默认还检查 PID，进程消失标 failed / `reason=process_gone`。容器的 `V2_HOST_PID_CHECK=0` 避免把容器 PID 误当宿主 PID；没有读取宿主进程的权限需求。

所有环境都只在 `V2_MIRROR_DIR`（宿主默认 `/var/tmp/v2-board-mirror`，容器 `/var/lib/v2-board`）维护 bare mirror。初次从 `/srv/v0-ai` 本地源执行 `clone --mirror --no-hardlinks`（禁用硬链接，在 mirror 卷的私有 Git 配置中限定 safe.directory 读取不同属主仓库，不改宿主 Git 配置），然后每 60 秒向实际 origin `fetch --prune`，比较远端分支 SHA，追加 `git:push` 事件，包括删除分支。绝不在业务仓库里 fetch，不写它的 FETCH_HEAD / refs；只读挂载也能运行。抓取独立于 15 秒心跳，不会拖慢状态检查。`fetch-status.json` 记录最近成功或脱敏错误；凭据不可用时其余看板照常工作。

需要私有远端凭据时，用 compose override **只读**挂载专用宿主凭据，例如：

```yaml
services:
  v2-board:
    volumes:
      - /path/to/board-gitconfig:/home/board/.gitconfig:ro
      - /path/to/board-git-credentials:/home/board/.git-credentials:ro
      # SSH 远端也可只读挂载专用 .ssh（含 known_hosts），UID 10001 须有读取权限。
```

SSH 私钥如果属于 root 且为 0600，不要放宽私钥权限。推荐使用仓库提供的 `docker-compose.ssh-agent.yml`，仅挂载 agent socket 和 public known_hosts；agent 的 UID 必须与容器 UID 10001 一致（否则 OpenSSH 会拒绝不同 UID 的请求）。例如在宿主准备专用 agent：

```sh
nice -n 19 install -d -o 10001 -g 10001 -m 700 /run/v2-board-ssh
nice -n 19 setpriv --reuid 10001 --regid 10001 --clear-groups ssh-agent -a /run/v2-board-ssh/agent.sock -s
SSH_AUTH_SOCK=/run/v2-board-ssh/agent.sock nice -n 19 ssh-add ~/.ssh/id_ed25519
nice -n 19 install -m 644 /root/.ssh/known_hosts /run/v2-board-ssh/known_hosts
# 环境变量只包含路径，不含私钥内容；正式上线将 BOARD_PORT 改为 3160：
BOARD_SSH_AUTH_SOCK=/run/v2-board-ssh/agent.sock BOARD_SSH_KNOWN_HOSTS=/run/v2-board-ssh/known_hosts BOARD_PORT=3161 nice -n 19 docker compose -p board-runs -f docker-compose.yml -f docker-compose.ssh-agent.yml up -d
```

生产需由宿主服务管理这个 agent，重启后重新加载凭据。验收使用的临时 agent 会在 compose down 后停止，没有新增常驻认证服务；上述两个私钥/信任文件路径按本机实际 SSH 配置替换。也可只读挂载一个归属容器 UID、权限正确的专用 deploy key。镜像里没有密钥。

专用 gitconfig 使用 `credential.helper = store` 即可读取 `.git-credentials`。不挂载整个宿主 home；凭据不写入镜像、Dockerfile、仓库。可用 `V2_FETCH_ENABLED=0` 禁用远端采集。

## 容器预览与正式切换

多阶段 Dockerfile：Node 22 构建 → Python slim + Git + uvicorn；运行 UID/GID 10001，默认北京时间。Compose 服务名 `v2-board`，restart `unless-stopped`，healthcheck 请求 `/api/board`。默认只监听宿主 127.0.0.1:3161，容器内部端口 3160；端口映射可配置。

```sh
nice -n 19 docker compose -p board-runs build
nice -n 19 docker compose -p board-runs up -d
nice -n 19 curl -fsS http://127.0.0.1:3161/api/agents
nice -n 19 docker compose -p board-runs ps
nice -n 19 docker compose -p board-runs down
```

挂载：看板数据读写、`/srv/v0-ai` **只读**、mirror 独立命名卷。容器从 mirror 查询分支历史，无需挂载 `/root/wt`。`BOARD_DATA`、`BOARD_REPO` 可替换两个宿主目录，`BOARD_BIND` 可更改绑定地址。`start.sh` 已改为本 checkout 前台预览，默认 :3161，不终止任何已有进程。

**正式切换步骤（本次未执行，:3160 保持原 uvicorn）：**

1. 从当前分支 checkout 构建好镜像，先用 3161 预览并确认 healthy，然后 `docker compose -p board-runs down`。需要外部访问时设 `BOARD_BIND=0.0.0.0`，保持原反代兼容。
2. 记录 `nice -n 19 ss -ltnp 'sport = :3160'` 的 PID 和启动配置。若是 systemd/supervisor 管理，先关闭该服务的自动重启；本机当前是直接 uvicorn。
3. 仅向核对过的那个 :3160 uvicorn PID 发送 TERM，等端口释放，**不用 pkill 匹配进程**。
4. `BOARD_PORT=3160 BOARD_BIND=0.0.0.0 nice -n 19 docker compose -p board-runs up -d`（私有远端按上文增加 SSH agent override 和两个路径变量），curl `/api/board` / `/api/agents`，用 `docker compose ps` 确认 healthy。

回滚：`nice -n 19 docker compose -p board-runs down`（不加 `-v`，保留 mirror 与历史），再按原启动配置从 `/srv/v2-board/backend` 执行 `nice -n 19 ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 3160`。本次没有切换，也没有合并到 `/srv/v2-board` 主 checkout，所以原代码可直接恢复。源码回滚可对本分支相应提交执行 `git revert`，不要 reset 公共分支。

宿主采集回滚：

```sh
nice -n 19 sh scripts/uninstall-hooks.sh /srv/v0-ai
nice -n 19 cp -p /usr/local/bin/agent-run.bak-20261117 /usr/local/bin/agent-run
nice -n 19 cp -p /usr/local/bin/board-report.bak-20261117 /usr/local/bin/board-report
```

不删除 runs/events，旧看板仍可读任务卡。若需要恢复数据目录 ACL，安装前可 `getfacl -R -p` 保存，回滚用 `setfacl --restore`。

## 只读 API 与 SSE 协议

| 接口 | 内容 |
| --- | --- |
| `GET /api/board` | 原任务卡快照、地图、系统状态 |
| `GET /api/agents` | 归一身份、CLI 可用性、工作量、当前/最近 10 次执行、成功率、总产出、今日汇总 |
| `GET /api/runs?agent=&card=&state=` | 不覆盖历史的执行列表，agent 支持原名字或归一 ID |
| `GET /api/runs/{id}` | 执行详情 + 脱敏、去 ANSI 的日志尾部 80 行 |
| `GET /api/runs/{id}/messages?after_seq=N` | 消息增量，严格按 run 的 seq 排序 |
| `GET /api/events?since=` | since 支持事件 seq、id 或 ISO 时间戳 |
| `GET /api/stream` | 原 `board/sys`，新增 `runs/events` 快照与 `run:message/status/progress`、`git:commit/push`、`card:status/report` |

SSE 连接和重连都会发最新 runs 与最近 200 个 events。消息使用 REST `after_seq` 补齐；前端按 seq 合并去重，不依赖进程重启后会重置的 SSE id。慢消费者溢出重放窗口会收到 `resync`，重新读取快照与消息。日志 JSON 中提取 agent_message 文本；API 和持久结构化消息屏蔽常见 token、密钥、Authorization、密码、私钥、URL userinfo，原始日志只保留在宿主数据目录中。上表的接口都只读；写入只有下一节的两个干预接口，默认关闭。

## 在看板上干预（默认关闭）

看板默认只读。写入只有两件事，都只写任务卡文件，不直接启动、停止或恢复任何 Agent，后续由 dispatcher 按卡片状态处理。

| 接口 | 作用 |
| --- | --- |
| `POST /api/interventions/answer` | 回答 `dispatch: asking` 卡片里的问题：追加到 `## 回答`，全部答完才把 `dispatch` 改成 `answered` |
| `POST /api/interventions/repair` | 把一条未通过、部分通过或未完成的验收项指派给某个执行器：在 `fix/` 下新建一张 `REPAIR-*` 修复卡，原卡和原报告不动 |
| `POST /api/interventions/message` | 「Agent 对话」页里对 Agent 说话：追加到卡片的 `## 留言`。看板不碰 Agent 进程；dispatcher 在这一轮结束后用执行器的 `resume` 命令接着同一个会话送达，已完成或失败的会话会被重新排进队列（`answered`） |
| `POST /api/interventions/stop` | 「Agent 对话」页的「停止」：只在卡片 front matter 里记一个 `stop` 请求。看板碰不到 Agent 进程；dispatcher 下一次巡检时结束这一轮（先 TERM，`stop_grace_seconds` 秒后还在就 KILL），把会话置为 `held` 并标 `stopped`，worktree 和会话号保留。之后留言会接着同一个会话继续 |
| `GET /api/session`、`POST /api/login`、`POST /api/logout` | 登录。配了密码的实例，没登录时 `/api/` 下的读接口一律回 401 `login_required`，页面显示登录页；输对密码后发一个 30 天有效的 HttpOnly cookie（签名密钥由密码派生，不落盘，改密码即全部失效）。登录后的写操作不用再填令牌，仍然要过同源检查。脚本和其他 Agent 不用 cookie，带 `Authorization: Bearer <写入令牌>` 读写。同一来源 5 分钟内输错 8 次会被暂时拒绝；所有来源合计输错太多时只放慢应答，不会把别人锁在外面。实时连接（`/api/stream`）在登录失效后自动断开 |
| `POST /api/interventions/session` | 给某个执行器开一个新会话：在 `chat/` 下写一张 `dispatch: queued` 的卡，从 `V2_REPAIR_BASE` 开独立 worktree；需要同时开启指派修复 |

| 环境变量 | 说明 |
| --- | --- |
| `V2_INTERVENTION_MODE` | `readonly`（默认）、`demo` 或 `enabled`；后两者都必须同时显式设置 `V2_BOARD_DIR` |
| `V2_INTERVENTION_TOKEN` | `enabled` 模式必填，请求用 `Authorization: Bearer` 携带；不写进卡片和日志 |
| `V2_BOARD_PASSWORD` | 看板的登录密码。不配就用 `V2_INTERVENTION_TOKEN` 当密码；两个都没有（只读、演示实例）不要求登录。`enabled` 模式下它同时是写入凭据：用它登录的浏览器不带令牌就能回答、指派、留言、停止，要像令牌一样保管 |
| `V2_BOARD_LOGIN` | 设为 `0` 关掉登录，回到「谁都能看、写操作填令牌」 |
| `V2_REPAIR_ENABLED=1` | 在 `enabled` 模式下另外开启指派修复 |
| `V2_REPAIR_BASE` | 验收项没有指向看板上存在的任务卡时（没写「卡：」，或写的卡号在看板上找不到）修复分支的起点，比如线上部署所在的分支。不设就不能指派这类验收项；只在 `enabled` 模式生效 |
| `V2_REPAIR_REPO` | 只用于上面这类不挂原卡的修复卡：登记的仓库路径，写 dispatcher 所在机器上的绝对路径；不设时取 `V2_REPO_DIR`。基于原卡的修复不用它 |
| `V2_REPO_HOST_DIR` | 看板跑在容器里、且仓库在容器里的挂载路径和宿主不一样时设置：`V2_REPO_DIR` 对应的宿主绝对路径。基于原卡的修复在原卡没登记 `repo:` 时用它；不设时取 `V2_REPO_DIR` |
| `V2_BROWSER_EXECUTORS` | 逗号分隔的执行器名；只有列在这里的才标「浏览器验收已确认」，其余标「未确认」 |
| `V2_INTERVENTION_TRUST_PROXY_HEADERS=1` | 同源校验改用 `X-Forwarded-Host` / `X-Forwarded-Proto`。经反向代理访问时不设它，写入请求会因来源与 Host 不一致被 403 拒绝；只在看板只能经由可信反代到达时开启 |

容器部署时这些变量不会自动进入容器：`docker-compose.yml` 没有转发它们，需要在 compose 的 `environment` 里逐个声明，否则写入保持 `readonly`、执行器列表为空。`DISPATCH_CONFIG` 要填容器内能读到的路径，并把该文件挂载进去。

- 请求都校验同源、JSON 类型和大小；回答和指派都带卡片（及报告）的 SHA-256 版本，内容变了就拒绝并提示刷新。
- 修复卡的报错文字和截图由服务端从验收报告里读，不采信浏览器传来的内容；同一验收项重复指派只会有一张卡。
- 执行器列表取自 `DISPATCH_CONFIG` 里 `executors` 的键名，且该配置的 `board` 必须就是本看板目录；命令和配置内容不外传。
- 修复卡写 `dispatch: queued`，带独立分支 `d/repair-*`，以原卡分支为基线；dispatcher 有空槽位时为它建独立 worktree。原卡需要登记分支，仓库取原卡的 `repo` 或 `V2_REPO_DIR`。
- 「验收报告」页和任务卡的「验收」页签都能指派。报告里写了「卡：」的验收项基于那张卡（写了多张时在表单里选）；没写、或写的卡号在看板上找不到的验收项不挂原卡，修复卡以 `V2_REPAIR_BASE` 为基线、仓库取 `V2_REPAIR_REPO`，正文注明需要 Agent 自己定位代码（找不到的卡号会原样写进去）。验收项写了看板上存在的卡（按卡的 id 或「批次/文件名」认）时，不能绕过那张卡指派。
- 指派时勾选了浏览器验收，修复卡正文才带云浏览器验收步骤（`scripts/cloud-browser/run.sh`，真实接口或 `--fixture`）和回收沙盒的要求；没勾选时只写自动化测试要求。修复后的验收报告在「卡：」一行同时写修复卡和原卡编号，就会显示在原卡的验收页签里。
- `demo` 模式不要求令牌，所以有两道限制：数据目录必须带标记文件，并且只改写 front matter 里标了 `demo: true` 的卡片，其余卡片一律拒绝。`python3 scripts/setup_intervention_demo.py` 生成合成卡片、一份合成验收报告和截图；演示里的修复卡是 `held` 状态，不会被执行。

## 验证

```sh
nice -n 19 python3 -m unittest discover -s tests -v
nice -n 19 node tests/test_stream.mjs  # Node 22.18+ 原生 TS 类型擦除
# 在空闲 :3161 启动独立临时数据预览，自动清理；不启动浏览器：
nice -n 19 python3 tests/probe_realtime.py
# frontend 目录中：
nice -n 19 npx tsc --noEmit
nice -n 19 npx vite build
```

单测包括多进程追加/UTF-8/半行恢复、消息与心跳并发、PID/失联恢复、历史回填、身份归一、日志脱敏、临时仓库钩子时限与失败静默、sleep 2 假 agent、124 超时、真实 fake codex 结构化输出与显式进度，以及镜像抓取不会改本地仓库引用。SSE 集成探测验证消息 2 秒内送达、断线补齐无丢失无重复和所有新接口 200。按要求不启动浏览器，因此不声称已验收真实浏览器绘制延迟或视觉效果。

概念参考 [Multica Agent / daemon 架构](https://github.com/multica-ai/multica)：Agent 身份与工作量分离、每次执行保留 Run、包装 CLI 自动采集，不移植其业务代码。
