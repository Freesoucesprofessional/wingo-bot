"""
WinGo Auto Bet Bot v5.0
========================
- Auto-only: no /auto, no /predict — Start/Stop via inline buttons only
- Images resolved relative to the script folder (fixes "Image missing" on Windows)
- Markdown parse errors fixed (no raw @ usernames in parse_mode=Markdown)
- Per-user pending dict — works correctly for many users simultaneously
- Buttons are vertical: 🟢 Start / 🔴 Stop on separate rows
"""

import hashlib
import http.server
import json
import logging
import os
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ── ENV ────────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
API_TOKEN = os.getenv("API_TOKEN", "")

if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN missing in .env")
if not MONGO_URI: raise RuntimeError("MONGO_URI missing in .env")

ADMIN_IDS = {1793697840}
GAME_CODE = "WinGo_1M"

# ── CHANNEL / OWNER LINKS (appended to every message) ─────────────────────────
CHANNEL_URL  = "https://t.me/danger_boy_op1"
OWNER_URL    = "https://t.me/danger_boy_op"
CHANNEL_NAME = "𒆜ﮩ٨ـﮩ٨ـ𝐉𝐎𝐈𝐍 𝐂𝐇𝐀𝐍𝐍𝐄𝐋ﮩ٨ـﮩ٨ـ𒆜"
OWNER_NAME   = "𒆜ﮩ٨ـﮩ٨ـ𝐂𝐎𝐍𝐓𝐄𝐂𝐓 𝐎𝐖𝐍𝐄𝐑ﮩ٨ـﮩ٨ـ𒆜"
JSON_URL  = "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json"
API_URL   = "https://api.ar-lottery01.com/api/Lottery/GetHistoryIssuePage"

HTTP_PORT = int(os.getenv("PORT", "8080"))   # set PORT in .env or env var if needed

# ── IMAGE PATHS — always resolved next to this script file ────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))
IMG_BIG   = os.path.join(_HERE, "big.jpg")
IMG_SMALL = os.path.join(_HERE, "small.jpg")
IMG_SKIP  = os.path.join(_HERE, "skip.jpg")
IMG_WIN   = os.path.join(_HERE, "win.jpg")
IMG_LOSE  = os.path.join(_HERE, "lose.jpg")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# Print image status at startup so you can verify paths immediately
def _check_images():
    for name, path in [("big",IMG_BIG),("small",IMG_SMALL),("skip",IMG_SKIP),
                        ("win",IMG_WIN),("lose",IMG_LOSE)]:
        status = "✅" if os.path.exists(path) else "❌ MISSING"
        print(f"  {name:6s}.jpg : {status}  ({path})")
_check_images()


# ── IMAGE SENDER ──────────────────────────────────────────────────────────────
def _open_img(path):
    try:
        return open(path, "rb")
    except FileNotFoundError:
        log.warning(f"Image file not found: {path}")
        return None


async def send_img(bot, chat_id, img_path, caption, reply_markup=None):
    """Send photo+caption; falls back to plain text if image missing."""
    photo = _open_img(img_path)
    try:
        if photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )
    finally:
        if photo:
            photo.close()


# ── MONGODB ────────────────────────────────────────────────────────────────────
_mongo = MongoClient(MONGO_URI)
_db    = _mongo["wingo_bot"]
users  = _db["approved_users"]
users.create_index("user_id", unique=True)


def is_approved(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    doc = users.find_one({"user_id": uid})
    if not doc:
        return False
    exp = doc["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        users.delete_one({"user_id": uid})
        return False
    return True


def approve_user(uid: int, uname: str, days: int, admin_id: int):
    exp = datetime.now(timezone.utc) + timedelta(days=days)
    users.update_one(
        {"user_id": uid},
        {"$set": {
            "user_id":     uid,
            "username":    uname,
            "expires_at":  exp,
            "approved_by": admin_id,
            "approved_at": datetime.now(timezone.utc),
            "days":        days,
        }},
        upsert=True,
    )
    return exp


def revoke_user(uid: int) -> bool:
    return users.delete_one({"user_id": uid}).deleted_count > 0


def list_users() -> list:
    return list(users.find({"expires_at": {"$gte": datetime.now(timezone.utc)}}))


def days_left(exp) -> str:
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    s = int((exp - datetime.now(timezone.utc)).total_seconds())
    if s <= 0:
        return "expired"
    d, r = divmod(s, 86400)
    return f"{d}d {r // 3600}h"


# ── HELPERS ───────────────────────────────────────────────────────────────────
def col_emoji(n: int) -> str:
    n = int(n)
    if n == 0: return "🔴🟣"
    if n == 5: return "🟢🟣"
    return "🟢" if n in [1, 3, 7, 9] else "🔴"

def to_bs(n: int) -> str: return "BIG"    if int(n) >= 5 else "SMALL"
def to_oe(n: int) -> str: return "ODD"    if int(n) % 2  else "EVEN"
def bs_e(bs: str) -> str: return "🔼 BIG" if bs == "BIG" else "🔽 SMALL"

def cbar(pct: float, w: int = 10) -> str:
    f = min(int(pct / (100 / w)), w)
    return "█" * f + "░" * (w - f)

def safe_username(uname: str) -> str:
    """Escape username for plain display — never use in Markdown with @."""
    return uname.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")


# ── DATA FETCHERS ─────────────────────────────────────────────────────────────
# Browser-like headers — required to avoid 403 on cloud/server IPs
_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://bdgah.com/",
    "Origin":          "https://bdgah.com",
}


def _sign(params: dict) -> str:
    """MD5 signature used by the WinGo API."""
    f = {k: v for k, v in params.items()
         if v is not None and v != ""
         and k != "signature" and not isinstance(v, (dict, list))}
    return hashlib.md5(
        json.dumps(dict(sorted(f.items())), separators=(",", ":")).encode()
    ).hexdigest().upper()[:32]


def fetch_latest(n: int = 15) -> list:
    """
    Fetch latest results.
    Strategy:
      1. Try the fast static JSON URL (no auth, works from local/most IPs)
      2. If that returns 403/empty (blocked on Render), fall back to the
         signed API endpoint with browser headers — works from any IP.
    Retries 3 times total before giving up.
    """
    for attempt in range(3):
        # ── Try 1: fast static JSON (no auth needed, works on most servers) ──
        try:
            r = requests.get(JSON_URL, headers=_HEADERS, timeout=8)
            if r.status_code == 200:
                text = r.text.strip()
                if text and text.startswith("{"):
                    data = r.json()
                    lst  = (data.get("data") or {}).get("list", [])
                    if lst:
                        log.debug(f"fetch_latest: JSON_URL OK ({len(lst)} records)")
                        return lst[:n]
        except Exception as e:
            log.debug(f"fetch_latest JSON_URL attempt {attempt+1}: {e}")

        # ── Try 2: signed API endpoint (works even when JSON_URL is blocked) ──
        try:
            params = {
                "gameCode": GAME_CODE,
                "language": "en",
                "pageNo":   1,
                "pageSize": max(n, 15),
                "random":   random.randint(100000000000, 999999999999),
            }
            params["signature"] = _sign(params)
            params["timestamp"] = int(time.time())
            headers = dict(_HEADERS)
            if API_TOKEN:
                headers["Authorization"] = f"Bearer {API_TOKEN}"
            r = requests.get(API_URL, params=params, headers=headers, timeout=10)
            if r.status_code == 200:
                text = r.text.strip()
                if text:
                    data = r.json()
                    lst  = (data.get("data") or {}).get("list", [])
                    if lst:
                        log.debug(f"fetch_latest: API_URL OK ({len(lst)} records)")
                        return lst[:n]
            log.warning(f"fetch_latest API attempt {attempt+1}: HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"fetch_latest API attempt {attempt+1}: {e}")

        time.sleep(1)

    log.error("fetch_latest: all attempts failed — both URLs unreachable")
    return []


def fetch_history_api(pages: int = 50) -> list:
    if not API_TOKEN:
        return []

    def zt(d):
        i = 10 ** d
        while True:
            t = random.randint(0, i - 1)
            if not (t < i // 10 and t != 0):
                return t

    nums = []
    for page in range(1, pages + 1):
        b = {"gameCode": GAME_CODE, "language": "en",
             "pageNo": page, "pageSize": 10, "random": zt(12)}
        b["signature"] = _sign(b)
        b["timestamp"] = int(time.time())
        try:
            r = requests.get(API_URL, params=b,
                headers={**_HEADERS, "Authorization": f"Bearer {API_TOKEN}"}, timeout=10)
            d = r.json()
            if d.get("code") == 0:
                for item in d["data"]["list"]:
                    nums.append(int(item["number"]))
            else:
                break
        except Exception as e:
            log.error(f"API p{page}: {e}")
            break
        time.sleep(0.1)
    return nums


# ── PREDICTION ENGINE ─────────────────────────────────────────────────────────
class WinGoPredictor:
    def __init__(self):
        self.history:    list = []
        self.bs_seq:     list = []
        self.last_issue: str  = ""
        self.stats = {"correct": 0, "total": 0,
                      "high_c":  0, "high_t": 0,
                      "med_c":   0, "med_t":  0}

    def load(self, nums: list):
        self.history = nums
        self.bs_seq  = [to_bs(n) for n in reversed(nums)]

    def _streak(self):
        if not self.bs_seq:
            return 0, ""
        v, c = self.bs_seq[-1], 1
        for x in reversed(self.bs_seq[:-1]):
            if x == v: c += 1
            else:       break
        return c, v

    def predict(self) -> dict:
        default = {"bs": "BIG", "oe": "ODD", "confidence": 50.0,
                   "signal": "LOW", "skip": True,
                   "streak": (0, ""), "evidence": [], "suggested": [5, 7, 9]}
        if len(self.bs_seq) < 10:
            return default

        seq = self.bs_seq; votes = Counter(); evidence = []

        # L1: Recency last-5
        r5 = seq[-5:]; r5b = r5.count("BIG"); r5s = 5 - r5b
        if r5b != r5s:
            w = "BIG" if r5b > r5s else "SMALL"
            votes[w] += abs(r5b - r5s) * 1.5
            evidence.append(f"Recency5:{w}({r5b}B/{r5s}S)")

        # L2: Streak
        sk_len, sk_val = self._streak()
        if sk_len >= 4:
            votes[sk_val] += sk_len * 1.0
            evidence.append(f"Streak{sk_len}->CONT({sk_val})")
        elif sk_len == 3:
            opp = "SMALL" if sk_val == "BIG" else "BIG"
            votes[opp] += 0.5
            evidence.append(f"Streak3->rev({opp})")

        # L3: Markov depth-3
        chains3 = defaultdict(Counter)
        for i in range(len(seq) - 3):
            chains3[tuple(seq[i:i+3])][seq[i+3]] += 1
        key3 = tuple(seq[-3:])
        if key3 in chains3:
            t3 = chains3[key3]; tot = sum(t3.values())
            if tot >= 5:
                best3, cnt3 = t3.most_common(1)[0]; conf = cnt3 / tot
                votes[best3] += conf * 1.2
                evidence.append(f"Markov3:{best3}({conf*100:.0f}%,n={tot})")

        # L4: Drift correction last-30
        r30 = seq[-30:]; big30 = r30.count("BIG")
        drift = (big30 - (len(r30) - big30)) / len(r30)
        if abs(drift) > 0.15:
            lesser = "SMALL" if big30 > len(r30) / 2 else "BIG"
            votes[lesser] += abs(drift) * 1.2
            evidence.append(f"Drift{drift*100:+.0f}%->{lesser}")

        if not votes:
            return {**default, "skip": False}

        pred_bs    = votes.most_common(1)[0][0]
        confidence = votes[pred_bs] / sum(votes.values()) * 100
        signal     = "HIGH" if confidence >= 65 else ("MEDIUM" if confidence >= 55 else "LOW")

        oe_seq  = [to_oe(x) for x in reversed(self.history[:10])]
        pred_oe = Counter(oe_seq[-5:]).most_common(1)[0][0] if oe_seq else "ODD"

        pool = [x for x in range(10) if to_bs(x) == pred_bs and to_oe(x) == pred_oe]
        if not pool:
            pool = [x for x in range(10) if to_bs(x) == pred_bs]
        r50 = self.history[:50]
        pool.sort(key=lambda x: r50.count(x))

        return {"bs": pred_bs, "oe": pred_oe, "confidence": round(confidence, 1),
                "signal": signal, "skip": signal == "LOW",
                "streak": (sk_len, sk_val), "evidence": evidence[:4], "suggested": pool[:3]}

    def record(self, pred_bs: str, actual_n: int, signal: str) -> bool:
        actual_bs = to_bs(actual_n)
        won = pred_bs == actual_bs
        self.stats["total"]   += 1
        self.stats["correct"] += int(won)
        if signal == "HIGH":
            self.stats["high_t"] += 1; self.stats["high_c"] += int(won)
        elif signal == "MEDIUM":
            self.stats["med_t"] += 1; self.stats["med_c"] += int(won)
        return won

    @property
    def acc(self):      return self.stats["correct"] / self.stats["total"] * 100 if self.stats["total"] else 0.0
    @property
    def high_acc(self): return self.stats["high_c"] / self.stats["high_t"] * 100 if self.stats["high_t"] else 0.0
    @property
    def med_acc(self):  return self.stats["med_c"] / self.stats["med_t"] * 100 if self.stats["med_t"] else 0.0


# ── STATE ─────────────────────────────────────────────────────────────────────
predictor     = WinGoPredictor()
auto_set: set = set()
last_seen_issue: str = ""   # tracks last issue we already predicted for

# Per-user pending: {uid: {issue: {pred_bs, signal}}}
# Allows many users to run simultaneously without overwriting each other
user_pending: dict = defaultdict(dict)


async def refresh() -> bool:
    latest = fetch_latest(10)
    if not latest:
        return False
    nums = [int(x["number"]) for x in latest]
    ext  = fetch_history_api(50)
    predictor.load(ext if len(ext) >= 20 else nums)
    predictor.last_issue = latest[0]["issueNumber"]
    return True


# ── INLINE KEYBOARDS (vertical layout) ───────────────────────────────────────
def _link_rows() -> list:
    """Channel + Owner URL buttons — appended to every keyboard."""
    return [
        [InlineKeyboardButton(CHANNEL_NAME, url=CHANNEL_URL)],
        [InlineKeyboardButton(OWNER_NAME,   url=OWNER_URL)],
    ]


def kb_running() -> InlineKeyboardMarkup:
    """Prediction / result message — Stats + Stop + links."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",     callback_data="stats")],
        [InlineKeyboardButton("🔴 Stop Auto", callback_data="stop_auto")],
        *_link_rows(),
    ])


def kb_stopped() -> InlineKeyboardMarkup:
    """Stopped state — Start + Stats + links."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Start Auto", callback_data="start_auto")],
        [InlineKeyboardButton("📊 Stats",      callback_data="stats")],
        *_link_rows(),
    ])


def kb_start() -> InlineKeyboardMarkup:
    """Welcome /start message — Start + Stats + links."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Start Auto", callback_data="start_auto")],
        [InlineKeyboardButton("📊 Stats",      callback_data="stats")],
        *_link_rows(),
    ])


# ── STATS TEXT ────────────────────────────────────────────────────────────────
def stats_text() -> str:
    s = predictor.stats
    if s["total"] == 0:
        return "No bets placed yet.\n\nPress Start Auto to begin."
    acc = predictor.acc / 100
    bar = "🟩" * int(acc * 12) + "🟥" * (12 - int(acc * 12))
    lines = [
        "📊 *Session Statistics*\n",
        f"{bar}\n",
        f"✅ Won:   {s['correct']}",
        f"❌ Lost:  {s['total'] - s['correct']}",
        f"📈 Total: {s['total']}",
        f"🎯 Overall: *{predictor.acc:.1f}%*",
    ]
    if s["high_t"]:  lines.append(f"🟢 HIGH:   {s['high_c']}/{s['high_t']} = *{predictor.high_acc:.0f}%*")
    if s["med_t"]:   lines.append(f"🟡 MEDIUM: {s['med_c']}/{s['med_t']} = *{predictor.med_acc:.0f}%*")
    lines.append("\n_Expected: 51-54% on a true RNG_")
    return "\n".join(lines)


# ── JOBS ──────────────────────────────────────────────────────────────────────
async def job_poll(ctx: ContextTypes.DEFAULT_TYPE):
    """
    Polls every 5 seconds.
    Detects the moment a new WinGo round starts (new issue number appears)
    and immediately sends the prediction — so users get it within ~5s of
    round start, giving them ~55s to place their bet before the round closes.
    Also handles WIN/LOSE results for settled rounds.
    """
    global last_seen_issue

    latest = fetch_latest(15)
    if not latest:
        return

    current_issue = latest[0]["issueNumber"]

    # ── Check WIN/LOSE for any settled pending issues ─────────────────────────
    for uid in list(auto_set):
        done = []
        for issue, p in list(user_pending[uid].items()):
            found = next((x for x in latest if x["issueNumber"] == issue), None)
            if not found:
                continue
            actual   = int(found["number"])
            won      = predictor.record(p["pred_bs"], actual, p["signal"])
            img_path = IMG_WIN if won else IMG_LOSE
            caption  = (
                f"{'✅ WIN' if won else '❌ LOSE'}\n\n"
                f"📋 Issue: `{issue}`\n"
                f"🎯 Predicted: *{p['pred_bs']}*\n"
                f"🎲 Actual: *{to_bs(actual)}* {actual} {col_emoji(actual)}\n\n"
                f"📊 {predictor.stats['correct']}/{predictor.stats['total']} "
                f"({predictor.acc:.0f}%)"
            )
            try:
                await send_img(ctx.bot, uid, img_path, caption, reply_markup=kb_running())
            except Exception as e:
                log.error(f"job_poll win/lose uid={uid}: {e}")
            done.append(issue)
        for i in done:
            user_pending[uid].pop(i, None)

    # ── New round detected? → send prediction immediately ────────────────────
    if current_issue == last_seen_issue:
        return  # same round, nothing to predict yet

    last_seen_issue = current_issue
    log.info(f"New issue detected: {current_issue} — sending predictions now")

    if not auto_set:
        return

    # Reload history for accurate prediction
    nums = [int(x["number"]) for x in latest]
    ext  = fetch_history_api(50)
    predictor.load(ext if len(ext) >= 20 else nums)
    predictor.last_issue = current_issue

    pred = predictor.predict()
    nxt  = str(int(current_issue) + 1)

    sig            = pred["signal"]
    sig_e          = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}[sig]
    sk_len, sk_val = pred["streak"]
    sk_info        = f"🔥 {sk_len}x {sk_val}" if sk_len >= 2 else "None"

    if pred["skip"]:
        img_path = IMG_SKIP
        caption  = (
            f"⚠️ *SKIP*  `{nxt}`\n\n"
            f"📊 `{cbar(pred['confidence'])}` {pred['confidence']:.0f}%\n"
            f"🔴 No bet this round\n\n"
            f"📈 Streak: {sk_info}\n"
            f"_{datetime.now().strftime('%H:%M:%S')}_"
        )
    else:
        img_path = IMG_BIG if pred["bs"] == "BIG" else IMG_SMALL
        sugg     = "  ".join(str(x) for x in pred["suggested"])
        caption  = (
            f"🎯 *{bs_e(pred['bs'])}*  `{nxt}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 *{pred['oe']}*\n"
            f"🎰 Numbers: `{sugg}`\n\n"
            f"📊 `{cbar(pred['confidence'])}` {pred['confidence']:.0f}%\n"
            f"{sig_e} Signal: *{sig}*\n"
            f"📈 Streak: {sk_info}\n\n"
            f"_{datetime.now().strftime('%H:%M:%S')}_"
        )

    for uid in list(auto_set):
        if not is_approved(uid):
            auto_set.discard(uid)
            try:
                await ctx.bot.send_message(uid, "⛔ Your access has expired.")
            except Exception:
                pass
            continue
        try:
            await send_img(ctx.bot, uid, img_path, caption, reply_markup=kb_running())
            if not pred["skip"]:
                user_pending[uid][nxt] = {"pred_bs": pred["bs"], "signal": sig}
        except Exception as e:
            log.error(f"job_poll send uid={uid}: {e}")


async def job_expire(ctx: ContextTypes.DEFAULT_TYPE):
    expired = list(_db["approved_users"].find(
        {"expires_at": {"$lt": datetime.now(timezone.utc)}}
    ))
    for doc in expired:
        uid = doc["user_id"]
        users.delete_one({"user_id": uid})
        auto_set.discard(uid)
        user_pending.pop(uid, None)
        try:
            await ctx.bot.send_message(
                uid,
                "⛔ *Access Expired*\n\nYour access has expired.\nContact admin to renew.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        log.info(f"Expired: {uid}")


# ── GUARDS ────────────────────────────────────────────────────────────────────
def req_approved(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_approved(uid):
            doc = users.find_one({"user_id": uid})
            msg = ("⛔ Access expired. Contact admin to renew."
                   if doc else "⛔ Not approved. Contact admin to get access.")
            await update.message.reply_text(msg)
            return
        return await fn(update, ctx)
    return w


def req_admin(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("⛔ Admin only.")
            return
        return await fn(update, ctx)
    return w


# ── USER COMMANDS ─────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name or "User"

    if uid in ADMIN_IDS:
        exp_line = "👑 Admin — permanent access"
    elif is_approved(uid):
        doc = users.find_one({"user_id": uid})
        exp_line = f"✅ Access active — {days_left(doc['expires_at'])} remaining"
    else:
        exp_line = "⛔ Not approved — contact admin"

    status = "🟢 Running" if uid in auto_set else "🔴 Stopped"

    await update.message.reply_text(
        f"👋 *{name}*\n\n"
        f"🎯 *WinGo Auto Bet Bot*\n"
        f"{'━'*26}\n"
        f"{exp_line}\n"
        f"Auto: {status}\n\n"
        f"Press *Start Auto* to begin receiving predictions every 60 seconds.",
        parse_mode="Markdown",
        reply_markup=kb_start() if uid not in auto_set else kb_running(),
    )


async def cmd_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_part = (
        "👤 *User Commands*\n"
        "/start — Welcome screen with Start button\n"
        "/history — Last 10 results\n"
        "/stats — Win rate this session\n"
        "/info — Algorithm info\n"
        "/status — Your access expiry\n"
        "/cmd — This list\n"
    )
    admin_part = (
        "\n👑 *Admin Commands*\n"
        "/approve `<id> <days> [name]` — Grant access\n"
        "/revoke `<id>` — Remove access\n"
        "/users — List approved users\n"
        "/addadmin `<id>` — Add admin (runtime)\n"
        "/broadcast `<msg>` — Message all users\n"
        "/resetstats — Reset session stats\n"
    ) if uid in ADMIN_IDS else ""

    await update.message.reply_text(user_part + admin_part, parse_mode="Markdown")


@req_approved
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    latest = fetch_latest(10)
    if not latest:
        await update.message.reply_text("Failed to fetch results.")
        return
    lines = ["📋 *Last 10 Results*\n"]
    for item in latest:
        n = int(item["number"])
        lines.append(f"`{item["issueNumber"]}` → *{n}* {col_emoji(n)}  {to_bs(n)} · {to_oe(n)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@req_approved
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(), parse_mode="Markdown")


async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Algorithm: Recency Ensemble v5*\n\n"
        "Backtested on 999 real WinGo rounds:\n"
        "• Markov depth-7: 64% in-sample → 49% real (overfitting)\n"
        "• Recency last-5: 53% real edge\n"
        "• Streak 4+: 63% continuation\n\n"
        "Signal guide:\n"
        "🟢 HIGH above 65% — bet\n"
        "🟡 MEDIUM 55-65% — bet small\n"
        "🔴 SKIP below 55% — no bet\n\n"
        "Expected accuracy: 51-54%\n"
        "Never risk more than you can afford to lose.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await update.message.reply_text("👑 Admin — permanent access.")
        return
    doc = users.find_one({"user_id": uid})
    if not doc:
        await update.message.reply_text("⛔ Not approved.")
        return
    exp = doc["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    status = "🟢 Running" if uid in auto_set else "🔴 Stopped"
    await update.message.reply_text(
        f"✅ *Access Active*\n\n"
        f"User ID: `{uid}`\n"
        f"Expires: `{exp.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        f"Remaining: *{days_left(exp)}*\n"
        f"Auto: {status}",
        parse_mode="Markdown",
    )


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
@req_admin
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /approve <user_id> <days> [username]\n"
            "Example: /approve 123456789 30 john"
        )
        return
    try:
        uid  = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("user_id and days must be numbers.")
        return
    if days <= 0 or days > 3650:
        await update.message.reply_text("Days must be 1 to 3650.")
        return

    uname = args[2] if len(args) > 2 else f"user{args[0]}"
    exp   = approve_user(uid, uname, days, update.effective_user.id)
    exp_s = exp.strftime("%Y-%m-%d %H:%M UTC")

    # No Markdown here — avoids parse errors with special chars in usernames
    await update.message.reply_text(
        f"Approved!\n\n"
        f"User ID: {uid}\n"
        f"Username: {uname}\n"
        f"Duration: {days} days\n"
        f"Expires: {exp_s}"
    )
    try:
        await ctx.bot.send_message(
            uid,
            f"Access Approved!\n\n"
            f"Duration: {days} days\n"
            f"Expires: {exp_s}\n\n"
            f"Press /start to begin.",
        )
    except Exception as e:
        log.warning(f"Could not notify {uid}: {e}")
        await update.message.reply_text(
            f"Approved but could not notify user {uid}. "
            f"They may not have started the bot yet."
        )


@req_admin
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    ok = revoke_user(uid)
    auto_set.discard(uid)
    user_pending.pop(uid, None)
    await update.message.reply_text(
        f"Revoked user {uid}." if ok else f"User {uid} not found."
    )
    if ok:
        try:
            await ctx.bot.send_message(uid, "Your access has been revoked by admin.")
        except Exception:
            pass


@req_admin
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    active = list_users()
    if not active:
        await update.message.reply_text("No approved users.")
        return
    lines = [f"Approved Users ({len(active)})\n"]
    for doc in active:
        exp = doc["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        running = "🟢" if doc["user_id"] in auto_set else "⚫"
        lines.append(
            f"{running} {doc['user_id']} ({doc.get('username','?')}) — {days_left(exp)}"
        )
    await update.message.reply_text("\n".join(lines))


@req_admin
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    text = " ".join(ctx.args)
    sent = fail = 0
    for doc in list_users():
        try:
            await ctx.bot.send_message(doc["user_id"], f"Admin message:\n\n{text}")
            sent += 1
        except Exception:
            fail += 1
    await update.message.reply_text(f"Sent: {sent}  Failed: {fail}")


@req_admin
async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /addadmin <user_id>  (runtime only)")
        return
    try:
        uid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    ADMIN_IDS.add(uid)
    await update.message.reply_text(f"Added {uid} as admin (runtime only).")
    try:
        await ctx.bot.send_message(uid, "You have been granted admin access.")
    except Exception:
        pass


@req_admin
async def cmd_resetstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    predictor.stats = {"correct": 0, "total": 0,
                       "high_c": 0, "high_t": 0,
                       "med_c": 0, "med_t": 0}
    await update.message.reply_text("Session stats reset.")


# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    uid     = q.from_user.id
    mo      = q.message
    data    = q.data

    if data == "stats":
        await mo.reply_text(stats_text(), parse_mode="Markdown")

    elif data == "start_auto":
        if not is_approved(uid):
            await mo.reply_text("Not approved. Contact admin.")
            return
        auto_set.add(uid)
        await mo.reply_text(
            "🟢 *Auto Betting ON*\n\n"
            "You will receive a prediction every 60 seconds automatically.\n\n"
            "Signal guide:\n"
            "🟢 HIGH — bet\n"
            "🟡 MEDIUM — bet small\n"
            "🔴 SKIP — no bet this round",
            parse_mode="Markdown",
            reply_markup=kb_running(),
        )

    elif data == "stop_auto":
        auto_set.discard(uid)
        await mo.reply_text(
            "🔴 *Auto Betting STOPPED*\n\nPress Start Auto to resume.",
            parse_mode="Markdown",
            reply_markup=kb_stopped(),
        )


# ── MAIN ──────────────────────────────────────────────────────────────────────

# ── HEALTH-CHECK HTTP SERVER ──────────────────────────────────────────────────
_bot_start_time = datetime.now(timezone.utc)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Tiny HTTP server — responds 200 on GET/HEAD / so render/railway/koyeb
    uptime monitors know the bot process is alive."""

    def do_GET(self):
        self._respond()

    def do_HEAD(self):
        self._respond(body=False)

    def _respond(self, body=True):
        uptime  = datetime.now(timezone.utc) - _bot_start_time
        hours, rem  = divmod(int(uptime.total_seconds()), 3600)
        minutes, secs = divmod(rem, 60)
        payload = (
            f"✅ Bot is running\n"
            f"Uptime : {hours}h {minutes}m {secs}s\n"
            f"Users  : {len(auto_set)} active\n"
            f"Since  : {_bot_start_time.strftime('%Y-%m-%d %H:%M UTC')}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass   # silence default access logs


def _start_health_server():
    server = http.server.HTTPServer(("0.0.0.0", HTTP_PORT), _HealthHandler)
    log.info(f"Health server listening on port {HTTP_PORT}")
    server.serve_forever()


def main():
    print("=" * 44)
    print("   WinGo Auto Bet Bot  v5.0")
    print("=" * 44)
    print(f"  BOT_TOKEN : {'OK' if BOT_TOKEN else 'MISSING'}")
    print(f"  MONGO_URI : {'OK' if MONGO_URI else 'MISSING'}")
    print(f"  API_TOKEN : {'OK (500-rec)' if API_TOKEN else 'none (10-rec)'}")
    print("  Images:")
    _check_images()
    print("=" * 44)

    app = Application.builder().token(BOT_TOKEN).build()

    for cmd_name, fn in [
        ("start",      cmd_start),
        ("cmd",        cmd_cmd),
        ("history",    cmd_history),
        ("stats",      cmd_stats),
        ("info",       cmd_info),
        ("status",     cmd_status),
        ("approve",    cmd_approve),
        ("revoke",     cmd_revoke),
        ("users",      cmd_users),
        ("broadcast",  cmd_broadcast),
        ("addadmin",   cmd_addadmin),
        ("resetstats", cmd_resetstats),
    ]:
        app.add_handler(CommandHandler(cmd_name, fn))
    app.add_handler(CallbackQueryHandler(on_cb))

    jq = app.job_queue
    jq.run_repeating(job_poll,   interval=5,    first=3)   # fires within 5s of each new round
    jq.run_repeating(job_expire, interval=3600, first=120)

    # Start health-check HTTP server in background thread
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()
    print(f"  Health : http://0.0.0.0:{HTTP_PORT}/")

    print("\nBot running. Ctrl+C to stop.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()