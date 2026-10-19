---
name: 报价机器人
description: 跨平台报价机器人（QQ / 企业微信）。收发报价、提取当日报价回复并生成归档邮件。用户提到「发报价」「提取报价回复」「报价机器人」「QQ 报价」「企微报价」时使用。支持 Windows 与 macOS（Apple 芯片 / Intel）。
---

# 报价机器人

通过 QQ 或企业微信长连接收发报价，本地归档全部对话；按客户姓名发送报价，
汇总当日报价回复并发送 HTML 归档邮件。

- **平台**：Windows / macOS（Apple 芯片 arm64、Intel x86_64 通用）
- **依赖**：纯 Python，仅标准库 + `websockets` / `cryptography` / 企微 SDK
- **渠道**：QQ、企业微信，可单开或同时运行

---

## 一、加载技能时：必须先确认收件人（强制）

本技能**不预设任何收件人邮箱**，也不允许沿用任何示例地址。

每次加载技能、或首次执行发信前：

1. 检查 `qq-ws/mail_config.json` 与 `wecom-ws/mail_config.json`。
2. 如果文件不存在，以同目录的 `mail_config.example.json` 为模板创建。
3. 若 `recipient` 为空，**向用户询问**：
   - 归档邮件收件人邮箱（必填）
   - 收件人显示名（可留空，默认「收件人」）
4. 把用户输入写入两个文件：

   ```json
   {
     "recipient": "用户输入的邮箱",
     "recipient_name": "用户输入的显示名"
   }
   ```

5. 用户不提供时：**不要发信**，只允许 `--no-send` 预览。

> 严禁猜测、沿用旧地址或使用任何示例邮箱。收件人只能来自用户本次输入。

---

## 二、发件人规则

发件人**永远是当前运行者自己的 Agent 邮箱**：

- 脚本只设置 `to`（收件人），**不设置 from**；
- 发件人由 WorkBuddy 注入的 Agent Mail 凭据决定；
- 换人运行 = 换发件人，无需改代码；
- 不要把任何人的邮箱写死进脚本或配置来代替发件人。

---

## 三、平台适配

先判断当前系统，再选择对应脚本：

| 平台 | 安装依赖 | 启动 / 停止 / 状态 | Python 命令 |
|---|---|---|---|
| macOS（arm64 / x86_64） | `./install.sh` | `./start.sh` / `./stop.sh` / `./status.sh` | `python3` |
| Windows | `install.bat` | `start.bat` / `stop.bat` / `status.bat` | `python` |

各渠道目录下也有同名脚本：`qq-ws/` 与 `wecom-ws/` 内的
`start.sh` / `stop.sh` / `status.sh`（macOS）或 `start.bat` / `stop.bat` / `status.bat`（Windows）。

> macOS 首次运行如提示无权限：`chmod +x *.sh qq-ws/*.sh wecom-ws/*.sh`

---

## 四、目录结构

```
报价机器人/
├── SKILL.md                     ← 本文件（技能说明 + 操作指南）
├── README.md
├── install.sh / install.bat     ← 安装依赖
├── start.sh / stop.sh / status.sh          ← macOS 一键启停
├── start.bat / stop.bat / status.bat       ← Windows 一键启停
├── qq-ws/
│   ├── qq_config.json           ← QQ 机器人凭据（占位符）
│   ├── mail_config.example.json ← 收件人模板（复制为 mail_config.json 后填写）
│   ├── send.py                  ← 发消息（唯一入口，自动补覆盖行）
│   ├── daily_quote_email.py     ← 提取当日回复并发归档邮件
│   ├── qq_client.py             ← QQ 长连接常驻进程
│   ├── daemonize.py             ← start / stop / status
│   └── start/stop/status.sh|bat
└── wecom-ws/
    ├── ws_client.py             ← 企微凭据 BOTS + 长连接
    ├── mail_config.example.json
    ├── send.py / daily_quote_email.py
    ├── daemonize.py
    └── start/stop/status.sh|bat
```

---

## 五、安装依赖

**macOS**

```bash
chmod +x *.sh qq-ws/*.sh wecom-ws/*.sh
./install.sh
```

**Windows**

```bat
install.bat
```

会安装 `websockets`、`cryptography` 与企微 SDK；pip 会自动匹配当前芯片架构。

---

## 六、配置凭据

### 6.1 QQ

编辑 `qq-ws/qq_config.json`：

```json
{
  "bots": {
    "机器人A": {
      "appid": "你的 QQ 机器人 AppID",
      "appsecret": "你的 QQ 机器人 AppSecret"
    }
  }
}
```

- `bots` 的 key 是机器人名，可自行命名；
- 新增机器人 = 加一个 key；下线 = 删掉整个对象，都不用改代码；
- 每个机器人需在 QQ 开放平台单独勾选「事件订阅」，漏勾 = 连上但收不到消息；
- 同一机器人同时只能有一个长连接（新连接会互踢旧的）。

### 6.2 企业微信

编辑 `wecom-ws/ws_client.py` 顶部 `BOTS`：

```python
BOTS = {
    "机器人A": {
        "bot_id": "你的 bot_id",
        "secret": "你的 secret",
    },
}
```

---

## 七、发报价

**只走各渠道的 `send.py`** —— 直接调 API 的内容不会进本地归档，报价与回复无法对账。

### 7.1 QQ

```bash
cd qq-ws
python3 send.py 机器人A 客户甲 "【今日报价】
1.42% 隔夜
如贵司无异议，请确认日终余额、较上日增量。"
```

- **同日第二条起自动补覆盖行**：`send.py` 检测到该机器人当天已有报价（查当日归档 + 待发队列），
  会自动插入 `注：本条覆盖前次报价，以本条为准。`；当天首条不加，正文已带则不重复。
- 一次发多台：

```bash
python3 send.py 机器人A 客户甲 "内容" --also "机器人B"
```

各台使用自己名下的 openid 映射，不会串号。

### 7.2 企业微信

```bash
cd wecom-ws
python3 send.py 机器人A 客户甲 "【今日报价】
1.42% 隔夜
如贵司无异议，请确认日终余额、较上日增量。"
```

- 企微 `send.py` 同样**自动补覆盖行**（逻辑与 QQ 一致，按该机器人当天归档 + 队列判断）。
- 企微默认 markdown 发送；加 `--text` 用纯文本。

### 7.3 自定义报价标题

默认标题为 `【今日报价】`。如需改成贵机构自己的标题（例如 `【XX银行今日报价】`）：

- 改 `send.py` 顶部的 `QUOTE_TITLE`；
- 同步改 `daily_quote_email.py` 顶部的 `QUOTE_TITLE`；
- 两个文件、两个渠道都要保持一致，否则识别不到报价。

---

## 八、提取当日报价回复 → 发归档邮件

### 8.1 QQ

```bash
cd qq-ws
python3 daily_quote_email.py 机器人A
```

多机器人时必须逐台执行、各发一封（合并会串号）。

### 8.2 企业微信

```bash
cd wecom-ws
python3 daily_quote_email.py --bot 机器人A
```

### 8.3 参数

| 参数 | 作用 |
|---|---|
| （默认） | 提取 + 直连 Agent Mail 发邮件 |
| `--no-send` | 只打印 HTML 预览（仅在用户明确要求预览时用） |
| `--html` | 导出 HTML 文件（无 Agent Mail 凭据时用） |

- 收件人来自 `mail_config.json` / 环境变量 `QUOTE_MAIL_TO` / 本次用户输入。
- 邮件标题带渠道与机器人名，便于区分。
- 报表规则：只有「最新报价发出之后」的回复才算确认；更早的回复属于此前报价。
  最新报价未获回复 → 判「未确认」，余额/增量用删除线标出，并在备注说明早先报价的确认情况。

---

## 九、启动 / 停止 / 状态

**macOS**

```bash
./start.sh      # 启动企微 + QQ
./status.sh     # 查看状态
./stop.sh       # 停止
```

**Windows**

```bat
start.bat
status.bat
stop.bat
```

单渠道：

```bash
cd qq-ws && ./start.sh      # macOS
cd qq-ws && start.bat       # Windows
```

- 常驻进程用独立会话运行，关闭终端不会停；
- **重启电脑后需手动启动**（不安装 launchd / 计划任务自启）；
- 同一机器人同时只能一个长连接：换机器前先在旧机器停止。

---

## 十、数据布局与多机器人隔离

### QQ（按机器人物理隔离）

```
qq-ws/
├── contacts_<机器人名>.json            # 人工联系人映射 openid → 名字
├── contacts_learned_<机器人名>.json    # 自动学习
├── state_<机器人名>.json              # 长连接状态
├── outbox/<机器人名>.jsonl            # 待发队列
└── data/messages_<机器人名>_<日期>.jsonl
```

### 企业微信

```
wecom-ws/
├── contacts.json                      # userid / chat_id → 显示名
├── contacts_learned.json              # 自动学习
├── outbox.jsonl                       # 待发队列（任务带 bot 字段）
└── data/messages_<机器人名>_<日期>.jsonl
```

> **openid 按机器人隔离**：同一个人在不同机器人下 openid 不同，混用会发错人。
> 企微的 userid / chat_id 同理，两个 ID 都映射到同一显示名，收发才会合并。

**归档是唯一可靠的对话来源**：QQ / 企微都不提供单聊历史回溯，离线期间的消息无法补回。

---

## 十一、故障排查

| 现象 | 排查 |
|---|---|
| 启动即退出 | 看 `qq_client.log` / `ws_client.log` 末尾；多为凭据错误或依赖缺失 |
| 连上但收不到消息 | QQ 开放平台「事件订阅」未勾选；企微机器人未加入会话 |
| `11255 请求的资源不存在` | openid 属于别的机器人；按机器人分别映射 |
| 发消息静默失败 | 用户关闭了「允许主动发送」；检查 `outbox_failed.jsonl` |
| 邮件没收到 | `mail_config.json` 的 recipient 为空/错误；或 `QUOTE_MAIL_TO` 覆盖了它 |
| 长连接频繁断 | 系统代理/VPN；`daemonize.py` 已清 `HTTP(S)_PROXY`，必要时关代理 |
| pip 安装失败 | 确认 Python 架构与芯片一致（`python3 -c "import platform;print(platform.machine())"`） |
| macOS 脚本无权限 | `chmod +x *.sh qq-ws/*.sh wecom-ws/*.sh` |

---

## 十二、红线

1. **发消息只走 `send.py`** —— 直接调 API 的内容不进归档，报价与回复无法对账。
2. **不向任何非用户指定的人发消息**；测试只用自己。
3. **收件人只能来自用户输入**（`mail_config.json` / 环境变量），不得写死或猜测。
4. **发件人永远是运行者本人的 Agent 邮箱**，不要伪造。
5. 归档文件缺失 = 无法确认，不可臆断客户未回复。
6. 同一机器人同时只能有一个长连接；换机器前先停旧机器。
7. 不要把 `mail_config.json`、`data/`、`outbox*`、日志提交到公开仓库。
