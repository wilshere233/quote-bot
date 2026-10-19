#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 版：一键提取当日 QQ 机器人报价回复并生成/发送邮件（HTML）。

目的：把「读 QQ 本地归档 → 解析报价/回复 → 拼 HTML → 发 Agent Mail」压成一次脚本
调用，避免经交互式 agent 的多轮工具往返（原流程约 7 次调用、体感 4~5 分钟）。

与企微版 daily_quote_email.py 同构，仅数据源与字段不同：
- 数据源：~/.workbuddy/qq-ws/data/messages_<date>.jsonl
  （qq_client.py WebSocket 长连接实时归档；详见 qq-ws/README.md）
- 入站(in)记录：frame.t=MESSAGE，frame.d.author.user_openid 为完整 openid，
  frame.d.content / markdown.content 为文本
- 出站(out)记录：frame.t=SEND，顶层 target 为联系人名，frame.d.content 为文本
- 报价识别：出站文本含「【今日报价】」；同日多次报价以最新为准（覆盖式）
- 回复识别：入站单聊文本；按「连续两个数字」识别余额/增量（与企微版一致），
  取当日该联系人最新一条回复（客户乙先拒后改口 → 取末条「余额200亿，增量10亿」）

用法（多机器人）：
  python daily_quote_email.py <机器人名> [YYYY-MM-DD]            # 提取并直连 Agent Mail 发送（默认）
  python daily_quote_email.py <机器人名> [YYYY-MM-DD] --no-send  # 仅打印 HTML 到stdout（预览）
  python daily_quote_email.py <机器人名> [YYYY-MM-DD] --html    # 导出 HTML 文件（无邮件凭据时用）
  python daily_quote_email.py --list                # 列出所有机器人

数据隔离：每个机器人的归档独立存放（data/messages_<机器人名>_<日期>.jsonl），
联系人映射取该机器人自己的 contacts_<机器人名>.json / contacts_learned_<机器人名>.json，
互不混用（QQ openid 按机器人隔离）。
依赖：仅标准库。收信凭据/端点从环境变量 CODEBUDDY_MCP_CONFIG 读取（WorkBuddy 会话内自动存在）。
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

WS = Path(__file__).parent
MAIL_CONFIG = WS / "mail_config.json"

# 报价标题（用于识别当日报价与报表标题）。
# 如需使用贵机构自己的标题（例如「XX银行今日报价」），改这一处，
# 并同步修改 send.py 顶部的 QUOTE_TITLE。
QUOTE_TITLE = "【今日报价】"


def load_mail_config():
    """读取归档邮件收件人：环境变量优先，其次 mail_config.json。

    本包不预设任何收件人；若为空且在终端交互，则提示用户输入并写回配置。
    发件人不由本函数决定——发件人永远是当前用户自己的 Agent 邮箱。
    """
    email = (os.environ.get("QUOTE_MAIL_TO") or "").strip()
    name = (os.environ.get("QUOTE_MAIL_NAME") or "").strip()
    if not email and MAIL_CONFIG.exists():
        try:
            cfg = json.loads(MAIL_CONFIG.read_text(encoding="utf-8"))
            email = (cfg.get("recipient") or "").strip()
            name = name or (cfg.get("recipient_name") or "").strip()
        except Exception:
            pass
    if not email and sys.stdin.isatty():
        print("未配置归档邮件收件人。", file=sys.stderr)
        email = input("请输入收件人邮箱：").strip()
        if email:
            name = input("请输入收件人显示名（可留空）：").strip()
            save_mail_config(email, name)
    return email, (name or "收件人")


def save_mail_config(email, name):
    MAIL_CONFIG.write_text(
        json.dumps({"recipient": email, "recipient_name": name or "收件人"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[配置] 已写入 {MAIL_CONFIG}", file=sys.stderr)


def load_json(p, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def safe(name):
    import re
    return re.sub(r"[^\w一-龥\-]+", "_", (name or "").strip()) or "default"


def bots_of_config():
    """qq_config.json 里配置的所有机器人名（多机器人判定用）。"""
    try:
        raw = json.loads((WS / "qq_config.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    bots = raw.get("bots")
    if isinstance(bots, dict) and bots:
        return list(bots)
    return [raw.get("bot_name") or "default"]


def build_name_map(bot="default"):
    """返回 (contacts, name_of)。name_of(openid_or_id)：把 openid / 显示名 解析成可读显示名。
    只读该机器人自己的联系人文件（不同机器人 openid 命名空间独立）。"""
    contacts = load_json(WS / f"contacts_{safe(bot)}.json", {})
    learned = load_json(WS / f"contacts_learned_{safe(bot)}.json", {})
    # 反向：显示名 -> 该人名对应的所有 key（同一人可能以两个 openid 出现，QQ 少见但保留兼容）
    name_to_keys = {}
    for k, v in contacts.items():
        if k.startswith("_"):
            continue
        name_to_keys.setdefault(v, set()).add(k)
    for k, v in learned.items():
        if k.startswith("_") or not isinstance(v, dict) or not v.get("nickname"):
            continue
        name_to_keys.setdefault(v["nickname"], set()).add(k)

    def name_of(ident):
        if not ident:
            return "未知"
        if ident in contacts and not ident.startswith("_"):
            return contacts[ident]            # ident 是 openid（key）
        if ident in name_to_keys:
            return ident                       # ident 本身就是显示名
        # ident 可能是 openid 但不在 contacts（仅 learned 有昵称）
        info = learned.get(ident)
        if isinstance(info, dict) and info.get("nickname"):
            return info["nickname"]
        return ident                           # 兜底：原样返回（多为未登记的 raw openid 前缀）

    return contacts, name_of


def extract_content(d):
    """从 QQ 事件体提取可读文本。"""
    if not isinstance(d, dict):
        return ""
    t = d.get("content")
    if t:
        return t
    md = d.get("markdown") or {}
    if isinstance(md, dict) and md.get("content"):
        return md["content"]
    for k in ("image", "file", "voice", "video"):
        if k in d:
            return f"[{k}]"
    return ""


def parse_amounts(text):
    """从回复文本里提取金额候选（数值 + 单位）。用于余额/增量识别。
    负号必须捕获：客户常回「50亿 ，-20亿」表示余额 50 亿、较上日减少 20 亿，
    丢掉负号会把「减少」误报成「增加」，业务含义完全相反。"""
    found = re.findall(r"(-?\d+(?:\.\d+)?)\s*(亿元|万元|亿|万|元)?", text)
    out = []
    for num, unit in found:
        try:
            out.append((float(num), unit or ""))
        except ValueError:
            continue
    return out


def fmt_amount(val, unit):
    """整数去 .0，保持金额可读（300.0亿 → 300亿）；负数保留负号。"""
    if val == int(val):
        return f"{int(val)}{unit}"
    return f"{val}{unit}"


def build_html(date, bot="default"):
    """提取 + 拼装邮件 HTML；无归档返回 None（stderr 提示）。"""
    fp = WS / "data" / f"messages_{safe(bot)}_{date}.jsonl"
    if not fp.exists():
        # 兼容改造前的单机器人归档名：仅在只有一个机器人时才回退，
        # 多机器人时回退会把别人的记录算到本机器人头上（串号）。
        legacy = WS / "data" / f"messages_{date}.jsonl"
        if legacy.exists() and len(bots_of_config()) <= 1:
            fp = legacy
        elif legacy.exists():
            print(f"[跳过] 机器人「{bot}」当日无独立归档；且当前为多机器人模式，"
                  f"不回退读取旧的 messages_{date}.jsonl（防串号）。", file=sys.stderr)
            return None
        else:
            print(f"[跳过] 机器人「{bot}」当日无 QQ 归档文件：{WS / 'data' / f'messages_{safe(bot)}_{date}.jsonl'}"
                  f"（服务可能离线或当日无消息）", file=sys.stderr)
            return None

    _, name_of = build_name_map(bot)

    records = []
    for line in open(fp, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # 隔离兜底：多机器人模式下，legacy 归档（无 bot 字段）只属于「唯一/默认」机器人，
    # 绝不把A 机器人的记录算到B 机器人头上（防报价串号）。
    legacy_mode = "bot" not in (records[0] if records else {})
    if len(bots_of_config()) > 1 and legacy_mode and safe(bot) != "default":
        print(f"[跳过] 机器人「{bot}」：{fp.name} 是改造前的单机器人归档，"
              f"已多机器人运行，为防串号不再回退读取。如需看该机器人请用 "
              f"daily_quote_email.py {bot}", file=sys.stderr)
        return None

    # 当日发出的报价（outbound SEND，文本含【今日报价】），同人按时间存全部（后发覆盖前发）
    quotes_all = {}      # 显示名 -> [(time, raw_id, content)]，时间升序
    for r in records:
        if r.get("direction") != "out":
            continue
        d = (r.get("frame") or {}).get("d") or {}
        content = d.get("content") or ""
        if QUOTE_TITLE not in content:
            continue
        nm = name_of(r.get("target") or "")
        quotes_all.setdefault(nm, []).append(
            (r.get("archived_at", ""), r.get("target") or nm, content))
    for nm in quotes_all:
        quotes_all[nm].sort(key=lambda x: x[0])

    # 当日 inbound 回复（单聊 / 群聊都收，按人归并）
    replies = {}         # 显示名 -> [(time, raw_id, text)]
    for r in records:
        if r.get("direction") != "in":
            continue
        d = (r.get("frame") or {}).get("d") or {}
        text = extract_content(d)
        if not text.strip():
            continue
        openid = ((d.get("author") or {}).get("user_openid")
                 or (d.get("author") or {}).get("id") or "")
        nm = name_of(openid or r.get("target") or "")
        replies.setdefault(nm, []).append((r.get("archived_at", ""), openid, text))

    # 组装表格
    def rate_line(content):
        """取报价正文的档位行（跳过标题与「注：」覆盖行），用于备注指代早先报价。"""
        for ln in content.split("\n"):
            ln = ln.strip()
            if ln and QUOTE_TITLE not in ln and not ln.startswith("注："):
                return ln
        return ""

    def pick_amount(items):
        """取「最新一条含金额数字」的回复 (time, text, amts)；无则 None。"""
        for t, uid, text in reversed(items):
            a = parse_amounts(text)
            if len(a) >= 2:
                return (t, text, a)
        for t, uid, text in reversed(items):
            a = parse_amounts(text)
            if len(a) >= 1:
                return (t, text, a)
        return None

    rows = []
    for nm, qlist in quotes_all.items():
        q_time, raw, content = qlist[-1]        # 最新报价（同日以后发为准）
        qhtml = content.replace("\n", "<br>")
        rep = sorted(replies.get(nm, []), key=lambda x: x[0])
        # 归属规则：只有「最新报价发出之后」的回复才算确认该报价；
        # 更早的回复属于此前报价，不能算到最新报价头上。
        after = [x for x in rep if not q_time or x[0] > q_time]
        before = [x for x in rep if q_time and x[0] <= q_time]

        # 业务规则：有新报价后，回复了（含金额）即视为同意，哪怕与之前逐字相同
        # （真实场景：先按某价成交规模，后市场价涨被迫提价、规模不变，客户照回同一组数）。
        conf = pick_amount(after)
        if conf:
            t, text, amts = conf
            if len(amts) >= 2:
                bal = fmt_amount(amts[0][0], amts[0][1])
                inc = fmt_amount(amts[1][0], amts[1][1])
                confirmed = "已确认"
                note = (f"{t} 回「{text}」（最新报价后回复，按连续两个数字识别，"
                        f"建议人工复核）")
            else:
                bal, inc = fmt_amount(amts[0][0], amts[0][1]), "—"
                confirmed = "部分（缺增量）"
                note = f"{t} 回「{text}」（最新报价后回复，仅一个数字）"
        else:
            # 最新报价未获有效金额回复 → 未确认；早先确认的金额杠掉并说明归属
            earlier = pick_amount(before)
            parts = []
            if earlier:
                et, etext, eamts = earlier
                if len(eamts) >= 2:
                    ebal = fmt_amount(eamts[0][0], eamts[0][1])
                    einc = fmt_amount(eamts[1][0], eamts[1][1])
                else:
                    ebal, einc = fmt_amount(eamts[0][0], eamts[0][1]), "—"
                prior_q = ""
                for qt, _, qc in qlist:          # 该回复归属其之前最近的一条报价
                    if qt <= et:
                        prior_q = rate_line(qc)
                bal = f"<s>{ebal}</s>"
                inc = f"<s>{einc}</s>"
                confirmed = "未确认（最新报价未回复）"
                parts.append(f"早先报价「{prior_q}」已于 {et} 确认"
                             f"（{ebal}/{einc}，「{etext}」）")
                parts.append(f"最新报价 {q_time} 发出后未回复有效金额")
            else:
                bal, inc = "—", "—"
                confirmed = "未确认（无回复）"
                parts.append(f"已发报价，当日无金额回复记录（最新报价 {q_time}）")
            # 报价后有回复但未含金额：如实标注（区别于"完全未回复"）
            no_amt = [x for x in after if not parse_amounts(x[2])]
            if no_amt:
                parts.append(f"报价后有 {len(no_amt)} 条回复但未含金额"
                             f"（最近 {no_amt[-1][0]}「{no_amt[-1][2]}」）")
            note = "；".join(parts) + "。"
        rows.append((nm, raw, qhtml, bal, inc, confirmed, note))

    h = []
    h.append(f"<h2>{QUOTE_TITLE}答复统计 {date}（QQ渠道 · {safe(bot)}）</h2>")
    if not rows:
        h.append(f"<p>当日 QQ 渠道（{safe(bot)}）无报价发送 / 回复记录。</p>")
    else:
        h.append('<table border="1" cellspacing="0" cellpadding="6" '
                 'style="border-collapse:collapse;font-size:14px">')
        h.append("<tr style=\"background:#f0f0f0\"><th>人员</th><th>渠道</th><th>报价</th>"
                 "<th>余额</th><th>较上日增量</th><th>是否确认</th><th>备注</th></tr>")
        for nm, raw, qhtml, bal, inc, confirmed, note in rows:
            h.append(f"<tr><td>{nm}</td><td>QQ·{safe(bot)}</td><td>{qhtml}</td>"
                     f"<td>{bal}</td><td>{inc}</td><td><b>{confirmed}</b></td><td>{note}</td></tr>")
        h.append("</table>")
    h.append('<p style="color:#888;font-size:12px">说明：QQ 单聊历史无法回溯，离线期间消息永久丢失；'
             '若某日无归档文件即视为无法确认，不可臆断客户未回复。本邮件由本机归档自动汇总。</p>')

    # 当日机器人发出的消息（报价 + 确认成交 + 自动应答），供"原始对话"双向展示
    out_msgs = {}        # 显示名 -> [(time, text)]
    for r in records:
        if r.get("direction") != "out":
            continue
        d = (r.get("frame") or {}).get("d") or {}
        text = d.get("content") or (d.get("markdown") or {}).get("content") or ""
        if not text.strip():
            continue
        nm = name_of(r.get("target") or "")
        out_msgs.setdefault(nm, []).append((r.get("archived_at", ""), text))

    # 原始对话清单（收 + 发全量，分对象按时间线展示，便于人工复核）
    h.append(f"<h3>原始对话（QQ · {safe(bot)}，当日 收+发 全量，分对象按时间线，供复核）</h3>")
    any_conv = False
    for nm in sorted(set(replies) | set(out_msgs)):
        timeline = []
        for t, uid, text in replies.get(nm, []):
            timeline.append((t, "对方", text))
        for t, text in out_msgs.get(nm, []):
            timeline.append((t, "机器人", text))
        timeline.sort(key=lambda x: x[0])
        if timeline:
            any_conv = True
        h.append(f"<h4 style=\"margin:14px 0 6px;color:#333\">{nm}（{len(timeline)} 条）</h4>")
        h.append('<div style="border-left:3px solid #ccc;padding-left:12px;margin:6px 0">')
        for t, who, text in timeline:
            who_html = ('<b style="color:#1a7f37">机器人</b>' if who == "机器人"
                        else '<b style="color:#8250df">对方</b>')
            h.append(f'<div style="margin:4px 0"><span style="color:#888;font-size:13px">{t}</span>'
                     f'　{who_html}：{text.replace(chr(10), "<br>")}</div>')
        h.append("</div>")
    if not any_conv:
        h.append("<p>（当日无收发记录）</p>")
    return "\n".join(h)


def send_via_agent_mail(html, subject):
    """直连 Agent Mail MCP（本地代理）发送，免去经交互式 agent 的多轮往返。

    收件人来自 mail_config.json / 环境变量，或运行时由用户输入；
    发件人始终是当前用户自己的 Agent 邮箱（由凭据决定，代码不设 from）。
    """
    recipient, recipient_name = load_mail_config()
    if not recipient:
        print("[send] 未配置归档邮件收件人：请填写 mail_config.json 的 recipient，"
              "或设置环境变量 QUOTE_MAIL_TO=xxx@example.com。", file=sys.stderr)
        return False
    import urllib.request
    import urllib.error
    cfg_raw = os.environ.get("CODEBUDDY_MCP_CONFIG")
    if not cfg_raw:
        print("[send] 未找到 CODEBUDDY_MCP_CONFIG，无法直连 Agent Mail（可先用无 --send 打印 HTML）。",
              file=sys.stderr)
        return False
    try:
        cfg = json.loads(cfg_raw)
        srv = cfg["mcpServers"]["agent-mail"]
        url = srv["url"]
        hdrs = srv["headers"]
    except (KeyError, json.JSONDecodeError, TypeError) as e:
        print(f"[send] 读取 Agent Mail 配置失败：{e}", file=sys.stderr)
        return False

    def post(payload, sid=None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in hdrs.items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if sid:
            req.add_header("Mcp-Session-Id", sid)
        try:
            r = urllib.request.urlopen(req, timeout=30)
            return r.headers.get("Mcp-Session-Id"), r.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            return sid, e.read().decode("utf-8", "replace"), e.code

    def parse(resp_text):
        t = resp_text.strip()
        if t.startswith("{"):
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return None
        for line in t.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                p = line[5:].strip()
                if p:
                    try:
                        return json.loads(p)
                    except json.JSONDecodeError:
                        continue
        return None

    sid, _, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "qq_daily_quote_email", "version": "1.0"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, sid=sid)
    _, data, code = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "SendMessage",
                                     "arguments": {
                                         "to": [{"email": recipient, "name": recipient_name}],
                                         "subject": subject,
                                         "body": html,
                                         "body_format": "HTML",
                                         "skip_confirmation": True}}}, sid=sid)
    if code:
        print(f"[send] Agent Mail HTTP 错误 {code}：{data[:300]}", file=sys.stderr)
        return False
    res = parse(data)
    if res and res.get("error"):
        print(f"[send] Agent Mail 返回错误：{res['error']}", file=sys.stderr)
        return False
    print(f"[send] 已提交 Agent Mail（queued），收件 {recipient}。", file=sys.stderr)
    return True


def list_bots():
    """从 qq_config.json 读出所有机器人名。"""
    try:
        raw = json.loads((WS / "qq_config.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    bots = raw.get("bots")
    if isinstance(bots, dict) and bots:
        return list(bots)
    return [raw.get("bot_name") or "default"]


def main():
    args = sys.argv[1:]
    # 默认即「提取 + 发邮件」：用户约定「提取回复命令包含发邮件动作」。
    # 仅预览时传 --no-send / --preview；--send 为显式等价写法。
    no_send = "--no-send" in args or "--preview" in args

    if "--list" in args:
        print("已配置 QQ 机器人：")
        for b in list_bots():
            print(f"  {b}")
        print()
        print("用法：python daily_quote_email.py <机器人名> [YYYY-MM-DD]")
        return

    bots = list_bots()
    pos = [a for a in args if not a.startswith("--")]
    bot = pos[0] if pos else (bots[0] if len(bots) == 1 else None)
    if bot is None:
        print("错误：请指定机器人名（多机器人时必填）。可用 --list 查看。", file=sys.stderr)
        sys.exit(1)
    if bots and bot not in bots:
        print(f"错误：未找到机器人「{bot}」。已配置：{'、'.join(bots)}", file=sys.stderr)
        sys.exit(1)

    rest = [a for a in pos if a != bot]
    date = rest[0] if rest else datetime.now().strftime("%Y-%m-%d")

    html = build_html(date, bot)
    if html is None:
        sys.exit(2)
    if no_send:
        print(html)
        sys.exit(0)
    if "--html" in args:
        # 导出 HTML 文件，供无 Agent Mail 凭据的同事人工转发/另存
        out = WS / f"报价回复统计_{safe(bot)}_{date}.html"
        out.write_text(html, encoding="utf-8")
        print(f"[导出] 已写入 {out}", file=sys.stderr)
        sys.exit(0)
    ok = send_via_agent_mail(html, f"{QUOTE_TITLE}答复统计 {date}（QQ渠道 · {bot}）")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
