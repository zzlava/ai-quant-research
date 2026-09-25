# 部署到 VPS + Telegram 提醒

效果：VPS 在每个交易日收盘后自动检查 ETF 组合。需要调仓时，Telegram 会收到一条消息，附带图形报告；
每周五还会发一条周报，同时证明系统还在正常运行。你仍然在券商 App 里手动下单。

> 机器人**不会下单**。它能做的写操作只有记账，而且每一笔都要你在手机上点“确认”才会写入。

## 一、选 VPS

VPS 必须**同时**能访问上交所行情接口和 Telegram：

| 位置 | 上交所行情 | Telegram | 结论 |
| --- | --- | --- | --- |
| 中国大陆 | ✅ | ❌ 被墙 | 不行 |
| 香港 | 通常 ✅ | ✅ | **推荐** |
| 海外（美国/日本/新加坡） | 可能被拦截 | ✅ | 先测试 |

最低 1 核 / 1GB 内存就够，系统用 Debian 12 或 Ubuntu 22.04/24.04。买之前如果能试用，先跑第四步的 `check-network`。

另外，你的手机要能收到 Telegram 消息，大陆网络下需要自备网络工具。

## 二、创建 Telegram 机器人

1. 在 Telegram 里搜索 **@BotFather**，发送 `/newbot`，按提示起名字，会得到一个 **token**（形如 `123456789:AA...`）。
   token 等于机器人的密码，不要发给别人，也不要提交到 git。
2. 在 Telegram 里搜索你刚建的机器人，点 **Start**，随便发一句话。
   这一步必须做，机器人才能主动给你发消息。

## 三、安装到 VPS

以下命令用 root 在 VPS 上执行：

```bash
apt update && apt install -y git curl

# 1. 把代码放到 /opt/ai-quant-research（仓库是私有的，用你自己的 GitHub 凭证或 deploy key）
git clone https://github.com/zzlava/ai-quant-research.git /opt/ai-quant-research
cd /opt/ai-quant-research
git checkout main          # PR 合并前用 claude/vibrant-euler-3cgchk

# 2. 一键安装：创建 aiq 用户、Python 3.12 环境、systemd 定时任务
bash deploy/vps/install.sh
```

## 四、配置和测试

```bash
cd /opt/ai-quant-research

# 1. 填 token，先拿 chat id
nano /etc/ai-quant/telegram.env                  # 填 AIQ_TELEGRAM_BOT_TOKEN
set -a; . /etc/ai-quant/telegram.env; set +a
.venv/bin/ai-quant etf telegram-chat-id           # 输出 AIQ_TELEGRAM_CHAT_ID=...
nano /etc/ai-quant/telegram.env                  # 把 chat id 填进去
set -a; . /etc/ai-quant/telegram.env; set +a

# 2. 测网络和消息
.venv/bin/ai-quant etf check-network             # 两项都要显示 OK
.venv/bin/ai-quant etf telegram-test             # 手机应收到“已连通”
```

`check-network` 里上交所行情显示 FAILED，说明这台 VPS 被上交所拦截了，需要换一台（优先香港）。

## 五、建账本或搬账本

账本放在 VPS 上，以 VPS 上的为准。**二选一：**

```bash
# A. 还没开始：在 VPS 上新建
sudo -u aiq .venv/bin/ai-quant etf init --cash 80000

# B. Mac 上已经有账本：从 Mac 复制过去（在 Mac 上执行）
scp -r data/manual/etf-multi-asset-v1 root@你的VPS:/opt/ai-quant-research/data/manual/
#    然后在 VPS 上：
chown -R aiq:aiq /opt/ai-quant-research/data
```

搬过去之后，就不要再在 Mac 上记账了，避免两份账本对不上。

如果改过佣金费率，记得把 VPS 上的 `config/manual/etf-multi-asset-policy-v1.json` 也改成一样的。

手动跑一次，确认整条链路是通的：
```bash
sudo -u aiq --preserve-env=AIQ_TELEGRAM_BOT_TOKEN,AIQ_TELEGRAM_CHAT_ID \
  .venv/bin/ai-quant etf plan --fetch-sse --notify always
```

## 六、自动提醒的时间表

| 定时任务 | 时间（北京时间） | 什么时候发消息 |
| --- | --- | --- |
| `aiq-etf-daily` | 周一至周五 15:20 | **只在需要调仓或有买入被拦截时**发；没事不打扰 |
| `aiq-etf-weekly` | 每周五 15:40 | **总是**发一份周报，等于告诉你系统还活着 |
| `aiq-etf-repo` | 周一至周五 14:40 | 周四或逆回购利率高时，提醒你把闲钱借出去 |

- **节假日：** 行情日期不是当天时，任务直接跳过，不发消息。
- **运行出错：** 例如行情抓取失败，会发一条"❗运行失败"。
- **一直没调仓：** 你没去调仓的话，每个交易日都会再提醒一次，直到持仓回到容忍带内。

**如果连续两个周五都没收到周报，就说明 VPS 或网络出问题了。**

查看状态和日志：
```bash
systemctl list-timers 'aiq-etf-*'
journalctl -u aiq-etf-daily -n 50 --no-pager
```

## 七、在手机上记账（Telegram 机器人）

`install.sh` 会在配置好 `telegram.env` 并建好账本之后，自动启动常驻服务 `aiq-etf-bot`。
如果第一次运行时还没配好，配好后再运行一次 `bash deploy/vps/install.sh` 即可。

**收到调仓提醒 → 在券商 App 成交 → 给机器人发一条消息：**

```
/fill 510300 卖 1200 7.999 5
```
格式是 `/fill 代码 买|卖 份数 成交价 [佣金] [plan_id]`：
- 买、卖也可以写成 `buy`/`sell`、`买入`/`卖出`；
- 佣金不填就按策略费率估算；
- plan_id 不填时，会自动关联最近一张包含这笔单子的调仓单。

机器人会回一条**预览**，写明记账后的现金和持仓，下面有两个按钮：

- **✅ 确认记账**：写入账本；
- **❌ 取消**：什么都不写。

其他命令：

| 命令 | 作用 |
| --- | --- |
| `/cash 3.21 逆回购利息` | 现金变动（负数是转出），同样要确认 |
| `/status` | 查看现金和持仓 |
| `/repo` | 看今天是否适合做国债逆回购、能借出多少 |
| `/plan` | 立刻抓行情重新生成调仓单，并发来图形报告；全部成交后用它复查 |
| `/help` | 使用说明 |

**安全设计：**
- **只认你：** 只处理 `AIQ_TELEGRAM_CHAT_ID` 这个聊天发来的消息，别人发的一律忽略，也不回复。
- **不开端口：** 机器人主动去 Telegram 取消息，VPS 不需要开放任何入站端口。
- **不会误记：**
  - 每笔都要点确认，确认按钮 10 分钟后失效；
  - 预览之后账本如果被别的操作改过（比如定时任务），确认会被拒绝，需要你重新发一次；
  - 超过 15 分钟的旧消息不处理，所以机器人重启后不会把积压的消息补记一遍。
- **防呆：** 卖出超过持仓、现金变负、买入不是整手、代码不在策略里，都会直接报错，不会写入。
- **防并发：** 定时任务、命令行和机器人写账本时共用一把文件锁，不会同时写。

命令行的 `etf record-fill` 仍然可以用，两边写的是同一个账本。

查看机器人状态：
```bash
systemctl status aiq-etf-bot --no-pager
journalctl -u aiq-etf-bot -n 50 --no-pager
```

## 七之二、国债逆回购提醒

定时任务 `aiq-etf-repo` 在周一至周五 14:40 检查一次。满足下面任一条件就发提醒，写明可借出金额、当前利率和预计利息：

- **周四**：1 天期逆回购在周四做，通常能拿到周末的利息（约 3 天）；
- **利率高**：上交所 `204001` 的实时年化利率不低于提醒门槛（默认 2.5%）。季末、年末、长假前资金紧张时常见，不需要维护节假日表。

**可借出金额**按账本现金向下取整到 1000 元，不改动 ETF 持仓。借出的钱到期自动回来，下一个交易日可用，不影响调仓。
想留一部分现金不借出，就改 `config/manual/reverse-repo-v1.json` 里的 `keep_cash_cny`；门槛改 `rate_threshold_pct`。

**实际收益会很小。** 按当前配置，账本现金大约只有 2000–2500 元（国债 ETF 按整手买，占掉了大部分现金目标），
一次周四借出的利息通常只有几毛钱，一年加起来大约几十元。它的价值是让闲钱不闲着，而且几乎没有风险，不是主要收益来源。

手动检查：
```bash
.venv/bin/ai-quant etf repo-check            # 看今天是否提醒
.venv/bin/ai-quant etf repo-check --rate 3.2 # 手动给利率试算
```
也可以在 Telegram 里给机器人发 `/repo`。利息到账后发 `/cash 金额 逆回购利息` 记账。

## 八、更新代码

```bash
cd /opt/ai-quant-research && git pull && bash deploy/vps/install.sh
```
安装脚本可以重复运行，不会覆盖账本和 `telegram.env`。

## 九、备份

账本 `data/manual/etf-multi-asset-v1/ledger.jsonl` 只存在 VPS 上。建议每月下载一份到 Mac：
```bash
scp root@你的VPS:/opt/ai-quant-research/data/manual/etf-multi-asset-v1/ledger.jsonl ~/Backups/
```
