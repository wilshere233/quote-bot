#!/usr/bin/env python3
"""
通过长连接服务发送消息（会自动归档「机器人发出」的内容）。

为什么必须走这里：
企微不会把机器人发出的消息回推给自己，API 也不提供单聊历史，
所以如果直接用 wecom-cli / MCP 发，发出的内容永远进不了归档。

用法：
    python send.py "机器人B" <chat_id> "消息内容"
    python send.py --list-bots

示例（客户乙 的单聊 chat_id）：
    python send.py "机器人B" <chat_id> "您好"

说明：
- 本脚本只往 outbox.jsonl 追加任务，由常驻的 ws_client.py 实际发送并归档
- 消息默认 markdown 格式；加 --text 用纯文本
- 发送是异步的，通常 2 秒内由常驻进程处理
- 改过名的机器人旧名仍可用（ws_client.py 的 BOT_ALIASES 自动转接）
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).parent
OUTBOX = DIR / "outbox.jsonl"

# 报价标题与同日覆盖行（规则固定，勿改措辞）。
# 如需使用贵机构自己的标题（例如「XX银行今日报价」），只改这一处，
# 并同步修改 daily_quote_email.py 顶部的 QUOTE_TITLE。
QUOTE_TITLE = "【今日报价】"
OVERRIDE_LINE = "注：本条覆盖前次报价，以本条为准。"


def safe_name(name: str) -> str:
    """机器人名转文件名安全形式（与 ws_client.py 规则一致）"""
    out = re.sub(r"[^\w一-龥\-]+", "_", (name or "").strip())
    return out or "default"


def resolve_target(target):
    """把联系人名解析成 chat_id

    支持两种输入：
    - 直接给 chat_id（wo 开头）→ 原样返回
    - 给联系人名（如「客户甲」「客户乙」「客户丙」）→ 查 contacts.json 反查：
      优先取 wo 开头的 chat_id 键；没有则取映射里的 userid 键（如 userid_a / userid_b）
      —— SDK 说明「单聊填用户的 userid」，userid 可直接作为发送 chat_id。
    """
    if target.startswith("wo"):
        return target

    path = DIR / "contacts.json"
    if not path.exists():
        return target
    try:
        contacts = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return target

    # 反查：所有 value == target 的键（跳过 _comment 等元数据键）
    candidates = [k for k, v in contacts.items()
                  if not k.startswith("_") and v == target]
    wo = [k for k in candidates if k.startswith("wo")]
    if wo:
        return wo[0]          # 优先历史沿用的 wo 形式 chat_id
    if candidates:
        return candidates[0]  # 否则用映射键（userid，如 userid_a）
    # 名字本身就是键（兜底）
    if target in contacts:
        return target
    return target


def has_quote_today(bot):
    """该机器人当天是否已发过报价（查当日归档 + 待发队列，避免快速连发漏判）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    archive = DIR / "data" / f"messages_{safe_name(bot)}_{today}.jsonl"
    if archive.exists():
        try:
            for line in archive.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("direction") != "out":
                    continue
                b = (o.get("frame") or {}).get("body") or {}
                c = ((b.get("markdown") or {}).get("content")
                     or (b.get("text") or {}).get("content") or "")
                if QUOTE_TITLE in c:
                    return True
        except OSError:
            pass
    if OUTBOX.exists():
        try:
            for line in OUTBOX.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if t.get("bot") == bot and QUOTE_TITLE in (t.get("content") or ""):
                    return True
        except OSError:
            pass
    return False


def ensure_override_line(bot, content):
    """报价正文自动补「注：本条覆盖前次报价，以本条为准。」。

    规则：同一机器人当天已有报价（含队列中待发的）且正文未带覆盖行时，
    在档位行之后、「如贵司无异议」之前插入；当天首条报价不加。
    """
    if QUOTE_TITLE not in content or "覆盖前次报价" in content:
        return content
    if not has_quote_today(bot):
        return content
    lines = content.split("\n")
    insert_at = None
    for i, line in enumerate(lines):
        if "如贵司无异议" in line:
            insert_at = i
            break
    if insert_at is None:
        for i, line in enumerate(lines):
            if i == 0 or not line.strip():
                continue
            insert_at = i + 1
            break
    if insert_at is None:
        insert_at = len(lines)
    lines.insert(insert_at, OVERRIDE_LINE)
    return "\n".join(lines)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_text = "--text" in sys.argv

    if "--list-bots" in sys.argv:
        bots = {}
        src = DIR / "ws_client.py"
        if src.exists():
            for line in src.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                # 抓取 BOTS 字典里的 "名字": { 起始行
                if s.startswith('"') and ": {" in s:
                    name = s.split('"')[1]
                    bots[name] = None
        print("可用机器人（发消息时用这些名字）：")
        for n in bots:
            print(f"  - {n}")
        return

    if "--list" in sys.argv or not args:
        print("用法：python send.py <机器人名> <联系人或chat_id> <内容>")
        print()
        print("联系人可直接用名字，例如：")
        print('  python send.py "机器人B" 客户甲 "今日报价 1.85%"')
        print('  python send.py "机器人B" 客户乙 "您好"')
        print()
        print("机器人名见 ws_client.py 顶部 BOTS 字典；联系人见 contacts.json。")
        return

    if len(args) < 3:
        print("错误：参数不足。用法：python send.py <机器人名> <联系人> <内容>")
        sys.exit(1)

    bot, target, content = args[0], args[1], args[2]

    # 报价正文自动补「同日覆盖」行（当天该机器人已有报价时），避免人工漏加
    raw_content = content
    content = ensure_override_line(bot, raw_content)
    if content != raw_content:
        print(f"[自动] [{bot}] 当天已有报价，已补「覆盖前次报价」行。")

    chat_id = resolve_target(target)
    if chat_id != target:
        print(f"联系人「{target}」→ chat_id {chat_id}")
    task = {
        "bot": bot,
        "chat_id": chat_id,
        "content": content,
        "msgtype": "text" if use_text else "markdown",
    }

    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps(task, ensure_ascii=False) + "\n")

    print(f"已加入发送队列：{bot} → {chat_id}")
    print(f"内容：{content}")
    print("常驻进程将在 2 秒内发送并归档。")
    print(f"查看结果：tail -f {DIR / 'ws_client.log'}")


if __name__ == "__main__":
    main()