#!/usr/bin/env python3
"""
QQ 官方机器人长连接客户端 —— 报价消息收发 + 全量归档

架构整体复用 ~/.workbuddy/wecom-ws：
  - 独立会话守护进程，脱离 WorkBuddy 工具会话生命周期
  - 落盘优先：收到事件先写 data/messages_日期.jsonl，再进业务逻辑
  - 发送走 outbox.jsonl 队列，由常驻进程实际发送并「自归档为发出」
    （QQ 不会把机器人自己发出的消息回推给自己，也没有单聊历史接口）
  - 联系人走 contacts.json 名字反查，不记 openid

与企微侧的差异（QQ 官方 Bot API v2）：
  - 网关协议是 opcode 制（类 Discord）：10 Hello / 2 Identify / 1 Heartbeat /
    6 Resume / 7 Reconnect / 9 InvalidSession
  - 断线重连支持 Resume（带 session_id + last_seq 重放丢失事件），比企微强
  - 出向有配额：单聊主动 20 条/分/单关系、1000 条/天/单关系（本地限速器兜底）
  - 主动消息可被用户客户端开关拦截、也可能被平台审核（MESSAGE_AUDIT）拦截

运行：python qq_client.py   （Ctrl+C 退出；生产用 daemonize.py start）

多机器人（v2）：qq_config.json 支持两种写法，自动识别
  - 新写法：{"bots": {"机器人A": {…}, "机器人B": {…}}}
  - 旧写法：{appid, appsecret, …}（整份视为单个机器人，名字取 bot_name）
每个机器人独立一条长连接、独立凭证/限速/联系人 openid 命名空间，
按机器人名分文件存放：
    data/messages_<bot>_<日期>.jsonl      归档
    data/chat_<bot>_<联系人>_<日期>.md    对话式记录
    contacts_<bot>.json                   人工联系人映射
    contacts_learned_<bot>.json           自动学习到的昵称
    state_<bot>.json                      限速日计数
原因：QQ 的 openid 按机器人隔离 —— 同一个人在两个机器人下是不同的 openid，
混用会导致按名字发到错误的人。
"""

import asyncio
import json
import logging
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

import websockets

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "qq_config.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("qq_client")


class _BotLog:
    """把日志自动加上「[机器人名]」前缀，多机器人并行时日志不串行难辨。"""

    def __init__(self, bot: str):
        self.bot = bot
        self._log = logging.getLogger(f"qq_client.{safe(bot)}")

    def _fmt(self, msg):
        return f"[{self.bot}] {msg}"

    def info(self, msg, *a, **k):
        self._log.info(self._fmt(msg), *a, **k)

    def warning(self, msg, *a, **k):
        self._log.warning(self._fmt(msg), *a, **k)

    def error(self, msg, *a, **k):
        self._log.error(self._fmt(msg), *a, **k)

    def debug(self, msg, *a, **k):
        self._log.debug(self._fmt(msg), *a, **k)


# ---------------------------------------------------------------------------
# 按机器人分文件的路径助手（多机器人隔离的核心）
# ---------------------------------------------------------------------------
def safe(name: str) -> str:
    """把机器人名转成适合做文件名的片段（去掉路径分隔符等危险字符）。"""
    out = re.sub(r"[^\w一-龥\-]+", "_", (name or "").strip())
    return out or "default"


def contacts_file(bot: str) -> Path:
    return BASE_DIR / f"contacts_{safe(bot)}.json"


def learned_file(bot: str) -> Path:
    return BASE_DIR / f"contacts_learned_{safe(bot)}.json"


def state_file(bot: str) -> Path:
    return BASE_DIR / f"state_{safe(bot)}.json"


def outbox_file(bot: str) -> Path:
    """outbox/<机器人名>.jsonl —— 队列也按机器人分开，避免误发到别的机器人。"""
    d = BASE_DIR / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe(bot)}.jsonl"


def outbox_failed_file(bot: str) -> Path:
    return BASE_DIR / "outbox" / f"{safe(bot)}.failed.jsonl"

# ---------------------------------------------------------------------------
# 默认配置（qq_config.json 不存在时使用）
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "bot_name": "default",
    "appid": "",
    "appsecret": "",
    # 静态 AppToken 鉴权官方已废弃，留空即可；Identify/Resume 改用 access_token
    "app_token": "",
    # true=沙箱环境（sandbox.api.sgroup.qq.com），false=正式环境
    "sandbox": True,
    # 事件订阅：与控制台「事件订阅」页面勾选保持一致，漏勾 = 连上也不推事件
    "intents": ["GUILDS", "GROUP_AND_C2C_EVENT", "INTERACTION", "MESSAGE_AUDIT"],
    # 主动消息配额（官方口径：单关系 20/分、1000/天）
    "rate_per_min": 20,
    "rate_per_day": 1000,
    # 收到消息是否自动应答（四类话术见下方 build_reply；默认关，开启后勿回显原文）
    "auto_reply": False,
    # 不应答的对象（openid 或显示名均可），如本人测试账号；留空=全部应答
    "auto_reply_skip": [],
}

INTENT_BITS = {
    "GUILDS": 1 << 0,
    "GUILD_MEMBERS": 1 << 1,
    "GUILD_MESSAGES": 1 << 9,          # 频道 @ 消息
    "GUILD_MESSAGE_REACTIONS": 1 << 10,
    "DIRECT_MESSAGE": 1 << 12,         # 频道私信
    "FORUMS_EVENT": 1 << 18,
    "AUDIO_ACTION": 1 << 20,
    "GROUP_AND_C2C_EVENT": 1 << 25,    # ★ 群聊 + 单聊消息事件
    "INTERACTION": 1 << 26,
    "MESSAGE_AUDIT": 1 << 27,          # 主动消息审核结果
}

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = {
    True: "https://sandbox.api.sgroup.qq.com",
    False: "https://api.sgroup.qq.com",
}


def load_config() -> dict:
    """返回 {机器人名: 该机器人的配置}。

    兼容两种写法：
      - 新写法 {"bots": {"机器人A": {…}, …}}  → 原样返回 bots
      - 旧写法 {appid, appsecret, …}         → 整份视为单个机器人，名字取 bot_name
    """
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("qq_config.json 解析失败：%s（用默认配置）", e)
            raw = {}
        bots = raw.get("bots")
        if isinstance(bots, dict) and bots:
            for name, sub in bots.items():
                if not isinstance(sub, dict):
                    log.error("bots['%s'] 不是对象，已跳过", name)
                    continue
                merged = dict(DEFAULT_CONFIG)
                merged.update(sub)
                merged["bot_name"] = name
                cfg[name] = merged
        else:
            merged = dict(DEFAULT_CONFIG)
            merged.update(raw)
            cfg[merged.get("bot_name") or "default"] = merged
    return cfg


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, obj) -> None:
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log.error("写 %s 失败：%s", path.name, e)


# ---------------------------------------------------------------------------
# HTTP 助手（用 urllib，避免额外依赖；websockets 已由 wecom-ws 环境提供）
# ---------------------------------------------------------------------------
def http_json(method: str, url: str, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_expires_in(v) -> int:
    """expires_in 容错解析：官方口径可能是 '7200s' 字符串，也可能是数字"""
    try:
        return int(str(v).strip().rstrip("sS")) or 7200
    except (TypeError, ValueError):
        return 7200


# ---------------------------------------------------------------------------
# 限速器：单关系 20 条/分、1000 条/天（滑动窗口 + 落盘日计数）
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_min: int, per_day: int, bot: str = "default"):
        self.per_min = per_min
        self.per_day = per_day
        self.bot = bot
        self.minute_window = OrderedDict()   # key -> deque[ts]
        state = load_json(state_file(bot), {})
        self.day = state.get("day") or datetime.now().strftime("%Y-%m-%d")
        self.day_counts = state.get("day_counts", {})

    def _roll_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_counts = today, {}
            self._persist()

    def _persist(self):
        save_json(state_file(self.bot), {"day": self.day, "day_counts": self.day_counts})

    def check(self, key: str) -> tuple:
        """返回 (是否放行, 原因)"""
        self._roll_day()
        now = time.time()
        q = self.minute_window.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_min:
            return False, f"分钟配额已满（{self.per_min}/分）"
        if self.day_counts.get(key, 0) >= self.per_day:
            return False, f"日配额已满（{self.per_day}/天）"
        return True, ""

    def commit(self, key: str):
        self.minute_window[key].append(time.time())
        self.day_counts[key] = self.day_counts.get(key, 0) + 1
        self._persist()


# ---------------------------------------------------------------------------
# 归档（与 wecom-ws 同构）
# ---------------------------------------------------------------------------
def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def archive_raw(bot: str, target: str, frame: dict, direction: str = "in") -> None:
    record = {
        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot": bot,
        "target": target,
        "direction": direction,
        "frame": frame,
    }
    path = DATA_DIR / f"messages_{safe(bot)}_{today()}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_text(d: dict) -> str:
    """从事件/消息体提取可读文本"""
    if not isinstance(d, dict):
        return str(d)
    if d.get("content"):
        return d["content"]
    md = d.get("markdown")
    if isinstance(md, dict) and md.get("content"):
        return md["content"]
    for k in ("image", "file", "voice", "video", "media"):
        if k in d:
            return f"[{k}]"
    return json.dumps(d, ensure_ascii=False)[:200]


def archive_md(bot: str, target: str, d: dict, text: str, direction: str = "in",
               scene: str = "单聊") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who = f"**{target}（收到）**" if direction == "in" else f"**机器人（发出）**"
    path = DATA_DIR / f"chat_{safe(bot)}_{target}_{today()}.md"
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {target} 对话记录 {today()}\n\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"**{ts}｜{scene}｜{who}：**\n\n{text}\n\n---\n\n")


def learn_contact(bot: str, openid: str, nickname: str = "") -> None:
    """把事件里见到的 openid 记到该机器人的 contacts_learned_<bot>.json，供人工确认后搬进 contacts_<bot>.json"""
    if not openid:
        return
    lf = learned_file(bot)
    learned = load_json(lf, {})
    if openid in learned and (learned[openid].get("nickname") or not nickname):
        return
    learned[openid] = {
        "nickname": nickname or learned.get(openid, {}).get("nickname", ""),
        "first_seen": learned.get(openid, {}).get("first_seen")
        or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_json(lf, learned)


# --- 方案 A：从对方消息自动识别自我介绍姓名（批量加人时区分身份）---
# 背景：C2C 事件只给 user_openid，不给 QQ 号、username 实测为空。一次加多人进
# 白名单、都发「你好」时无法区分是谁。若对方首条带「我是X」即可自动对上身份。
_ORG_HINTS = ("银行", "公司", "证券", "基金", "保险", "集团", "理财", "资本",
              "财务", "资管", "农商", "信用社", "村镇银行", "租赁", "信托",
              "期货", "投资", "商行")


def extract_self_intro_name(text: str) -> str:
    """从对方消息里提取自我介绍姓名（方案 A）。

    命中「我是X / 我叫X / 我叫做X」句式；兼容机构在前、姓名在后的常见写法
    （如「我是某银行 客户乙」取末段「客户乙」，「我是某银行的客户乙」取「的」后）。
    取不到返回空串。刻意保守：宁可多留整段（保可区分、保安全），也不乱猜单字。
    """
    if not text:
        return ""
    # (?!不|没|们) 挡掉「我是不是 / 我没有 / 我们是」这类误命中
    m = re.search(r"我[是叫](?!不|没|们)(?:做)?\s*([^\n，。！？,.!?；;]+)", text)
    if not m:
        return ""
    raw = m.group(1).strip().strip("　 \t")
    if not raw:
        return ""
    parts = raw.split()
    if len(parts) >= 2:
        # 多段：首段像机构名 → 取末段当姓名；否则整段保留
        if any(h in parts[0] for h in _ORG_HINTS):
            return parts[-1]
        return raw
    if "的" in raw:                       # 单段「XX的YY」→ 取 YY
        return raw.split("的")[-1].strip() or raw
    return raw


def _name_taken(bot: str, name: str, except_openid: str) -> bool:
    """该显示名是否已被别的 openid 占用（该机器人的 contacts / learned 内）。"""
    if not name:
        return False
    for oid, nm in load_json(contacts_file(bot), {}).items():
        if oid.startswith("_") or oid == except_openid:
            continue
        if nm == name:
            return True
    for oid, info in load_json(learned_file(bot), {}).items():
        if oid == except_openid or not isinstance(info, dict):
            continue
        if info.get("nickname") == name:
            return True
    return False


def _unique_nickname(bot: str, openid: str, name: str) -> str:
    """重名时加 openid 短缀，确保该机器人内每个发送目标名唯一（防发错人）。"""
    if not _name_taken(bot, name, openid):
        return name
    suffixed = f"{name}({openid[:6]})"
    log.warning("[%s] 自动识别姓名「%s」与既有联系人重名，改用「%s」以示区分；"
                "请在该机器人的 contacts_%s.json 人工确认/改名（openid=%s…）",
                bot, name, suffixed, safe(bot), openid[:12])
    return suffixed


def resolve_openid(bot: str, target: str) -> str:
    """联系人名 → openid（仅在该机器人自己的命名空间内解析）；支持直接传 openid"""
    if not target:
        return ""
    contacts = load_json(contacts_file(bot), {})
    if len(target) >= 28 and target not in contacts.values():
        # 长度像 openid（32 位十六进制）且不是已登记的名字 → 视为原始 openid
        return target
    for openid, name in contacts.items():
        if name == target and openid and not openid.startswith("_"):
            return openid
    for openid, name in contacts.items():
        if openid == target:
            return openid
    # 回退：方案 A 自动识别的昵称（contacts_learned_<bot>.json）。
    # 人工映射永远优先，这里只兜底，便于新联系人直接按名发送。
    for openid, info in load_json(learned_file(bot), {}).items():
        if openid and isinstance(info, dict) and info.get("nickname") == target:
            return openid
    return ""


def display_name(bot: str, openid: str) -> str:
    contacts = load_json(contacts_file(bot), {})
    name = contacts.get(openid)
    if name:
        return name
    # 回退：方案 A 自动识别的昵称，让新联系人的归档也有可读标签
    learned = load_json(learned_file(bot), {}).get(openid) or {}
    if isinstance(learned, dict) and learned.get("nickname"):
        return learned["nickname"]
    return openid[:12] + "…"


# ---------------------------------------------------------------------------
# 应答话术（四类规则；与企微 wecom-ws/ws_client.py 保持完全同步，单侧改动请同步另一侧）
# 优先级：问题4 人工服务 > 问题2 带"介绍"字样 > 问题1 对方自我介绍 > 问题3 报价回复/兜底
# ---------------------------------------------------------------------------
REPLY_WELCOME = (
    "欢迎使用智能报价机器人，本机器人用于基金存款报价：\n"
    "1.添加后请发送“公司名+姓名”；\n"
    "2.收到报价后，如可接受，请回复【余额XX亿、日增量XX亿】，如成交，机器人将及时告知；\n"
    "3.如有其他需求，请向机器人留言，后续交易员将与您主动联系。"
)
REPLY_SELF_INTRO = (
    "我是智能报价机器人，用于基金存款报价：\n"
    "1.添加后请发“公司名+姓名”；\n"
    "2.收到报价后，如可接受，请回复【余额XX亿、日增量XX亿】，如成交，机器人将及时告知；\n"
    "3.如有其他需求，请向机器人留言，后续交易员将与您主动联系。"
)
REPLY_RECEIPT = "已收到您的消息，我们会尽快处理并回复"
REPLY_HUMAN = "已将您的诉求反馈交易员，交易员将与您主动联系。"

# 问题4 触发词：要求直接与交易员对话（"凡是客户要求人工服务"）
_HUMAN_KWS = ("人工", "交易员", "真人")
# 问题1 触发句式：「我是X / 我叫X / 我叫做X」，与方案 A extract_self_intro_name 同一句式（此处只判有无、不取名）
_SELF_INTRO_RE = re.compile(r"我[是叫](?!不|没|们)(?:做)?\s*[^\n，。！？,.!?；;]+")
# 问题1 触发：公司名+姓名（即使不带"我是"），如「某基金公司 客户甲」「某银行 客户丙」
# 要求机构关键词后接 2-4 字姓名且在句尾，避免「保险起见」「理财产品」等误判
_INTRO_ORG_RE = re.compile(
    r"(?:基金|银行|证券|保险|资管|信托|理财|财务|集团|公司|有限|农商|城商|农信|信用社)"
    r"[\s：:，,、·]{0,2}[一-龥]{2,4}\s*$")


def build_reply(text: str) -> str:
    """按四类应答话术选择回复文本；返回空串表示不回复（空消息）。

    问题4 人工服务：含「人工 / 交易员 / 真人」字样（优先）；
    问题2 机器人自我介绍：含「介绍」字样（凡是）；
    问题1 对方自我介绍：「我是X / 我叫X」句式，或短消息含机构名（公司名+姓名）→ 欢迎语；
    问题3 兜底：报价相关回复及一切其他消息 → 通用回执。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if any(k in t for k in _HUMAN_KWS):
        return REPLY_HUMAN
    if "介绍" in t:
        return REPLY_SELF_INTRO
    if _SELF_INTRO_RE.search(t):
        return REPLY_WELCOME
    # 公司名+姓名（不带"我是"）：机构名后接姓名且在句尾 → 也按自我介绍欢迎
    if _INTRO_ORG_RE.search(t):
        return REPLY_WELCOME
    return REPLY_RECEIPT


# ---------------------------------------------------------------------------
# 网关客户端
# ---------------------------------------------------------------------------
class QQGateway:
    def __init__(self, bot: str, cfg: dict, stop_event: asyncio.Event):
        self.bot = bot
        self.cfg = cfg
        self.stop_event = stop_event
        self.log = _BotLog(bot)
        self.base = API_BASE[bool(cfg.get("sandbox", True))]
        self.ws = None
        self.session_id = None
        self.last_seq = None
        self.hb_interval = 45.0
        self._token = None
        self._token_expire = 0.0
        self.limiter = RateLimiter(cfg.get("rate_per_min", 20),
                                    cfg.get("rate_per_day", 1000), bot)
        self.seen_msgids = OrderedDict()   # msg_id -> ts，去重
        self._warned_missing = {}          # target -> 上次告警时间
        self._ready_logged = False

    # ---- 凭证 ----
    async def get_token(self) -> str:
        if self._token and time.time() < self._token_expire - 300:
            return self._token
        if not (self.cfg.get("appid") and self.cfg.get("appsecret")):
            raise RuntimeError("qq_config.json 未配置 appid/appsecret")
        resp = await asyncio.to_thread(
            http_json, "POST", TOKEN_URL,
            {"appId": self.cfg["appid"], "clientSecret": self.cfg["appsecret"]},
        )
        if not resp.get("access_token"):
            raise RuntimeError(f"获取 access_token 失败：{resp}")
        self._token = resp["access_token"]
        self._token_expire = time.time() + parse_expires_in(resp.get("expires_in"))
        self.log.info("access_token 已获取，有效期约 %d 秒",
                 parse_expires_in(resp.get("expires_in")))
        return self._token

    def identify_token(self, token: str) -> str:
        """Identify/Resume 鉴权串。静态 AppToken 方式官方已废弃，统一用 access_token。"""
        return f"QQBot {token}"

    async def get_gateway(self, token: str) -> str:
        headers = {"Authorization": f"QQBot {token}"}
        resp = await asyncio.to_thread(
            http_json, "GET", f"{self.base}/gateway/bot", None, headers)
        url = resp.get("url")
        if not url:
            raise RuntimeError(f"获取网关地址失败：{resp}")
        return url

    # ---- 发送 ----
    async def send_frame(self, obj: dict):
        if self.ws is None:
            raise RuntimeError("WebSocket 未连接")
        await self.ws.send(json.dumps(obj))

    async def send_message(self, openid: str, content: str, msg_type: int = 0,
                           group: bool = False, msg_id: str = None,
                           msg_seq: int = None) -> tuple:
        """返回 (成功?, 响应)"""
        token = await self.get_token()
        if group:
            url = f"{self.base}/v2/groups/{openid}/messages"
        else:
            url = f"{self.base}/v2/users/{openid}/messages"
        body = {"msg_type": msg_type}
        if msg_type == 0:
            body["content"] = content
        elif msg_type == 2:
            body["markdown"] = {"content": content}
        else:
            body["content"] = content
        if msg_id:
            body["msg_id"] = msg_id
        if msg_seq:
            body["msg_seq"] = msg_seq
        try:
            resp = await asyncio.to_thread(
                http_json, "POST", url, body, {"Authorization": f"QQBot {token}"})
        except urllib.error.HTTPError as e:
            try:
                resp = json.loads(e.read().decode("utf-8"))
            except Exception:
                resp = {"errcode": e.code, "errmsg": str(e)}
        except Exception as e:
            return False, {"errmsg": str(e)}
        errcode = resp.get("errcode") or resp.get("code")
        if errcode:
            return False, resp
        return True, resp

    # ---- 主循环 ----
    async def run_forever(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                await self.run_once()
                backoff = 1
                if not self.stop_event.is_set():
                    self.log.info("连接结束，1 秒后重连")
                    await asyncio.sleep(1)
            except Exception as e:
                self.log.warning("连接异常：%s（%.0fs 后重连）", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def run_once(self):
        token = await self.get_token()
        gw = await self.get_gateway(token)
        self.log.info("连接网关 %s", gw)
        async with websockets.connect(gw, ping_interval=None) as ws:
            self.ws = ws
            hello = json.loads(await ws.recv())
            if hello.get("op") != 10:
                raise RuntimeError(f"未收到 Hello，收到：{hello}")
            self.hb_interval = float(hello["d"].get("heartbeat_interval", 45000)) / 1000
            self.log.info("Hello，心跳周期 %.0fs（按 80%% 发送）", self.hb_interval)

            hb_task = asyncio.create_task(self.heartbeat_loop())

            if self.session_id:
                self.log.info("尝试 Resume（session=%s）", self.session_id[:8] + "…")
                await self.send_frame({"op": 6, "d": {
                    "token": self.identify_token(token),
                    "session_id": self.session_id,
                    "seq": self.last_seq,
                }})
            else:
                intents = 0
                for name in self.cfg.get("intents", []):
                    intents |= INTENT_BITS.get(name, 0)
                self.log.info("Identify，intents=%d（%s）", intents,
                         ",".join(self.cfg.get("intents", [])))
                await self.send_frame({"op": 2, "d": {
                    "token": self.identify_token(token),
                    "intents": intents,
                    "shard": [0, 1],
                    "properties": {"$os": "macos", "$browser": "qq-ws", "$device": "qq-ws"},
                }})

            async for raw in ws:
                if self.stop_event.is_set():
                    break
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                op = frame.get("op")
                if op == 0:
                    if frame.get("s") is not None:
                        self.last_seq = frame["s"]
                    await self.on_dispatch(frame)
                elif op == 7:
                    self.log.info("服务端要求重连（op7），下次走 Resume")
                    return
                elif op == 9:
                    self.log.warning("Invalid Session（op9）：清空 session 重新鉴权。"
                                "若反复出现，请检查本机器人的 appid/appsecret 是否正确、"
                                "是否被同一机器人的另一个连接（如 Claw 频道）抢占导致互踢")
                    self.session_id = None
                    self.last_seq = None
                    return
                elif op == 11:
                    pass
                else:
                    self.log.debug("op=%s %s", op, frame)

            hb_task.cancel()
            self.log.info("WebSocket 已关闭")
            # 连接已关闭：清掉 session_id，下次重连走全新 Identify，
            # 避免拿失效的旧 session 反复 RESUME 被服务端关连接（踢断后假活真掉线）。
            self.session_id = None

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.hb_interval * 0.8)
            try:
                await self.send_frame({"op": 1, "d": self.last_seq})
            except Exception as e:
                self.log.warning("心跳发送失败：%s", e)
                return

    # ---- 事件分发 ----
    @staticmethod
    def _dedup_seen(store: OrderedDict, key: str, ttl: float = 600) -> bool:
        now = time.time()
        while store and now - next(iter(store.values())) > ttl:
            store.popitem(last=False)
        if key in store:
            return True
        store[key] = now
        return False

    async def on_dispatch(self, frame: dict):
        t = frame.get("t")
        d = frame.get("d") or {}

        # 诊断：把所有收到的 dispatch 事件类型都打出来（含当前未处理的），
        # 便于确认平台到底推了什么事件、是不是 C2C 没推到。
        if t not in ("READY", "RESUMED"):
            self.log.info("dispatch 事件 t=%s d=%s", t, json.dumps(d, ensure_ascii=False)[:300])

        if t == "READY":
            self.session_id = d.get("session_id")
            if not self._ready_logged:
                self.log.info("认证成功，长连接已建立（session=%s，user=%s）",
                         (self.session_id or "")[:8] + "…",
                         (d.get("user") or {}).get("username", "?"))
                self._ready_logged = True
            return
        if t == "RESUMED":
            self.log.info("Resume 成功，已恢复会话")
            return

        if t == "C2C_MESSAGE_CREATE":
            await self.on_message(d, scene="单聊")
        elif t == "GROUP_AT_MESSAGE_CREATE":
            await self.on_message(d, scene="群聊@")
        elif t == "MESSAGE_AUDIT":
            audit = d.get("audit_result") or d
            self.log.info("主动消息审核：%s", json.dumps(audit, ensure_ascii=False)[:200])
            archive_raw(self.bot, "[MESSAGE_AUDIT]", {"t": t, "d": d})
        elif t == "INTERACTION_CREATE":
            self.log.info("互动事件：%s", json.dumps(d, ensure_ascii=False)[:150])
        else:
            self.log.debug("未处理事件 %s", t)

    async def on_message(self, d: dict, scene: str):
        msg_id = d.get("id") or ""
        if msg_id and self._dedup_seen(self.seen_msgids, msg_id):
            self.log.debug("重复推送，已去重：%s", msg_id[:16])
            return

        author = d.get("author") or {}
        openid = author.get("user_openid") or author.get("id") or ""
        group_openid = d.get("group_openid") or ""
        text = extract_text(d)

        if openid:
            nickname = author.get("username") or ""
            if not nickname:
                cand = extract_self_intro_name(text)
                if cand:
                    nickname = _unique_nickname(self.bot, openid, cand)
                    self.log.info("自动识别姓名：%s… →「%s」", openid[:12], nickname)
            learn_contact(self.bot, openid, nickname)

        # 归档 key：群聊按「群openid」分开存，单聊按联系人
        if scene.startswith("群聊"):
            target = group_openid or f"群{group_openid[:8]}"
            archive_key = group_openid
        else:
            archive_key = openid
        name = display_name(self.bot, archive_key) if archive_key else "未知"

        archive_raw(self.bot, name, {"t": "MESSAGE", "scene": scene, "d": d})
        archive_md(self.bot, name, d, text, direction="in", scene=scene)
        self.log.info("[%s] 收到%s消息 from %s: %s", name, scene, openid[:12] + "…", text[:50])

        # 自动应答（四类话术，见 build_reply）：由该机器人的 auto_reply 控制；
        # auto_reply_skip 名单内对象不回复；绝不回显原文、绝不涉报价/确认成交
        if self.cfg.get("auto_reply") and openid:
            skip = self.cfg.get("auto_reply_skip") or []
            if name not in skip and openid not in skip:
                reply = build_reply(text)
                if reply:
                    try:
                        ok, resp = await self.send_message(
                            openid, reply, msg_type=0, group=bool(group_openid),
                            msg_id=msg_id, msg_seq=1)
                        if ok:
                            archive_raw(self.bot, name, {"t": "REPLY", "d": {"content": reply}},
                                        direction="out")
                            archive_md(self.bot, name, {"content": reply}, reply,
                                       direction="out", scene=scene)
                            self.log.info("[%s] 已自动应答：%s", name, reply[:40])
                        else:
                            self.log.error("[%s] 自动应答失败：%s", name, resp)
                    except Exception as e:
                        self.log.error("[%s] 自动应答异常：%s", name, e)

    # ---- 发送队列（每个机器人一个独立队列文件）----
    async def outbox_loop(self):
        # 轮询间隔 0.5s：空跑一次仅约 0.06s（读小文件+遍历），CPU 开销可忽略；
        # 换来的是消息入队后最多 0.5s 即被发出（原先 2s 间隔有最长 2 秒延迟）。
        while not self.stop_event.is_set():
            try:
                await self.process_outbox()
            except Exception as e:
                self.log.error("outbox 扫描异常：%s", e)
            await asyncio.sleep(0.5)

    async def process_outbox(self):
        outbox = outbox_file(self.bot)
        if not outbox.exists():
            return
        lines = [l.strip() for l in outbox.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            return

        remaining, failed, changed = [], [], False

        for line in lines:
            try:
                task = json.loads(line)
            except json.JSONDecodeError:
                self.log.warning("outbox 有无法解析的行，跳过：%s", line[:80])
                continue

            target = task.get("target", "")
            openid = resolve_openid(self.bot, target)
            content = task.get("content", "")
            msg_type = 2 if task.get("msg_type") == "markdown" else 0
            group = bool(task.get("group"))

            if not openid:
                now = time.time()
                last = self._warned_missing.get(target, 0)
                if now - last > 60:
                    self.log.warning("联系人「%s」的 openid 未配置（contacts.json），任务继续挂起。"
                                "等他给机器人发过消息后，openid 会出现在 "
                                "contacts_learned.json 里", target)
                    self._warned_missing[target] = now
                remaining.append(line)
                continue

            if not content:
                self.log.warning("outbox 任务缺少 content，跳过")
                continue

            key = ("g:" if group else "u:") + openid
            allowed, why = self.limiter.check(key)
            if not allowed:
                self.log.warning("「%s」触发限速：%s，任务保留", target, why)
                remaining.append(line)
                continue

            ok, resp = await self.send_message(
                openid, content, msg_type=msg_type, group=group,
                msg_id=task.get("msg_id"), msg_seq=task.get("msg_seq"))

            if ok:
                self.limiter.commit(key)
                archive_raw(self.bot, target, {"t": "SEND", "d": {"content": content,
                                                        "msg_type": msg_type,
                                                        "scene": "群聊" if group else "单聊"}},
                            direction="out")
                archive_md(self.bot, target, {"content": content}, content, direction="out",
                           scene="群聊" if group else "单聊")
                self.log.info("已发出 → %s: %s", target, content[:50])
                changed = True
            else:
                self.log.error("发送失败（保留队列待重试）→ %s：%s", target, resp)
                failed.append(line)
                changed = True

        if changed:
            try:
                outbox.write_text(
                    ("\n".join(remaining) + "\n") if remaining else "", encoding="utf-8")
                if failed:
                    with open(outbox_failed_file(self.bot), "a", encoding="utf-8") as f:
                        for l in failed:
                            f.write(l + "\n")
            except OSError as e:
                self.log.error("outbox 回写失败：%s", e)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def main():
    bots = load_config()
    if not bots:
        log.error("=" * 60)
        log.error("qq_config.json 未配置任何机器人")
        log.error("支持两种写法：{\"bots\": {\"机器人A\": {…}}} 或旧版单机器人 {appid, appsecret, …}")
        log.error("=" * 60)
        return

    usable = {}
    for name, cfg in bots.items():
        if cfg.get("appid") and cfg.get("appsecret"):
            usable[name] = cfg
        else:
            log.error("[%s] 缺少 appid / appsecret，已跳过", name)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        log.info("收到退出信号，正在断开……")
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, request_stop)

    log.info("=" * 60)
    log.info("共%d 个机器人：%s", len(usable), "、".join(usable) or "（无）")
    for name, cfg in usable.items():
        log.info("  · %s  appid=%s  环境=%s", name, cfg.get("appid"),
                 "沙箱" if cfg.get("sandbox") else "正式")
    log.info("=" * 60)

    tasks = []
    for name, cfg in usable.items():
        gw = QQGateway(name, cfg, stop_event)
        tasks.append(asyncio.create_task(gw.run_forever()))
        tasks.append(asyncio.create_task(gw.outbox_loop()))

    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("全部连接已关闭，退出。")


if __name__ == "__main__":
    asyncio.run(main())
