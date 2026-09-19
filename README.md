# LarkPilot —— 你所有 AI 编码助手的遥控器

在外面用手机，也能看到电脑上**所有**正在跑的 AI 会话（Claude Desktop / Claude Code / Codex / Pi），
接着发指令让它继续干活。复用你现有的 CLI 登录态，**零 API 成本**。

两条通道并存：**飞书**负责叫你和下指令，**网页**负责看和操作（列表、对话历史、发消息）。
两条通道共用一把会话锁，不会互相打架。

> ⚠️ 定位红线：这是单人单机工具。密码是唯一防线，请只在 Tailscale / 局域网等
> 私有网络里用，不要暴露公网。

---

## 效果

**网页端（手机浏览器）：**

```
🟢 桥在线 · 到飞书网络正常
[平台·全] [Claude客户端] [Claude Code] [Codex] [π Pi]
[12 小时] [1 天] [3 天] [7 天] [30 天]

🟢 知识库        · Claude Code · 3 秒前 · 正在跑
   你说：帮我把 session_scanner 里的 _tail 改成…

🟡 claude-files  · Codex · 22 分钟前 · 已完成
   # claude-files 目录分类清单 总计 854 个文件…
```

点进去看完整对话历史（Markdown 渲染，代码块表格都正常显示），底部输入框发消息。

**飞书端：**

```
你：/列表
机器人：当前会话（10 个）：
         1. 🟢 知识库 ［Claude Code］
            正在跑 · 3 秒前 · 5bf315ae

你：1 接着把测试补上
机器人：给窗口 1「知识库」发消息，在做了…
（几分钟后）
机器人：［知识库］测试已补全，共 12 个新用例……
```

---

## 能做什么

- **全本机聚合**：一个列表收齐 Claude Desktop、Claude Code、Codex、Pi 的所有会话，
  按活跃度分 🟢🟡⚪ 三档
- 平台细分筛选 + 时间筛选（默认最近 12 小时，可选 1/3/7/30 天）
- 🔍 全文搜索：跨所有会话的内容里找关键词，中文子串可搜（SQLite FTS 索引，
  后台增量更新）
- 详情页完整对话历史（Markdown 渲染），发消息接着聊；顶栏标题与 Claude Code 一致
- 新建对话时选引擎（Claude / Codex / Pi）、选工作目录（白名单内）
- 权限四档：全开 / 改文件 / 只读 / 聊天（**出厂默认只读**，自己机器可配 full）
- 🙈 隐藏 / 🗑 回收站删除：隐藏零风险可随时恢复；彻底删除移入回收站保留
  30 天自动清空，期间可找回
- 飞书双向：`/列表` `/新` `/切`，序号直达；网页派的任务完成后自动推送到绑定群
- 心跳灯：桥挂了 / 到飞书的网络断了，列表页第一眼就能看到
- 📎 图片直发：手机选一张截图随消息发出，AI 读图后作答（≤10MB）
- 流式预览：任务跑动时提示条实时滚动 AI 的最新输出，不用干等
- token 用量：详情页标题栏显示最近一轮 ↑输入 ↓输出（Codex 另有累计值）
- 解析器自检：上游改格式导致读不出会话时，页面顶部横幅报警而不是默默空白
- 飞书和网页共用会话锁，防止两边同时操作同一会话导致上下文乱掉

### Zcode（只读接入）

自动读取本机 `~/.zcode/cli/db/db.sqlite`：支持未归档会话列表、标题和最近历史文本。
数据库以只读模式打开，不改写会话；不展示工具输出和推理内容。

- 当前只支持 Zcode CLI 数据库，不包括 v2 桌面端导入快照。
- 不能新建或续接 Zcode 会话；网页和服务端均拒绝发送任务。
- 支持隐藏，不支持删除：多个会话共用同一个数据库，不能把整库移入回收站。
- 暂不支持 Zcode 全文搜索、token 统计、真实运行状态判断。
- 数据库缺失、损坏或结构不兼容时返回空结果，尚无专用故障横幅。

离线验证：`python -m unittest test_zcode_sessions test_zcode_http test_extra_sources -v`。
测试仅使用临时数据库和本机临时 HTTP 服务，不调用 AI 或发送飞书消息。

### Antigravity（只读接入）

自动读取本机 `~/.gemini/antigravity/`：`conversation_summaries.db` 提供会话清单
（37+ 个，标题/预览/时间），每个会话的内容在其独立数据库
`conversations/<uuid>.db` 里，以只读模式逐条打开。

- 对话内容是私有 protobuf 编码，靠裸 wire-format 解析提取用户/助手可见文本；
  上游改字段号会导致取不到文本（给空列表），不会报错崩溃。
- 不展示工具调用和推理内容；不能新建或续接会话，网页和服务端均拒绝发送。
- 会话号和 Claude 一样是 uuid：详情页先查 Antigravity 摘要库认领，认不下的
  才落回 Claude 的查找；删除出口双层封死（前端禁用 + 服务端按摘要库认领拒绝）。
- 支持隐藏，不支持删除；暂不支持全文搜索、token 统计、真实运行状态判断。

离线验证：`python -m unittest test_antigravity_sessions -v`（6 项）。

### 接入其他 AI 工具（多客户端配置化）

来源清单在 `clients.json`（参考 `clients.example.json`）：

```json
{ "sources": [{
    "name": "Pi", "engine": "pi",
    "dir": "~/.pi/agent/sessions", "glob": "**/*.jsonl",
    "extractor": "envelope", "can_run": true
}]}
```

- `extractor`：`envelope`（pi 式，顶层 `type:"message"`）或 `claude`
  （Claude Code 式，顶层 `type:"user"/"assistant"`）二选一；
  全新格式需在 session_scanner 里加一个抽取函数
- `can_run: true` 且 bridge 注册了对应 runner 才能从手机续接，
  否则自动降级为只读来源
- Claude Code 的套壳工具通常直接复用 `~/.claude/projects`，无需配置即自动出现

服务管理不用记命令行参数：

```bash
python manager.py start | stop | restart | status | doctor
```

## 做不到什么（硬限制，不是没做）

1. **操控不了你屏幕上那个窗口。** 「发消息」是新起 `claude -p -r <id>` 进程续接上下文，
   结果不会出现在屏幕上已开着的窗口里。
2. **判断不了「窗口还开着吗」。** 活跃度按文件修改时间估算，所以文案是「没了下文」
   而不是武断的「断了」。
3. **Claude Desktop 的云端会话看不到。** 只能看它落在本地的部分。

---

## 安装

### 前提条件

- Python 3.8+
- Claude Code 已安装（要用 Codex 就再装 Codex CLI）
- 飞书企业自建应用（免费，见 [怎么用.md](怎么用.md) 教程）

```bash
git clone https://github.com/你的用户名/larkpilot.git
cd larkpilot
pip install -r requirements.txt

cp bridge_config.example.json bridge_config.json   # 非敏感配置（按需改 cwd 等）
cp clients.example.json clients.json               # 可选：第三方来源（Pi 等）

# 飞书凭据放 .env 文件（两行：FEISHU_APP_ID=xxx、FEISHU_APP_SECRET=xxx），
# 详见 怎么用.md 的第二步和第三步

python bridge.py        # 飞书长连接
python web_server.py    # 网页（默认 :8000，可在 web_config.json 改端口）
```

第一次打开网页会引导设置密码。**首次使用记得生成绑定码把自己绑定上**
（`python auth.py --code`，在飞书里把码发给机器人）——默认谁都不许用，
这是故意的。飞书应用从零到跑通的完整教程见 [怎么用.md](怎么用.md)。

---

## 测试

```bash
python test_scanner_formats.py   # 解析器格式快照（老/新/分叉/pi 四种样本）
python test_bridge.py            # 逻辑测试（会真调 claude 和 codex 各一次）
python test_parallel.py          # 并发压力测试
python test_lock.py              # 会话锁测试（含跨进程）
python test_web.py               # Web 集成测试（需要 web_server.py 跑着）
python test_security.py          # 安全与权限测试
python test_loop.py              # 飞书闭环测试（往飞书发真实消息）
python test_e2e.py               # 端到端（自建群自测；读回消息需后台加 im:message.group_msg 权限）
```

## 配置说明

### bridge_config.json（飞书侧）

| 字段 | 说明 |
|---|---|
| `app_id` / `app_secret` | 飞书应用凭据 |
| `cwd` | 默认工作目录 |
| `allowed_dirs` | 工作目录白名单，新建对话只能选这里面的 |
| `permission` | 飞书侧默认权限档位（出厂 `read`） |
| `timeout_seconds` | 单个任务最长跑多久（秒） |

### web_config.json（网页侧）

| 字段 | 说明 |
|---|---|
| `port` | 监听端口（出厂 8000） |
| `default_perm` | 网页发消息的默认权限档位，出厂 `read`；想要顺手全开就写 `full` |
| `notify_feishu` | 网页派的任务完成后要不要推送到飞书绑定群，默认 `true` |
| `task_timeout` | 任务子进程超时（秒） |

---

## 安全说明

- 密码只存哈希（werkzeug），不存明文；登录连错 5 次封 15 分钟
- 出厂权限默认是**只读**：陌生环境第一次跑不会自带删文件的能力。
  自己的机器在两个配置文件里显式写 `full`
- 所有配置、日志、绑定关系文件都在 `.gitignore` 里，不会被提交
- 危险操作默认需二次确认；工作目录永远过白名单校验

## License

MIT
