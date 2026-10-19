#!/usr/bin/env python3
"""
企微智能机器人长连接客户端 —— 双机器人消息归档

功能：
- 同时订阅「报价机器人」「机器人A」两个智能机器人
- 通过 WebSocket 长连接 (wss://openws.work.weixin.qq.com) 接收所有回调消息
- 归档到 data/messages_YYYY-MM-DD.jsonl（原始帧）+ data/chat_机器人_YYYY-MM-DD.md（对话式）
- 无需服务器 / 公网 IP / Token / AESKey，仅 Bot ID + Secret

运行：python ws_client.py   （Ctrl+C 退出）
"""

import asyncio
import json
import logging
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from aibot import WSClient, WSClientOptions, generate_req_id

# ===========================================================================
# 凭据配置：可同时挂多个机器人，每个机器人一条独立长连接（互不影响）
# 填入真实 bot_id / secret；同一机器人同时只能有一个长连接（新的会互踢旧的）
# ===========================================================================
BOTS = {
    "机器人A": {
        "bot_id": "在此填入企业微信机器人的 bot_id",
        "secret": "在此填入对应的 secret",
    },
    "机器人B": {
        "bot_id": "在此填入企业微信机器人的 bot_id",
        "secret": "在此填入对应的 secret",
    },
    # 再加机器人照此格式往下写即可，键名（机器人名）必须唯一：
    # "机器人名称": {"bot_id": "...", "secret": "..."},
}

# 历史别名 → 现机器人名。改名后旧名仍可用于 send.py / outbox 路由，避免存量任务被丢弃。
# 部署时按实际情况改：左侧旧名，右侧现用名。
BOT_ALIASES = {
    # "旧机器人名": "现机器人名",
}


def canonical_bot(name: str) -> str:
    return BOT_ALIASES.get(name, name)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

# 收到消息后是否自动应答（四类话术，见下方 build_reply）
# 默认开启；如需对某人不应答，把其 userid / 显示名加入 AUTO_REPLY_SKIP 即可。
AUTO_REPLY = True
# 不应答的对象（userid 或 显示名）：当前为空 = 全员自动应答
AUTO_REPLY_SKIP = set()
# 应答话术：四类规则见下方 build_reply（人工服务 / 机器人介绍 / 对方自我介绍 / 报价回复兜底）；
# 绝不回显原文、绝不涉报价/确认成交
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # 目录已存在或被宿主环境代理拦截，直接使用
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ws_client")


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------
def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def raw_file(bot_name: str) -> Path:
    """每个机器人一个独立 JSONL：data/messages_<机器人名>_<日期>.jsonl

    按机器人物理分文件，避免多机器人共用一个文件导致客户沟通内容混淆
    （取数脚本 daily_quote_email.py --bot 也按此命名读取）。
    """
    return DATA_DIR / f"messages_{safe_name(bot_name)}_{today()}.jsonl"


def safe_name(name: str) -> str:
    """机器人名转文件名安全形式（与 qq-ws 侧 safe() 规则一致）"""
    out = re.sub(r"[^\w一-龥\-]+", "_", (name or "").strip())
    return out or "default"


def archive_raw(bot_name: str, frame_dict: dict, direction: str = "in") -> None:
    """原始帧追加到本机器人的 JSONL

    direction: in=收到（客户→机器人），out=发出（机器人→客户）
    """
    record = {
        "archived_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bot": bot_name,
        "direction": direction,
        "frame": frame_dict,
    }
    path = raw_file(bot_name)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_text(body: dict) -> str:
    """从消息 body 提取可读文本"""
    msgtype = body.get("msgtype", "")
    if msgtype == "text":
        return body.get("text", {}).get("content", "")
    if msgtype == "markdown":
        # 发出的消息默认是 markdown 格式
        return body.get("markdown", {}).get("content", "")
    if msgtype == "image":
        return "[图片]"
    if msgtype == "voice":
        return f"[语音] {body.get('voice', {}).get('content', '')}"
    if msgtype == "file":
        return f"[文件] {body.get('file', {}).get('filename', '')}"
    if msgtype == "video":
        return "[视频]"
    if msgtype == "mixed":
        parts = []
        for item in body.get("mixed", {}).get("msg_item", []):
            if item.get("msgtype") == "text":
                parts.append(item.get("text", {}).get("content", ""))
            elif item.get("msgtype") == "image":
                parts.append("[图片]")
        return " ".join(parts)
    return f"[{msgtype}]"


# ---------------------------------------------------------------------------
# 应答话术（四类规则；与 QQ qq-ws/qq_client.py 保持完全同步，单侧改动请同步另一侧）
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
# 问题1 触发句式：「我是X / 我叫X / 我叫做X」
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




def archive_md(bot_name: str, body: dict, text: str, direction: str = "in",
               chat_id: str = "") -> None:
    """对话式 MD 追加

    direction: in=收到，out=发出
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chattype = body.get("chattype", "single")  # single / group
    sender = body.get("from", {}).get("userid", "?")

    if direction == "out":
        # 发出的消息：说话人是机器人自己
        who = f"**{bot_name}（发出）**"
        target = chat_id or body.get("chatid") or sender
        scene = f"群聊 {target}" if chattype == "group" else f"单聊 → {target}"
    else:
        who = f"**{sender}**"
        scene = f"群聊 {body.get('chatid', '')}" if chattype == "group" else "单聊"

    path = DATA_DIR / f"chat_{bot_name}_{today()}.md"
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {bot_name} 对话记录 {today()}\n\n")

    with open(path, "a", encoding="utf-8") as f:
        f.write(f"**{ts}｜{scene}｜{who}：**\n\n{text}\n\n---\n\n")


# ---------------------------------------------------------------------------
# 发送队列（outbox）
# ---------------------------------------------------------------------------
# 为什么需要：企微不会把「机器人发出的消息」回推给自己，API 也不提供单聊历史，
# 所以发出的内容必须自己留一份，否则归档只有单向。
#
# 用法：往 outbox.jsonl 追加一行即可由常驻进程自动发送并归档
#   {"bot": "机器人A", "chat_id": "<chat_id>", "content": "您好"}
# 可选字段：msgtype（默认 markdown）
OUTBOX = BASE_DIR / "outbox.jsonl"
OUTBOX_FAILED = BASE_DIR / "outbox_failed.jsonl"

# 发送节奏控制：避免触发企微 aibot 频率限制（errcode=846607）
# - SEND_GAP：两条发送之间的最小间隔（秒），防止瞬间连发被限流
# - RATE_LIMIT_BACKOFF：命中 846607 后的退避时间（秒），给频控窗口留出恢复时间
# - RATE_LIMIT_UNTIL：全局时间戳，在此之前暂停发送（由 process_outbox 写入）
SEND_GAP = 3.0
RATE_LIMIT_BACKOFF = 60.0
RATE_LIMIT_UNTIL = 0.0


def read_outbox():
    """读取待发队列，返回 (行列表, 剩余行列表)"""
    if not OUTBOX.exists():
        return [], []
    lines = [l.strip() for l in OUTBOX.read_text(encoding="utf-8").splitlines() if l.strip()]
    return lines, []


async def process_outbox(bot_name: str, client: WSClient) -> None:
    """扫描并发送本机器人待发消息，发送后归档并从队列移除"""
    if not OUTBOX.exists():
        return
    lines = [l.strip() for l in OUTBOX.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return

    global RATE_LIMIT_UNTIL
    # 退避期内：本次扫描直接跳过，不改动队列，等频控窗口恢复
    if time.time() < RATE_LIMIT_UNTIL:
        return

    remaining = []
    changed = False

    for idx, line in enumerate(lines):
        try:
            task = json.loads(line)
        except json.JSONDecodeError:
            log.warning("[%s] outbox 有无法解析的行，跳过：%s", bot_name, line[:80])
            continue

        # 只处理属于自己的机器人（兼容改名前的旧名）
        if canonical_bot(task.get("bot", "")) != bot_name:
            remaining.append(line)
            continue

        chat_id = task.get("chat_id")
        content = task.get("content")
        if not chat_id or not content:
            log.warning("[%s] outbox 任务缺少 chat_id/content，跳过", bot_name)
            continue

        msgtype = task.get("msgtype", "markdown")
        if msgtype == "markdown":
            body = {"msgtype": "markdown", "markdown": {"content": content}}
        else:
            body = {"msgtype": "text", "text": {"content": content}}

        try:
            await client.send_message(chat_id, body)
        except Exception as e:
            if "846607" in str(e):
                # 命中频率限制：写入退避时间戳并中止本轮，避免持续连发越限
                RATE_LIMIT_UNTIL = time.time() + RATE_LIMIT_BACKOFF
                log.warning("[%s] 命中频率限制 846607，退避 %.0fs 后下次扫描再试：%s",
                            bot_name, RATE_LIMIT_BACKOFF, e)
                remaining.append(line)
                # 保留本轮尚未处理的后续任务，避免 break 时丢失
                remaining.extend(lines[idx + 1:])
                changed = True
                break
            log.error("[%s] 发送失败（保留队列待重试）：%s", bot_name, e)
            remaining.append(line)
            changed = True
            continue

        # 发送成功 → 归档为「发出」
        archive_raw(bot_name, {
            "cmd": "aibot_send_msg",
            "body": {
                "chatid": chat_id,
                "chattype": task.get("chattype", "single"),
                "msgtype": msgtype,
                "markdown" if msgtype == "markdown" else "text": {"content": content},
            },
        }, direction="out")
        archive_md(bot_name,
                   {"chattype": task.get("chattype", "single")},
                   content, direction="out", chat_id=chat_id)
        log.info("[%s] 已发出 → %s: %s", bot_name, chat_id, content[:50])
        changed = True
        # 两条发送之间留出间隔，避免瞬间连发触发频控
        await asyncio.sleep(SEND_GAP)

    if changed:
        # 回写剩余（单进程，无并发）
        try:
            OUTBOX.write_text(
                ("\n".join(remaining) + "\n") if remaining else "", encoding="utf-8"
            )
        except OSError as e:
            log.error("[%s] outbox 回写失败：%s", bot_name, e)


# ---------------------------------------------------------------------------
# 事件处理
# ---------------------------------------------------------------------------
def make_handlers(bot_name: str, client: WSClient):
    def on_authenticated():
        log.info("[%s] 认证成功，长连接已建立", bot_name)

    def on_connected():
        log.info("[%s] WebSocket 已连接", bot_name)

    def on_disconnected(reason):
        log.warning("[%s] 连接断开：%s，将自动重连", bot_name, reason)

    def on_error(err):
        log.error("[%s] 错误：%s", bot_name, err)

    async def on_message(frame):
        body = frame.get("body", {}) if isinstance(frame, dict) else {}
        if not body:
            # frame 可能是 WsFrameHeaders 对象
            try:
                body = frame.body or {}
            except AttributeError:
                body = {}
        archive_raw(bot_name, {"cmd": frame.get("cmd") if isinstance(frame, dict) else str(frame), "body": body})
        text = extract_text(body)
        archive_md(bot_name, body, text)
        sender = body.get("from", {}).get("userid", "?")
        chattype = body.get("chattype", "?")
        log.info("[%s] 收到%s消息 from %s: %s", bot_name, chattype, sender, text[:50])

        # 自动应答：由 AUTO_REPLY 控制（四类话术，见 build_reply）
        # 跳过名单内对象（如本人），且非空消息才应答
        if AUTO_REPLY and sender not in AUTO_REPLY_SKIP and text:
            reply_text = build_reply(text)
            if reply_text:
                try:
                    # 用与发报价相同的已验证通道发送（单聊 chat_id = 发送方 userid）
                    # 注意：aibot SDK 的 send_message 仅支持 markdown / template_card，
                    # 不支持 text，发 text 会被服务端以 40008 invalid message type 拒绝。
                    await client.send_message(
                        sender, {"msgtype": "markdown", "markdown": {"content": reply_text}}
                    )
                    # 应答也归档，保证对话双向完整
                    archive_raw(bot_name, {
                        "cmd": "aibot_send_msg",
                        "body": {
                            "chatid": sender,
                            "chattype": chattype,
                            "msgtype": "markdown",
                            "markdown": {"content": reply_text},
                        },
                    }, direction="out")
                    archive_md(bot_name, {"chattype": chattype}, reply_text,
                               direction="out", chat_id=sender)
                    log.info("[%s] 已自动应答 %s: %s", bot_name, sender, reply_text[:50])
                except Exception as e:
                    log.error("[%s] 自动应答失败：%s", bot_name, e)

    async def on_enter_chat(frame):
        body = frame.get("body", {}) if isinstance(frame, dict) else {}
        user = body.get("from", {}).get("userid", "?")
        log.info("[%s] 用户 %s 进入会话", bot_name, user)
        archive_raw(bot_name, {"cmd": "aibot_event_callback", "body": body})

    return {
        "authenticated": on_authenticated,
        "connected": on_connected,
        "disconnected": on_disconnected,
        "error": on_error,
        "message": on_message,
        "event.enter_chat": on_enter_chat,
    }


async def run_bot(bot_name: str, bot_id: str, secret: str, stop_event: asyncio.Event):
    client = WSClient(
        WSClientOptions(
            bot_id=bot_id,
            secret=secret,
            reconnect_interval=1000,
            max_reconnect_attempts=-1,  # 无限重连
            heartbeat_interval=30000,
        )
    )
    handlers = make_handlers(bot_name, client)
    for event, handler in handlers.items():
        client.on(event, handler)

    connect_task = asyncio.create_task(client.connect())

    async def watch_stop():
        await stop_event.wait()
        try:
            await client.disconnect()
        except Exception:
            pass

    async def outbox_loop():
        """每 2 秒扫描一次待发队列；退避期内仅空转等待频控窗口恢复"""
        while not stop_event.is_set():
            try:
                if client.is_connected and time.time() >= RATE_LIMIT_UNTIL:
                    await process_outbox(bot_name, client)
            except Exception as e:
                log.error("[%s] outbox 扫描异常：%s", bot_name, e)
            await asyncio.sleep(2)

    asyncio.create_task(watch_stop())
    await connect_task
    asyncio.create_task(outbox_loop())
    # connect() 返回后保持事件循环存活（SDK 内部管理重连）
    try:
        while not stop_event.is_set():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


async def main():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        log.info("收到退出信号，正在断开……")
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, request_stop)

    tasks = []
    for name, cred in BOTS.items():
        tasks.append(
            asyncio.create_task(run_bot(name, cred["bot_id"], cred["secret"], stop_event))
        )
        log.info("[%s] 启动订阅……", name)

    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("全部连接已关闭，退出。")


if __name__ == "__main__":
    asyncio.run(main())
