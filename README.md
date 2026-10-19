# 报价机器人

跨平台（Windows / macOS）的 QQ / 企业微信报价机器人技能。

## 特性

- 通过 QQ / 企业微信长连接收发报价，本地实时归档
- 按客户姓名发送报价，消息走本地队列，发出内容自动归档
- 同日多次报价自动补「覆盖前次报价」行（QQ / 企微均支持）
- 一键提取当日报价回复，生成 HTML 归档邮件
- 收件人由用户加载技能时输入，**不预设任何邮箱**
- 发件人永远是运行者本人的 Agent 邮箱
- 纯 Python，Apple 芯片（arm64）与 Intel（x86_64）通用

## 快速开始

### macOS

```bash
chmod +x *.sh qq-ws/*.sh wecom-ws/*.sh
./install.sh
```

### Windows

```bat
install.bat
```

然后：

1. 配置凭据：`qq-ws/qq_config.json`（QQ）或 `wecom-ws/ws_client.py` 的 `BOTS`（企微）
2. 加载技能，按提示输入归档邮件收件人；或复制 `mail_config.example.json` 为 `mail_config.json` 后填写
3. 启动：macOS `./start.sh`，Windows `start.bat`

完整说明见 [SKILL.md](SKILL.md)。

## 安全

- 仓库内所有凭据均为占位符
- 不含任何真实机器人、联系人或邮箱
- `mail_config.json`、`data/`、`outbox*`、日志已加入 `.gitignore`

## License

MIT
