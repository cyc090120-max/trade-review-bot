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
- 标注：**不用数字，用"买 / 卖"标签**（买蓝、卖红，带张数，外圈颜色 = AI 评价）。全天总览只放买卖标签；每笔 1 分钟特写 = 买卖标签 + 一个从进场到出场的交易框（赚绿亏红）。点评在特写图下面，以"买 / 卖"开头。参考了 TradingView 成交标记、TradeZella、LinnSoft TradeViewer 的画法。
- **每张图限量**：最多 7 条关键位线、3 个 FVG/OB 色块，按重要程度和离价格远近挑；靠太近的线合并；1H 大周期图只画大周期的东西。用户明确说过图太乱"根本没法看"。
- **交易逻辑是 SMC / ICT，只学了核心概念：流动性获取、CISD、FVG、OB**（加关键位、缺口）。**不要**拿溢价折价、银弹窗口、Killzone 这类细节要求用户。用户希望复盘顺便**教学**（`lessons` → 卡片"今日教学"）。
- MES 的整数关口对照 **SPX 每 10 点**（程序按当天 MES−SPX 基差换算，约 +35 点）。
- 用户看图会参考 5m/15m/1H/4H/日线，主要看阻力和缺口（前日/前周高低、隔夜/亚盘/伦敦高低、大周期前高前低、整数关口、VWAP、K 线前高），1 分钟看 K 线表现。常用操作：扫流动性 + 1m 反转进场；大周期阻力/支撑止盈；进场后 1m 不对劲就走。复盘必须**站在当时**评价，事后走势只能放 hindsight。

## 代码结构
| 路径 | 作用 |
|---|---|
| `reviewbot/main.py` | 主流程：成交 → 交易 → 进出场事件 → 原图给 Gemini → 点评 → 标注图 + 卡片 → Discord |
| `reviewbot/trades.py` | 成交按合约配对成完整交易（flat→flat，支持加减仓、反手），盘中过滤，统计 |
| `reviewbot/events.py` | 每次进场/加仓/减仓/平仓 = 一个事件，全天按时间编号；`describe_day` 生成给 AI 的文字 |
| `reviewbot/market.py` | yfinance 免费行情：当天 5m、1m（只保留约 30 天）、开盘前 1H；开盘前已知的关键位：前日/前周高低、前日收（缺口回补位）、隔夜/亚盘/伦敦高低、15m/1H/4H/日线未突破的前高前低、整数关口；1m 算 VWAP。同价位合并成一条（名字用 / 连接）|
| `reviewbot/smc.py` | SMC 概念的固定定义：FVG/IFVG、OB、CISD、区域状态跟踪（已回踩 / 回补到 CE / 反转 / 失效） |
| `reviewbot/context.py` | **站在当时看**：每次出手时可见的关键位（加上盘中 5m/15m 前高低、盘中高低、VWAP）、前 20 分钟的流动性扫荡（刺破后 3 根内收回）、出手前或持仓中的 1m K 线、浮盈浮亏；事后 30 分钟单独标"仅供参考" |
| `reviewbot/chart.py` + `templates/chart.html.j2` | 一个模板出三种图：全天 5m 总览（只标编号圆圈）、开盘前 1H 大周期背景、每笔交易的 1m 特写（下面附该笔点评，大字号，手机能看清；没有 1m 或持仓太久就用 5m） |
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
- ✅ SMC 版（2026-10-30）：`smc.py` 按 LuxAlgo 概念库的定义算 FVG/IFVG/OB/CISD（ICT 没有官方定义，取引用最多的公开版本，每项只用一种约定：FVG 实体收穿对侧变 IFVG；OB = 位移前最后一根反向 K 线、含影线、位移=留 FVG 且打穿前 10 根极值；CISD = 进场前最近 10 根里推到极值那段连续同向 K 线的第一根开盘价，实体收破才算）。另有 NDOG/NWOG/开盘缺口、EQH/EQL、ICT 亚盘 20–00 / 伦敦 02–05。1m 特写画 1m FVG/OB。已删除溢价折价、时段窗口、MSS。
- ✅ 复盘改版（2026-10-30）：站在当时评价 + 多周期关键位 + 1m 特写。用 10/29 真实数据在服务器上跑过并推送，点评能认出扫流动性反转、关键位止盈、1m 不对劲离场。
- ⏳ `main.get_chat_knowledge()`：Discord 群聊读取还没写。

## 下一步（按顺序）
1. **Discord 读取**：用 `session.open_page(root, "discord")` 打开那个频道，往上翻到当天开盘前，提取文字（作者、时间、内容）。用 Gemini Flash-Lite 筛出与行情或交易相关的观点，填入 `{{CHAT_KNOWLEDGE}}`。可以顺便积累到长期知识库文件。这和用户的另一个项目"蒸馏夜航社交易员 junkman 的交易思路"可以共用。先对着真实页面看结构再写，选择器不要凭空猜。
2. **从 Tradovate 读真实止损/止盈**：同步数据里有 `orders`/`orderVersions`，先看真实结构，再把每笔的止损位置交给 AI。
3. **提示词细调**（用户说先放后面）：还没问完的有止损规则、出场规则、不做的情况、点评风格（要不要打分）。用户以后自己改 `prompts/review.md`。

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
