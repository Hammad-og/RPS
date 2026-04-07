"""
Rock, Paper, Scissors Telegram Bot
PvP: Both players pick on the GROUP message — choices hidden until both lock in.
No DM required. Railway-safe.
"""

import logging
import random
import sqlite3
import os
import sys
import time
from enum import Enum
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── Enums ─────────────────────────────────────────────────────────────────────
class Choice(Enum):
    ROCK     = "🪨 Rock"
    PAPER    = "📄 Paper"
    SCISSORS = "✂️ Scissors"

# ── PvP game store ────────────────────────────────────────────────────────────
pvp_games: dict = {}
CHALLENGE_TIMEOUT = 60
CHOICE_TIMEOUT    = 120

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "rps_stats.db")

def init_database():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id      INTEGER PRIMARY KEY,
        username     TEXT,
        first_name   TEXT,
        last_name    TEXT,
        wins         INTEGER DEFAULT 0,
        losses       INTEGER DEFAULT 0,
        draws        INTEGER DEFAULT 0,
        total_games  INTEGER DEFAULT 0,
        pvp_wins     INTEGER DEFAULT 0,
        pvp_losses   INTEGER DEFAULT 0,
        pvp_draws    INTEGER DEFAULT 0,
        pvp_games    INTEGER DEFAULT 0,
        last_played  TIMESTAMP,
        joined_date  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS game_history (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER,
        opponent_id INTEGER,
        user_choice TEXT,
        bot_choice  TEXT,
        result      TEXT,
        game_type   TEXT DEFAULT 'bot',
        played_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )''')
    for col, default in [("pvp_wins","0"),("pvp_losses","0"),("pvp_draws","0"),("pvp_games","0"),("last_name","''")]:
        try:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT {default}")
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE game_history ADD COLUMN game_type TEXT DEFAULT 'bot'")
    except Exception:
        pass
    conn.commit()
    conn.close()
    logger.info("DB ready: %s", DB_PATH)

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ── Helpers ───────────────────────────────────────────────────────────────────
def display_name(user) -> str:
    if user.first_name:
        return (user.first_name + (" " + user.last_name if user.last_name else "")).strip()
    return f"@{user.username}" if user.username else "Player"

def display_name_db(row) -> str:
    full = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
    return full or (f"@{row['username']}" if row.get('username') else "Player")

def ensure_user(user):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    if c.fetchone():
        c.execute("UPDATE users SET username=?,first_name=?,last_name=? WHERE user_id=?",
                  (user.username or "", user.first_name or "", user.last_name or "", user.id))
    else:
        c.execute("INSERT INTO users(user_id,username,first_name,last_name) VALUES(?,?,?,?)",
                  (user.id, user.username or "", user.first_name or "", user.last_name or ""))
    conn.commit()
    conn.close()

def get_stats(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def save_bot_game(user_id, user_choice, bot_choice, result):
    conn = get_db()
    c = conn.cursor()
    col = {'win':'wins','loss':'losses','draw':'draws'}[result]
    c.execute(f"UPDATE users SET {col}={col}+1, total_games=total_games+1, last_played=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO game_history(user_id,opponent_id,user_choice,bot_choice,result,game_type) VALUES(?,?,?,?,?,?)",
              (user_id, 0, user_choice, bot_choice, result, 'bot'))
    conn.commit()
    conn.close()

def save_pvp_game(user_id, opp_id, user_choice, opp_choice, result):
    conn = get_db()
    c = conn.cursor()
    col = {'win':'pvp_wins','loss':'pvp_losses','draw':'pvp_draws'}[result]
    c.execute(f"UPDATE users SET {col}={col}+1, pvp_games=pvp_games+1, last_played=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))
    c.execute("INSERT INTO game_history(user_id,opponent_id,user_choice,bot_choice,result,game_type) VALUES(?,?,?,?,?,?)",
              (user_id, opp_id, user_choice, opp_choice, result, 'pvp'))
    conn.commit()
    conn.close()

def beats(c1: Choice, c2: Choice) -> int:
    if c1 == c2: return 0
    return 1 if (c1,c2) in {(Choice.ROCK,Choice.SCISSORS),(Choice.SCISSORS,Choice.PAPER),(Choice.PAPER,Choice.ROCK)} else -1

# ── Keyboard builders ─────────────────────────────────────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪨 Rock",     callback_data="play_rock"),
         InlineKeyboardButton("📄 Paper",    callback_data="play_paper"),
         InlineKeyboardButton("✂️ Scissors", callback_data="play_scissors")],
        [InlineKeyboardButton("⚔️ Challenge a Friend", callback_data="pvp_challenge")],
        [InlineKeyboardButton("📊 My Stats",    callback_data="show_stats"),
         InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard")],
        [InlineKeyboardButton("ℹ️ Rules", callback_data="rules")],
    ])

def rematch_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play Again", callback_data="main_menu"),
        InlineKeyboardButton("📊 My Stats",   callback_data="show_stats"),
    ]])

def pvp_pick_kb(cid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🪨", callback_data=f"pvp_pick_{cid}_rock"),
        InlineKeyboardButton("📄", callback_data=f"pvp_pick_{cid}_paper"),
        InlineKeyboardButton("✂️", callback_data=f"pvp_pick_{cid}_scissors"),
    ]])

def pvp_newgame_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play vs Bot",   callback_data="main_menu"),
        InlineKeyboardButton("⚔️ New Challenge", callback_data="pvp_challenge"),
    ]])

# ── send helpers ──────────────────────────────────────────────────────────────
async def answer_query(query, text="", alert=False):
    try:
        await query.answer(text, show_alert=alert)
    except TelegramError:
        pass

async def edit_or_send(context, chat_id, message_id, text, markup=None, thread_id=None):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
        )
        return message_id
    except TelegramError:
        try:
            m = await context.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup,
                message_thread_id=thread_id,
            )
            return m.message_id
        except TelegramError as e:
            logger.warning("edit_or_send failed: %s", e)
            return message_id

async def reply_or_send(update, context, text, markup=None):
    """In groups send new message; in DMs edit existing."""
    q = update.callback_query
    await answer_query(q)
    if update.effective_chat.type in ("group", "supergroup"):
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text,
                parse_mode=ParseMode.HTML, reply_markup=markup,
                message_thread_id=update.effective_message.message_thread_id,
            )
        except TelegramError as e:
            logger.warning("reply_or_send group: %s", e)
    else:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError:
            pass

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    await update.message.reply_text(
        f"🎮 <b>Rock, Paper, Scissors!</b>\n\nHi {display_name(user)}! 👋\n\n"
        f"🤖 Play vs Bot\n"
        f"⚔️ Challenge a friend — pick secretly, reveal together\n"
        f"📊 Stats &amp; 🏆 Leaderboard\n\n"
        f"<i>Pick a move below!</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(),
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    await update.message.reply_text(
        _stats_text(display_name(user), get_stats(user.id)),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎮 Play", callback_data="main_menu"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
        ]]),
    )

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        _lb_text(), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Play", callback_data="main_menu")]]),
    )

# ── vs Bot ────────────────────────────────────────────────────────────────────
async def play_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user
    ensure_user(user)

    choice_map = {"play_rock": Choice.ROCK, "play_paper": Choice.PAPER, "play_scissors": Choice.SCISSORS}
    user_choice = choice_map.get(query.data)
    if not user_choice:
        return

    bot_choice  = random.choice(list(Choice))
    r           = beats(user_choice, bot_choice)
    result_type = {1:"win", -1:"loss", 0:"draw"}[r]
    emoji       = {"win":"✨","loss":"💔","draw":"⚖️"}[result_type]
    headline    = {"win":"🎉 <b>YOU WIN!</b>","loss":"😔 <b>YOU LOSE!</b>","draw":"🤝 <b>DRAW!</b>"}[result_type]

    save_bot_game(user.id, user_choice.name, bot_choice.name, result_type)
    stats = get_stats(user.id)
    tot   = stats['total_games']
    wr    = stats['wins'] / tot * 100 if tot else 0

    text = (
        f"{emoji} <b>{display_name(user)}</b> — {headline}\n\n"
        f"<b>You:</b> {user_choice.value}   <b>Bot:</b> {bot_choice.value}\n\n"
        f"<b>📊 Bot record:</b> {stats['wins']}W {stats['losses']}L {stats['draws']}D — {wr:.1f}%"
    )
    await reply_or_send(update, context, text, rematch_kb())

# ── PvP: Challenge ────────────────────────────────────────────────────────────
async def pvp_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user
    name  = display_name(user)
    ensure_user(user)
    await answer_query(query)

    cid = f"{user.id}_{int(time.time())}"
    pvp_games[cid] = {
        "cid":      cid,
        "c_id":     user.id,
        "c_name":   name,
        "a_id":     None,
        "a_name":   None,
        "c_choice": None,
        "a_choice": None,
        "chat_id":  update.effective_chat.id,
        "thread_id":update.effective_message.message_thread_id,
        "msg_id":   None,
        "state":    "waiting",
    }

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"pvp_accept_{cid}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"pvp_cancel_{cid}"),
    ]])

    try:
        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚔️ <b>{name} challenges you to Rock Paper Scissors!</b>\n\n"
                f"Tap <b>Accept</b> within 60 s.\n"
                f"<i>({name} cannot accept their own challenge.)</i>"
            ),
            parse_mode=ParseMode.HTML, reply_markup=markup,
            message_thread_id=update.effective_message.message_thread_id,
        )
        pvp_games[cid]["msg_id"] = sent.message_id
    except TelegramError as e:
        logger.warning("pvp_challenge send: %s", e)
        pvp_games.pop(cid, None)
        return

    context.job_queue.run_once(
        _expire_challenge, CHALLENGE_TIMEOUT,
        data={"cid": cid}, name=f"expire_{cid}",
    )

async def _expire_challenge(context: ContextTypes.DEFAULT_TYPE):
    cid  = context.job.data["cid"]
    game = pvp_games.pop(cid, None)
    if not game or game["state"] != "waiting":
        return
    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"], message_id=game["msg_id"],
            text=f"⏰ <b>Challenge expired.</b>\n{game['c_name']}'s challenge wasn't accepted in time.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

# ── PvP: Accept ───────────────────────────────────────────────────────────────
async def pvp_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user
    cid   = query.data[len("pvp_accept_"):]
    game  = pvp_games.get(cid)

    if not game:
        return await answer_query(query, "⏰ Challenge expired!", alert=True)
    if game["state"] != "waiting":
        return await answer_query(query, "❌ Already accepted!", alert=True)
    if user.id == game["c_id"]:
        return await answer_query(query, "😅 You can't accept your own challenge!", alert=True)

    ensure_user(user)
    game["a_id"]   = user.id
    game["a_name"] = display_name(user)
    game["state"]  = "choosing"

    for job in context.job_queue.get_jobs_by_name(f"expire_{cid}"):
        job.schedule_removal()

    await answer_query(query, "✅ Accepted! Tap your move below 👇")

    await edit_or_send(
        context, game["chat_id"], game["msg_id"],
        text=(
            f"⚔️ <b>{game['c_name']} vs {game['a_name']}</b>\n\n"
            f"⏳ {game['c_name']} — not picked yet\n"
            f"⏳ {game['a_name']} — not picked yet\n\n"
            f"<b>Both players: tap your move below!</b>\n"
            f"<i>Your pick is hidden until both have chosen.</i>"
        ),
        markup=pvp_pick_kb(cid),
        thread_id=game["thread_id"],
    )

    context.job_queue.run_once(
        _expire_choice, CHOICE_TIMEOUT,
        data={"cid": cid}, name=f"choice_{cid}",
    )

async def _expire_choice(context: ContextTypes.DEFAULT_TYPE):
    cid  = context.job.data["cid"]
    game = pvp_games.pop(cid, None)
    if not game or game["state"] != "choosing":
        return
    who = []
    if not game["c_choice"]: who.append(game["c_name"])
    if not game["a_choice"]: who.append(game["a_name"])
    didnt = " &amp; ".join(who) if who else "Someone"
    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"], message_id=game["msg_id"],
            text=f"⏰ <b>Game timed out!</b>\n{didnt} didn't pick in time.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

# ── PvP: Cancel ───────────────────────────────────────────────────────────────
async def pvp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user
    cid   = query.data[len("pvp_cancel_"):]
    game  = pvp_games.get(cid)

    if not game:
        return await answer_query(query, "Already expired.", alert=True)
    if user.id != game["c_id"]:
        return await answer_query(query, "Only the challenger can cancel!", alert=True)

    pvp_games.pop(cid, None)
    for job in context.job_queue.get_jobs_by_name(f"expire_{cid}"):
        job.schedule_removal()

    await answer_query(query, "Cancelled.")
    try:
        await query.edit_message_text(
            f"❌ <b>{game['c_name']} cancelled the challenge.</b>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

# ── PvP: Pick ─────────────────────────────────────────────────────────────────
async def pvp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user  = update.effective_user

    # data = "pvp_pick_{cid}_{choice}"  — cid itself may contain underscores
    raw        = query.data[len("pvp_pick_"):]
    choice_str = raw.rsplit("_", 1)[1]   # last segment = rock/paper/scissors
    cid        = raw.rsplit("_", 1)[0]   # everything before = challenge id

    game = pvp_games.get(cid)

    if not game or game["state"] != "choosing":
        await answer_query(query, "⏰ This game has already ended!", alert=True)
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass
        return

    is_c = user.id == game["c_id"]
    is_a = user.id == game["a_id"]

    if not is_c and not is_a:
        return await answer_query(query, "❌ You're not in this game!", alert=True)

    chosen = {"rock": Choice.ROCK, "paper": Choice.PAPER, "scissors": Choice.SCISSORS}[choice_str]

    if is_c:
        if game["c_choice"]:
            return await answer_query(query, "✅ Already picked! Waiting for opponent…")
        game["c_choice"] = chosen
    else:
        if game["a_choice"]:
            return await answer_query(query, "✅ Already picked! Waiting for opponent…")
        game["a_choice"] = chosen

    # Private confirmation toast — only the button-tapper sees this
    await answer_query(query, f"✅ Locked in {chosen.value}! Waiting for opponent…")

    c_done = game["c_choice"] is not None
    a_done = game["a_choice"] is not None

    # ── Both picked → resolve ─────────────────────────────────────────────────
    if c_done and a_done:
        for job in context.job_queue.get_jobs_by_name(f"choice_{cid}"):
            job.schedule_removal()

        c_ch = game["c_choice"]
        a_ch = game["a_choice"]
        r    = beats(c_ch, a_ch)

        if r == 1:
            headline = f"🏆 <b>{game['c_name']} wins!</b>"
            save_pvp_game(game["c_id"], game["a_id"], c_ch.name, a_ch.name, "win")
            save_pvp_game(game["a_id"], game["c_id"], a_ch.name, c_ch.name, "loss")
        elif r == -1:
            headline = f"🏆 <b>{game['a_name']} wins!</b>"
            save_pvp_game(game["c_id"], game["a_id"], c_ch.name, a_ch.name, "loss")
            save_pvp_game(game["a_id"], game["c_id"], a_ch.name, c_ch.name, "win")
        else:
            headline = "🤝 <b>It's a Draw!</b>"
            save_pvp_game(game["c_id"], game["a_id"], c_ch.name, a_ch.name, "draw")
            save_pvp_game(game["a_id"], game["c_id"], a_ch.name, c_ch.name, "draw")

        pvp_games.pop(cid, None)

        await edit_or_send(
            context, game["chat_id"], game["msg_id"],
            text=(
                f"⚔️ <b>{game['c_name']} vs {game['a_name']}</b>\n\n"
                f"{game['c_name']}: {c_ch.value}\n"
                f"{game['a_name']}: {a_ch.value}\n\n"
                f"{headline}"
            ),
            markup=pvp_newgame_kb(),
            thread_id=game["thread_id"],
        )
        return

    # ── One picked, waiting for other ────────────────────────────────────────
    c_status = f"✅ {game['c_name']} — ready!" if c_done else f"⏳ {game['c_name']} — choosing…"
    a_status = f"✅ {game['a_name']} — ready!" if a_done else f"⏳ {game['a_name']} — choosing…"

    await edit_or_send(
        context, game["chat_id"], game["msg_id"],
        text=(
            f"⚔️ <b>{game['c_name']} vs {game['a_name']}</b>\n\n"
            f"{c_status}\n{a_status}\n\n"
            f"<i>Choices are hidden until both lock in.</i>"
        ),
        markup=pvp_pick_kb(cid),
        thread_id=game["thread_id"],
    )

# ── Stats / Leaderboard builders ──────────────────────────────────────────────
def _stats_text(name: str, stats) -> str:
    if not stats or (stats['total_games'] == 0 and stats['pvp_games'] == 0):
        return f"📊 <b>{name}'s Stats</b>\n\nNo games yet! Hit Play 👇"
    bt, pt = stats['total_games'], stats['pvp_games']
    bwr = stats['wins']     / bt * 100 if bt else 0
    pwr = stats['pvp_wins'] / pt * 100 if pt else 0
    return (
        f"📊 <b>{name}'s Stats</b>\n\n"
        f"🤖 <b>vs Bot</b> ({bt} games)\n"
        f"{stats['wins']}W {stats['losses']}L {stats['draws']}D — {bwr:.1f}%\n\n"
        f"⚔️ <b>vs Players</b> ({pt} games)\n"
        f"{stats['pvp_wins']}W {stats['pvp_losses']}L {stats['pvp_draws']}D — {pwr:.1f}%"
    )

def _lb_text() -> str:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM users WHERE total_games>0 OR pvp_games>0 ORDER BY (wins+pvp_wins) DESC LIMIT 10"
    ).fetchall()
    conn.close()
    if not rows:
        return "🏆 <b>Leaderboard</b>\n\nNo players yet!"
    out    = "🏆 <b>Top 10 Players</b>\n\n"
    medals = ["🥇","🥈","🥉"]
    for i, u in enumerate(rows, 1):
        u  = dict(u)
        m  = medals[i-1] if i <= 3 else f"{i}."
        tw = u['wins'] + u['pvp_wins']
        tl = u['losses'] + u['pvp_losses']
        td = u['draws'] + u['pvp_draws']
        tg = u['total_games'] + u['pvp_games']
        wr = tw / tg * 100 if tg else 0
        out += f"{m} <b>{display_name_db(u)}</b>\n   {tw}W {td}D {tl}L ({wr:.0f}%)\n\n"
    return out

# ── Callback handlers: stats / leaderboard / rules / menu ────────────────────
async def cb_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play", callback_data="main_menu"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
    ]])
    await reply_or_send(update, context, _stats_text(display_name(user), get_stats(user.id)), markup)

async def cb_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Play", callback_data="main_menu")]])
    await reply_or_send(update, context, _lb_text(), markup)

async def cb_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>How to Play</b>\n\n"
        "🪨 Rock beats ✂️ Scissors\n"
        "✂️ Scissors beats 📄 Paper\n"
        "📄 Paper beats 🪨 Rock\n\n"
        "<b>🤖 vs Bot:</b> Pick a move — bot picks randomly.\n\n"
        "<b>⚔️ vs Friend:</b>\n"
        "1. Tap ⚔️ Challenge a Friend\n"
        "2. Friend taps Accept (within 60 s)\n"
        "3. Both tap your move on the same message\n"
        "4. Picks stay secret until both lock in\n"
        "5. Result revealed automatically! 🎉\n\n"
        "<i>No DMs needed — works entirely in chat.</i>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Back", callback_data="main_menu")]])
    await reply_or_send(update, context, text, markup)

async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await answer_query(query)
    try:
        await query.edit_message_text(
            "🎮 <b>Rock, Paper, Scissors</b>\n\nPick your move!",
            parse_mode=ParseMode.HTML, reply_markup=main_kb(),
        )
    except TelegramError:
        pass

# ── Error handler ─────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Error: %s", context.error, exc_info=context.error)

# ── post_init ─────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start",       "Start the bot 🎮"),
            BotCommand("stats",       "My statistics 📊"),
            BotCommand("leaderboard", "Global leaderboard 🏆"),
        ])
    except TelegramError as e:
        logger.warning("set_my_commands: %s", e)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    init_database()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN not set!")
        sys.exit(1)

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

    app.add_handler(CallbackQueryHandler(play_single,    pattern="^play_"))
    app.add_handler(CallbackQueryHandler(pvp_challenge,  pattern="^pvp_challenge$"))
    app.add_handler(CallbackQueryHandler(pvp_accept,     pattern="^pvp_accept_"))
    app.add_handler(CallbackQueryHandler(pvp_cancel,     pattern="^pvp_cancel_"))
    app.add_handler(CallbackQueryHandler(pvp_pick,       pattern="^pvp_pick_"))
    app.add_handler(CallbackQueryHandler(cb_stats,       pattern="^show_stats$"))
    app.add_handler(CallbackQueryHandler(cb_leaderboard, pattern="^show_leaderboard$"))
    app.add_handler(CallbackQueryHandler(cb_rules,       pattern="^rules$"))
    app.add_handler(CallbackQueryHandler(cb_main_menu,   pattern="^main_menu$"))

    app.add_error_handler(error_handler)

    logger.info("Bot started.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
