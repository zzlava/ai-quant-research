# 部署到 VPS + Telegram 提醒

效果：VPS 在每个交易日收盘后自动检查 ETF 组合。需要调仓时，Telegram 会收到一条消息，附带图形报告；
每周五还会发一条周报，同时证明系统还在正常运行。你仍然在券商 App 里手动下单。

> 机器人**只发消息、不收指令**。它不能下单，也不能改账本。

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

- **节假日：** 行情日期不是当天时，任务直接跳过，不发消息。
- **运行出错：** 例如行情抓取失败，会发一条"❗运行失败"。
- **一直没调仓：** 你没去调仓的话，每个交易日都会再提醒一次，直到持仓回到容忍带内。

**如果连续两个周五都没收到周报，就说明 VPS 或网络出问题了。**

查看状态和日志：
```bash
systemctl list-timers 'aiq-etf-*'
journalctl -u aiq-etf-daily -n 50 --no-pager
```

## 七、收到提醒之后

1. 打开消息附带的 HTML 报告，看调仓单；
2. 在券商 App 按顺序下限价单（先卖后买）；
3. 成交后登录 VPS 记账：
   ```bash
   cd /opt/ai-quant-research
   sudo -u aiq .venv/bin/ai-quant etf record-fill --symbol 510300 --side sell \
     --quantity 1200 --price 7.999 --commission 5 --plan-id <消息里的plan_id>
   ```
   现金变动（分红、逆回购利息）用 `etf cash`，用法见 `docs/etf-multi-asset-manual.md`。

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
