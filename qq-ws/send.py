#!/usr/bin/env python3
"""
通过长连接服务发送消息（会自动归档「机器人发出」的内容）。

为什么必须走这里：
QQ 不会把机器人自己发出的消息回推给自己，也没有单聊历史接口，
所以直接调 REST 发的内容永远进不了本地归档。

用法（多机器人）：
    python send.py <机器人名> <联系人> <内容> [--markdown] [--group] [--target <openid>]
    python send.py <机器人名> <联系人> <内容> --also <另一个机器人名>   # 一次发给多台
    python send.py --list                 # 列出所有机器人
    python send.py --list 机器人A          # 列出某个机器人已登记的联系人

示例：
    python send.py 机器人A 客户甲 "【报价】隔夜 1.44%"
    python send.py 机器人A 客户甲 "长内容，支持 **markdown**" --markdown
    python send.py 机器人A --target 7A3B9C1D... "直接按 openid 发"
    python send.py 机器人A 客户甲 "【今日报价】1.42% 隔夜" --also "机器人B"

说明：
- 本脚本只往 outbox/<机器人名>.jsonl 追加任务，由常驻的 qq_client.py 实际发送并归档
- 每个机器人一个独立队列，消息只会由该机器人发出（不会串到别的机器人）
- --also 可重复指定多台机器人；每台用**自己**的联系人映射解析 openid（openid 按机器人隔离）
- 报价自动覆盖：正文含「【今日报价】」且该机器人当天已有报价（归档或队列中）时，
  自动插入「注：本条覆盖前次报价，以本条为准。」；当天首条不加，正文已带则不重复加
- 默认纯文本（msg_type=0，最稳，不需要 markdown 权限）；--markdown 用 msg_type=2
- 群发加 --group（此时联系人名要能解析到 group_openid）
- 发送是异步的，通常 0.5 秒内由常驻进程处理
- 联系人名既不在该机器人的 contacts_<机器人名>.json 也不在自动识别结果中时，
  任务会挂起（不丢），待对方给该机器人发消息学到 openid
- ⚠️ openid 按机器人隔离：同一个人在「机器人A」和「机器人B」下是不同的 openid，
  各机器人的联系人映射互不通用（详见 qq-ws/README.md）
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

DIR = Path(__file__).parent
CONFIG_FILE = DIR / "qq_config.json"

# 报价识别与同日覆盖行（规则固定，勿改措辞）
QUOTE_MARK = "【今日报价】"
OVERRIDE_LINE = "注：本条覆盖前次报价，以本条为准。"


def safe(name: str) -> str:
    out = re.sub(r"[^\w一-龥\-]+", "_", (name or "").strip())
    return out or "default"


def load_bots():
    """返回 {机器人名: 配置}；兼容旧的单机器人写法。"""
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    bots = raw.get("bots")
    if isinstance(bots, dict) and bots:
        return {name: sub for name, sub in bots.items() if isinstance(sub, dict)}
    name = raw.get("bot_name") or "default"
    return {name: raw}


def contacts_file(bot):
    return DIR / f"contacts_{safe(bot)}.json"


def learned_file(bot):
    return DIR / f"contacts_learned_{safe(bot)}.json"


def outbox_file(bot):
    d = DIR / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe(bot)}.jsonl"


def has_quote_today(bot):
    """该机器人当天是否已发过报价（查当日归档 + 待发队列，避免快速连发漏判）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    archive = DIR / "data" / f"messages_{safe(bot)}_{today}.jsonl"
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
                c = ((o.get("frame") or {}).get("d") or {}).get("content") or ""
                if QUOTE_MARK in c:
                    return True
        except OSError:
            pass
    q = outbox_file(bot)
    if q.exists():
        try:
            for line in q.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    t = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if QUOTE_MARK in (t.get("content") or ""):
                    return True
        except OSError:
            pass
    return False


def ensure_override_line(bot, content):
    """报价正文自动补「注：本条覆盖前次报价，以本条为准。」。

    规则：同一机器人当天已有报价（含队列中待发的）且正文未带覆盖行时，
    在档位行之后、「如贵司无异议」之前插入；当天首条报价不加。
    """
    if QUOTE_MARK not in content or "覆盖前次报价" in content:
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


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def resolve_openid(bot, target):
    """联系人名 → openid（仅在该机器人自己的命名空间内解析）。"""
    if not target:
        return ""
    contacts = load_json(contacts_file(bot), {})
    for openid, name in contacts.items():
        if name == target and openid and not openid.startswith("_"):
            return openid
    for openid, info in load_json(learned_file(bot), {}).items():
        if openid and isinstance(info, dict) and info.get("nickname") == target:
            return openid
    return target


def list_contacts(bot):
    contacts = load_json(contacts_file(bot), {})
    print(f"已登记联系人（{bot} · contacts_{safe(bot)}.json）：")
    any_c = False
    for openid, name in contacts.items():
        if openid.startswith("_"):
            continue
        any_c = True
        print(f"  {name}  →  {openid or '(openid 待填)'}")
    if not any_c:
        print("  （空）")
    auto = [(info.get("nickname"), oid) for oid, info in load_json(learned_file(bot), {}).items()
            if isinstance(info, dict) and info.get("nickname")]
    auto = [(n, oid) for n, oid in auto if n not in contacts.values()]
    if auto:
        print()
        print(f"自动识别的联系人（{bot}，可直接按名发送）：")
        for n, oid in auto:
            print(f"  {n}  →  {oid}")
    print()
    print(f"对方首次给该机器人发消息后，其 openid 会出现在 contacts_learned_{safe(bot)}.json")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_md = "--markdown" in sys.argv
    is_group = "--group" in sys.argv
    direct = None
    if "--target" in sys.argv:
        i = sys.argv.index("--target")
        if i + 1 < len(sys.argv):
            direct = sys.argv[i + 1]

    # --also <机器人名>：可重复，用于一次命令发给多台机器人
    also = []
    for i, a in enumerate(sys.argv):
        if a == "--also" and i + 1 < len(sys.argv):
            also.append(sys.argv[i + 1])
    # 形如 --also=机器人B 的写法也支持
    also += [a.split("=", 1)[1] for a in sys.argv
             if a.startswith("--also=") and len(a.split("=", 1)) > 1]

    bots = load_bots()

    if "--list" in sys.argv:
        rest = [a for a in args]
        if rest:
            if rest[0] not in bots:
                print(f"错误：未找到机器人「{rest[0]}」。已配置：{'、'.join(bots) or '（无）'}")
                sys.exit(1)
            list_contacts(rest[0])
        else:
            print("已配置机器人：")
            for name, cfg in bots.items():
                print(f"  {name}  (appid {cfg.get('appid') or '未配置'})")
            print()
            print("查看某个机器人的联系人：python send.py --list <机器人名>")
        return

    if not args:
        print("用法：python send.py <机器人名> <联系人> <内容> [--markdown] [--group]")
        print()
        print("已配置机器人：")
        for name, cfg in bots.items():
            print(f"  {name}  (appid {cfg.get('appid') or '未配置'})")
        return

    # 第一个参数是机器人名
    bot = args[0]
    if bot not in bots:
        print(f"错误：未找到机器人「{bot}」。已配置：{'、'.join(bots) or '（无）'}")
        print("可用 python send.py --list 查看。")
        sys.exit(1)

    rest = args[1:]
    if not rest:
        print(f"错误：缺少联系人和内容。用法：python send.py {bot} <联系人> <内容>")
        sys.exit(1)

    if direct:
        target = direct
        content = rest[0] if rest else ""
    else:
        target = rest[0]
        content = rest[1] if len(rest) > 1 else ""

    if not content:
        print("错误：缺少内容。用法：python send.py <机器人名> <联系人> <内容>")
        sys.exit(1)

    # 报价正文自动补「同日覆盖」行（当天该机器人已有报价时），避免人工漏加
    raw_content = content
    content = ensure_override_line(bot, raw_content)
    if content != raw_content:
        print(f"[自动] [{bot}] 当天已有报价，已补「覆盖前次报价」行。")

    openid = resolve_openid(bot, target)

    task = {
        "target": target,
        "content": content,
        "msg_type": "markdown" if use_md else "text",
        "group": is_group,
        "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 主机器人
    with open(outbox_file(bot), "a", encoding="utf-8") as f:
        f.write(json.dumps(task, ensure_ascii=False) + "\n")

    print(f"已加入发送队列：[{bot}] → {target}（{'群聊' if is_group else '单聊'}）")
    if openid and openid != target:
        oid_disp = openid
    elif openid and len(openid) >= 28:
        oid_disp = openid
    else:
        oid_disp = f"（未登记/未识别，任务将挂起，待对方给「{bot}」发消息学到 openid）"
    print(f"openid：{oid_disp}")

    # 附加机器人（--also "机器人名"）：一条命令发多台，省掉多次调用往返。
    # 各机器人用**自己**的联系人映射解析 openid —— openid 按机器人隔离，不可混用。
    for other in also:
        if other not in bots:
            print(f"[跳过] --also 指定的机器人「{other}」不存在。已配置：{'、'.join(bots) or '（无）'}")
            continue
        o_openid = resolve_openid(other, target)
        o_task = dict(task)
        o_task["content"] = ensure_override_line(other, raw_content)
        if o_task["content"] != raw_content:
            print(f"[自动] [{other}] 当天已有报价，已补「覆盖前次报价」行。")
        o_task["queued_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(outbox_file(other), "a", encoding="utf-8") as f:
            f.write(json.dumps(o_task, ensure_ascii=False) + "\n")
        if o_openid and o_openid != target and len(o_openid) >= 28:
            o_disp = o_openid
        else:
            o_disp = f"（未登记/未识别，任务将挂起，待对方给「{other}」发消息学到 openid）"
        print(f"已加入发送队列：[{other}] → {target}（openid：{o_disp}）")

    print(f"内容：{content}")
    print("常驻进程将在 0.5 秒内发送并归档。")
    print(f"查看结果：tail -f {DIR / 'qq_client.log'}")


if __name__ == "__main__":
    main()