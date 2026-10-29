# CLAUDE.md — 每日交易复盘机器人

本文件给接手这个项目的 Claude 看：项目背景、已经定下的决定、当前进度和下一步。

> **所有回复一律用中文**，包括说明、提问、选项、总结，每一条都是，不要中途切成英文。代码、命令、文件名保持原样。

## 用户
- 不写代码。所有操作都要给能直接复制的命令，并解释每一步在做什么。
- 日内交易，只做 **MNQ / MES**，只在 **美股盘中 09:30–16:00 ET** 交易，5 分钟图入场。
- 打法按市场状态切换：大级别趋势 → 顺大逆小（等 5m 回调顺大趋势进）；大级别区间 → 做区间内的 5m 趋势；5m 震荡 → 高抛低吸。
- 账户：FundedNext（FN）期货考试盘，底层是 Tradovate；用 TradingView 看图下单。
- 住在新西兰奥克兰。复盘在美东 16:15 运行（新西兰早上 9:15 / 美国冬令时期间 10:15）。

## 已定下的决定（不要推翻，除非用户提出）
1. **全程免费**。不用 CrossTrade 等付费 API，不用付费行情。
2. **全自动**，不要求用户每天手动导出任何东西。
3. **AI 用 Gemini**（免费额度只有 Flash / Flash-Lite），日常运行不消耗 Claude 用量。
4. **复盘只看用户自己的操作**，不管考试规则。提示词在 `prompts/review.md`，用户以后会自己改。
5. **推送到用户自己的私人 Discord 服务器**（webhook）。
6. **最终效果：直接在 5 分钟 K 线上评价每一次出手/出场**。图由代码自己画（TradingView 开源 lightweight-charts），不截 TradingView。
   - 原因一：TradingView 明确禁止自动化使用，会封号。
   - 原因二：截图没法把点评精确贴到对应 K 线旁边。
7. **群聊读取**：夜航社 Discord 的一个频道。管理员不会加 bot，这个频道需要会员身份，用户没有小号，所以只能用**用户大号、已登录的 Discord 网页版、无头浏览器**读取。
   - 这违反 Discord 条款，用户已知情并接受风险。
   - 每天只读一次，只读这一个频道，只读不发，按人工速度翻页。
   - 不用 user-token 类的 self-bot MCP，那种封号风险更高。
8. **运行在用户自己的 Linux 服务器上**。服务器在用户另一个家里，是家庭宽带 IP。
   - 第一次登录需要的人工步骤（验证码、二次验证）在用户的 Mac 上完成。
   - 登录状态保存成文件，再拷到服务器上。

## 图表规范（用户偏好）
- 国际配色：**绿涨红跌**。用户说中文，但不要用 A 股配色。
- 标注：按时间顺序（K 线从左到右）编号 ①②③。标签放在对应 K 线旁边，箭头指向具体 K 线或成交价；图下方的文字列表用同样的编号和顺序。

## 代码结构
| 路径 | 作用 |
|---|---|
| `reviewbot/main.py` | 主流程：成交 → 交易 → 进出场事件 → 原图给 Gemini → 点评 → 标注图 + 卡片 → Discord |
| `reviewbot/trades.py` | 成交按合约配对成完整交易（flat→flat，支持加减仓、反手），盘中过滤，统计 |
| `reviewbot/events.py` | 每次进场/加仓/减仓/平仓 = 一个事件，全天按时间编号；`describe_day` 生成给 AI 的文字 |
| `reviewbot/market.py` | yfinance 拉 `MNQ=F`/`MES=F` 5 分钟线，计算前日高低（PDH/PDL）、隔夜高低（ONH/ONL）；`check_fills_in_range` 提示换月价差 |
| `reviewbot/chart.py` + `templates/chart.html.j2` | K 线标注图。标签按时间从左到右排，避开 K 线，箭头指向成交价 |
| `reviewbot/gemini_review.py` | 提示词组装（`%%` 开头的行是备注，`{{X}}` 是占位符），结构化输出 `Review`，每个事件一条 `EventComment(n, verdict, tag≤12字, comment)` |
| `reviewbot/card.py` + `templates/card.html.j2` | 总结卡片：盈亏表、当日环境、评分汇总、群聊观点、明日要点 |
| `reviewbot/push.py` | Discord webhook 发图 |
| `login.sh` + `reviewbot/login.py` | **在 Mac 上运行**：用真 Chrome（独立 profile `secrets/mac-profile`）手动登录，按网站分别保存 `secrets/<site>_state.json`（cookie+localStorage，Tradovate 含 IndexedDB）、`_session.json`（sessionStorage）、`_info.json`（只记键名，用来分析登录存在哪），再 scp 到服务器并检查。服务器地址在 Mac 的 `.env`（`SERVER_SSH` 等） |
| `reviewbot/tradovate.py` | 当天成交：监听网页版 `user/syncrequest`，解析 fills，按天缓存到 `data/fills/`（Tradovate 只给当前交易日，美东 18:00 换日） |
| `reviewbot/session.py` | 服务器端：`open_page(root, site)` 带登录打开无头页面（第 3、4 步用它）；`check`/`require` 判断登录是否有效，有效时把刷新后的 cookie 写回；`python -m reviewbot.session check` 失效时推送提醒 |
| `reviewbot/sample.py`, `samples/` | 示例数据（`--demo`），成交价取自示例 K 线 |
| `install.sh`, `run.sh`, `deploy/` | Ubuntu/Debian 一键安装，systemd 定时 `Mon..Fri 16:15 America/New_York` |
| `reviewbot/vendor/` | lightweight-charts 5.2.1（Apache-2.0），不要删 LICENSE |

## 当前进度
- ✅ 成交配对、事件编号、K 线标注图、总结卡片、Gemini 调用、Discord 推送、安装脚本、定时任务。用示例数据跑通（`--demo --offline`）。
- ✅ 已部署到用户服务器（Debian 13，root 用户，`/root/trade-review-bot`），2026-10-28 实测通过：真实 Gemini 调用、webhook 推送、yfinance 行情（10/27 MNQ/MES 各 78 根）、中文字体渲染。
- ✅ Mac 登录工具 + 服务器登录检查。Tradovate token 约 80 分钟过期且**过期后网页无法恢复**，所以 `review-bot-keepalive.timer` 每 20 分钟调用 `/v1/auth/renewaccesstoken` 续期（失效只提醒一次，标记文件 `secrets/tradovate_alerted`）。服务器断网超过约 80 分钟就要用户重新 `./login.sh tradovate`。
- ✅ Tradovate 成交抓取：2026-10-29 用真实成交跑通全流程（MES 14 次进出场，字段与官方文档一致），已推送。
- ✅ 两个定时任务已在服务器启用：`review-bot.timer`（Mon..Fri 16:15 ET）、`review-bot-keepalive.timer`（每 20 分钟）。
- ⚠️ **K 线图标注在出手多的日子会乱**：10/29 有 14 个编号集中在 10:00–13:10，标签被铺满整个宽度，引线很长且互相交叉，⑤ 的标签被 ⑬ 盖住。`chart.py` 的布局需要改成：标签尽量贴近自己的 K 线、允许上下多行、禁止重叠。
- ⏳ `main.get_chat_knowledge()`：Discord 群聊读取还没写。

## 下一步（按顺序）
1. **修 K 线图标注布局**（见上面 ⚠️），用 `output/2026-10-29` 的真实数据（`data/fills/2026-10-29.json`）复现。
2. **Discord 读取**：用 `session.open_page(root, "discord")` 打开那个频道，往上翻到当天开盘前，提取文字（作者、时间、内容）。用 Gemini Flash-Lite 筛出与行情或交易相关的观点，填入 `{{CHAT_KNOWLEDGE}}`。可以顺便积累到长期知识库文件。这和用户的另一个项目"蒸馏夜航社交易员 junkman 的交易思路"可以共用。先对着真实页面看结构再写，选择器不要凭空猜。
3. **提示词**（用户说先放后面）：已经问过用户 6 个问题（大级别怎么判断、回调进场条件、止损、出场、不做的情况、点评风格），还没回答。同时可以改代码给 AI 补数据：A. 多日 1 小时 K 线做大级别背景；B. 当天 5 分钟 K 线表格让价位精确；C. 从 Tradovate 同步数据的 `orders` 读真实止损/止盈；D. 提示词要求不推测数据里没有的信息。

## 常用命令
```bash
./run.sh --demo --offline --no-push   # 示例数据、示例点评、不推送
./run.sh --demo                        # 示例数据 + 真实 Gemini + 推送
./run.sh --date 2026-10-27             # 复盘某天
systemctl list-timers review-bot.timer # 下次定时运行
tail -50 output/run.log
```
本地没有 Playwright 自带的 Chromium 时，设置 `CHROMIUM_PATH=/path/to/chrome`。

## 坑
- lightweight-charts 把时间戳当 UTC 显示，所以 K 线时间一律用"美东墙上时间当作 UTC"编码（`market.wall_epoch`）。
- 标注层 `#ov` 必须 `z-index:20`，否则会被图表画布盖住。
- 渲染 HTML 时传给 Playwright 的必须是绝对路径。
- Gemini 可能漏掉编号或多编号，`main.complete_review` 会补齐并去掉多余的，别删。
- Yahoo 用主力连续合约，换月那一周成交价可能和 K 线对不上，推送里会出现提示。
- 服务器需要中文字体（`fonts-noto-cjk`），`install.sh` 已经装了。
- 用户的路由器会断开约 60 秒没有输出的 SSH 连接。耗时命令（安装、pip）用 `nohup ... &` 在服务器后台跑，SSH 加 `-o ServerAliveInterval=15`。
- 仓库是公开的：服务器地址、端口、密码一类信息不要写进仓库文件。
- 判断登录：出现登录页 URL 或密码框 = 失效。Discord 未登录时约 4 秒以上才跳登录页，所以 Discord 还要求看到 `/api/v*/users/@me*` 返回 200 才算有效。Tradovate 暂时只靠密码框判断，第 3 步看到真实页面后补一个可靠的"已登录证据"。
- **Tradovate 网页版的登录在 sessionStorage `api_authenticator_state`**（含 token、`expiration`、`apiHosts`），token 约 80 分钟过期，页面开着时会自己续。cookie 里没有登录。`save_state` 每次都把续过的 token 写回 `_session.json`。网页加载后会调 `live/demo.tradovateapi.com/v1/...`（FN 考试盘在 **demo**：`account/list`、`cashBalance/getcashbalancesnapshot`），第 3 步可以直接监听这些接口的响应拿成交，不用扒 DOM。
- Discord 会删掉页面里的 `window.localStorage/sessionStorage`，但 Playwright 的 `storage_state` 仍能读到 localStorage。
- Gemini Flash 会编造一些图上看不出来的细节，比如"剩余仓位保本"。点评的 tag 也经常超过 12 个字，不过目前排版没问题。
