#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键提取当日企微报价回复并生成/发送邮件用 HTML。

目的：把「读归档 → 解析 → 拼 HTML → 发 Agent Mail」压成一次脚本调用，
避免经交互式 agent 的多轮工具往返（原流程约 7 次调用、体感 4~5 分钟）。
用法：
  python daily_quote_email.py [YYYY-MM-DD]            # 提取并直连 Agent Mail 发送（默认）
  python daily_quote_email.py [YYYY-MM-DD] --no-send  # 仅打印 HTML 到 stdout（预览）
  python daily_quote_email.py [YYYY-MM-DD] --send     # 等同默认，显式发邮件
  python daily_quote_email.py [YYYY-MM-DD] --bot 机器人B   # 只取指定机器人的沟通记录
依赖：仅标准库。读取脚本所在目录下的 data/ 与 contacts.json / contacts_learned.json。
发信凭据/端点从环境变量 CODEBUDDY_MCP_CONFIG 读取（WorkBuddy 会话内自动存在）。

多机器人隔离：归档已按机器人分文件 data/messages_<机器人名>_<日期>.jsonl，
故取数默认只读「唯一存在的那一个」机器人文件；有两个及以上时必须用 --bot 指定，
避免把不同机器人与客户的沟通混在一起。
"""
import argparse
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


def safe_name(name: str) -> str:
    """机器人名转文件名安全形式（与 ws_client.py 的 safe_name 规则保持一致）"""
    out = re.sub(r"[^\w一-龥\-]+", "_", (name or "").strip())
    return out or "default"


def list_bot_files(date):
    """列出当日各机器人的 JSONL 归档，返回 [(机器人名, 路径), ...]"""
    d = WS / "data"
    if not d.exists():
        return []
    suffix = f"_{date}.jsonl"
    out = []
    for p in sorted(d.glob(f"messages_*{suffix}")):
        out.append((p.name[len("messages_"):-len(suffix)], p))
    return out


def resolve_archive(date, bot=None):
    """确定要读取的归档文件。

    - 传了 --bot → 只读该机器人文件（不存在则报错）；同时接受原始机器人名
      与文件名安全化后的名字（如「机器人A」与「机器人_A」等价）
    - 未传--bot 且只有一个机器人文件 → 用它
    - 未传 --bot 且存在多个 → 报错并列出候选，要求显式指定（防止沟通记录混淆）
    - 兼容旧版单文件 messages_<日期>.jsonl
    """
    legacy = WS / "data" / f"messages_{date}.jsonl"
    files = list_bot_files(date)
    if bot:
        want = safe_name(bot)
        for name, p in files:
            if name == want:
                return p
        raise SystemExit(f"[错误] 未找到机器人「{bot}」在 {date} 的归档文件。"
                         f"当日可用：{[n for n, _ in files] or '（无）'}")
    if len(files) == 1:
        return files[0][1]
    if len(files) > 1:
        raise SystemExit(
            "[错误] 当日存在多个机器人的归档，为避免客户沟通记录混淆，必须用 --bot 指定：\n"
            + "\n".join(f"  --bot {n}" for n, _ in files)
            + "\n（各机器人记录已物理分文件，互不混合）")
    if legacy.exists():
        return legacy
    return None


def load_json(p, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def build_name_map():
    """返回 (contacts, name_of) 。
    name_of(id_or_name): 把 userid / chat_id / 显示名 解析成可读显示名。
    """
    contacts = load_json(WS / "contacts.json", {})
    learned = load_json(WS / "contacts_learned.json", {})
    # 反向：显示名 -> 该人名对应的所有 key（userid / chat_id 同人）
    name_to_keys = {}
    for k, v in contacts.items():
        if k.startswith("_"):
            continue
        name_to_keys.setdefault(v, set()).add(k)
    # learned 昵称补充（仅当该 userid 在 contacts 里没有更好名字时）
    for k, v in learned.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and v.get("nickname") and k not in name_to_keys:
            name_to_keys.setdefault(v["nickname"], set()).add(k)

    def name_of(ident):
        if ident in contacts and not ident.startswith("_"):
            return contacts[ident]          # 直接是 key（userid / chat_id）
        # ident 本身是显示名？
        if ident in name_to_keys:
            return ident
        return ident                        # 兜底：原样返回（多为未登记的 raw id）

    return contacts, name_of


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


def parse_bal_inc(text):
    """按'余额/增量'关键词 + 位置兜底提取；(bal, inc) 各为 (val, unit) 或 None。
    - 有'余额X'→余额；有'增量X'或'较上日(增加)X'→增量；
    - 都没有但整句≥2个数字→按位置(第1=余额, 第2=增量)（兼容'我司400亿,较上日增100亿'）；
    - 仅1个数字且无关键词→不填（避免把'放100亿'等成交金额误当余额）。"""
    amts = parse_amounts(text)
    bal = inc = None
    m = re.search(r"余额[^0-9]{0,8}(-?\d+(?:\.\d+)?)\s*(亿元|万元|亿|万|元)?", text)
    if m:
        bal = (float(m.group(1)), m.group(2) or "")
    m = (re.search(r"(?:较上日|上日|日)?(?:增加|增|多)?增量[^0-9]{0,8}(-?\d+(?:\.\d+)?)\s*(亿元|万元|亿|万|元)?", text)
         or re.search(r"(?:较上日|上日)(?:增加|增|多)[^0-9]{0,4}(-?\d+(?:\.\d+)?)\s*(亿元|万元|亿|万|元)?", text))
    if m:
        inc = (float(m.group(1)), m.group(2) or "")
    # 位置兜底：仅当整句数字≥2 时，把没用到的数字按顺序补空位
    if len(amts) >= 2:
        used = {v[0] for v in (bal, inc) if v is not None}
        leftover = [a for a in amts if a[0] not in used]
        if bal is None and leftover:
            bal = leftover.pop(0)
        if inc is None and leftover:
            inc = leftover.pop(0)
    return bal, inc


def build_html(date, bot=None):
    """按机器人取当日归档并拼装邮件 HTML；无归档返回 None（stderr 提示）。"""
    fp = resolve_archive(date, bot)
    if fp is None:
        print(f"[跳过] 当日无归档文件：data/messages_*_{date}.jsonl（服务可能离线或当日无消息）",
              file=sys.stderr)
        return None

    records = []
    for line in open(fp, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    _, name_of = build_name_map()

    # 当日发出的报价（outbound markdown 含【今日报价】），同人按时间存全部（后发覆盖前发）
    quotes_all = {}      # 显示名 -> [(time, raw_id, content)]，时间升序
    for r in records:
        if r.get("direction") != "out":
            continue
        b = (r.get("frame") or {}).get("body") or {}
        md = (b.get("markdown") or {}).get("content") or ""
        if QUOTE_TITLE not in md:
            continue
        raw = b.get("chatid", "")
        nm = name_of(raw)
        quotes_all.setdefault(nm, []).append((r.get("archived_at", ""), raw, md))
    for nm in quotes_all:                       # 同人按时间排序，取末条为最新报价
        quotes_all[nm].sort(key=lambda x: x[0])

    # 当日 inbound 回复（单聊、非事件）
    replies = {}         # 显示名 -> [(time, raw_id, text)]
    for r in records:
        if r.get("direction") != "in":
            continue
        b = (r.get("frame") or {}).get("body") or {}
        if b.get("chattype") != "single":
            continue
        if (b.get("event") or {}).get("eventtype"):
            continue        # enter_chat 等事件不算回复
        text = (b.get("text") or {}).get("content") or (b.get("markdown") or {}).get("content") or ""
        if not text.strip():
            continue
        uid = (b.get("from") or {}).get("userid", "")
        nm = name_of(uid)
        replies.setdefault(nm, []).append((r.get("archived_at", ""), uid, text))

    # 组装表格
    def rate_line(content):
        """取报价正文的档位行（跳过标题与「注：」覆盖行），用于备注指代早先报价。"""
        for ln in content.split("\n"):
            ln = ln.strip()
            if ln and QUOTE_TITLE not in ln and not ln.startswith("注："):
                return ln
        return ""

    def scan_bal_inc(items):
        """在给定回复区间内按'余额/增量'关键词累计；后者覆盖前者（改价/更正以最新为准）。
        返回 (bal, inc, bal_t, inc_t, bal_src, inc_src)。"""
        bal = inc = None
        bal_t = inc_t = ""
        bal_src = inc_src = ""
        for t, uid, text in items:
            b, i = parse_bal_inc(text)
            if b is not None:
                bal, bal_t, bal_src = b, t, text
            if i is not None:
                inc, inc_t, inc_src = i, t, text
        return bal, inc, bal_t, inc_t, bal_src, inc_src

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
        bal, inc, bal_t, inc_t, bal_src, inc_src = scan_bal_inc(after)
        if bal is not None or inc is not None:
            bal_s = fmt_amount(*bal) if bal else "—"
            inc_s = fmt_amount(*inc) if inc else "—"
            if bal is not None and inc is not None:
                confirmed = "已确认"
            elif bal is not None:
                confirmed = "已回复（缺增量）"
            else:
                confirmed = "已回复（缺余额）"
            src_t, src = (bal_t, bal_src) if bal_t >= inc_t else (inc_t, inc_src)
            note = f"{src_t} 回「{src}」（最新报价后回复，按余额/增量关键词识别，建议人工复核）"
        else:
            # 最新报价未获有效金额回复 → 未确认；早先确认的金额杠掉并说明归属
            ebal, einc, ebal_t, einc_t, ebal_src, einc_src = scan_bal_inc(before)
            parts = []
            if ebal is not None or einc is not None:
                bal_s = f"<s>{fmt_amount(*ebal)}</s>" if ebal else "—"
                inc_s = f"<s>{fmt_amount(*einc)}</s>" if einc else "—"
                e_t = max(ebal_t, einc_t)
                e_src = ebal_src if ebal_t >= einc_t else einc_src
                prior_q = ""
                for qt, _, qc in qlist:          # 该回复归属其之前最近的一条报价
                    if qt <= e_t:
                        prior_q = rate_line(qc)
                confirmed = "未确认（最新报价未回复）"
                parts.append(f"早先报价「{prior_q}」已于 {e_t} 确认"
                             f"（{fmt_amount(*ebal) if ebal else '—'}/"
                             f"{fmt_amount(*einc) if einc else '—'}，「{e_src}」）")
                parts.append(f"最新报价 {q_time} 发出后未回复有效金额")
            else:
                bal_s = inc_s = "—"
                confirmed = "未确认（无回复）"
                parts.append(f"已发报价，当日无金额回复记录（最新报价 {q_time}）")
            # 报价后有回复但未含余额/增量：如实标注（区别于"完全未回复"）
            no_amt = [x for x in after if parse_bal_inc(x[2]) == (None, None)]
            if no_amt:
                parts.append(f"报价后有 {len(no_amt)} 条回复但未含余额/增量"
                             f"（最近 {no_amt[-1][0]}「{no_amt[-1][2]}」）")
            note = "；".join(parts) + "。"
        rows.append((nm, raw, qhtml, bal_s, inc_s, confirmed, note))

    # 生成 HTML
    h = []
    h.append(f"<h2>{QUOTE_TITLE}答复统计 {date}（企微渠道）</h2>")
    if not rows:
        h.append("<p>当日企微无报价发送 / 回复记录。</p>")
    else:
        h.append('<table border="1" cellspacing="0" cellpadding="6" '
                 'style="border-collapse:collapse;font-size:14px">')
        h.append("<tr style=\"background:#f0f0f0\"><th>人员</th><th>渠道</th><th>报价</th>"
                 "<th>余额</th><th>较上日增量</th><th>是否确认</th><th>备注</th></tr>")
        for nm, raw, qhtml, bal, inc, confirmed, note in rows:
            h.append(f"<tr><td>{nm}</td><td>企微</td><td>{qhtml}</td>"
                     f"<td>{bal}</td><td>{inc}</td><td><b>{confirmed}</b></td><td>{note}</td></tr>")
        h.append("</table>")
    h.append('<p style="color:#888;font-size:12px">说明：企微单聊历史无法回溯，离线期间消息永久丢失；'
             '若某日无归档文件即视为无法确认，不可臆断客户未回复。本邮件由本机归档自动汇总。</p>')

    # 当日机器人发出的消息（报价 + 自动应答），供"原始对话"双向展示
    out_msgs = {}        # 显示名 -> [(time, text)]
    for r in records:
        if r.get("direction") != "out":
            continue
        b = (r.get("frame") or {}).get("body") or {}
        text = (b.get("markdown") or {}).get("content") or (b.get("text") or {}).get("content") or ""
        if not text.strip():
            continue
        nm = name_of(b.get("chatid", ""))
        out_msgs.setdefault(nm, []).append((r.get("archived_at", ""), text))

    # 原始对话清单（收 + 发全量，分对象按时间线展示，便于人工复核）
    h.append("<h3>原始对话（企微，当日 收+发 全量，分对象按时间线，供复核）</h3>")
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
    端点与凭据从 CODEBUDDY_MCP_CONFIG 读取，无需额外配置；仅依赖标准库。
    收件人来自 mail_config.json / 环境变量，或运行时由用户输入；
    发件人始终是当前用户自己的 Agent 邮箱（由凭据决定，代码不设 from）。"""
    recipient, recipient_name = load_mail_config()
    if not recipient:
        print("[send] 未配置归档邮件收件人：请填写 mail_config.json 的 recipient，"
              "或设置环境变量 QUOTE_MAIL_TO=xxx@example.com。", file=sys.stderr)
        return False
    import os
    import urllib.request
    import urllib.error
    cfg_raw = os.environ.get("CODEBUDDY_MCP_CONFIG")
    if not cfg_raw:
        print("[send] 未找到 CODEBUDDY_MCP_CONFIG，无法直连 Agent Mail。", file=sys.stderr)
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
        for line in t.splitlines():       # 兼容 SSE：取 data: 行
            line = line.strip()
            if line.startswith("data:"):
                p = line[5:].strip()
                if p:
                    try:
                        return json.loads(p)
                    except json.JSONDecodeError:
                        continue
        return None

    # 按规定走 initialize 握手（本地代理无需 session id）
    sid, _, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "daily_quote_email", "version": "1.0"}}})
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


def main():
    args = sys.argv[1:]
    # 默认即「提取 + 发邮件」：用户约定「提取回复命令包含发邮件动作」。
    # 仅预览时传 --no-send / --preview；--send 为显式等价写法。
    no_send = "--no-send" in args or "--preview" in args
    date_args = [a for a in args if not a.startswith("--")]
    date = date_args[0] if date_args else datetime.now().strftime("%Y-%m-%d")

    # --bot <机器人名>：只取指定机器人的沟通记录（多机器人并存时必填，防混淆）
    bot = None
    if "--bot" in args:
        i = args.index("--bot")
        if i + 1 >= len(args):
            sys.exit("[错误] --bot 需要指定机器人名")
        bot = args[i + 1]

    html = build_html(date, bot)
    if html is None:
        sys.exit(2)
    label = f"（企微·{bot}）" if bot else "（企微渠道）"
    if no_send:
        print(html)
        sys.exit(0)
    ok = send_via_agent_mail(html, f"{QUOTE_TITLE}答复统计 {date}{label}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
