# 部署到 Muse 虚拟机（WhatsApp 提醒 + 对话记账）

适用于没有 root、没有 systemd，但有 cron、能联网的 Linux 环境。Meta Muse 的 Secure VM 就是这类环境。

整体分工：

| 谁 | 做什么 |
| --- | --- |
| **cron**（虚拟机上的定时任务） | 按北京时间定时运行检查，把要发给你的提醒写进"待发消息"文件夹（outbox） |
| **Muse** | 定时读取待发消息，通过 WhatsApp 原样转发给你；你告诉它成交情况时，它先预览、等你确认再记账 |
| **你** | 在券商 App 手动下单；在 WhatsApp 里告诉 Muse 成交结果并回复"确认" |

> 工具本身不下单、不连券商。Muse 按长期指令只做转发和经你确认的记账。

## 一、安装（在 Muse 虚拟机里执行，或让 Muse 帮你执行）

```bash
git clone https://github.com/zzlava/ai-quant-research.git ~/ai-quant-research
cd ~/ai-quant-research
git checkout main            # PR 合并前用 claude/vibrant-euler-3cgchk
bash deploy/muse/install.sh
```

安装脚本会做这几件事，可以重复运行：

- 建 Python 3.12 虚拟环境并安装项目；优先用 uv，没有就用 python3.12；
- 创建配置文件 `~/.config/ai-quant/env`，默认通知方式是 `outbox`；
- 在你的用户 crontab 里写入三个定时任务，并**自动换算成虚拟机本地时间**。你原有的其他 cron 任务会保留。

| 任务 | 北京时间 | 作用 |
| --- | --- | --- |
| `repo` | 周一至周五 14:40 | 周四或逆回购利率高时，提醒你借出闲钱 |
| `daily` | 周一至周五 15:20 | 需要调仓时，提醒并附上图形报告 |
| `weekly` | 周五 15:40 | 无论要不要调仓都发一份周报，兼作"系统还活着"的信号 |

如果脚本提示虚拟机时区有夏令时，把虚拟机时区设成 UTC 或 Asia/Shanghai 后，再运行一次 `install.sh`。

## 二、建账本

```bash
cd ~/ai-quant-research
.venv/bin/ai-quant etf init --cash 80000
```

如果 Mac 或 VPS 上已经有账本，就把整个 `data/manual/etf-multi-asset-v1/` 目录复制过来，不要重新 `init`。以后只在一台机器上记账。

记得把 `config/manual/etf-multi-asset-policy-v1.json` 里的佣金改成你券商的实际费率。

## 三、给 Muse 设置长期指令

1. 打开 [`deploy/muse/MUSE_INSTRUCTIONS.md`](../deploy/muse/MUSE_INSTRUCTIONS.md)，把里面的 `REPO` 换成实际路径，例如 `/home/<你的用户名>/ai-quant-research`。
2. 把全文发给 Muse，让它保存成长期指令，或做成一个 Skill。
3. 让 Muse 按指令里的时间建三个定时任务，用来转发提醒：
   - 周一至周五 14:45；
   - 周一至周五 15:35；
   - 周五 15:50。
4. 如果 Sentinel 询问能否访问 `yunhq.sse.com.cn`（上交所行情），选择长期允许。

## 四、测试一遍

```bash
cd ~/ai-quant-research
bash deploy/muse/run.sh daily                  # 手动跑一次调仓检查
tail -5 data/manual/logs/daily.log              # 看是否运行成功
AIQ_NOTIFY_CHANNEL=outbox .venv/bin/ai-quant etf plan --fetch-sse --notify always   # 强制生成一条提醒
.venv/bin/ai-quant etf outbox                    # 应看到待发消息
```

然后在 WhatsApp 里问 Muse"有没有提醒"。它应该把消息原样发给你，然后执行 `--ack`，这时再运行 `etf outbox` 应显示 `pending=0`。

再测一次记账，先测"不确认"的情况：

1. 对 Muse 说"510300 买入 100 份 4.7 元 佣金 5"；
2. 它应该先发来"预览（未写入）"；
3. 你回复"取消"；
4. 运行 `etf status`，确认账本没有变化。

## 五、日常使用

- **收到调仓提醒：** 打开附件里的 HTML 报告，在券商 App 按顺序下限价单（先卖后买）。
- **成交后：** 告诉 Muse，例如"510300 卖出 1200 份 成交价 7.999 佣金 5"。Muse 发来预览后，回复"**确认**"才会记账。
- **分红、逆回购利息：** 告诉 Muse"逆回购利息 0.31 元"，流程同上。
- **随时查询：** "看看持仓""现在要不要调仓""今天适合做逆回购吗"。

## 六、排查

| 现象 | 看哪里 |
| --- | --- |
| 周五没收到周报 | 看 `data/manual/logs/weekly.log`；运行 `crontab -l` 确认定时任务还在 |
| 收到"❗运行失败" | 通常是行情接口连不上：运行 `.venv/bin/ai-quant etf check-network`，确认 Sentinel 放行了上交所 |
| Muse 发的内容和预期不符 | 运行 `.venv/bin/ai-quant etf outbox` 看原始消息；已发的消息在 `outbox/sent/` |
| 记账报"账本在预览之后有变化" | 预览之后定时任务写了记录。让 Muse 重新预览一次即可 |

## 七、备份

账本只存在这台虚拟机上，建议每月把 `data/manual/etf-multi-asset-v1/ledger.jsonl` 下载保存一份。

## 八、和 VPS + Telegram 方案的区别

| | Muse | VPS + Telegram |
| --- | --- | --- |
| 需要 root / systemd | 不需要 | 需要 |
| 接收提醒 | WhatsApp（由 Muse 转发） | Telegram 机器人 |
| 记账确认 | 回复"确认"文字；由 Muse 按指令执行 | 点按钮；由程序强制执行 |
| 确定性 | 依赖 Muse 按指令行事 | 完全由程序控制 |

两种方式可以随时切换：把 `~/.config/ai-quant/env` 里的 `AIQ_NOTIFY_CHANNEL` 改成 `telegram`，并填上 Telegram 配置即可。
