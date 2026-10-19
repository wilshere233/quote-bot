#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微侧联系人自动学习（移植 qq-ws「方案 A：自动识别姓名，批量加人防混淆」）。

从本地归档 data/messages_*.jsonl 的 inbound 单聊消息中：
  - 记录每个 from.userid 的首/末次出现时间（谁给机器人发过消息 → 可触达）
  - 消息命中「我是X / 我叫X」句式时，保守提取姓名（宁缺勿猜）

输出 contacts_learned.json（人工 contacts.json 优先级永远更高，可覆盖）。
零三方依赖。

用法：
  python3 learn_contacts.py [--ws-dir ~/.workbuddy/wecom-ws]
"""
import argparse
import glob
import json
import os
import re

DEFAULT_WS = os.path.dirname(os.path.abspath(__file__))  # 默认取脚本所在目录，解压即跑无需 --ws-dir

# 「我是X / 我叫X / 我叫做X / 我名叫X」；先抓到标点为止，再做末段/「的」后处理
SELF_INTRO = re.compile(r"我(?:是|叫|叫做|名叫)\s*([^，。！？；,.!?;\n]{1,20})")


def extract_self_intro_name(text):
    """保守提取自我介绍姓名；识别不到返回空（不猜）。"""
    m = SELF_INTRO.search(text or "")
    if not m:
        return ""
    seg = m.group(1).strip()
    parts = seg.split()
    if parts:
        seg = parts[-1]            # 「某银行 客户乙」→ 客户乙
    if "的" in seg:
        seg = seg.split("的")[-1]  # 「某银行的客户乙」→ 客户乙
    seg = seg.strip()
    if not (1 <= len(seg) <= 8):
        return ""
    if re.fullmatch(r"[0-9]+", seg):  # 纯数字不猜
        return ""
    return seg


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return default


def main():
    ap = argparse.ArgumentParser(description="从归档学习企微联系人")
    ap.add_argument("--ws-dir", default=DEFAULT_WS)
    args = ap.parse_args()

    learned_path = os.path.join(args.ws_dir, "contacts_learned.json")
    learned = load_json(learned_path, {})
    learned.setdefault(
        "_comment", "自动学习：userid → {nickname, first_seen, last_seen}。"
                    "人工 contacts.json 优先；对方发「我是X」可自动识别姓名。本文件由 learn_contacts.py 生成。")

    files = sorted(glob.glob(os.path.join(args.ws_dir, "data", "messages_*.jsonl")))
    n_msg = 0
    for fp in files:
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("direction") != "in":
                    continue
                body = (rec.get("frame", {}) or {}).get("body", {}) or {}
                if body.get("chattype") != "single":
                    continue
                uid = (body.get("from", {}) or {}).get("userid", "")
                if not uid:
                    continue
                n_msg += 1
                ts = rec.get("archived_at", "")
                text = ((body.get("text") or {}).get("content")
                        or (body.get("markdown") or {}).get("content") or "")
                e = learned.get(uid)
                if not isinstance(e, dict):
                    e = {"nickname": "", "first_seen": ts, "last_seen": ts}
                    learned[uid] = e
                if ts:
                    if not e.get("first_seen") or ts < e["first_seen"]:
                        e["first_seen"] = ts
                    if not e.get("last_seen") or ts > e["last_seen"]:
                        e["last_seen"] = ts
                name = extract_self_intro_name(text)
                if name and not e.get("nickname"):
                    e["nickname"] = name

    with open(learned_path, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)

    users = [k for k in learned if not k.startswith("_")]
    named = [k for k in users if learned[k].get("nickname")]
    print("已学习 %d 个 userid（inbound 单聊消息 %d 条），其中 %d 个识别出姓名"
          % (len(users), n_msg, len(named)))
    for k in users:
        print("  %s  name=%r  first=%s" % (k, learned[k].get("nickname", ""),
                                           learned[k].get("first_seen", "")))


if __name__ == "__main__":
    main()
