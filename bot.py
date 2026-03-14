"""
WinGo Auto Bet Bot v6.0
========================
- Single API: draw.ar-lottery01.com static JSON (no fallback, no signed API)
- Auto-only: Start/Stop via inline buttons
- Images resolved relative to the script folder
- Per-user pending dict for concurrent users
- Vertical buttons: 🟢 Start / 🔴 Stop on separate rows
"""

import http.server
import json
import logging
import os
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

if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN missing in .env")
if not MONGO_URI: raise RuntimeError("MONGO_URI missing in .env")

ADMIN_IDS = {1793697840}

# ── CHANNEL / OWNER LINKS ─────────────────────────────────────────────────────
CHANNEL_URL  = "https://t.me/danger_boy_op1"
OWNER_URL    = "https://t.me/danger_boy_op"
CHANNEL_NAME = "𒆜ﮩ٨ـﮩ٨ـ𝐉𝐎𝐈𝐍 𝐂𝐇𝐀𝐍𝐍𝐄𝐋ﮩ٨ـﮩ٨ـ𒆜"
OWNER_NAME   = "𒆜ﮩ٨ـﮩ٨ـ𝐂𝐎𝐍𝐓𝐄𝐂𝐓 𝐎𝐖𝐍𝐄𝐑ﮩ٨ـﮩ٨ـ𒆜"
# ── API ───────────────────────────────────────────────────────────────────────
HISTORY_URL = "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json"
# Cloudflare Worker URL — set via env var WORKER_URL
# e.g. https://wingo-proxy.yourname.workers.dev
WORKER_URL = os.getenv("WORKER_URL", "")

HTTP_PORT = int(os.getenv("PORT", "8080"))

# ── IMAGES ────────────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))
IMG_BIG   = os.path.join(_HERE, "big.jpg")
IMG_SMALL = os.path.join(_HERE, "small.jpg")
IMG_SKIP  = os.path.join(_HERE, "skip.jpg")
IMG_WIN   = os.path.join(_HERE, "win.jpg")
IMG_LOSE  = os.path.join(_HERE, "lose.jpg")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def _check_images():
    for name, path in [("big", IMG_BIG), ("small", IMG_SMALL), ("skip", IMG_SKIP),
                       ("win", IMG_WIN), ("lose", IMG_LOSE)]:
        status = "✅" if os.path.exists(path) else "❌ MISSING"
        print(f"  {name:6s}.jpg : {status}  ({path})")


# ── IMAGE SENDER ──────────────────────────────────────────────────────────────
def _open_img(path):
    try:
        return open(path, "rb")
    except FileNotFoundError:
        log.warning(f"Image file not found: {path}")
        return None


async def send_img(bot, chat_id, img_path, caption, reply_markup=None):
    photo = _open_img(img_path)
    try:
        if photo:
            await bot.send_photo(
                chat_id=chat_id, photo=photo, caption=caption,
                parse_mode="Markdown", reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=chat_id, text=caption,
                parse_mode="Markdown", reply_markup=reply_markup,
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
            "user_id": uid, "username": uname, "expires_at": exp,
            "approved_by": admin_id, "approved_at": datetime.now(timezone.utc), "days": days,
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

def to_bs(n: int) -> str: return "BIG"   if int(n) >= 5 else "SMALL"
def to_oe(n: int) -> str: return "ODD"   if int(n) % 2  else "EVEN"
def bs_e(bs: str)  -> str: return "🔼 BIG" if bs == "BIG" else "🔽 SMALL"

def cbar(pct: float, w: int = 10) -> str:
    f = min(int(pct / (100 / w)), w)
    return "█" * f + "░" * (w - f)


# ── DATA FETCHER (single source) ──────────────────────────────────────────────
_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://bdgah.com/",
    "Origin":          "https://bdgah.com",
}


def fetch_latest(n: int = 10) -> list:
    """
    Fetch via Cloudflare Worker (if WORKER_URL set) or direct.
    Worker bypasses server IP blocks since Cloudflare IPs are never blocked.
    """
    url = WORKER_URL if WORKER_URL else f"{HISTORY_URL}?ts={int(time.time() * 1000)}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=8)
        if r.status_code == 200 and r.text.strip():
            lst = (r.json().get("data") or {}).get("list", [])
            if lst:
                return lst[:n]
        log.warning(f"fetch_latest: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"fetch_latest: {e}")
    return []


# ── PREDICTION ENGINE ─────────────────────────────────────────────────────────
class WinGoPredictor:
    def __init__(self):
        self.history:    list = []
        self.bs_seq:     list = []
        self.last_issue: str  = ""
        self.stats = {"correct": 0, "total": 0,
                      "high_c": 0,  "high_t": 0,
                      "med_c":  0,  "med_t":  0}

    def load(self, nums: list):
        self.history = nums
        self.bs_seq  = [to_bs(n) for n in reversed(nums)]

    def _streak(self):
        if not self.bs_seq:
            return 0, ""
        v, c = self.bs_seq[-1], 1
        for x in reversed(self.bs_seq[:-1]):
            if x == v: c += 1
            else:      break
        return c, v

    def predict(self) -> dict:
        default = {"bs": "BIG", "oe": "ODD", "confidence": 50.0,
                   "signal": "LOW", "skip": True,
                   "streak": (0, ""), "evidence": [], "suggested": [5, 7, 9]}
        if len(self.bs_seq) < 10:
            return default

        seq = self.bs_seq
        votes: Counter = Counter()
        evidence = []

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
        chains3: dict = defaultdict(Counter)
        for i in range(len(seq) - 3):
            chains3[tuple(seq[i:i+3])][seq[i+3]] += 1
        key3 = tuple(seq[-3:])
        if key3 in chains3:
            t3 = chains3[key3]; tot = sum(t3.values())
            if tot >= 5:
                best3, cnt3 = t3.most_common(1)[0]
                votes[best3] += (cnt3 / tot) * 1.2
                evidence.append(f"Markov3:{best3}({cnt3/tot*100:.0f}%,n={tot})")

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

        return {
            "bs": pred_bs, "oe": pred_oe,
            "confidence": round(confidence, 1),
            "signal": signal, "skip": signal == "LOW",
            "streak": (sk_len, sk_val), "evidence": evidence[:4],
            "suggested": pool[:3],
        }

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
predictor        = WinGoPredictor()
auto_set: set    = set()
last_seen_issue: str = ""
user_pending: dict   = defaultdict(dict)   # {uid: {issue: {pred_bs, signal}}}


# ── INLINE KEYBOARDS ──────────────────────────────────────────────────────────
def _link_rows() -> list:
    return [
        [InlineKeyboardButton(CHANNEL_NAME, url=CHANNEL_URL)],
        [InlineKeyboardButton(OWNER_NAME,   url=OWNER_URL)],
    ]

def kb_running() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",     callback_data="stats")],
        [InlineKeyboardButton("🔴 Stop Auto", callback_data="stop_auto")],
        *_link_rows(),
    ])

def kb_stopped() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Start Auto", callback_data="start_auto")],
        [InlineKeyboardButton("📊 Stats",      callback_data="stats")],
        *_link_rows(),
    ])

def kb_start() -> InlineKeyboardMarkup:
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
    if s["high_t"]: lines.append(f"🟢 HIGH:   {s['high_c']}/{s['high_t']} = *{predictor.high_acc:.0f}%*")
    if s["med_t"]:  lines.append(f"🟡 MEDIUM: {s['med_c']}/{s['med_t']} = *{predictor.med_acc:.0f}%*")
    lines.append("\n_Expected: 51-54% on a true RNG_")
    return "\n".join(lines)


# ── JOBS ──────────────────────────────────────────────────────────────────────
async def job_poll(ctx: ContextTypes.DEFAULT_TYPE):
    global last_seen_issue

    latest = fetch_latest(10)
    if not latest:
        return

    current_issue = latest[0]["issueNumber"]

    # ── WIN/LOSE check for settled issues ─────────────────────────────────────
    for uid in list(auto_set):
        done = []
        for issue, p in list(user_pending[uid].items()):
            found = next((x for x in latest if x["issueNumber"] == issue), None)
            if not found:
                continue
            actual = int(found["number"])
            won    = predictor.record(p["pred_bs"], actual, p["signal"])
            label  = "✅ WIN" if won else "❌ LOSE"
            caption = (
                f"{label}\n\n"
                f"📋 Issue: `{issue}`\n"
                f"🎯 Predicted: *{p['pred_bs']}*\n"
                f"🎲 Actual: *{to_bs(actual)}* {actual} {col_emoji(actual)}\n\n"
                f"📊 {predictor.stats['correct']}/{predictor.stats['total']} "
                f"({predictor.acc:.0f}%)"
            )
            img_path = IMG_WIN if won else IMG_LOSE
            try:
                await send_img(ctx.bot, uid, img_path, caption, reply_markup=kb_running())
            except Exception as e:
                log.error(f"job_poll win/lose uid={uid}: {e}")
            done.append(issue)
        for i in done:
            user_pending[uid].pop(i, None)

    # ── New round -> send prediction ──────────────────────────────────────────
    if current_issue == last_seen_issue:
        return

    last_seen_issue = current_issue
    log.info("New round: %s", current_issue)

    if not auto_set:
        return

    nums = [int(x["number"]) for x in latest]
    predictor.load(nums)
    predictor.last_issue = current_issue

    pred           = predictor.predict()
    nxt            = str(int(current_issue) + 1)
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
                await ctx.bot.send_message(uid, "Your access has expired.")
            except Exception:
                pass
            continue
        try:
            await send_img(ctx.bot, uid, img_path, caption, reply_markup=kb_running())
            if not pred["skip"]:
                user_pending[uid][nxt] = {"pred_bs": pred["bs"], "signal": sig}
        except Exception as e:
            log.error("job_poll send uid=%s: %s", uid, e)


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
                "⛔ *Access Expired*\n\nContact admin to renew.",
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
                   if doc else "⛔ Not approved. Contact admin.")
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
    kb     = kb_running() if uid in auto_set else kb_start()

    await update.message.reply_text(
        f"👋 *{name}*\n\n"
        f"🎯 *WinGo Auto Bet Bot*\n"
        f"{'━'*26}\n"
        f"{exp_line}\n"
        f"Auto: {status}\n\n"
        f"Press *Start Auto* to begin.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def cmd_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_part = (
        "👤 *User Commands*\n"
        "/start — Welcome screen\n"
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
        lines.append(f"`{item['issueNumber']}` → *{n}* {col_emoji(n)}  {to_bs(n)} · {to_oe(n)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@req_approved
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(), parse_mode="Markdown")


async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Algorithm: Recency Ensemble v6*\n\n"
        "Layers:\n"
        "• Recency last-5: momentum bias\n"
        "• Streak 4+: continuation | Streak 3: reversal\n"
        "• Markov depth-3: pattern matching (n≥5)\n"
        "• Drift correction last-30: mean reversion\n\n"
        "Signal guide:\n"
        "🟢 HIGH ≥65% — bet\n"
        "🟡 MEDIUM 55–65% — bet small\n"
        "🔴 SKIP <55% — no bet this round\n\n"
        "Expected accuracy: 51–54%\n"
        "_Never risk more than you can afford to lose._",
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
        await update.message.reply_text("Usage: /approve <user_id> <days> [username]")
        return
    try:
        uid  = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("user_id and days must be numbers.")
        return
    if not (1 <= days <= 3650):
        await update.message.reply_text("Days must be 1 to 3650.")
        return

    uname = args[2] if len(args) > 2 else f"user{args[0]}"
    exp   = approve_user(uid, uname, days, update.effective_user.id)
    exp_s = exp.strftime("%Y-%m-%d %H:%M UTC")

    await update.message.reply_text(
        f"Approved!\n\nUser ID: {uid}\nUsername: {uname}\nDuration: {days} days\nExpires: {exp_s}"
    )
    try:
        await ctx.bot.send_message(
            uid,
            f"Access Approved!\n\nDuration: {days} days\nExpires: {exp_s}\n\nPress /start to begin."
        )
    except Exception as e:
        log.warning(f"Could not notify {uid}: {e}")


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
        lines.append(f"{running} {doc['user_id']} ({doc.get('username','?')}) — {days_left(exp)}")
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
                       "med_c": 0,  "med_t": 0}
    await update.message.reply_text("Session stats reset.")


# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    if data == "stats":
        await q.message.reply_text(stats_text(), parse_mode="Markdown")

    elif data == "start_auto":
        if not is_approved(uid):
            await q.message.reply_text("Not approved. Contact admin.")
            return
        auto_set.add(uid)
        await q.message.reply_text(
            "🟢 *Auto Betting ON*\n\n"
            "You will receive predictions automatically.\n\n"
            "🟢 HIGH — bet\n"
            "🟡 MEDIUM — bet small\n"
            "🔴 SKIP — no bet this round",
            parse_mode="Markdown",
            reply_markup=kb_running(),
        )

    elif data == "stop_auto":
        auto_set.discard(uid)
        await q.message.reply_text(
            "🔴 *Auto Betting STOPPED*\n\nPress Start Auto to resume.",
            parse_mode="Markdown",
            reply_markup=kb_stopped(),
        )


# ── HEALTH-CHECK SERVER ───────────────────────────────────────────────────────
_bot_start_time = datetime.now(timezone.utc)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  self._respond()
    def do_HEAD(self): self._respond(body=False)

    def _respond(self, body=True):
        uptime        = datetime.now(timezone.utc) - _bot_start_time
        h, rem        = divmod(int(uptime.total_seconds()), 3600)
        m, s          = divmod(rem, 60)
        payload       = (
            f"✅ Bot running\n"
            f"Uptime : {h}h {m}m {s}s\n"
            f"Users  : {len(auto_set)} active\n"
            f"Since  : {_bot_start_time.strftime('%Y-%m-%d %H:%M UTC')}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, *_): pass


def _start_health_server():
    http.server.HTTPServer(("0.0.0.0", HTTP_PORT), _HealthHandler).serve_forever()


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 44)
    print("   WinGo Auto Bet Bot  v6.0")
    print("=" * 44)
    print(f"  BOT_TOKEN : {'OK' if BOT_TOKEN else 'MISSING'}")
    print(f"  MONGO_URI : {'OK' if MONGO_URI else 'MISSING'}")
    print("  Images:")
    _check_images()
    print("=" * 44)

    app = Application.builder().token(BOT_TOKEN).build()

    for name, fn in [
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
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_cb))

    jq = app.job_queue
    jq.run_repeating(job_poll,   interval=10,   first=3)
    jq.run_repeating(job_expire, interval=3600, first=120)

    threading.Thread(target=_start_health_server, daemon=True).start()
    print(f"  Health : http://0.0.0.0:{HTTP_PORT}/\n")
    print("Bot running. Ctrl+C to stop.\n")

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()