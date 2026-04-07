"""
Rock Paper Scissors — Telegram Bot
PvP works entirely in the group chat, no DM needed.
Railway-safe: stdout logging, no close_loop, dotenv optional.
"""

import logging
import random
import sqlite3
import os
import sys
import time
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Choices ───────────────────────────────────────────────────────────────────
class Choice(Enum):
    ROCK     = "🪨 Rock"
    PAPER    = "📄 Paper"
    SCISSORS = "✂️ Scissors"

CHOICE_FROM_STR = {"rock": Choice.ROCK, "paper": Choice.PAPER, "scissors": Choice.SCISSORS}

def beats(a: Choice, b: Choice) -> int:
    """Return 1 if a beats b, -1 if b beats a, 0 for draw."""
    if a == b:
        return 0
    wins = {(Choice.ROCK, Choice.SCISSORS), (Choice.SCISSORS, Choice.PAPER), (Choice.PAPER, Choice.ROCK)}
    return 1 if (a, b) in wins else -1

# ── In-memory PvP store ───────────────────────────────────────────────────────
pvp_games: dict = {}          # cid -> game dict
CHALLENGE_TTL = 60            # seconds to accept
PICK_TTL      = 120           # seconds to pick after accepting

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "rps_stats.db")

def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                wins        INTEGER DEFAULT 0,
                losses      INTEGER DEFAULT 0,
                draws       INTEGER DEFAULT 0,
                total_games INTEGER DEFAULT 0,
                pvp_wins    INTEGER DEFAULT 0,
                pvp_losses  INTEGER DEFAULT 0,
                pvp_draws   INTEGER DEFAULT 0,
                pvp_games   INTEGER DEFAULT 0,
                last_played TIMESTAMP,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS game_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                opponent_id INTEGER,
                user_choice TEXT,
                opp_choice  TEXT,
                result      TEXT,
                game_type   TEXT DEFAULT 'bot',
                played_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    # safe column migrations for old DBs
    with db() as conn:
        for col in ["pvp_wins INTEGER DEFAULT 0", "pvp_losses INTEGER DEFAULT 0",
                    "pvp_draws INTEGER DEFAULT 0", "pvp_games INTEGER DEFAULT 0",
                    "last_name TEXT"]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
    logger.info("DB ready → %s", DB_PATH)

def upsert_user(u):
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id,username,first_name,last_name) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
            "first_name=excluded.first_name, last_name=excluded.last_name",
            (u.id, u.username or "", u.first_name or "", u.last_name or ""),
        )

def fetch_stats(uid: int):
    row = db().execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    return dict(row) if row else None

def record_bot(uid, uchoice, bchoice, result):
    col = {"win": "wins", "loss": "losses", "draw": "draws"}[result]
    with db() as conn:
        conn.execute(
            f"UPDATE users SET {col}={col}+1, total_games=total_games+1, "
            "last_played=CURRENT_TIMESTAMP WHERE user_id=?", (uid,)
        )
        conn.execute(
            "INSERT INTO game_history(user_id,opponent_id,user_choice,opp_choice,result,game_type) "
            "VALUES(?,0,?,?,?,'bot')", (uid, uchoice, bchoice, result)
        )

def record_pvp(uid, oid, uchoice, ochoice, result):
    col = {"win": "pvp_wins", "loss": "pvp_losses", "draw": "pvp_draws"}[result]
    with db() as conn:
        conn.execute(
            f"UPDATE users SET {col}={col}+1, pvp_games=pvp_games+1, "
            "last_played=CURRENT_TIMESTAMP WHERE user_id=?", (uid,)
        )
        conn.execute(
            "INSERT INTO game_history(user_id,opponent_id,user_choice,opp_choice,result,game_type) "
            "VALUES(?,?,?,?,?,'pvp')", (uid, oid, uchoice, ochoice, result)
        )

# ── Name helpers ──────────────────────────────────────────────────────────────
def name(user) -> str:
    n = (user.first_name or "")
    if user.last_name:
        n += f" {user.last_name}"
    return n.strip() or f"@{user.username}" if user.username else "Player"

def name_db(row: dict) -> str:
    n = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
    return n or (f"@{row['username']}" if row.get("username") else "Player")

# ── Keyboards ─────────────────────────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪨 Rock",     callback_data="play_rock"),
            InlineKeyboardButton("📄 Paper",    callback_data="play_paper"),
            InlineKeyboardButton("✂️ Scissors", callback_data="play_scissors"),
        ],
        [InlineKeyboardButton("⚔️ Challenge a Friend", callback_data="pvp_challenge")],
        [
            InlineKeyboardButton("📊 My Stats",    callback_data="show_stats"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
        ],
        [InlineKeyboardButton("ℹ️ Rules", callback_data="rules")],
    ])

def kb_rematch() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play Again", callback_data="main_menu"),
        InlineKeyboardButton("📊 Stats",      callback_data="show_stats"),
    ]])

def kb_pick(cid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🪨 Rock",     callback_data=f"pvp_pick_{cid}_rock"),
        InlineKeyboardButton("📄 Paper",    callback_data=f"pvp_pick_{cid}_paper"),
        InlineKeyboardButton("✂️ Scissors", callback_data=f"pvp_pick_{cid}_scissors"),
    ]])

def kb_after_pvp() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 vs Bot",       callback_data="main_menu"),
        InlineKeyboardButton("⚔️ New Challenge", callback_data="pvp_challenge"),
    ]])

# ── Safe Telegram helpers ─────────────────────────────────────────────────────
async def safe_answer(query, text="", alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except TelegramError as e:
        logger.debug("answer failed: %s", e)

async def safe_edit(bot, chat_id, msg_id, text, markup=None):
    """Edit a message. Returns True on success."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        return True
    except BadRequest as e:
        # "message is not modified" is fine — everything else log it
        if "not modified" not in str(e).lower():
            logger.warning("edit_message_text BadRequest: %s", e)
        return False
    except TelegramError as e:
        logger.warning("edit_message_text error: %s", e)
        return False

async def safe_send(bot, chat_id, text, markup=None, thread_id=None):
    """Send a new message. Returns message_id or None."""
    try:
        m = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            message_thread_id=thread_id,
        )
        return m.message_id
    except TelegramError as e:
        logger.warning("send_message error: %s", e)
        return None

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    await update.message.reply_text(
        f"🎮 <b>Rock Paper Scissors!</b>\n\n"
        f"Hi <b>{name(user)}</b>! 👋\n\n"
        f"🤖 Play vs Bot\n"
        f"⚔️ Challenge friends in your group\n"
        f"📊 Stats &amp; 🏆 Leaderboard\n\n"
        f"<i>Pick a move to start!</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main(),
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    await update.message.reply_text(
        _fmt_stats(name(user), fetch_stats(user.id)),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎮 Play",        callback_data="main_menu"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
        ]]),
    )

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _fmt_lb(), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎮 Play", callback_data="main_menu"),
        ]]),
    )

# ── vs Bot ────────────────────────────────────────────────────────────────────
async def cb_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user
    upsert_user(user)

    uc = {"play_rock": Choice.ROCK, "play_paper": Choice.PAPER, "play_scissors": Choice.SCISSORS}.get(q.data)
    if not uc:
        return

    bc      = random.choice(list(Choice))
    outcome = beats(uc, bc)
    rtype   = {1: "win", -1: "loss", 0: "draw"}[outcome]
    icon    = {"win": "✨", "loss": "💔", "draw": "⚖️"}[rtype]
    head    = {"win": "🎉 <b>YOU WIN!</b>", "loss": "😔 <b>YOU LOSE!</b>", "draw": "🤝 <b>DRAW!</b>"}[rtype]

    record_bot(user.id, uc.name, bc.name, rtype)
    s   = fetch_stats(user.id)
    tot = s["total_games"]
    wr  = s["wins"] / tot * 100 if tot else 0

    text = (
        f"{icon} {head}\n\n"
        f"You: {uc.value}  |  Bot: {bc.value}\n\n"
        f"📊 {s['wins']}W {s['losses']}L {s['draws']}D — {wr:.1f}% win rate"
    )

    await safe_answer(q)
    if update.effective_chat.type in ("group", "supergroup"):
        await safe_send(
            context.bot, update.effective_chat.id, text, kb_rematch(),
            thread_id=update.effective_message.message_thread_id,
        )
    else:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_rematch())
        except TelegramError:
            pass

# ── PvP: Issue challenge ──────────────────────────────────────────────────────
async def cb_pvp_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user
    upsert_user(user)
    await safe_answer(q)

    cid = f"{user.id}_{int(time.time())}"
    pvp_games[cid] = {
        "c_id":     user.id,
        "c_name":   name(user),
        "a_id":     None,
        "a_name":   None,
        "c_choice": None,
        "a_choice": None,
        "chat_id":  update.effective_chat.id,
        "thread_id":update.effective_message.message_thread_id,
        "msg_id":   None,
        "state":    "waiting",          # waiting → choosing → done
    }

    msg_id = await safe_send(
        context.bot,
        update.effective_chat.id,
        f"⚔️ <b>{name(user)} wants to play Rock Paper Scissors!</b>\n\n"
        f"Tap <b>Accept</b> within {CHALLENGE_TTL}s to play.\n"
        f"<i>({name(user)} cannot accept their own challenge.)</i>",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"pvp_accept_{cid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"pvp_cancel_{cid}"),
        ]]),
        thread_id=update.effective_message.message_thread_id,
    )

    if msg_id is None:
        pvp_games.pop(cid, None)
        return

    pvp_games[cid]["msg_id"] = msg_id

    context.job_queue.run_once(
        _job_expire_challenge, CHALLENGE_TTL,
        data={"cid": cid}, name=f"expire_{cid}",
    )
    logger.info("Challenge created: %s by %s", cid, name(user))

async def _job_expire_challenge(context: ContextTypes.DEFAULT_TYPE):
    cid  = context.job.data["cid"]
    game = pvp_games.pop(cid, None)
    if not game or game["state"] != "waiting":
        return
    logger.info("Challenge expired: %s", cid)
    await safe_edit(
        context.bot, game["chat_id"], game["msg_id"],
        f"⏰ <b>Challenge expired.</b>\n"
        f"{game['c_name']}'s challenge wasn't accepted in time.",
    )

# ── PvP: Accept ───────────────────────────────────────────────────────────────
async def cb_pvp_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user
    cid  = q.data[len("pvp_accept_"):]

    logger.info("pvp_accept triggered: cid=%s user=%s", cid, user.id)

    game = pvp_games.get(cid)

    # ── Guards ────────────────────────────────────────────────────────────────
    if game is None:
        await safe_answer(q, "⏰ This challenge has expired!", alert=True)
        return

    if game["state"] != "waiting":
        await safe_answer(q, "❌ Challenge already accepted by someone else!", alert=True)
        return

    if user.id == game["c_id"]:
        await safe_answer(q, "😅 You can't accept your own challenge!", alert=True)
        return

    # ── Accept ────────────────────────────────────────────────────────────────
    upsert_user(user)
    game["a_id"]   = user.id
    game["a_name"] = name(user)
    game["state"]  = "choosing"

    # Cancel expiry job
    for job in context.job_queue.get_jobs_by_name(f"expire_{cid}"):
        job.schedule_removal()

    logger.info("Challenge accepted: %s by %s", cid, name(user))

    # Answer the query FIRST (Telegram requires this within a few seconds)
    await safe_answer(q, "✅ Challenge accepted! Pick your move 👇")

    # Update the group message with pick buttons
    pick_text = (
        f"⚔️ <b>{game['c_name']} vs {game['a_name']}</b>\n\n"
        f"⏳ {game['c_name']} — hasn't picked yet\n"
        f"⏳ {game['a_name']} — hasn't picked yet\n\n"
        f"<b>Tap your move below!</b>\n"
        f"<i>Picks are secret until both players choose.</i>"
    )

    edited = await safe_edit(
        context.bot, game["chat_id"], game["msg_id"],
        pick_text, kb_pick(cid),
    )

    if not edited:
        # edit failed — send a fresh message and store its id
        new_id = await safe_send(
            context.bot, game["chat_id"], pick_text, kb_pick(cid),
            thread_id=game["thread_id"],
        )
        if new_id:
            game["msg_id"] = new_id
        else:
            logger.error("Could not send pick message for cid=%s — game aborted", cid)
            pvp_games.pop(cid, None)
            return

    # Start pick timeout
    context.job_queue.run_once(
        _job_expire_picks, PICK_TTL,
        data={"cid": cid}, name=f"picks_{cid}",
    )

async def _job_expire_picks(context: ContextTypes.DEFAULT_TYPE):
    cid  = context.job.data["cid"]
    game = pvp_games.pop(cid, None)
    if not game or game["state"] != "choosing":
        return
    who = []
    if not game["c_choice"]: who.append(game["c_name"])
    if not game["a_choice"]: who.append(game["a_name"])
    logger.info("Pick timeout: %s — didn't pick: %s", cid, who)
    await safe_edit(
        context.bot, game["chat_id"], game["msg_id"],
        f"⏰ <b>Time's up!</b>\n"
        f"{' &amp; '.join(who)} didn't pick in time.\n"
        f"Game cancelled.",
    )

# ── PvP: Cancel ───────────────────────────────────────────────────────────────
async def cb_pvp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user
    cid  = q.data[len("pvp_cancel_"):]
    game = pvp_games.get(cid)

    if game is None:
        await safe_answer(q, "Already expired.", alert=True)
        return
    if user.id != game["c_id"]:
        await safe_answer(q, "Only the challenger can cancel!", alert=True)
        return

    pvp_games.pop(cid, None)
    for job in context.job_queue.get_jobs_by_name(f"expire_{cid}"):
        job.schedule_removal()

    await safe_answer(q, "Challenge cancelled.")
    await safe_edit(
        context.bot, game["chat_id"], game["msg_id"],
        f"❌ <b>{game['c_name']} cancelled the challenge.</b>",
    )

# ── PvP: Pick move ────────────────────────────────────────────────────────────
async def cb_pvp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user

    # callback_data = "pvp_pick_{cid}_{choice}"
    # cid itself contains underscores → split from right once
    raw        = q.data[len("pvp_pick_"):]          # "{cid}_{choice}"
    choice_str = raw.rsplit("_", 1)[1]               # rock / paper / scissors
    cid        = raw.rsplit("_", 1)[0]               # everything before

    logger.info("pvp_pick: cid=%s user=%s choice=%s", cid, user.id, choice_str)

    game = pvp_games.get(cid)
    if not game or game["state"] != "choosing":
        await safe_answer(q, "⏰ This game has already ended!", alert=True)
        # Remove stale buttons
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    is_c = user.id == game["c_id"]
    is_a = user.id == game["a_id"]

    if not is_c and not is_a:
        await safe_answer(q, "❌ You're not part of this game!", alert=True)
        return

    chosen = CHOICE_FROM_STR.get(choice_str)
    if not chosen:
        return

    # Record pick (prevent double-pick)
    if is_c:
        if game["c_choice"]:
            await safe_answer(q, "✅ You already picked! Waiting for opponent…")
            return
        game["c_choice"] = chosen
    else:
        if game["a_choice"]:
            await safe_answer(q, "✅ You already picked! Waiting for opponent…")
            return
        game["a_choice"] = chosen

    # Confirm pick privately — only the tapper sees this toast
    await safe_answer(q, f"✅ You picked {chosen.value}! Waiting for opponent…")

    c_done = game["c_choice"] is not None
    a_done = game["a_choice"] is not None

    # ── Both picked → resolve now ─────────────────────────────────────────────
    if c_done and a_done:
        for job in context.job_queue.get_jobs_by_name(f"picks_{cid}"):
            job.schedule_removal()

        cc = game["c_choice"]
        ac = game["a_choice"]
        r  = beats(cc, ac)

        if r == 1:
            headline   = f"🏆 <b>{game['c_name']} wins!</b>"
            c_res, a_res = "win", "loss"
        elif r == -1:
            headline   = f"🏆 <b>{game['a_name']} wins!</b>"
            c_res, a_res = "loss", "win"
        else:
            headline   = "🤝 <b>It's a Draw!</b>"
            c_res, a_res = "draw", "draw"

        record_pvp(game["c_id"], game["a_id"], cc.name, ac.name, c_res)
        record_pvp(game["a_id"], game["c_id"], ac.name, cc.name, a_res)
        pvp_games.pop(cid, None)
        logger.info("PvP result: %s vs %s → %s", game["c_name"], game["a_name"], headline)

        await safe_edit(
            context.bot, game["chat_id"], game["msg_id"],
            f"⚔️ <b>{game['c_name']} vs {game['a_name']}</b>\n\n"
            f"{game['c_name']}: {cc.value}\n"
            f"{game['a_name']}: {ac.value}\n\n"
            f"{headline}",
            kb_after_pvp(),
        )
        return

    # ── Still waiting for one player ──────────────────────────────────────────
    c_line = f"✅ {game['c_name']} — locked in!" if c_done else f"⏳ {game['c_name']} — choosing…"
    a_line = f"✅ {game['a_name']} — locked in!" if a_done else f"⏳ {game['a_name']} — choosing…"

    await safe_edit(
        context.bot, game["chat_id"], game["msg_id"],
        f"⚔️ <b>{game['c_name']} vs {game['a_name']}</b>\n\n"
        f"{c_line}\n{a_line}\n\n"
        f"<i>Picks hidden until both choose.</i>",
        kb_pick(cid),
    )

# ── Stats / Leaderboard (text builders) ──────────────────────────────────────
def _fmt_stats(uname: str, s) -> str:
    if not s or (s["total_games"] == 0 and s["pvp_games"] == 0):
        return f"📊 <b>{uname}'s Stats</b>\n\nNo games yet! Play below 👇"
    bt  = s["total_games"]
    pt  = s["pvp_games"]
    bwr = s["wins"]     / bt * 100 if bt else 0
    pwr = s["pvp_wins"] / pt * 100 if pt else 0
    return (
        f"📊 <b>{uname}'s Stats</b>\n\n"
        f"🤖 <b>vs Bot</b> ({bt} games)\n"
        f"{s['wins']}W  {s['losses']}L  {s['draws']}D  —  {bwr:.1f}%\n\n"
        f"⚔️ <b>vs Players</b> ({pt} games)\n"
        f"{s['pvp_wins']}W  {s['pvp_losses']}L  {s['pvp_draws']}D  —  {pwr:.1f}%"
    )

def _fmt_lb() -> str:
    rows = db().execute(
        "SELECT * FROM users WHERE total_games>0 OR pvp_games>0 "
        "ORDER BY (wins+pvp_wins) DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return "🏆 <b>Leaderboard</b>\n\nNo players yet!"
    medals = ["🥇", "🥈", "🥉"]
    out = "🏆 <b>Top 10 Players</b>\n\n"
    for i, r in enumerate(rows, 1):
        r  = dict(r)
        tw = r["wins"] + r["pvp_wins"]
        tl = r["losses"] + r["pvp_losses"]
        td = r["draws"] + r["pvp_draws"]
        tg = r["total_games"] + r["pvp_games"]
        wr = tw / tg * 100 if tg else 0
        out += f"{medals[i-1] if i<=3 else f'{i}.'} <b>{name_db(r)}</b>\n   {tw}W {td}D {tl}L  ({wr:.0f}%)\n\n"
    return out

# ── Navigation callbacks ──────────────────────────────────────────────────────
async def cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = update.effective_user
    upsert_user(user)
    await safe_answer(q)
    text   = _fmt_stats(name(user), fetch_stats(user.id))
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play",        callback_data="main_menu"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
    ]])
    if update.effective_chat.type in ("group", "supergroup"):
        await safe_send(context.bot, update.effective_chat.id, text, markup,
                        thread_id=update.effective_message.message_thread_id)
    else:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError:
            pass

async def cb_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Play", callback_data="main_menu")]])
    if update.effective_chat.type in ("group", "supergroup"):
        await safe_send(context.bot, update.effective_chat.id, _fmt_lb(), markup,
                        thread_id=update.effective_message.message_thread_id)
    else:
        try:
            await q.edit_message_text(_fmt_lb(), parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError:
            pass

async def cb_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    text = (
        "📖 <b>How to Play</b>\n\n"
        "🪨 Rock beats ✂️ Scissors\n"
        "✂️ Scissors beats 📄 Paper\n"
        "📄 Paper beats 🪨 Rock\n\n"
        "<b>🤖 vs Bot:</b> Pick any move — bot picks randomly.\n\n"
        "<b>⚔️ vs Friend (group):</b>\n"
        "1. Tap ⚔️ Challenge a Friend\n"
        "2. A friend taps ✅ Accept (60 s window)\n"
        "3. Both tap 🪨 📄 or ✂️ on the same message\n"
        "4. Picks stay <b>secret</b> until both lock in\n"
        "5. Winner revealed automatically! 🎉\n\n"
        "<i>No DMs needed — everything happens right here.</i>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Back", callback_data="main_menu")]])
    await safe_answer(q)
    if update.effective_chat.type in ("group", "supergroup"):
        await safe_send(context.bot, update.effective_chat.id, text, markup,
                        thread_id=update.effective_message.message_thread_id)
    else:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError:
            pass

async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    try:
        await q.edit_message_text(
            "🎮 <b>Rock Paper Scissors</b>\n\nPick your move!",
            parse_mode=ParseMode.HTML, reply_markup=kb_main(),
        )
    except TelegramError:
        pass

# ── Error handler ─────────────────────────────────────────────────────────────
async def err_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)

# ── Bot setup ─────────────────────────────────────────────────────────────────
async def on_startup(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("start",       "Play the game 🎮"),
            BotCommand("stats",       "My stats 📊"),
            BotCommand("leaderboard", "Leaderboard 🏆"),
        ])
        logger.info("Commands registered.")
    except TelegramError as e:
        logger.warning("set_my_commands: %s", e)

def main():
    init_db()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN env var not set — exiting.")
        sys.exit(1)

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(on_startup)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

    # vs Bot
    app.add_handler(CallbackQueryHandler(cb_play,          pattern="^play_"))

    # PvP  (order matters — most specific patterns first)
    app.add_handler(CallbackQueryHandler(cb_pvp_challenge, pattern="^pvp_challenge$"))
    app.add_handler(CallbackQueryHandler(cb_pvp_accept,    pattern="^pvp_accept_"))
    app.add_handler(CallbackQueryHandler(cb_pvp_cancel,    pattern="^pvp_cancel_"))
    app.add_handler(CallbackQueryHandler(cb_pvp_pick,      pattern="^pvp_pick_"))

    # Navigation
    app.add_handler(CallbackQueryHandler(cb_stats,       pattern="^show_stats$"))
    app.add_handler(CallbackQueryHandler(cb_leaderboard, pattern="^show_leaderboard$"))
    app.add_handler(CallbackQueryHandler(cb_rules,       pattern="^rules$"))
    app.add_handler(CallbackQueryHandler(cb_main_menu,   pattern="^main_menu$"))

    app.add_error_handler(err_handler)

    logger.info("Bot polling…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
