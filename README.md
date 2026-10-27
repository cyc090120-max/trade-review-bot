# 每日交易复盘机器人

每个交易日美东 16:15（新西兰时间早上 9:15，美国冬令时期间是 10:15）自动运行：

1. 抓取当天 FundedNext 账户在 Tradovate 上的成交，配对成一笔笔完整交易，只保留美股盘中（09:30–16:00 ET）开仓的
2. 把每一次出手 / 加仓 / 减仓 / 出场按时间编号 ①②③…
3. 拉取当天 MNQ / MES 的 5 分钟 K 线（免费的 Yahoo 行情），标出前日高低点、隔夜高低点
4. 读取 Discord 指定频道当天的消息，筛出有用观点
5. 把带编号的 K 线图交给 Gemini，按 `prompts/review.md` 的要求逐个点评
6. 生成最终图片推送到你的私人 Discord 频道：
   - **K 线标注图**：每个编号贴在对应 K 线旁边、箭头指向成交价，带结论和短评；图下方按同样编号列出完整点评
   - **总结卡片**：盈亏统计、当日环境、打法匹配、评分汇总、群聊观点、明日要点

## 当前进度

| 部分 | 状态 |
|---|---|
| 成交配对、进出场编号、盈亏统计 | ✅ 完成 |
| 5 分钟 K 线与关键位（Yahoo 免费行情） | ✅ 完成（需在服务器上实测） |
| Gemini 逐个进出场点评（读提示词文件 + 看图） | ✅ 完成 |
| K 线标注图 + 总结卡片 + Discord 推送 | ✅ 完成 |
| 安装脚本 + 定时运行 | ✅ 完成 |
| Tradovate 成交自动抓取 | ⏳ 下一步 |
| Discord 群聊读取 | ⏳ |

注：Yahoo 行情用的是主力连续合约。换月那一周，如果你交易的合约和主力不同，成交点可能和 K 线对不上，推送里会出现提示。

在接入真实数据之前，可以用 `./run.sh --demo` 跑示例数据，验证 Gemini 和推送都正常。

## 安装（在服务器上）

```bash
git clone <你的仓库地址> trade-review-bot
cd trade-review-bot
./install.sh
nano .env          # 填入 GEMINI_API_KEY 和 DISCORD_WEBHOOK_URL，Ctrl+O 保存，Ctrl+X 退出
./run.sh --demo    # 用示例数据测试一次，Discord 里应该收到两张图
```

## 常用命令

| 想做什么 | 命令 |
|---|---|
| 用示例数据测试（会调用 Gemini、会推送） | `./run.sh --demo` |
| 示例数据，不调用 Gemini、不推送 | `./run.sh --demo --offline --no-push` |
| 重新复盘某一天 | `./run.sh --date 2026-10-27` |
| 只生成图片不推送 | 加上 `--no-push` |
| 更新代码 | `git pull` |
| 查看下次自动运行时间 | `systemctl list-timers review-bot.timer` |
| 查看运行记录 | `tail -50 output/run.log` |

生成的文件保存在 `output/日期/`：`chart_品种.png`（K 线标注图）、`card.png`（总结卡片）、`plain_品种.png`（发给 Gemini 的原图）、`review.json`（Gemini 原始回复）、`prompt.txt`（完整提示词）。

## 修改复盘要求

直接编辑 `prompts/review.md`，下次运行自动生效。
- `{{TRADES}}`、`{{STATS}}`、`{{CHAT_KNOWLEDGE}}`、`{{DATE}}` 会被替换成当天数据，请保留。
- 以 `%%` 开头的行是备注，不会发给 AI。

K 线图上的短评来自提示词里"12 个字以内的短评"这条要求；想让图上写得更多或更少，改这一条即可。

## 其他设置

`config.yaml` 里可以改：盘中时间段、合约每点价值、手续费、Gemini 模型、当天没交易时是否提醒。

## 出错时

运行失败时，机器人会往同一个 Discord 频道发一条"⚠️ 复盘机器人出错"的消息，详细记录在 `output/run.log`。
