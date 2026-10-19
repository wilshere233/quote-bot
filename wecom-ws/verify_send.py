#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企微发送前双重验证：名字 ↔ userid 双源核对（移植并强化 qq-ws「方案 A」）。

双源：
  1. 人工登记 contacts.json（权威，优先级最高）
  2. 归档学习 contacts_learned.json（该 userid 实际给机器人发过消息）

判定（双重验证 = 两源都要能对得上）：
  ✅ 双重验证通过：名字能唯一解析到 userid，且该 userid 在归档中真实出现过
  ⚠ 单源（仅登记）：解析到了 userid，但归档无该 userid 记录（未实际交互，触达存疑）
  ⚠ 重名冲突：同一名字对应多个 userid（机制上禁止歧义，需人工消歧）
  ✗ 未登记：两个源都查不到该名字

用法：
  python3 verify_send.py 客户丙 客户乙 客户丙 客户乙 [--ws-dir ~/.workbuddy/wecom-ws]
退出码：全部 ✅ 为 0，存在 ⚠/✗ 为 1（供发送前把关）。
"""
import argparse
import json
import os
import sys

DEFAULT_WS = os.path.dirname(os.path.abspath(__file__))  # 默认取脚本所在目录，解压即跑无需 --ws-dir


def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}


def main():
    ap = argparse.ArgumentParser(description="发送前双重验证")
    ap.add_argument("names", nargs="+", help="待验证的联系人名")
    ap.add_argument("--ws-dir", default=DEFAULT_WS)
    args = ap.parse_args()

    contacts = load_json(os.path.join(args.ws_dir, "contacts.json"))
    learned = load_json(os.path.join(args.ws_dir, "contacts_learned.json"))
    seen_ids = {k for k in learned if not k.startswith("_")}  # 归档中出现过的 userid

    rows = []
    all_ok = True
    for name in args.names:
        manual = [k for k, v in contacts.items() if not k.startswith("_") and v == name]
        auto = [k for k, v in learned.items()
                if not k.startswith("_") and isinstance(v, dict) and v.get("nickname") == name]
        ids = list(dict.fromkeys(manual + auto))
        # 同一人可能有 userid 与 chat_id（wo...）两个键，属同人双形态，不算冲突；
        # 真正冲突 = 同一名字对应 ≥2 个非 wo 的 userid（不同的人）
        primaries = [i for i in ids if not i.startswith("wo")]
        if not ids:
            status, resolved = "✗ 未登记（双源均无）", "—"
            all_ok = False
        elif len(primaries) > 1:
            status, resolved = "⚠ 重名冲突（同一名字对应多个 userid）", " / ".join(primaries)
            all_ok = False
        else:
            resolved = primaries[0] if primaries else ids[0]
            if any(i in seen_ids for i in ids):
                status = "✅ 双重验证通过（登记 + 归档在案）"
            else:
                status = "⚠ 单源：仅登记，归档无记录（未实际交互，触达存疑）"
                all_ok = False
        rows.append((name, resolved, status))

    print("%-10s %-28s %s" % ("名字", "解析 userid", "结论"))
    print("-" * 78)
    for name, resolved, status in rows:
        print("%-10s %-28s %s" % (name, resolved, status))
    print()
    print("说明：双重验证要求「人工登记」与「归档在案」两源一致；⚠ 项建议先让对方给机器人发一条消息（自动学习后可转 ✅），或人工确认后放行。")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
