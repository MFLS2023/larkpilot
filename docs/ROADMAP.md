# feishu-bridge 路线图与决策记录

> 最后更新：2026-08-24。本文档是后续所有开发会话的对齐基准：
> 动手前先读这里，别靠对话记忆重建上下文。

---

## 一、项目定位

**一句话**：把你电脑上的所有 AI 编码助手（Claude Desktop / Claude Code / Codex / Pi）
装进飞书和一个网页——统一收件箱、手机续命、零 API 成本、默认安全。

**边界红线（不许破）**：
- 单人单机工具。不做多用户、不做 RBAC、不做 App、不自建 E2E 加密。
  这些是 Happy / Omnara 的战场，进去必输。
- 永不暴露公网。访问模型 = Tailscale 内网 + 密码单因子，仅在此前提下成立。
- 桥只读各客户端的原始记录，永不拥有/改写会话数据。
  这是「Codex 改格式、切供应商，数据一次没丢」的根基。

## 二、现状盘点（2026-08-24 实测）

已上线的能力：
- 飞书机器人（长连接）+ 手机网页（Flask :58080）双通道发任务，共享文件锁防串写
- 全盘扫描聚合：Claude Desktop 133 / Claude Code 85 / Codex 483 / Pi 14 个会话
- 解析器兼容 Codex 新旧两种 rollout 格式 + 分叉文件名双 UUID；宽容匹配
  （认「带角色的 message」不死抠外层类型名）+ parser_health 自检报警横幅
- 平台细分筛选（claude-desktop / claude-code / codex / pi，动态下发）+ 时间筛选
  （默认 12 小时，另有 1/3/7/30 天）；筛选选择存 localStorage
- 详情页 Markdown 渲染（marked + DOMPurify，CDN 失败退回纯文本）
- 状态判定：倒序取最新记录说了算；turn_aborted → 已手动中止；
  15 分钟无写入才标「没了下文」（文案不断言死活）
- 扫描缓存 per-level 各存一份，TTL 4 秒；切换筛选毫秒级
- 新建对话选引擎（claude/codex）；Pi 只读拦截
- 绑定码机制（默认拒绝，600 秒 TTL）；权限四档 full/write/read/chat

已有但别忽略的资产（2026-08-24 盘点目录所得，此前低估了完成度）：
- 7 个测试文件（test_bridge/e2e/lock/loop/parallel/security/web），但原六阶段
  计划里记过「修复测试假阳性」——存在但可信度未验证
- README.md、怎么用.md、PLAN.md（六阶段原计划）、企业通知怎么配.md
- LICENSE 已是 MIT；bridge_config / web_config 两份 example 配置已存在
- notify_to_enterprise.py 企业通知模块、config.py 配置层、tools/ 目录

已知短板（按疼的程度排序）：可靠性黑箱（电脑睡了/代理崩了/飞书断了分不清）、
进程管理纯人肉（每次改代码手动杀进程拉 vbs）、测试可信度未知（有文件没验证）、
扫描全量 IO（2.5 秒 @715 文件，随文件数线性恶化）、日志无轮转、
web 重启后 task_id 失效。

## 三、竞品格局（2026-08-24 GitHub 实查）

| 项目 | 星 | 一句话 |
|---|---|---|
| [slopus/happy](https://github.com/slopus/happy) | 23.4k | 手机 App 遥控 Claude/Codex，E2E 加密，MIT |
| [omnara-ai/omnara](https://github.com/omnara-ai/omnara) | 2.7k | 企业级 managed agents 平台，Apache-2.0 |
| [joewongjc/feishu-claude-code](https://github.com/joewongjc/feishu-claude-code) | 154 | 飞书流式卡片，复用 Max 订阅叙事 |
| [feir/feishu-bridge](https://github.com/feir/feishu-bridge) | - | **与本项目重名**！80+ 命令、pipx 安装 |
| [chatur-dev/sessiondeck](https://github.com/chatur-dev/sessiondeck) | - | ⚠️ 2026-08-24 补查发现：TUI 聚合浏览 Claude Code / Codex / Cursor / Pi / Gemini 等会话——**聚合赛道最近的直接竞品**，但只有本地 TUI 没有远程入口，差异化仍在 |
| Telegram 系若干（remotecode / teleclaw 等） | 14~17 | 会话接管、审批按钮；有的裸奔 skip-permissions |

**差异化（只有我们有的）**：① 全本机客户端聚合（别家只管自己拉起的会话）；
② 飞书+网页双通道共享锁纪律；③ 格式漂移攻防经验（宽容解析+自检横幅）；
④ 免装 App 的网页端；⑤ 权限四档细粒度。

## 四、工作梯队

> 2026-08-24 用户授权「全部推进、自主决策、文档留痕」，以下为当日执行结果。

### 第一梯队 · 开源门槛
- [x] 登录防爆破（2026-08-25）：同源连错 5 次封 15 分钟，成功即清零；
      实测第 6 次起 429 —— 开源安全清单上最后一项硬伤清零
- [x] 测试可信化：7 个既有套件实跑全绿
      （lock 39 / security 139 / web 33 / parallel 18 / bridge 124 / loop 35，
      配置文件哈希前后一致零污染）；新增 test_scanner_formats.py 11 项快照
      （老格式/新格式/双UUID分叉/pi/自检误报）。**e2e 有 1 项真失败**：
      机器人缺 im:message.group_msg 读权限 → 只能用户去飞书开发者后台加，
      见第七节待办
- [x] 文档更新：README 按 LarkPilot 品牌重写并随功能迭代补充；
      怎么用.md 未动（飞书配置教程仍然准确）；GIF 录制留给发布时
- [x] 改名定案 LarkPilot（GitHub 零占用实测）；本地目录名不改（会弄坏运行中的
      服务和启动脚本），仓库发布时用 larkpilot
- [x] 脱敏与 .gitignore（旧文件因未先读即覆盖而丢失内容，
      已按目录全量清单重建并在 git 历史中不可考——教训已记）
- [x] `manager.py` start/stop/restart/status/doctor（2026-08-25 完成）：
      pythonw 脱离终端启动、日志落各自 .log；stop 前查锁表防误掐（--force 强制）；
      doctor 七项体检（配置解析/凭据经 config.load_config 合并层检查/
      allowed_dirs 存在性/绑定文件/心跳新鲜度/端口）。实测踩坑：
      进程正则里分隔符要 [\\\\/] 不能 \\.（反斜杠不是点号），凭据必须查合并后的配置

### 第二梯队 · 竞争力
- [x] 心跳可见性（当日完成）：bridge 每 20 秒写 bridge_heartbeat.json
      （含到 open.feishu.cn:443 的连通探测），网页列表页顶部状态灯四态
- [x] 新建对话选工作目录（当日完成）：/api/workdirs 下发白名单内真实存在的目录，
      落盘前 safe_cwd 再校验一次；前端新建面板下拉选择并记住上次
- [x] 会话全文搜索（2026-08-25 完成）：session_search.py + FTS5(trigram)，
      按 mtime 增量、后台线程每 60 秒啃一批；中文子串可搜（≥3 字走 trigram，
      更短降级 LIKE）；网页 🔍 搜索框 + /api/search。
      实测：739 文件 23 秒建索引，「OpenRouter/pagefile/佛系万梅」全部命中。
      扫描冷启动加速暂不需要单独做——文件过千后再把列表元信息也进索引
- [x] git 版本控制（2026-08-25）：init + 首版快照 c5f8e41 + tag v0.1-internal；
      暂存清单逐个核对过，敏感文件零混入。教训：git init 前必须确认 cwd
      （曾误在 claude-files 建仓并开始 add，靠超时中断+删除空仓纠错）

### 第三梯队 · 追平竞品体验
- [x] 会话隐藏 + 回收站删除（2026-08-25 完成，用户选定档位）：
  隐藏只记名单零文件操作；彻底删除 = 文件移入 trash/ 保留 30 天自动清、
  可找回；详情页 🙈/🗑 两段确认，列表页 👁 管理视图；
  操作后即时清扫描缓存。红线处理：隐藏完全不动文件，
  删除是「移动」不是销毁——把对「只读不拥有」原则的破坏降到最低
- [x] 详情页顶栏标题与列表/CC 对齐（2026-08-25）：接口下发 title
  （摘要 > 首句 > 项目名）。实测 custom-title 摘要机制 70 个会话全部一致，
  不匹配的真因是顶栏此前显示项目目录名且接口未下发标题
- [x] Pi 可从手机续接（2026-08-25 完成，用户点名要求）：run_pi 走
      `pi -p --mode json`；**续接方式实测三选一只有 `--session <文件路径>` 可靠**
      —— 给 id 会触发跨项目 fork 交互卡死、--fork 在 -p 下不发请求（usage 全 0）。
      权限映射 chat→--no-tools / read→--tools read。供应商故障（stop=error 空响应）
      如实透传「换供应商」提示而不是误报格式问题。
      新建对话引擎选择也加了 π Pi
- [x] 任务完成推送：网页派的任务结束后自动推送到飞书绑定群，
      notify_feishu 可关；假客户端验证拼装
- [x] 权限拦截提示（审批卡片的可验证 v1，2026-08-25）：非 full 档位下引擎回复
      命中高置信短语 → 详情页横幅「切全开重试」。真·审批卡片仍属 v2：
      headless -p 无交互通道，需换 SDK 流式架构才能做到「先批再跑」
- [x] 图片直发·网页路径（2026-08-25）：POST /api/upload 存 uploads/
      （扩展名白名单 + 10MB 上限 + 时间戳重命名），路径拼进消息让引擎自己读图。
      飞书收图路径未做（需 im 资源下载权限，等后台加权限后一起）

### 计划项 · 多客户端配置化 —— 框架已完成（2026-08-25）
「哪些客户端可聚合、哪些可续接」已从代码变成 clients.json 配置：
- 字段：name / engine / dir / glob / extractor(envelope|claude) / can_run
- 文件缺失自动退回内置 Pi 条目；写坏不致命；clients.example.json 入库
- 续接名单动态计算：can_run 且 RUNNERS 里有 runner 才放行——
  配置声称能跑但没实现的自动降级只读（双向实测）
- 边界（诚实声明）：记录格式是全新家族仍需写一个抽取函数；
  CLI 协议全新仍需照 run_pi 模板写一个 runner 函数。框架消灭的是
  「接线」成本，不是「协议适配」成本
- 接入清单（照 pi 的路）：help 摸接口 → 要一份样本会话文件 → 写抽取 →
  三种续接方式逐一实测（pi 踩坑：id 卡死 / fork 空响应 / 只有路径可靠）
- [x] 流式输出·网页 v1（2026-08-25）：claude 引擎 --output-format stream-json
      增量解析，on_progress 回调写 TASKS[tid].partial，/api/task 下发，
      详情页提示条实时滚动最新 70 字。codex 未接（exec 无稳定事件流）；
      飞书侧流式卡片未做（需卡片更新 API，v2）。真实调用实测：回调触发、
      最终结果正确、test_bridge 124 项零回归
- [x] token 用量显示（2026-08-25）：get_usage(sid) 读尾部最近一轮——
      Codex 取 token_count 的 last/total 两级，Claude 取 message.usage
      （输入含缓存三件套）；详情接口下发 usage，标题栏 ↑入 ↓出 展示
- [ ] 语音转文字 —— **唯一未实施项，规格如下**（不写没验证过的盲代码）：
      1. 依赖：pip install faster-whisper + ffmpeg 进 PATH（模型 ~1GB 首跑下载）
      2. bridge.py 消息分发处加 msg_type=="audio" 分支：用 lark SDK 的
         GetMessageResourceRequest 下载 opus → ffmpeg 转 wav → faster-whisper
         transcribe(language="zh") → 当作文本走现有任务流
      3. 网页侧可选：MediaRecorder 录音上传 /api/upload 扩展音频类型
      4. 降级：依赖缺失时回复安装指引而不是报错
      5. 必须在装好依赖后用真实语音消息实测一遍才算完成

### 明确不做
多用户/RBAC、移动 App、自建 E2E 加密、追 Happy 的全平台化。

## 五、传播策略要点

1. 「白嫖」放标题：复用 Max 订阅、零 API 成本是第一传播点
2. 五分钟跑起来：clone → 一条命令 → 手机收到消息，每多一步流失一半人
3. README 第一屏必须是 GIF（手机派活→电脑干活→手机收结果）
4. 首发 linux.do（分享创造节点），V2EX 只做放大器
5. 默认安全的姿态是道德高地：对比裸奔 skip-permissions 的竞品要明说
6. 前两周 issue 当天回，比多做三个功能更能留住星数走势

## 六、技术备忘（给未来开发会话）

### 环境事实：地址使用规则与诊断修正（2026-08-26 最终版）
**两个地址的适用范围（用户规则卡）**：
- `192.168.1.111:58080` —— 只在家里（同一 Wi-Fi）有效；私网地址出门必然连不上，
  这是性质不是故障。实测到家即恢复。
- `100.111.212.2:58080` / **`jinmo`（MagicDNS 名）** —— 出门用，需手机 Tailscale 开启。
  MagicDNS 已开启（tail6a214c.ts.net），推荐手机收藏 `http://jinmo:58080` 一个通吃。

### 诊断修正记录（诚实留痕）
曾诊断「家里路由器 AP 隔离」——后被新证据推翻：用户到家后 192.168.1.111 恢复可用，
说明家庭内设备通信正常，隔离不存在或已无关。复盘统一解释：
1. 「在家怎么刷都不出来」的主犯是 list.html 的 JS 崩溃 bug（已修复）——
   页面框架在但列表永远转圈，观感等同打不开，且与网络无关地持续存在；
2. 「下午在外用不了」是用了只在家有效的局域网地址；
3. 在家时 Tailscale 打洞可能受 Mihomo TUN 干扰而偏慢（次要因素）。
教训：多现象归因时，先排除应用层 bug 再下网络结论；私网地址的适用范围
要在给出地址时就向用户说明。

### 2026-08-25 全项目 Review 修复记录（7 项，全部实测回归通过）
1. 流式路径把「进程没起来/中途崩」伪装成空回复成功 —— 已改为如实报错，
   超时和中途退出都会带上已生成的部分内容
2. manager doctor 的结论翻转 bug：网络不通时 `ok=True` 会把前面真实失败项
   翻成"一切正常" —— 已删掉复位逻辑，网络问题照常计入失败
3. manager 端口写死 58080 —— 改为从 web_config.json 读（web_port()）
4. 发送关键路径 `_engine_of` 的兜底全盘扫（2.5 秒）—— 改用 scan_all_cached
5. manager stop web 不查锁表 —— 改为停任何服务都先拦（--force 跳过）
6. uploads/ 只进不出 —— 上传后顺手清 7 天前旧图
7. parser_health 同步扫描会卡住那一次列表轮询 —— 改为后台线程刷新、
   调用方立即拿旧值（首查给空）；test_scanner_formats 断言已同步改轮询等待

已知但刻意不修（记录在案）：bridge_state.json 双进程整读整写有理论竞态
（低频：仅续接换 sid 时写）；check_dir 黑名单 ".git\\config" 只防反斜杠形式；
make_handler 的 seen 去重集合超 500 全清（重推窗口极小）。

### 其余备忘

- Codex rollout 2026-08 新格式：对话在 `response_item/message`
  （文本在 content 块 text 字段），`event_msg/user_message` 可能整份缺席；
  user 消息混 AGENTS.md 与 `<environment_context>` 注入必须过滤；
  切供应商续接产生 `rollout-时间-旧id_新id.jsonl` 双 UUID 文件名，
  sid 提取必须正则锚定结尾完整 UUID
- 解析原则：宽容匹配（认消息形状不认类型名）+ parser_health 自检兜底
  （>20KB 且 >8 行却无可识别消息 = 报警横幅），10 分钟缓存
- stalled 判定 STALLED_AFTER=900s；状态语义「没了下文」不断言死活；
  倒序扫描最新记录说了算（勿再引入家族优先级）
- RECENT_WITHIN=43200（12 小时，2026-08-24 用户定的）；days 参数支持小数
  （0.5=12h）；时间筛选无「跟随标签」，第一档固定 12 小时
- 服务重启流程：查 session_locks.json 是否 `{}` → 杀 python 进程 →
  Startup 目录两个 vbs（飞书桥.vbs / 网页监控.vbs）→ curl 验证 HTTP 200
- 后台守望脚本会随 Claude Code 会话退出而被杀（已发生 3 次），
  output 文件显示 [killed] ≠ 任务失败，先看输出再下结论
- 本机环境：WebFetch 常被企业策略拦 → 用 fetchWebContent；
  无 jq；Bash 工具 cwd 会漂移，python 脚本记得带 cd 前缀
- 敏感面：web_config.json 含 password_hash + session_secret（值永不打印）；
  bridge_state.json 含飞书 uid/chat 绑定；.env 存在（2026-08-22 建，内容勿读值勿入库）；
  bridge_auth.json 为绑定数据；以上全部绝不入库

## 七、已拍板事项

- [x] **项目名定为 LarkPilot**（2026-08-24 GitHub 实查零占用；Lark=飞书国际名，
      Pilot=驾驶所有 AI 助手）。PyPI 占用发布注册时再核实。
      开源改造时仓库/文档统一用此名。
- [x] LICENSE —— MIT（已有）
- [x] **默认权限反转已批准**（2026-08-24）：出厂默认 read +
      本机 web_config.json 显式 full，自己体验不变。当日已实施。
