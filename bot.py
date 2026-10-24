# -*- coding: utf-8 -*-
"""
Vidox Pay | بوت شراء نقاط Vidox
Telegram Stars payment bot. Shares the SAME Turso database as the Vidox bot:
it credits points straight into Vidox's `users.points_balance`.
Everything (prices, packages, support, terms, welcome text) is editable from /admin - no code edits needed.

ENVIRONMENT (set in Render):
    BOT_TOKEN            - token of the PAYMENT bot
    TURSO_DATABASE_URL   - same value as the Vidox bot
    TURSO_AUTH_TOKEN     - same value as the Vidox bot
  optional:
    ADMIN_IDS            - extra admin ids, comma separated (the main admin is hardcoded below)
    VIDOX_BOT_TOKEN      - token of the Vidox bot, so the "points added" notice comes from Vidox itself
    VIDOX_BOT_USERNAME   - Vidox bot username without @ (default below; also editable in /admin)
"""
import asyncio
import base64
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, ChatMemberUpdated, ErrorEvent,
                           InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery)

import keep_alive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vidoxpay")

# ----------------------------------------------------------------- config (hardcoded, not secret)
ADMIN_IDS = {6043858925}
VIDOX_BOT_USERNAME = os.getenv("VIDOX_BOT_USERNAME", "vdox_2_bot").strip().lstrip("@")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
VIDOX_BOT_TOKEN = os.getenv("VIDOX_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
for _p in re.split(r"[,\s;]+", os.getenv("ADMIN_IDS", "")):
    if _p.strip().isdigit():
        ADMIN_IDS.add(int(_p))

esc = html.escape


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def iso_now() -> str:
    return iso(now_utc())


def bahrain_time(dt_str: Optional[str]) -> str:
    if not dt_str:
        return "-"
    try:
        d = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return (d + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt_str


# =====================================================================
#  Turso (libSQL) over HTTP
# =====================================================================
class DBError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"DB {status}: {text[:300]}")
        self.status = status
        self.text = text


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTRE = re.compile(r"^-?\d+$")
_OPS = {"eq": "=", "neq": "<>", "gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
_NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%S+00:00','now')"


def _sv(v: Any) -> dict:
    """python value -> Turso (Hrana) value"""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, (dict, list)):
        return {"type": "text", "value": json.dumps(v, ensure_ascii=False)}
    return {"type": "text", "value": str(v)}


def _pv(c: dict) -> Any:
    """Turso value -> python value"""
    t = c.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(c["value"])
    if t == "float":
        return float(c["value"])
    if t == "blob":
        s = c.get("base64", "")
        return base64.b64decode(s + "=" * (-len(s) % 4))
    return c.get("value")


def _stmt(sql: str, args: Optional[list] = None) -> dict:
    return {"sql": sql, "args": [_sv(a) for a in (args or [])]}


def _cv(s: str) -> Any:
    if s == "true":
        return 1
    if s == "false":
        return 0
    if _INTRE.match(s):
        return int(s)
    return s


def _where(filters: Optional[dict], prefix: str = "") -> tuple:
    parts: list = []
    args: list = []
    for k, v in (filters or {}).items():
        if not _IDENT.match(k):
            raise DBError(400, f"bad column {k}")
        op, _, rest = str(v).partition(".")
        col = prefix + k
        if op == "in":
            vals = [_cv(x) for x in rest.strip("()").split(",") if x != ""]
            if not vals:
                parts.append("0")
            else:
                parts.append(f"{col} IN ({','.join('?' * len(vals))})")
                args += vals
        elif op == "is":
            parts.append(f"{col} IS NULL" if rest == "null" else f"{col} IS NOT NULL")
        elif op in _OPS:
            parts.append(f"{col} {_OPS[op]} ?")
            args.append(_cv(rest))
        else:
            raise DBError(400, f"bad filter {v}")
    return (" WHERE " + " AND ".join(parts)) if parts else "", args


def _cols(columns: str) -> str:
    if columns.strip() == "*":
        return "*"
    out = []
    for c in columns.split(","):
        c = c.strip()
        if not _IDENT.match(c):
            raise DBError(400, f"bad column {c}")
        out.append(c)
    return ", ".join(out)


def _order(order: Optional[str]) -> str:
    if not order:
        return ""
    parts = []
    for tok in order.split(","):
        col, _, d = tok.strip().partition(".")
        if not _IDENT.match(col):
            raise DBError(400, f"bad order {tok}")
        parts.append(f"{col} {'DESC' if d.lower() == 'desc' else 'ASC'}")
    return " ORDER BY " + ", ".join(parts)



class DB:
    """Turso (libSQL) over its HTTP API. Same small interface the rest of the bot uses."""

    def __init__(self, url: str, token: str):
        url = url.strip().rstrip("/")
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        elif url.startswith("wss://"):
            url = "https://" + url[len("wss://"):]
        elif not url.startswith("http"):
            url = "https://" + url
        self.endpoint = url + "/v2/pipeline"
        self.headers = {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
        self.lock = asyncio.Lock()

    # ------------------------------------------------------------ transport
    async def _post(self, requests: list, safe_retry: bool) -> list:
        body = {"requests": requests + [{"type": "close"}]}
        last: Any = None
        for attempt in range(4):
            try:
                r = await self.client.post(self.endpoint, headers=self.headers, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last = e
                await asyncio.sleep(0.7 * (attempt + 1))
                continue
            except httpx.TransportError as e:
                last = e
                if safe_retry:
                    await asyncio.sleep(0.7 * (attempt + 1))
                    continue
                raise DBError(0, f"connection lost: {e}")
            if r.status_code == 429 or r.status_code >= 500:
                last = r.text
                if attempt < 3 and (safe_retry or r.status_code in (429, 502, 503)):
                    await asyncio.sleep(0.7 * (attempt + 1))
                    continue
                raise DBError(r.status_code, r.text)
            if r.status_code >= 400:
                raise DBError(r.status_code, r.text)
            return r.json()["results"]
        raise DBError(0, f"connection failed: {last}")

    @staticmethod
    def _parse(res: dict) -> dict:
        cols = [c.get("name") for c in res.get("cols", [])]
        rows = [{cols[i]: _pv(cell) for i, cell in enumerate(row)} for row in res.get("rows", [])]
        return {"rows": rows, "affected": int(res.get("affected_row_count") or 0)}

    async def _run(self, stmts: list) -> list:
        """Run independent statements in one round-trip. Returns [{rows, affected}]."""
        reqs = [{"type": "execute", "stmt": _stmt("PRAGMA foreign_keys = ON")}]
        reqs += [{"type": "execute", "stmt": _stmt(s, a)} for s, a in stmts]
        safe = all(s.lstrip()[:6].upper() == "SELECT" for s, _ in stmts)
        results = await self._post(reqs, safe)
        out = []
        for res in results[1:1 + len(stmts)]:
            if res.get("type") == "error":
                raise DBError(400, res.get("error", {}).get("message", "error"))
            out.append(self._parse(res["response"]["result"]))
        return out

    async def _q(self, sql: str, args: Optional[list] = None) -> list:
        return (await self._run([(sql, args or [])]))[0]["rows"]

    async def tx(self, stmts: list) -> list:
        """All statements succeed together or none is applied (one atomic round-trip)."""
        n = len(stmts)
        steps: list = [{"stmt": _stmt("PRAGMA foreign_keys = ON")}, {"stmt": _stmt("BEGIN")}]
        for i, (s, a) in enumerate(stmts):
            steps.append({"stmt": _stmt(s, a), "condition": {"type": "ok", "step": 1 + i}})
        commit = 2 + n
        steps.append({"stmt": _stmt("COMMIT"), "condition": {"type": "ok", "step": commit - 1}})
        steps.append({"stmt": _stmt("ROLLBACK"),
                      "condition": {"type": "not", "cond": {"type": "ok", "step": commit}}})
        results = await self._post([{"type": "batch", "batch": {"steps": steps}}], False)
        r = results[0]
        if r.get("type") == "error":
            raise DBError(400, r.get("error", {}).get("message", "error"))
        res = r["response"]["result"]
        sr, se = res["step_results"], res["step_errors"]
        if sr[commit] is None:
            for i in range(2, commit + 1):
                if se[i]:
                    raise DBError(409, se[i].get("message", "transaction failed"))
            raise DBError(409, "transaction failed")
        return [self._parse(sr[2 + i]) for i in range(n)]

    async def init_schema(self) -> None:
        results = await self._post(
            [{"type": "execute", "stmt": _stmt(s)} for s in SCHEMA], False)
        for res in results[:len(SCHEMA)]:
            if res.get("type") == "error":
                raise DBError(400, res.get("error", {}).get("message", "schema error"))


# =====================================================================
#  payment bot schema (all tables are prefixed pay_ so they never clash with Vidox)
# =====================================================================
SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS pay_packages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stars INTEGER NOT NULL CHECK (stars > 0), points INTEGER NOT NULL CHECK (points > 0),
        active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT ({_NOW_SQL}))""",
    f"""CREATE TABLE IF NOT EXISTS pay_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, package_id INTEGER,
        stars INTEGER NOT NULL, points INTEGER NOT NULL,
        charge_id TEXT NOT NULL UNIQUE, provider_charge_id TEXT,
        status TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid','refunded','unclaimed')),
        created_at TEXT NOT NULL DEFAULT ({_NOW_SQL}), refunded_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS pay_orders_user_idx ON pay_orders (telegram_id)",
    "CREATE INDEX IF NOT EXISTS pay_orders_date_idx ON pay_orders (created_at)",
    f"""CREATE TABLE IF NOT EXISTS pay_users (
        telegram_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, lang TEXT,
        terms_ok INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT ({_NOW_SQL}), last_active_at TEXT NOT NULL DEFAULT ({_NOW_SQL}))""",
    "CREATE TABLE IF NOT EXISTS pay_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    f"""CREATE TABLE IF NOT EXISTS pay_state (
        admin_id INTEGER PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{{}}')""",
    f"""CREATE TABLE IF NOT EXISTS pay_broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, audience TEXT NOT NULL,
        src_chat INTEGER NOT NULL, src_message INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        total INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT ({_NOW_SQL}), finished_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS pay_broadcast_recipients (
        broadcast_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        PRIMARY KEY (broadcast_id, telegram_id))""",
    f"""CREATE TABLE IF NOT EXISTS pay_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT NOT NULL, details TEXT,
        created_at TEXT NOT NULL DEFAULT ({_NOW_SQL}))""",
    # default packages (only when the table is empty) - edit them later from /admin
    """INSERT INTO pay_packages (stars, points, sort_order)
        SELECT 150, 200, 1 WHERE NOT EXISTS (SELECT 1 FROM pay_packages)""",
    """INSERT INTO pay_packages (stars, points, sort_order)
        SELECT 300, 500, 2 WHERE (SELECT COUNT(*) FROM pay_packages) = 1""",
    """INSERT INTO pay_packages (stars, points, sort_order)
        SELECT 600, 1200, 3 WHERE (SELECT COUNT(*) FROM pay_packages) = 2""",
    """INSERT INTO pay_packages (stars, points, sort_order)
        SELECT 1200, 3000, 4 WHERE (SELECT COUNT(*) FROM pay_packages) = 3""",
]

DEFAULT_TERMS = (
    "📜 <b>الشروط والأحكام | Terms</b>\n\n"
    "1) النقاط سلعة رقمية تُضاف إلى رصيدك في بوت Vidox فور نجاح الدفع.\n"
    "2) النقاط خاصة بحسابك ولا يمكن تحويلها أو بيعها.\n"
    "3) الدفع يتم بنجوم تيليجرام (⭐) فقط.\n"
    "4) إذا لم تصلك النقاط بعد الدفع تواصل معنا عبر /paysupport وسنعالج الأمر أو نعيد النجوم.\n"
    "5) دعم تيليجرام لا يستطيع مساعدتك في مشتريات هذا البوت، الدعم عندنا فقط.\n"
    "6) لا يُستردّ الشراء بعد استخدام النقاط إلا بقرار الإدارة.\n\n"
    "Points are a digital good credited to your Vidox balance right after payment. Non-transferable. "
    "Paid with Telegram Stars only. If points do not arrive, use /paysupport. Telegram support cannot help "
    "with purchases made in this bot."
)

SETTING_DEFAULTS = {
    "support_username": "ti_tan_7",
    "vidox_username": VIDOX_BOT_USERNAME,
    "terms_text": DEFAULT_TERMS,
    "welcome_text": "",
    "maintenance": "false",
}
_settings: dict = {}


def S(key: str) -> str:
    return _settings.get(key, SETTING_DEFAULTS.get(key, ""))


async def load_settings() -> None:
    rows = await db._q("SELECT key, value FROM pay_settings")
    _settings.clear()
    _settings.update({r["key"]: r["value"] for r in rows})


async def set_setting(key: str, value: str) -> None:
    await db._run([("INSERT INTO pay_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value", [key, value])])
    _settings[key] = value


# =====================================================================
#  texts (Arabic / English)
# =====================================================================
TX = {
    "ar": {
        "welcome": "💦 <b>شراء نقاط Vidox</b>\n\nاشحن رصيدك بنجوم تيليجرام وتصلك النقاط فوراً.\n\n💰 رصيدك الحالي: <b>{bal}</b> نقطة\n\nاختر الباقة 👇🏻",
        "terms_gate": "قبل الشراء يرجى قراءة الشروط والموافقة عليها 👇🏻",
        "agree": "أوافق ✅",
        "agreed": "تمت الموافقة ✅",
        "pkg_btn": "{stars} ⭐  ←  {points} نقطة 💦",
        "open_vidox": "🎬 العودة إلى Vidox",
        "need_vidox": "⚠️ لازم تفتح بوت Vidox وتضغط /start أولاً حتى يُنشأ حسابك، ثم ارجع هنا واشحن 👇🏻",
        "unavailable": "هذه الباقة غير متاحة الآن.",
        "maint": "🛠 الشراء متوقف مؤقتاً للصيانة، حاول لاحقاً.",
        "invoice_title": "{points} نقطة Vidox",
        "invoice_desc": "شحن {points} نقطة في رصيدك داخل Vidox",
        "ok": "✅ <b>تمت العملية بنجاح!</b>\n\nأُضيفت <b>{points} نقطة</b> 💦 إلى رصيدك.\nرصيدك الآن: <b>{bal}</b>\n\nاستمتع بالمشاهدة 🎬",
        "vidox_notice": "✅ تم شحن رصيدك بـ <b>{points} نقطة</b> 💦\nرصيدك الآن: <b>{bal}</b>\nاستمتع بالمشاهدة 🎬",
        "auto_refund": "⚠️ تعذر ربط الدفع بحسابك في Vidox، فأعدنا لك النجوم كاملة. افتح بوت Vidox واضغط /start ثم أعد المحاولة.",
        "pay_error": "⚠️ حدثت مشكلة أثناء إضافة النقاط. لا تقلق، تم تسجيل دفعتك وستُعالج قريباً. تواصل معنا عبر /paysupport.",
        "support": "🛟 <b>الدعم</b>\n\nلأي مشكلة في الدفع أو النقاط تواصل معنا وأرسل رقم العملية.\n⚠️ دعم تيليجرام (@botsupport) لا يستطيع مساعدتك في مشتريات هذا البوت.",
        "support_btn": "💬 تواصل مع الدعم",
        "last_orders": "\n\n🧾 <b>آخر عملياتك:</b>\n{rows}",
        "no_orders": "\n\nلا توجد عمليات بعد.",
        "balance": "💰 رصيدك في Vidox: <b>{bal}</b> نقطة",
        "banned": "⛔ حسابك محظور.",
    },
    "en": {
        "welcome": "💦 <b>Buy Vidox points</b>\n\nTop up with Telegram Stars and get your points instantly.\n\n💰 Current balance: <b>{bal}</b> points\n\nChoose a package 👇🏻",
        "terms_gate": "Please read and accept the terms before buying 👇🏻",
        "agree": "I agree ✅",
        "agreed": "Accepted ✅",
        "pkg_btn": "{stars} ⭐  →  {points} points 💦",
        "open_vidox": "🎬 Back to Vidox",
        "need_vidox": "⚠️ Open the Vidox bot and press /start first so your account is created, then come back and top up 👇🏻",
        "unavailable": "This package is not available right now.",
        "maint": "🛠 Purchases are paused for maintenance, try again later.",
        "invoice_title": "{points} Vidox points",
        "invoice_desc": "Top up {points} points to your Vidox balance",
        "ok": "✅ <b>Payment successful!</b>\n\n<b>{points} points</b> 💦 were added to your balance.\nBalance: <b>{bal}</b>\n\nEnjoy watching 🎬",
        "vidox_notice": "✅ Your balance was topped up with <b>{points} points</b> 💦\nBalance: <b>{bal}</b>\nEnjoy watching 🎬",
        "auto_refund": "⚠️ We couldn't link the payment to your Vidox account, so your Stars were refunded in full. Open the Vidox bot, press /start, then try again.",
        "pay_error": "⚠️ Something went wrong while adding your points. Don't worry, your payment is recorded and will be handled soon. Contact us via /paysupport.",
        "support": "🛟 <b>Support</b>\n\nFor any payment or points problem contact us and send your transaction id.\n⚠️ Telegram support (@botsupport) cannot help with purchases made in this bot.",
        "support_btn": "💬 Contact support",
        "last_orders": "\n\n🧾 <b>Your last orders:</b>\n{rows}",
        "no_orders": "\n\nNo orders yet.",
        "balance": "💰 Your Vidox balance: <b>{bal}</b> points",
        "banned": "⛔ Your account is banned.",
    },
}


def tr(lang: str, key: str, **kw: Any) -> str:
    s = TX.get(lang, TX["en"])[key]
    return s.format(**kw) if kw else s


# =====================================================================
#  globals / helpers
# =====================================================================
router = Router()
db: DB
bot: Bot
_bc_tasks: dict = {}
_bc_cancel: set = set()


def ab(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def akb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


BACK_HOME = [ab("🏠 اللوحة الرئيسية", "a:home")]


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


async def vidox_user(uid: int) -> Optional[dict]:
    rows = await db._q("SELECT telegram_id, language, points_balance, is_banned FROM users WHERE telegram_id = ?", [uid])
    return rows[0] if rows else None


def pick_lang(vu: Optional[dict], tg_code: Optional[str]) -> str:
    if vu and vu.get("language") in ("ar", "en"):
        return vu["language"]
    return "ar" if (tg_code or "").startswith("ar") else "en"


async def touch_user(u: Any) -> None:
    await db._run([(
        "INSERT INTO pay_users (telegram_id, username, full_name, lang) VALUES (?, ?, ?, ?) "
        f"ON CONFLICT (telegram_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name, "
        f"is_active = 1, last_active_at = {_NOW_SQL}",
        [u.id, u.username, u.full_name, u.language_code])])


async def get_pay_user(uid: int) -> Optional[dict]:
    rows = await db._q("SELECT * FROM pay_users WHERE telegram_id = ?", [uid])
    return rows[0] if rows else None


async def log_admin(aid: Optional[int], action: str, details: str = "") -> None:
    try:
        await db._run([("INSERT INTO pay_logs (admin_id, action, details) VALUES (?, ?, ?)", [aid, action, details[:500]])])
    except Exception as e:
        log.debug("log failed: %s", e)


async def safe_send(chat_id: int, text: str, **kw: Any) -> Optional[Message]:
    for _ in range(2):
        try:
            return await bot.send_message(chat_id, text, **kw)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramAPIError as e:
            log.debug("send failed %s: %s", chat_id, e)
            return None
    return None


async def alert_admins(text: str) -> None:
    for a in ADMIN_IDS:
        await safe_send(a, text)


async def notify_vidox(uid: int, lang: str, points: int, bal: int) -> None:
    """Optional: send the activation notice through the Vidox bot itself (needs VIDOX_BOT_TOKEN)."""
    if not VIDOX_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"https://api.telegram.org/bot{VIDOX_BOT_TOKEN}/sendMessage",
                         json={"chat_id": uid, "parse_mode": "HTML",
                               "text": tr(lang, "vidox_notice", points=points, bal=bal)})
    except Exception as e:
        log.warning("vidox notify failed: %s", e)


# ---- admin state (multi-step flows)
async def get_astate(aid: int) -> Optional[dict]:
    rows = await db._q("SELECT state, data FROM pay_state WHERE admin_id = ?", [aid])
    if not rows:
        return None
    try:
        d = json.loads(rows[0]["data"] or "{}")
    except ValueError:
        d = {}
    return {"state": rows[0]["state"], "data": d}


async def set_astate(aid: int, state: str, data: Optional[dict] = None) -> None:
    await db._run([("INSERT INTO pay_state (admin_id, state, data) VALUES (?, ?, ?) "
                    "ON CONFLICT (admin_id) DO UPDATE SET state = excluded.state, data = excluded.data",
                    [aid, state, json.dumps(data or {}, ensure_ascii=False)])])


async def clear_astate(aid: int) -> None:
    try:
        await db._run([("DELETE FROM pay_state WHERE admin_id = ?", [aid])])
    except Exception:
        pass


async def show(ev: Any, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
        await bot.send_message(ev.message.chat.id, text, reply_markup=markup)
    else:
        await ev.answer(text, reply_markup=markup)


# =====================================================================
#  USER SIDE
# =====================================================================
async def packages_markup(lang: str) -> InlineKeyboardMarkup:
    rows = await db._q("SELECT * FROM pay_packages WHERE active = 1 ORDER BY sort_order, stars")
    kb = [[ab(tr(lang, "pkg_btn", stars=r["stars"], points=r["points"]), f"p:buy:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton(text=tr(lang, "open_vidox"), url=f"https://t.me/{S('vidox_username')}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def send_shop(uid: int, lang: str, vu: Optional[dict]) -> None:
    text = S("welcome_text") or tr(lang, "welcome", bal=(vu or {}).get("points_balance", 0))
    if S("welcome_text"):
        text = text.replace("{bal}", str((vu or {}).get("points_balance", 0)))
    await safe_send(uid, text, reply_markup=await packages_markup(lang))


def vidox_btn(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr(lang, "open_vidox"), url=f"https://t.me/{S('vidox_username')}")]])


async def user_gate(m: Message) -> Optional[tuple]:
    """returns (lang, vidox_user) or None when blocked."""
    u = m.from_user
    if not u or u.is_bot:
        return None
    await touch_user(u)
    vu = await vidox_user(u.id)
    lang = pick_lang(vu, u.language_code)
    if vu and vu.get("is_banned") and not is_admin(u.id):
        await m.answer(tr(lang, "banned"))
        return None
    return lang, vu


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message) -> None:
    g = await user_gate(m)
    if not g:
        return
    lang, vu = g
    pu = await get_pay_user(m.from_user.id)
    if not (pu and pu["terms_ok"]):
        await m.answer(tr(lang, "terms_gate") + "\n\n" + S("terms_text"),
                       reply_markup=akb([[ab(tr(lang, "agree"), "p:agree")]]))
        return
    await send_shop(m.from_user.id, lang, vu)


@router.message(Command("terms"), F.chat.type == "private")
async def cmd_terms(m: Message) -> None:
    g = await user_gate(m)
    if not g:
        return
    lang, _ = g
    pu = await get_pay_user(m.from_user.id)
    kb = None if (pu and pu["terms_ok"]) else akb([[ab(tr(lang, "agree"), "p:agree")]])
    await m.answer(S("terms_text"), reply_markup=kb)


async def support_text(uid: int, lang: str) -> tuple:
    text = tr(lang, "support")
    rows = await db._q("SELECT id, stars, points, status, charge_id FROM pay_orders WHERE telegram_id = ? "
                       "ORDER BY id DESC LIMIT 5", [uid])
    if rows:
        lines = [f"#{r['id']} • {r['stars']}⭐ → {r['points']} • {r['status']}\n<code>{esc(r['charge_id'])}</code>"
                 for r in rows]
        text += tr(lang, "last_orders", rows="\n".join(lines))
    else:
        text += tr(lang, "no_orders")
    sup = S("support_username").lstrip("@")
    url = f"https://t.me/{sup}" if sup else f"https://t.me/{S('vidox_username')}"
    return text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tr(lang, "support_btn"), url=url)]])


@router.message(Command("support", "paysupport"), F.chat.type == "private")
async def cmd_support(m: Message) -> None:
    g = await user_gate(m)
    if not g:
        return
    lang, _ = g
    text, kb = await support_text(m.from_user.id, lang)
    await m.answer(text, reply_markup=kb)


@router.message(Command("balance"), F.chat.type == "private")
async def cmd_balance(m: Message) -> None:
    g = await user_gate(m)
    if not g:
        return
    lang, vu = g
    if not vu:
        await m.answer(tr(lang, "need_vidox"), reply_markup=vidox_btn(lang))
        return
    await m.answer(tr(lang, "balance", bal=vu["points_balance"]))


@router.callback_query(F.data.startswith("p:"))
async def cb_user(cb: CallbackQuery) -> None:
    u = cb.from_user
    parts = (cb.data or "").split(":")
    act = parts[1] if len(parts) > 1 else ""
    await touch_user(u)
    vu = await vidox_user(u.id)
    lang = pick_lang(vu, u.language_code)

    if act == "agree":
        await db._run([("UPDATE pay_users SET terms_ok = 1 WHERE telegram_id = ?", [u.id])])
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.answer(tr(lang, "agreed"))
        await send_shop(u.id, lang, vu)
        return

    if act == "buy" and len(parts) > 2:
        pu = await get_pay_user(u.id)
        if not (pu and pu["terms_ok"]):
            await cb.answer()
            await bot.send_message(u.id, S("terms_text"), reply_markup=akb([[ab(tr(lang, "agree"), "p:agree")]]))
            return
        if S("maintenance") == "true" and not is_admin(u.id):
            await cb.answer(tr(lang, "maint"), show_alert=True)
            return
        if not vu:
            await cb.answer()
            await bot.send_message(u.id, tr(lang, "need_vidox"), reply_markup=vidox_btn(lang))
            return
        if vu.get("is_banned") and not is_admin(u.id):
            await cb.answer(tr(lang, "banned"), show_alert=True)
            return
        rows = await db._q("SELECT * FROM pay_packages WHERE id = ? AND active = 1", [int(parts[2])])
        if not rows:
            await cb.answer(tr(lang, "unavailable"), show_alert=True)
            return
        pk = rows[0]
        await cb.answer()
        try:
            await bot.send_invoice(
                chat_id=u.id,
                title=tr(lang, "invoice_title", points=pk["points"]),
                description=tr(lang, "invoice_desc", points=pk["points"]),
                payload=f"pk:{pk['id']}",
                currency="XTR",
                prices=[LabeledPrice(label=tr(lang, "invoice_title", points=pk["points"]), amount=int(pk["stars"]))],
                provider_token="",
            )
        except TelegramAPIError as e:
            log.warning("invoice failed: %s", e)
            await bot.send_message(u.id, tr(lang, "pay_error"))
        return
    await cb.answer()


@router.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery) -> None:
    vu = await vidox_user(q.from_user.id)
    lang = pick_lang(vu, q.from_user.language_code)
    try:
        m = re.match(r"^pk:(\d+)$", q.invoice_payload or "")
        rows = await db._q("SELECT * FROM pay_packages WHERE id = ? AND active = 1", [int(m.group(1))]) if m else []
        if not rows or int(rows[0]["stars"]) != q.total_amount or q.currency != "XTR":
            await q.answer(ok=False, error_message=tr(lang, "unavailable"))
        elif not vu:
            await q.answer(ok=False, error_message=tr(lang, "need_vidox").split("\n")[0][:200])
        elif S("maintenance") == "true" and not is_admin(q.from_user.id):
            await q.answer(ok=False, error_message=tr(lang, "maint"))
        elif vu.get("is_banned") and not is_admin(q.from_user.id):
            await q.answer(ok=False, error_message=tr(lang, "banned"))
        else:
            await q.answer(ok=True)
    except Exception as e:
        log.exception("pre_checkout failed: %s", e)
        try:
            await q.answer(ok=False, error_message=tr(lang, "pay_error")[:200])
        except Exception:
            pass


async def credit_order(uid: int, pk_id: int, stars: int, points: int, charge: str, prov: str) -> dict:
    """Atomic: order row + points transaction + balance. The unique charge id makes it idempotent."""
    try:
        await db.tx([
            ("INSERT INTO pay_orders (telegram_id, package_id, stars, points, charge_id, provider_charge_id) "
             "VALUES (?, ?, ?, ?, ?, ?)", [uid, pk_id, stars, points, charge, prov]),
            ("INSERT INTO points_transactions (telegram_id, amount, type, ref_id, note, balance_after) "
             "VALUES (?, ?, 'stars_purchase', ?, ?, (SELECT points_balance FROM users WHERE telegram_id = ?) + ?)",
             [uid, points, charge, f"{stars} stars", uid, points]),
            ("UPDATE users SET points_balance = points_balance + ?, is_premium = 1 WHERE telegram_id = ?",
             [points, uid]),
        ])
    except DBError as e:
        if "UNIQUE" in str(e).upper():
            return {"ok": False, "error": "duplicate"}
        raise
    vu = await vidox_user(uid)
    return {"ok": True, "balance": (vu or {}).get("points_balance", 0)}


@router.message(F.successful_payment)
async def on_paid(m: Message) -> None:
    sp = m.successful_payment
    uid = m.from_user.id
    vu = await vidox_user(uid)
    lang = pick_lang(vu, m.from_user.language_code)
    mm = re.match(r"^pk:(\d+)$", sp.invoice_payload or "")
    pk_id = int(mm.group(1)) if mm else 0
    rows = await db._q("SELECT * FROM pay_packages WHERE id = ?", [pk_id]) if pk_id else []
    # points come from the package; fall back to the paid amount mapping if the package was deleted
    points = int(rows[0]["points"]) if rows else 0
    if not points:
        await db._run([("INSERT OR IGNORE INTO pay_orders (telegram_id, package_id, stars, points, charge_id, "
                        "provider_charge_id, status) VALUES (?, ?, ?, 0, ?, ?, 'unclaimed')",
                        [uid, pk_id, sp.total_amount, sp.telegram_payment_charge_id, sp.provider_payment_charge_id])])
        await alert_admins(f"⚠️ دفعة بلا باقة معروفة\nالمستخدم: <code>{uid}</code>\nالنجوم: {sp.total_amount}\n"
                           f"<code>{esc(sp.telegram_payment_charge_id)}</code>")
        await m.answer(tr(lang, "pay_error"))
        return
    if not vu:
        # no Vidox account: refund automatically
        try:
            await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=sp.telegram_payment_charge_id)
            await db._run([("INSERT OR IGNORE INTO pay_orders (telegram_id, package_id, stars, points, charge_id, "
                            "provider_charge_id, status, refunded_at) VALUES (?, ?, ?, ?, ?, ?, 'refunded', "
                            f"{_NOW_SQL})", [uid, pk_id, sp.total_amount, points, sp.telegram_payment_charge_id,
                                              sp.provider_payment_charge_id])])
            await m.answer(tr(lang, "auto_refund"), reply_markup=vidox_btn(lang))
        except Exception as e:
            log.exception("auto refund failed: %s", e)
            await alert_admins(f"⚠️ دفعة بلا حساب Vidox وفشل الاسترداد التلقائي\nالمستخدم: <code>{uid}</code>\n"
                               f"<code>{esc(sp.telegram_payment_charge_id)}</code>")
        return
    res = None
    for attempt in range(5):
        try:
            res = await credit_order(uid, pk_id, sp.total_amount, points,
                                     sp.telegram_payment_charge_id, sp.provider_payment_charge_id or "")
            break
        except Exception as e:
            log.warning("credit attempt %s failed: %s", attempt + 1, e)
            await asyncio.sleep(1.5 * (attempt + 1))
    if res is None:
        log.error("PAYMENT NOT CREDITED user=%s charge=%s points=%s", uid, sp.telegram_payment_charge_id, points)
        await alert_admins("🚨 <b>دفعة لم تُضف نقاطها (مشكلة قاعدة البيانات)</b>\n"
                           f"المستخدم: <code>{uid}</code>\nالنقاط: {points}\nالنجوم: {sp.total_amount}\n"
                           f"<code>{esc(sp.telegram_payment_charge_id)}</code>\nأضفها يدوياً أو استرد المبلغ.")
        await m.answer(tr(lang, "pay_error"))
        return
    if not res["ok"]:      # duplicate update, already credited
        return
    bal = res["balance"]
    await m.answer(tr(lang, "ok", points=points, bal=bal), reply_markup=vidox_btn(lang))
    await notify_vidox(uid, lang, points, bal)
    uname = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    await alert_admins(f"💰 <b>شراء جديد</b>\n👤 {esc(uname)} (<code>{uid}</code>)\n"
                       f"⭐ {sp.total_amount} ← 💦 {points}\n🧾 <code>{esc(sp.telegram_payment_charge_id)}</code>")


# =====================================================================
#  ADMIN
# =====================================================================
def bahrain_day_start_utc(days_back: int = 0) -> str:
    local = now_utc() + timedelta(hours=3)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
    return iso(start - timedelta(hours=3))


async def panel_home(ev: Any) -> None:
    today = bahrain_day_start_utc()
    r = (await db._q("SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS st FROM pay_orders "
                     "WHERE status = 'paid' AND created_at >= ?", [today]))[0]
    users = (await db._q("SELECT COUNT(*) AS n FROM pay_users"))[0]["n"]
    text = (f"🎛 <b>لوحة تحكم بوت الدفع</b>\n━━━━━━━━━━\n"
            f"👥 مستخدمو البوت: <b>{users}</b>\n"
            f"📅 اليوم: <b>{r['n']}</b> عملية • <b>{r['st']}</b> ⭐")
    if S("maintenance") == "true":
        text += "\n🛠 وضع الصيانة: <b>مفعّل</b>"
    await show(ev, text, akb([
        [ab("📦 الباقات والأسعار", "a:pk:list"), ab("📊 الإحصائيات", "a:st:7")],
        [ab("📢 إذاعة", "a:bc:start"), ab("🧾 العمليات والاسترداد", "a:od:list")],
        [ab("⚙️ الإعدادات", "a:se")],
    ]))


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(m: Message) -> None:
    if not is_admin(m.from_user.id):
        return
    await clear_astate(m.from_user.id)
    await panel_home(m)


@router.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(m: Message) -> None:
    if is_admin(m.from_user.id):
        await clear_astate(m.from_user.id)
        await m.answer("تم الإلغاء.", reply_markup=akb([BACK_HOME]))


# ---------------------------------------------------------------- packages
async def pk_list(ev: Any) -> None:
    rows = await db._q("SELECT * FROM pay_packages ORDER BY sort_order, stars")
    kb = [[ab(f"{'🟢' if r['active'] else '🔴'} {r['stars']}⭐ ← {r['points']} نقطة", f"a:pk:{r['id']}")] for r in rows]
    kb.append([ab("➕ باقة جديدة", "a:pk:new")])
    kb.append(BACK_HOME)
    await show(ev, "📦 <b>الباقات والأسعار</b>\nاضغط على باقة لتعديلها.", akb(kb))


async def pk_view(ev: Any, pid: int) -> None:
    rows = await db._q("SELECT * FROM pay_packages WHERE id = ?", [pid])
    if not rows:
        await pk_list(ev)
        return
    r = rows[0]
    sold = (await db._q("SELECT COUNT(*) AS n FROM pay_orders WHERE package_id = ? AND status = 'paid'", [pid]))[0]["n"]
    await show(ev, f"📦 <b>باقة #{pid}</b>\n⭐ النجوم: <b>{r['stars']}</b>\n💦 النقاط: <b>{r['points']}</b>\n"
                   f"الحالة: {'🟢 ظاهرة' if r['active'] else '🔴 مخفية'}\n🛒 مبيعاتها: <b>{sold}</b>",
               akb([[ab("✏️ السعر بالنجوم", f"a:pk:{pid}:stars"), ab("✏️ النقاط", f"a:pk:{pid}:points")],
                    [ab("👁 إظهار/إخفاء", f"a:pk:{pid}:vis")],
                    [ab("⬆️ أعلى", f"a:pk:{pid}:up"), ab("⬇️ أسفل", f"a:pk:{pid}:down")],
                    [ab("🗑 حذف", f"a:pk:{pid}:del"), ab("🔙 رجوع", "a:pk:list")]]))


async def adm_pk(cb: CallbackQuery, parts: list) -> None:
    aid = cb.from_user.id
    sub = parts[2] if len(parts) > 2 else "list"
    if sub == "list":
        await clear_astate(aid)
        await pk_list(cb)
        return
    if sub == "new":
        await set_astate(aid, "pk_new_stars")
        await show(cb, "➕ <b>باقة جديدة</b>\n1/2 — أرسل <b>السعر بالنجوم</b> (رقم).\n(أو /cancel)",
                   akb([[ab("❌ إلغاء", "a:pk:list")]]))
        return
    if not sub.isdigit():
        return
    pid = int(sub)
    act = parts[3] if len(parts) > 3 else "view"
    if act == "view":
        await pk_view(cb, pid)
    elif act in ("stars", "points"):
        await set_astate(aid, "pk_edit", {"pid": pid, "field": act})
        await show(cb, "أرسل القيمة الجديدة (رقم).\n(أو /cancel)", akb([[ab("❌ إلغاء", f"a:pk:{pid}")]]))
    elif act == "vis":
        await db._run([("UPDATE pay_packages SET active = 1 - active WHERE id = ?", [pid])])
        await log_admin(aid, "pkg_vis", str(pid))
        await pk_view(cb, pid)
    elif act in ("up", "down"):
        ids = [r["id"] for r in await db._q("SELECT id FROM pay_packages ORDER BY sort_order, stars")]
        if pid in ids:
            i = ids.index(pid)
            j = i - 1 if act == "up" else i + 1
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
            await db._run([("UPDATE pay_packages SET sort_order = ? WHERE id = ?", [n + 1, x]) for n, x in enumerate(ids)])
        await pk_view(cb, pid)
    elif act == "del":
        await show(cb, "❓ هل تريد حذف هذه الباقة؟ (الطلبات القديمة تبقى محفوظة)",
                   akb([[ab("✅ نعم احذف", f"a:pk:{pid}:delok"), ab("❌ لا", f"a:pk:{pid}")]]))
    elif act == "delok":
        await db._run([("DELETE FROM pay_packages WHERE id = ?", [pid])])
        await log_admin(aid, "pkg_deleted", str(pid))
        await pk_list(cb)


# ---------------------------------------------------------------- stats
def _pct(a: int, b: int) -> str:
    return f"{(a * 100 / b):.1f}%" if b else "0%"


async def stats_view(ev: Any, days: int) -> None:
    since = {0: "1970-01-01T00:00:00+00:00", 1: bahrain_day_start_utc(0)}.get(days, bahrain_day_start_utc(days - 1))
    label = {1: "اليوم", 7: "آخر 7 أيام", 30: "آخر 30 يوماً", 0: "كل الفترة"}[days]
    o = (await db._q(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS stars, COALESCE(SUM(points),0) AS pts, "
        "COUNT(DISTINCT telegram_id) AS buyers FROM pay_orders WHERE status = 'paid' AND created_at >= ?", [since]))[0]
    rf = (await db._q(
        "SELECT COUNT(*) AS n, COALESCE(SUM(stars),0) AS stars FROM pay_orders "
        "WHERE status = 'refunded' AND created_at >= ?", [since]))[0]
    repeat = (await db._q(
        "SELECT COUNT(*) AS n FROM (SELECT telegram_id FROM pay_orders WHERE status = 'paid' AND created_at >= ? "
        "GROUP BY telegram_id HAVING COUNT(*) > 1)", [since]))[0]["n"]
    new_users = (await db._q("SELECT COUNT(*) AS n FROM pay_users WHERE created_at >= ?", [since]))[0]["n"]
    total_users = (await db._q("SELECT COUNT(*) AS n FROM pay_users"))[0]["n"]
    active_users = (await db._q("SELECT COUNT(*) AS n FROM pay_users WHERE last_active_at >= ?", [since]))[0]["n"]
    ever_buyers = (await db._q("SELECT COUNT(DISTINCT telegram_id) AS n FROM pay_orders WHERE status = 'paid'"))[0]["n"]
    top = await db._q(
        "SELECT stars, points, COUNT(*) AS n FROM pay_orders WHERE status = 'paid' AND created_at >= ? "
        "GROUP BY stars, points ORDER BY n DESC LIMIT 5", [since])
    net = o["stars"]
    lines = [f"📊 <b>الإحصائيات — {label}</b>", "━━━━━━━━━━",
             f"🛒 العمليات الناجحة: <b>{o['n']}</b>",
             f"⭐ إجمالي النجوم: <b>{o['stars']}</b>",
             f"💦 النقاط المباعة: <b>{o['pts']}</b>",
             f"👤 عدد المشترين: <b>{o['buyers']}</b> (مكرّرون: {repeat})",
             f"📈 متوسط الشراء: <b>{(o['stars'] / o['n']):.0f}</b> ⭐" if o["n"] else "📈 متوسط الشراء: <b>0</b>",
             f"↩️ المستردّ: <b>{rf['n']}</b> عملية ({rf['stars']} ⭐)",
             "━━━━━━━━━━",
             f"👥 مستخدمو البوت: <b>{total_users}</b> (جدد: {new_users} • نشطون: {active_users})",
             f"🎯 نسبة التحويل (مشترٍ ÷ مستخدم): <b>{_pct(ever_buyers, total_users)}</b>"]
    if top:
        lines.append("━━━━━━━━━━\n🏆 <b>الأكثر مبيعاً:</b>")
        for r in top:
            lines.append(f"• {r['stars']}⭐ ← {r['points']} نقطة: <b>{r['n']}</b>")
    if days in (7, 30):
        daily = await db._q(
            "SELECT date(created_at, '+3 hours') AS d, COUNT(*) AS n, SUM(stars) AS s FROM pay_orders "
            "WHERE status = 'paid' AND created_at >= ? GROUP BY d ORDER BY d DESC LIMIT 10", [since])
        if daily:
            lines.append("━━━━━━━━━━\n📅 <b>يومياً:</b>")
            for r in daily:
                lines.append(f"• {r['d']}: {r['n']} عملية • {r['s']}⭐")
    kb = [[ab("اليوم", "a:st:1"), ab("7 أيام", "a:st:7")], [ab("30 يوماً", "a:st:30"), ab("الكل", "a:st:0")], BACK_HOME]
    await show(ev, "\n".join(lines), akb(kb))


# ---------------------------------------------------------------- orders / refunds
def _order_line(r: dict) -> str:
    icon = {"paid": "✅", "refunded": "↩️", "unclaimed": "⚠️"}.get(r["status"], "•")
    return (f"{icon} #{r['id']} • <code>{r['telegram_id']}</code> • {r['stars']}⭐→{r['points']} • "
            f"{bahrain_time(r['created_at'])}")


async def orders_view(ev: Any, uid: Optional[int] = None) -> None:
    if uid:
        rows = await db._q("SELECT * FROM pay_orders WHERE telegram_id = ? ORDER BY id DESC LIMIT 10", [uid])
        title = f"🧾 <b>عمليات المستخدم {uid}</b>"
    else:
        rows = await db._q("SELECT * FROM pay_orders ORDER BY id DESC LIMIT 10")
        title = "🧾 <b>آخر العمليات</b>"
    lines = [title, "━━━━━━━━━━"] + ([_order_line(r) for r in rows] or ["لا توجد عمليات."])
    kb = [[ab(f"↩️ استرداد #{r['id']}", f"a:od:ref:{r['id']}")] for r in rows if r["status"] == "paid"]
    kb.append([ab("🔍 بحث بآيدي مستخدم", "a:od:find")])
    kb.append(BACK_HOME)
    await show(ev, "\n".join(lines), akb(kb))


async def refund_order(cb: CallbackQuery, oid: int) -> None:
    rows = await db._q("SELECT * FROM pay_orders WHERE id = ?", [oid])
    if not rows or rows[0]["status"] != "paid":
        await cb.answer("العملية غير قابلة للاسترداد.", show_alert=True)
        return
    o = rows[0]
    try:
        await bot.refund_star_payment(user_id=o["telegram_id"], telegram_payment_charge_id=o["charge_id"])
    except TelegramAPIError as e:
        await cb.answer(f"فشل الاسترداد من تيليجرام: {str(e)[:150]}", show_alert=True)
        return
    vu = await vidox_user(o["telegram_id"])
    deduct = min(int((vu or {}).get("points_balance", 0)), int(o["points"]))
    stmts = [("UPDATE pay_orders SET status = 'refunded', refunded_at = " + _NOW_SQL + " WHERE id = ?", [oid])]
    if deduct > 0:
        stmts += [
            ("INSERT INTO points_transactions (telegram_id, amount, type, ref_id, note, balance_after) "
             "VALUES (?, ?, 'stars_refund', ?, ?, (SELECT points_balance FROM users WHERE telegram_id = ?) - ?)",
             [o["telegram_id"], -deduct, o["charge_id"], "refund", o["telegram_id"], deduct]),
            ("UPDATE users SET points_balance = points_balance - ? WHERE telegram_id = ?", [deduct, o["telegram_id"]]),
        ]
    await db.tx(stmts)
    await log_admin(cb.from_user.id, "refund", f"order={oid} user={o['telegram_id']} deducted={deduct}")
    await safe_send(o["telegram_id"], f"↩️ تم استرداد {o['stars']} ⭐ لحسابك.\nRefunded {o['stars']} ⭐.")
    await show(cb, f"✅ تم استرداد الطلب #{oid} ({o['stars']}⭐).\nخُصم من رصيده: {deduct} نقطة"
                   + ("" if deduct == o["points"] else f" (من أصل {o['points']}، لأن رصيده أقل)"),
               akb([[ab("🧾 العمليات", "a:od:list")], BACK_HOME]))


async def adm_od(cb: CallbackQuery, parts: list) -> None:
    aid = cb.from_user.id
    act = parts[2] if len(parts) > 2 else "list"
    if act == "list":
        await clear_astate(aid)
        await orders_view(cb)
    elif act == "find":
        await set_astate(aid, "od_find")
        await show(cb, "🔍 أرسل آيدي المستخدم (أرقام).\n(أو /cancel)", akb([[ab("❌ إلغاء", "a:od:list")]]))
    elif act == "ref" and len(parts) > 3:
        oid = int(parts[3])
        await show(cb, f"❓ استرداد الطلب #{oid}؟\nستُرد النجوم للمستخدم ويُخصم من رصيده في Vidox ما يعادلها (حتى حدّ رصيده).",
                   akb([[ab("✅ نعم، استرد", f"a:od:refok:{oid}"), ab("❌ لا", "a:od:list")]]))
    elif act == "refok" and len(parts) > 3:
        await refund_order(cb, int(parts[3]))


# ---------------------------------------------------------------- settings
async def settings_view(ev: Any) -> None:
    sup = S("support_username") or "—"
    text = (f"⚙️ <b>الإعدادات</b>\n━━━━━━━━━━\n"
            f"🛟 يوزر الدعم: <b>{esc(sup)}</b>\n"
            f"🎬 يوزر بوت Vidox: <b>@{esc(S('vidox_username'))}</b>\n"
            f"📜 نص الشروط: {len(S('terms_text'))} حرف\n"
            f"👋 رسالة الترحيب: {'مخصصة' if S('welcome_text') else 'الافتراضية'}\n"
            f"🛠 وضع الصيانة: <b>{'مفعّل' if S('maintenance') == 'true' else 'متوقف'}</b>\n"
            f"🔔 إشعار Vidox عبر البوت: {'مفعّل' if VIDOX_BOT_TOKEN else 'غير مفعّل (يحتاج VIDOX_BOT_TOKEN)'}")
    await show(ev, text, akb([
        [ab("🛟 يوزر الدعم", "a:se:support"), ab("🎬 يوزر Vidox", "a:se:vidox")],
        [ab("📜 تعديل الشروط", "a:se:terms"), ab("👋 رسالة الترحيب", "a:se:welcome")],
        [ab("🛠 تبديل الصيانة", "a:se:maint")],
        BACK_HOME]))


async def adm_se(cb: CallbackQuery, parts: list) -> None:
    aid = cb.from_user.id
    act = parts[2] if len(parts) > 2 else "view"
    if act == "view":
        await clear_astate(aid)
        await settings_view(cb)
    elif act == "maint":
        await set_setting("maintenance", "false" if S("maintenance") == "true" else "true")
        await log_admin(aid, "maintenance", S("maintenance"))
        await settings_view(cb)
    elif act in ("support", "vidox", "terms", "welcome"):
        prompts = {
            "support": "أرسل يوزر الدعم (مثال: @MySupport) أو - لحذفه.",
            "vidox": "أرسل يوزر بوت Vidox (مثال: @vdox_2_bot).",
            "terms": "أرسل نص الشروط الجديد (يدعم HTML بسيط مثل <b>..</b>). أرسل - للرجوع للنص الافتراضي.",
            "welcome": "أرسل رسالة الترحيب فوق الباقات (استخدم {bal} لعرض رصيد المستخدم). أرسل - للنص الافتراضي.",
        }
        await set_astate(aid, "set_text", {"key": act})
        await show(cb, prompts[act] + "\n(أو /cancel)", akb([[ab("❌ إلغاء", "a:se")]]))


# ---------------------------------------------------------------- broadcast
AUDIENCES = {"all": "👥 كل مستخدمي البوت", "buyers": "💎 من اشترى", "nobuy": "🛍 من لم يشترِ بعد"}


async def audience_ids(aud: str) -> list:
    if aud == "buyers":
        sql = ("SELECT telegram_id FROM pay_users WHERE is_active = 1 AND telegram_id IN "
               "(SELECT telegram_id FROM pay_orders WHERE status = 'paid')")
    elif aud == "nobuy":
        sql = ("SELECT telegram_id FROM pay_users WHERE is_active = 1 AND telegram_id NOT IN "
               "(SELECT telegram_id FROM pay_orders WHERE status = 'paid')")
    else:
        sql = "SELECT telegram_id FROM pay_users WHERE is_active = 1"
    return [r["telegram_id"] for r in await db._q(sql)]


def _bar(pct: int) -> str:
    f = max(0, min(10, pct // 10))
    return "█" * f + "░" * (10 - f)


async def _bc_one(src_chat: int, src_msg: int, uid: int) -> tuple:
    for _ in range(2):
        try:
            r = await bot.copy_message(uid, src_chat, src_msg)
            return "ok", r.message_id
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramForbiddenError:
            return "blocked", 0
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower() or "deactivated" in str(e).lower():
                return "blocked", 0
            return "fail", 0
        except TelegramAPIError:
            return "fail", 0
    return "fail", 0


async def run_broadcast(bid: int, admin_chat: int, progress_id: int, src_chat: int, src_msg: int, ids: list) -> None:
    total = len(ids)
    sent = failed = blocked = 0
    recs: list = []
    blocked_ids: list = []
    status = "done"
    last_edit = 0.0

    async def render(final: bool) -> None:
        pct = 100 if total == 0 else int((sent + failed + blocked) * 100 / total)
        head = "📢 <b>اكتملت الإذاعة</b>" if final and status == "done" else (
            "⛔ <b>تم إيقاف الإذاعة</b>" if final else "📢 <b>جارٍ الإذاعة...</b>")
        text = (f"{head}\n{_bar(pct)} {pct}%\n✅ وصلت: <b>{sent}</b>   ❌ فشلت: <b>{failed}</b>   "
                f"🚫 حظروا البوت: <b>{blocked}</b>\n📈 نسبة الوصول: <b>{(sent * 100 // total) if total else 0}%</b> من {total}")
        kb = akb([[ab("🗑 حذف من عند الجميع", f"a:bc:del:{bid}")], BACK_HOME]) if final else \
            akb([[ab("⛔ إلغاء الإذاعة", f"a:bc:stop:{bid}")]])
        try:
            await bot.edit_message_text(text, chat_id=admin_chat, message_id=progress_id, reply_markup=kb)
        except Exception:
            pass

    async def flush() -> None:
        nonlocal recs
        if recs:
            try:
                await db._run([("INSERT OR REPLACE INTO pay_broadcast_recipients (broadcast_id, telegram_id, message_id) "
                                "VALUES (?, ?, ?)", [bid, u, mid]) for u, mid in recs])
            except Exception as e:
                log.warning("recipients insert failed: %s", e)
            recs = []

    try:
        for i in range(0, total, 20):
            if bid in _bc_cancel:
                status = "cancelled"
                break
            chunk = ids[i:i + 20]
            results = await asyncio.gather(*(_bc_one(src_chat, src_msg, uid) for uid in chunk))
            for uid, (res, mid) in zip(chunk, results):
                if res == "ok":
                    sent += 1
                    recs.append((uid, mid))
                elif res == "blocked":
                    blocked += 1
                    blocked_ids.append(uid)
                else:
                    failed += 1
            if len(recs) >= 100:
                await flush()
            if time.time() - last_edit > 3:
                last_edit = time.time()
                await render(False)
            await asyncio.sleep(1.0)
    except Exception as e:
        log.exception("broadcast crashed: %s", e)
        status = "interrupted"
    finally:
        await flush()
        for u in blocked_ids:
            try:
                await db._run([("UPDATE pay_users SET is_active = 0 WHERE telegram_id = ?", [u])])
            except Exception:
                pass
        try:
            await db._run([("UPDATE pay_broadcasts SET status = ?, sent = ?, failed = ?, blocked = ?, finished_at = "
                            + _NOW_SQL + " WHERE id = ?", [status, sent, failed, blocked, bid])])
        except Exception:
            pass
        _bc_tasks.pop(bid, None)
        _bc_cancel.discard(bid)
        await render(True)


async def delete_broadcast(bid: int, admin_chat: int) -> None:
    rows = await db._q("SELECT telegram_id, message_id FROM pay_broadcast_recipients WHERE broadcast_id = ?", [bid])
    deleted = 0
    for i in range(0, len(rows), 20):
        async def _del(r: dict) -> int:
            try:
                await bot.delete_message(r["telegram_id"], r["message_id"])
                return 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass
            return 0
        deleted += sum(await asyncio.gather(*(_del(r) for r in rows[i:i + 20])))
        await asyncio.sleep(1.0)
    await db._run([("UPDATE pay_broadcasts SET status = 'deleted' WHERE id = ?", [bid])])
    await safe_send(admin_chat, f"🗑 تم حذف الإذاعة من عند <b>{deleted}</b> مستخدم.\n(تيليجرام يسمح بالحذف خلال 48 ساعة فقط)")


async def adm_bc(cb: CallbackQuery, parts: list) -> None:
    aid = cb.from_user.id
    act = parts[2] if len(parts) > 2 else "start"
    if act == "start":
        if _bc_tasks:
            await show(cb, "⚠️ توجد إذاعة جارية الآن. انتظر انتهاءها أو أوقفها.", akb([BACK_HOME]))
            return
        kb = [[ab(n, f"a:bc:aud:{k}")] for k, n in AUDIENCES.items()] + [[ab("❌ إلغاء", "a:bc:cancel")]]
        await show(cb, "📢 <b>إذاعة</b>\nاختر الجمهور:", akb(kb))
    elif act == "aud":
        key = parts[3] if len(parts) > 3 else "all"
        if key not in AUDIENCES:
            return
        await set_astate(aid, "bc_content", {"aud": key})
        await show(cb, f"📢 الجمهور: <b>{AUDIENCES[key]}</b>\n\nأرسل الآن الرسالة (نص أو صورة أو فيديو...).\n(أو /cancel)",
                   akb([[ab("❌ إلغاء", "a:bc:cancel")]]))
    elif act == "cancel":
        await clear_astate(aid)
        await panel_home(cb)
    elif act == "go":
        st = await get_astate(aid)
        if not st or st["state"] != "bc_confirm":
            await show(cb, "انتهت صلاحية الإذاعة، ابدأ من جديد.", akb([BACK_HOME]))
            return
        if _bc_tasks:
            await cb.answer("توجد إذاعة جارية الآن.", show_alert=True)
            return
        d = st["data"]
        ids = await audience_ids(d["aud"])
        if not ids:
            await show(cb, "لا يوجد مستخدمون في هذا الجمهور.", akb([BACK_HOME]))
            return
        res = await db._run([("INSERT INTO pay_broadcasts (admin_id, audience, src_chat, src_message, total) "
                              "VALUES (?, ?, ?, ?, ?) RETURNING id", [aid, d["aud"], d["src_chat"], d["src_msg"], len(ids)])])
        bid = res[0]["rows"][0]["id"]
        await clear_astate(aid)
        await log_admin(aid, "broadcast", f"id={bid} aud={d['aud']} n={len(ids)}")
        await show(cb, f"📢 <b>جارٍ الإذاعة...</b>\n{_bar(0)} 0%", akb([[ab("⛔ إلغاء الإذاعة", f"a:bc:stop:{bid}")]]))
        _bc_tasks[bid] = asyncio.create_task(
            run_broadcast(bid, cb.message.chat.id, cb.message.message_id, d["src_chat"], d["src_msg"], ids))
    elif act == "stop" and len(parts) > 3:
        bid = int(parts[3])
        if bid in _bc_tasks:
            _bc_cancel.add(bid)
            await cb.answer("⛔ جارٍ الإيقاف...")
        else:
            await cb.answer("الإذاعة منتهية.")
    elif act == "del" and len(parts) > 3:
        await cb.answer("🗑 جارٍ الحذف من عند المستخدمين...")
        asyncio.create_task(delete_broadcast(int(parts[3]), cb.message.chat.id))


# ---------------------------------------------------------------- admin callbacks router
@router.callback_query(F.data.startswith("a:"))
async def cb_admin(cb: CallbackQuery) -> None:
    aid = cb.from_user.id
    if not is_admin(aid):
        await cb.answer("غير مصرح", show_alert=True)
        return
    parts = (cb.data or "").split(":")
    sec = parts[1] if len(parts) > 1 else "home"
    try:
        if sec == "home":
            await clear_astate(aid)
            await panel_home(cb)
        elif sec == "pk":
            await adm_pk(cb, parts)
        elif sec == "st":
            await stats_view(cb, int(parts[2]) if len(parts) > 2 else 7)
        elif sec == "od":
            await adm_od(cb, parts)
        elif sec == "se":
            await adm_se(cb, parts)
        elif sec == "bc":
            await adm_bc(cb, parts)
    except DBError as e:
        log.error("admin action failed: %s", e)
        try:
            await cb.answer(f"خطأ في قاعدة البيانات: {str(e)[:150]}", show_alert=True)
        except Exception:
            pass
        return
    try:
        await cb.answer()
    except Exception:
        pass


# ---------------------------------------------------------------- admin text input
class AdminStateFilter(Filter):
    async def __call__(self, message: Message) -> Any:
        if not message.from_user or not is_admin(message.from_user.id):
            return False
        if (message.text or "").startswith("/"):
            return False
        row = await get_astate(message.from_user.id)
        return {"astate": row} if row else False


@router.message(F.chat.type == "private", AdminStateFilter())
async def on_admin_input(m: Message, astate: dict) -> None:
    aid = m.from_user.id
    st = astate["state"]
    data = astate.get("data") or {}
    txt = (m.text or "").strip()
    num = norm_digits(txt)

    if st == "pk_new_stars":
        if not num.isdigit() or int(num) <= 0:
            await m.answer("أرسل رقماً صحيحاً أكبر من 0.")
            return
        await set_astate(aid, "pk_new_points", {"stars": int(num)})
        await m.answer("2/2 — أرسل <b>عدد النقاط</b> التي تمنحها الباقة.")
        return
    if st == "pk_new_points":
        if not num.isdigit() or int(num) <= 0:
            await m.answer("أرسل رقماً صحيحاً أكبر من 0.")
            return
        mx = (await db._q("SELECT COALESCE(MAX(sort_order), 0) AS m FROM pay_packages"))[0]["m"]
        await db._run([("INSERT INTO pay_packages (stars, points, sort_order) VALUES (?, ?, ?)",
                        [data["stars"], int(num), mx + 1])])
        await clear_astate(aid)
        await log_admin(aid, "pkg_created", f"{data['stars']}->{num}")
        await m.answer(f"✅ تمت إضافة الباقة: {data['stars']}⭐ ← {num} نقطة.", reply_markup=akb([[ab("📦 الباقات", "a:pk:list")], BACK_HOME]))
        return
    if st == "pk_edit":
        if not num.isdigit() or int(num) <= 0:
            await m.answer("أرسل رقماً صحيحاً أكبر من 0.")
            return
        col = "stars" if data["field"] == "stars" else "points"
        await db._run([(f"UPDATE pay_packages SET {col} = ? WHERE id = ?", [int(num), data["pid"]])])
        await clear_astate(aid)
        await log_admin(aid, "pkg_edit", f"{data['pid']} {col}={num}")
        await m.answer("✅ تم التعديل.", reply_markup=akb([[ab("📦 رجوع للباقة", f"a:pk:{data['pid']}")], BACK_HOME]))
        return
    if st == "od_find":
        if not num.isdigit():
            await m.answer("أرسل آيدي المستخدم أرقاماً فقط.")
            return
        await clear_astate(aid)
        await orders_view(m, int(num))
        return
    if st == "set_text":
        key = data["key"]
        if key == "support":
            v = "" if txt == "-" else txt.lstrip("@")
            await set_setting("support_username", v)
        elif key == "vidox":
            v = txt.lstrip("@")
            if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", v):
                await m.answer("يوزر غير صالح. أعد الإرسال.")
                return
            await set_setting("vidox_username", v)
        elif key == "terms":
            await set_setting("terms_text", DEFAULT_TERMS if txt == "-" else m.html_text)
        elif key == "welcome":
            await set_setting("welcome_text", "" if txt == "-" else m.html_text)
        await clear_astate(aid)
        await log_admin(aid, "setting", key)
        await m.answer("✅ تم الحفظ.", reply_markup=akb([[ab("⚙️ الإعدادات", "a:se")], BACK_HOME]))
        return
    if st == "bc_content":
        ids = await audience_ids(data["aud"])
        await set_astate(aid, "bc_confirm", {"aud": data["aud"], "src_chat": m.chat.id, "src_msg": m.message_id})
        try:
            await bot.copy_message(m.chat.id, m.chat.id, m.message_id)
        except TelegramAPIError:
            pass
        await m.answer(f"👆 معاينة الرسالة.\nالجمهور: <b>{AUDIENCES[data['aud']]}</b>\nعدد المستلمين: <b>{len(ids)}</b>",
                       reply_markup=akb([[ab("✅ إرسال", "a:bc:go"), ab("❌ إلغاء", "a:bc:cancel")]]))
        return


_DIG = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm_digits(s: str) -> str:
    return (s or "").translate(_DIG)


@router.my_chat_member()
async def on_my_chat_member(ev: ChatMemberUpdated) -> None:
    if ev.chat.type != "private":
        return
    st = getattr(ev.new_chat_member.status, "value", ev.new_chat_member.status)
    try:
        await db._run([("UPDATE pay_users SET is_active = ? WHERE telegram_id = ?",
                        [0 if st in ("kicked", "left") else 1, ev.chat.id])])
    except Exception:
        pass


@router.errors()
async def on_error(ev: ErrorEvent) -> bool:
    log.exception("handler error: %r", ev.exception)
    return True


# =====================================================================
#  startup
# =====================================================================
ADMIN_COMMANDS = [
    BotCommand(command="admin", description="🎛 لوحة التحكم"),
    BotCommand(command="start", description="🛒 المتجر"),
    BotCommand(command="terms", description="📜 الشروط"),
    BotCommand(command="support", description="🛟 الدعم"),
    BotCommand(command="paysupport", description="💳 دعم الدفع"),
    BotCommand(command="cancel", description="❌ إلغاء"),
]
USER_COMMANDS = [
    BotCommand(command="start", description="🛒 Buy points | شراء نقاط"),
    BotCommand(command="terms", description="📜 Terms | الشروط"),
    BotCommand(command="support", description="🛟 Support | الدعم"),
    BotCommand(command="paysupport", description="💳 Payment support | دعم الدفع"),
    BotCommand(command="balance", description="💰 Balance | الرصيد"),
]


async def main() -> None:
    global db, bot
    missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("TURSO_DATABASE_URL", TURSO_DATABASE_URL),
                              ("TURSO_AUTH_TOKEN", TURSO_AUTH_TOKEN)) if not v]
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))
    await keep_alive.start(PORT)      # open the port FIRST so Render sees the service as live right away
    db = DB(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN)
    await db.init_schema()
    await load_settings()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    me = await bot.get_me()
    log.info("Starting payment bot @%s", me.username)
    try:
        await db._run([("UPDATE pay_broadcasts SET status = 'interrupted' WHERE status = 'running'", [])])
    except Exception:
        pass
    try:
        await bot.set_my_commands(USER_COMMANDS)
        for a in ADMIN_IDS:
            try:
                await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=a))
            except Exception:
                pass
    except Exception as e:
        log.warning("set commands failed: %s", e)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "pre_checkout_query", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
