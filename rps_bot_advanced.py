"""
Rock, Paper, Scissors Telegram Bot
Features: vs Bot, PvP Challenge (private DM picks), Stats, Leaderboard, Groups & Topics
Railway-compatible deployment
"""

import logging
import random
import sqlite3
import os
import sys
import time
import asyncio
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
    handlers=[
        logging.StreamHandler(sys.stdout)   # stdout only — Railway streams logs
    ]
)
logger = logging.getLogger(__name__)

# ── Enums ─────────────────────────────────────────────────────────────────────
class Choice(Enum):
    ROCK     = "🪨 Rock"
    PAPER    = "📄 Paper"
    SCISSORS = "✂️ Scissors"

# ── In-memory PvP game store ──────────────────────────────────────────────────
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
    # Safe migration for old DBs
    for col, default in [
        ("pvp_wins",   "0"),
        ("pvp_losses", "0"),
        ("pvp_draws",  "0"),
        ("pvp_games",  "0"),
        ("last_name",  "''"),
    ]:
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
    logger.info("Database initialized at %s", DB_PATH)

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ── Name helpers ──────────────────────────────────────────────────────────────
def get_display_name(user) -> str:
    if user.first_name:
        full = user.first_name
        if user.last_name:
            full += f" {user.last_name}"
        return full
    if user.username:
        return f"@{user.username}"
    return "Player"

def get_display_name_from_db(row: dict) -> str:
    first = row.get('first_name') or ''
    last  = row.get('last_name')  or ''
    full  = f"{first} {last}".strip()
    if full:
        return full
    if row.get('username'):
        return f"@{row['username']}"
    return "Player"

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_or_create_user(user_id: int, username: str, first_name: str, last_name: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    if not c.fetchone():
        c.execute(
            'INSERT INTO users (user_id, username, first_name, last_name) VALUES (?,?,?,?)',
            (user_id, username, first_name, last_name)
        )
    else:
        c.execute(
            'UPDATE users SET username=?, first_name=?, last_name=? WHERE user_id=?',
            (username, first_name, last_name, user_id)
        )
    conn.commit()
    conn.close()

def get_user_stats(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def update_bot_game(user_id: int, user_choice: str, bot_choice: str, result: str):
    conn = get_db_connection()
    c = conn.cursor()
    col = {'win': 'wins', 'loss': 'losses', 'draw': 'draws'}[result]
    c.execute(
        f'UPDATE users SET {col}={col}+1, total_games=total_games+1, last_played=CURRENT_TIMESTAMP WHERE user_id=?',
        (user_id,)
    )
    c.execute(
        'INSERT INTO game_history (user_id, opponent_id, user_choice, bot_choice, result, game_type) VALUES (?,?,?,?,?,?)',
        (user_id, 0, user_choice, bot_choice, result, 'bot')
    )
    conn.commit()
    conn.close()

def update_pvp_game(user_id: int, opponent_id: int, user_choice: str, opp_choice: str, result: str):
    conn = get_db_connection()
    c = conn.cursor()
    col = {'win': 'pvp_wins', 'loss': 'pvp_losses', 'draw': 'pvp_draws'}[result]
    c.execute(
        f'UPDATE users SET {col}={col}+1, pvp_games=pvp_games+1, last_played=CURRENT_TIMESTAMP WHERE user_id=?',
        (user_id,)
    )
    c.execute(
        'INSERT INTO game_history (user_id, opponent_id, user_choice, bot_choice, result, game_type) VALUES (?,?,?,?,?,?)',
        (user_id, opponent_id, user_choice, opp_choice, result, 'pvp')
    )
    conn.commit()
    conn.close()

# ── Game logic ────────────────────────────────────────────────────────────────
def get_winner(c1: Choice, c2: Choice) -> int:
    if c1 == c2:
        return 0
    beats = {(Choice.ROCK, Choice.SCISSORS), (Choice.SCISSORS, Choice.PAPER), (Choice.PAPER, Choice.ROCK)}
    return 1 if (c1, c2) in beats else -1

# ── Buttons ───────────────────────────────────────────────────────────────────
def create_game_buttons() -> InlineKeyboardMarkup:
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

def create_rematch_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play Again",  callback_data="main_menu"),
        InlineKeyboardButton("📊 My Stats",    callback_data="show_stats"),
    ]])

def pvp_private_choice_buttons(challenge_id: str) -> InlineKeyboardMarkup:
    """Buttons sent in DM for private move selection."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🪨 Rock",     callback_data=f"pvp_pick_{challenge_id}_rock"),
        InlineKeyboardButton("📄 Paper",    callback_data=f"pvp_pick_{challenge_id}_paper"),
        InlineKeyboardButton("✂️ Scissors", callback_data=f"pvp_pick_{challenge_id}_scissors"),
    ]])

# ── Chat type helper ──────────────────────────────────────────────────────────
def is_group_chat(update: Update) -> bool:
    t = update.effective_chat.type if update.effective_chat else None
    return t in ("group", "supergroup")

async def send_private_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup,
):
    """In groups: send a new message. In DMs: edit the existing one."""
    query = update.callback_query
    if is_group_chat(update):
        try:
            await query.answer()
        except TelegramError:
            pass
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                message_thread_id=update.effective_message.message_thread_id,
            )
        except TelegramError as e:
            logger.warning("send_private_result group error: %s", e)
    else:
        try:
            await query.answer()
        except TelegramError:
            pass
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except TelegramError:
            pass

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", user.first_name or "", user.last_name or "")

    await update.message.reply_text(
        f"🎮 <b>Welcome to Rock, Paper, Scissors!</b>\n\n"
        f"Hi {display}! 👋\n\n"
        f"<b>✨ Features:</b>\n"
        f"🤖 Play against the bot\n"
        f"⚔️ Challenge a friend — each picks privately via DM\n"
        f"📊 Separate bot & PvP stats\n"
        f"🏆 Global leaderboard\n"
        f"✅ Works in group topics\n\n"
        f"<i>Let's play!</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=create_game_buttons(),
    )

# ── /stats command ────────────────────────────────────────────────────────────
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user    = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    stats = get_user_stats(user.id)
    text  = _build_stats_text(display, stats)
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play",        callback_data="main_menu"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
    ]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=buttons)

# ── /leaderboard command ──────────────────────────────────────────────────────
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text    = _build_leaderboard_text()
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Play", callback_data="main_menu")]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=buttons)

# ── vs Bot ────────────────────────────────────────────────────────────────────
async def play_single(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user    = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", user.first_name or "", user.last_name or "")

    choice_map = {
        "play_rock":     Choice.ROCK,
        "play_paper":    Choice.PAPER,
        "play_scissors": Choice.SCISSORS,
    }
    user_choice = choice_map.get(query.data)
    if not user_choice:
        return

    bot_choice = random.choice(list(Choice))
    result     = get_winner(user_choice, bot_choice)

    if result == 1:
        result_text, result_emoji, result_type = "🎉 <b>YOU WIN!</b>", "✨", "win"
    elif result == -1:
        result_text, result_emoji, result_type = "😔 <b>YOU LOSE!</b>", "💔", "loss"
    else:
        result_text, result_emoji, result_type = "🤝 <b>DRAW!</b>", "⚖️", "draw"

    update_bot_game(user.id, user_choice.name, bot_choice.name, result_type)
    stats    = get_user_stats(user.id)
    total    = stats['total_games']
    win_rate = (stats['wins'] / total * 100) if total > 0 else 0

    text = (
        f"{result_emoji} <b>{display}</b> — {result_text}\n\n"
        f"<b>Your choice:</b> {user_choice.value}\n"
        f"<b>Bot choice:</b> {bot_choice.value}\n\n"
        f"<b>📊 Bot Stats:</b>\n"
        f"🎉 Wins: {stats['wins']}  😔 Losses: {stats['losses']}  🤝 Draws: {stats['draws']}\n"
        f"📈 Win Rate: {win_rate:.1f}%"
    )
    await send_private_result(update, context, text, create_rematch_buttons())

# ── PvP: Issue challenge ───────────────────────────────────────────────────────
async def pvp_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user    = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", user.first_name or "", user.last_name or "")

    try:
        await query.answer()
    except TelegramError:
        pass

    challenge_id = f"{user.id}_{int(time.time())}"

    pvp_games[challenge_id] = {
        "challenger_id":     user.id,
        "challenger_name":   display,
        "acceptor_id":       None,
        "acceptor_name":     None,
        "challenger_choice": None,
        "acceptor_choice":   None,
        "created_at":        time.time(),
        "chat_id":           update.effective_chat.id,
        "thread_id":         update.effective_message.message_thread_id,
        "message_id":        None,
        # DM message IDs so we can update "waiting…" → "you picked ✅"
        "challenger_dm_msg": None,
        "acceptor_dm_msg":   None,
        "state":             "waiting",
    }

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept Challenge", callback_data=f"pvp_accept_{challenge_id}"),
        InlineKeyboardButton("❌ Cancel",            callback_data=f"pvp_cancel_{challenge_id}"),
    ]])

    try:
        sent = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚔️ <b>{display} wants to play Rock Paper Scissors!</b>\n\n"
                f"Tap <b>Accept Challenge</b> within 60 seconds!\n\n"
                f"<i>Both players will choose their move privately via DM.</i>\n"
                f"<i>{display} cannot accept their own challenge.</i>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            message_thread_id=update.effective_message.message_thread_id,
        )
        pvp_games[challenge_id]["message_id"] = sent.message_id
    except TelegramError as e:
        logger.warning("pvp_challenge send error: %s", e)
        pvp_games.pop(challenge_id, None)
        return

    context.job_queue.run_once(
        expire_challenge,
        CHALLENGE_TIMEOUT,
        data={"challenge_id": challenge_id},
        name=f"expire_{challenge_id}",
    )


async def _send_dm_pick(context: ContextTypes.DEFAULT_TYPE, user_id: int,
                         challenge_id: str, opponent_name: str) -> int | None:
    """
    Send a private DM to a player asking them to pick their move.
    Returns the message_id of the sent DM, or None on failure.
    """
    try:
        msg = await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"⚔️ <b>You're in a Rock Paper Scissors match vs {opponent_name}!</b>\n\n"
                f"Choose your move — your opponent won't see it until both have picked:\n"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=pvp_private_choice_buttons(challenge_id),
        )
        return msg.message_id
    except TelegramError as e:
        logger.warning("Could not DM user %s: %s", user_id, e)
        return None


async def expire_challenge(context: ContextTypes.DEFAULT_TYPE) -> None:
    challenge_id = context.job.data["challenge_id"]
    game         = pvp_games.get(challenge_id)
    if not game or game["state"] != "waiting":
        return
    pvp_games.pop(challenge_id, None)
    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=(
                f"⏰ <b>Challenge Expired!</b>\n\n"
                f"{game['challenger_name']}'s challenge was not accepted in time."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

# ── PvP: Accept challenge ─────────────────────────────────────────────────────
async def pvp_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query        = update.callback_query
    user         = update.effective_user
    display      = get_display_name(user)
    challenge_id = query.data.replace("pvp_accept_", "")
    game         = pvp_games.get(challenge_id)

    if not game:
        try:
            await query.answer("⏰ This challenge has expired!", show_alert=True)
        except TelegramError:
            pass
        return

    if game["state"] != "waiting":
        try:
            await query.answer("❌ Challenge already accepted!", show_alert=True)
        except TelegramError:
            pass
        return

    if user.id == game["challenger_id"]:
        try:
            await query.answer("😅 You can't accept your own challenge!", show_alert=True)
        except TelegramError:
            pass
        return

    get_or_create_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    game["acceptor_id"]   = user.id
    game["acceptor_name"] = display
    game["state"]         = "choosing"

    # Cancel the expiry job
    for job in context.job_queue.get_jobs_by_name(f"expire_{challenge_id}"):
        job.schedule_removal()

    # Update group message to show the match is on
    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=(
                f"⚔️ <b>{game['challenger_name']} vs {display}</b>\n\n"
                f"⏳ {game['challenger_name']} — choosing…\n"
                f"⏳ {display} — choosing…\n\n"
                f"<i>Both players are picking their moves privately!</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("pvp_accept group edit error: %s", e)

    try:
        await query.answer("✅ Challenge accepted! Check your DMs to pick your move.")
    except TelegramError:
        pass

    # Send DM pick buttons to BOTH players
    c_dm = await _send_dm_pick(context, game["challenger_id"], challenge_id, display)
    a_dm = await _send_dm_pick(context, user.id, challenge_id, game["challenger_name"])

    game["challenger_dm_msg"] = c_dm
    game["acceptor_dm_msg"]   = a_dm

    # If we can't DM either player, warn them in the group
    failed = []
    if c_dm is None:
        failed.append(game["challenger_name"])
    if a_dm is None:
        failed.append(display)

    if failed:
        names = " and ".join(failed)
        try:
            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    f"⚠️ <b>Couldn't send DM to {names}.</b>\n\n"
                    f"Please start the bot first: tap <b>Start</b> in a private chat with @{context.bot.username}, "
                    f"then try accepting the challenge again."
                ),
                parse_mode=ParseMode.HTML,
                message_thread_id=game["thread_id"],
            )
        except TelegramError:
            pass
        # Clean up if a DM failed
        pvp_games.pop(challenge_id, None)
        return

    context.job_queue.run_once(
        expire_choice,
        CHOICE_TIMEOUT,
        data={"challenge_id": challenge_id},
        name=f"choice_{challenge_id}",
    )


async def expire_choice(context: ContextTypes.DEFAULT_TYPE) -> None:
    challenge_id = context.job.data["challenge_id"]
    game         = pvp_games.get(challenge_id)
    if not game or game["state"] != "choosing":
        return
    pvp_games.pop(challenge_id, None)

    # Update the group message
    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=(
                f"⚔️ <b>{game['challenger_name']} vs {game['acceptor_name']}</b>\n\n"
                f"⏰ <b>Game timed out!</b>\n"
                f"Not both players picked in time."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

    # Tell each player via DM
    for uid, dm_msg_id in [
        (game["challenger_id"], game["challenger_dm_msg"]),
        (game["acceptor_id"],   game["acceptor_dm_msg"]),
    ]:
        if dm_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=uid,
                    message_id=dm_msg_id,
                    text="⏰ <b>Game timed out!</b>\n\nYou or your opponent didn't pick in time.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass

# ── PvP: Cancel challenge ─────────────────────────────────────────────────────
async def pvp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query        = update.callback_query
    user         = update.effective_user
    challenge_id = query.data.replace("pvp_cancel_", "")
    game         = pvp_games.get(challenge_id)

    if not game:
        try:
            await query.answer("Already expired.", show_alert=True)
        except TelegramError:
            pass
        return

    if user.id != game["challenger_id"]:
        try:
            await query.answer("Only the challenger can cancel!", show_alert=True)
        except TelegramError:
            pass
        return

    pvp_games.pop(challenge_id, None)
    for job in context.job_queue.get_jobs_by_name(f"expire_{challenge_id}"):
        job.schedule_removal()

    try:
        await query.answer("Challenge cancelled.")
    except TelegramError:
        pass
    try:
        await query.edit_message_text(
            f"❌ <b>{game['challenger_name']} cancelled the challenge.</b>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

# ── PvP: Pick move (via private DM) ──────────────────────────────────────────
async def pvp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user  = update.effective_user

    # callback format: pvp_pick_{challenge_id}_{choice}
    raw          = query.data[len("pvp_pick_"):]
    choice_str   = raw.rsplit("_", 1)[1]
    challenge_id = raw.rsplit("_", 1)[0]

    game = pvp_games.get(challenge_id)

    if not game or game["state"] != "choosing":
        try:
            await query.answer("⏰ This game has expired!", show_alert=True)
        except TelegramError:
            pass
        # Remove stale buttons
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        return

    is_challenger = (user.id == game["challenger_id"])
    is_acceptor   = (user.id == game["acceptor_id"])

    if not is_challenger and not is_acceptor:
        try:
            await query.answer("❌ You are not part of this game!", show_alert=True)
        except TelegramError:
            pass
        return

    choice_map = {"rock": Choice.ROCK, "paper": Choice.PAPER, "scissors": Choice.SCISSORS}
    chosen = choice_map.get(choice_str)

    if is_challenger:
        if game["challenger_choice"]:
            try:
                await query.answer("✅ Already picked! Waiting for opponent…")
            except TelegramError:
                pass
            return
        game["challenger_choice"] = chosen
        dm_msg_id = game["challenger_dm_msg"]
    else:
        if game["acceptor_choice"]:
            try:
                await query.answer("✅ Already picked! Waiting for opponent…")
            except TelegramError:
                pass
            return
        game["acceptor_choice"] = chosen
        dm_msg_id = game["acceptor_dm_msg"]

    # Acknowledge the pick privately
    try:
        await query.answer(f"✅ You picked {chosen.value}! Waiting for opponent…")
    except TelegramError:
        pass

    # Update this player's DM: remove buttons, confirm pick
    if dm_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=user.id,
                message_id=dm_msg_id,
                text=f"✅ <b>You picked {chosen.value}!</b>\n\nWaiting for your opponent to choose…",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    # Update group status
    c_done = "✅" if game["challenger_choice"] else "⏳"
    a_done = "✅" if game["acceptor_choice"]   else "⏳"

    if not (game["challenger_choice"] and game["acceptor_choice"]):
        try:
            await context.bot.edit_message_text(
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                text=(
                    f"⚔️ <b>{game['challenger_name']} vs {game['acceptor_name']}</b>\n\n"
                    f"{c_done} {game['challenger_name']}\n"
                    f"{a_done} {game['acceptor_name']}\n\n"
                    f"<i>Waiting for both to lock in their move…</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    # ── Both chose — resolve ──────────────────────────────────────────────────
    for job in context.job_queue.get_jobs_by_name(f"choice_{challenge_id}"):
        job.schedule_removal()

    c_choice = game["challenger_choice"]
    a_choice = game["acceptor_choice"]
    outcome  = get_winner(c_choice, a_choice)

    if outcome == 1:
        result_line = f"🏆 <b>{game['challenger_name']} wins!</b>"
        c_result, a_result = "win", "loss"
        c_emoji, a_emoji   = "🎉", "😔"
    elif outcome == -1:
        result_line = f"🏆 <b>{game['acceptor_name']} wins!</b>"
        c_result, a_result = "loss", "win"
        c_emoji, a_emoji   = "😔", "🎉"
    else:
        result_line = "🤝 <b>It's a Draw!</b>"
        c_result, a_result = "draw", "draw"
        c_emoji, a_emoji   = "🤝", "🤝"

    update_pvp_game(game["challenger_id"], game["acceptor_id"],  c_choice.name, a_choice.name, c_result)
    update_pvp_game(game["acceptor_id"],   game["challenger_id"], a_choice.name, c_choice.name, a_result)

    pvp_games.pop(challenge_id, None)

    play_again_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play vs Bot",   callback_data="main_menu"),
        InlineKeyboardButton("⚔️ New Challenge", callback_data="pvp_challenge"),
    ]])

    result_text = (
        f"⚔️ <b>{game['challenger_name']} vs {game['acceptor_name']}</b>\n\n"
        f"{game['challenger_name']}: {c_choice.value}\n"
        f"{game['acceptor_name']}: {a_choice.value}\n\n"
        f"{result_line}"
    )

    # Post result to group
    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=result_text,
            parse_mode=ParseMode.HTML,
            reply_markup=play_again_markup,
        )
    except TelegramError as e:
        logger.warning("pvp_pick group result edit error: %s", e)

    # Update each player's DM with full result
    for uid, dm_msg_id, my_emoji, my_choice, opp_name, opp_choice in [
        (game["challenger_id"], game["challenger_dm_msg"], c_emoji, c_choice, game["acceptor_name"],   a_choice),
        (game["acceptor_id"],   game["acceptor_dm_msg"],   a_emoji, a_choice, game["challenger_name"], c_choice),
    ]:
        if dm_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=uid,
                    message_id=dm_msg_id,
                    text=(
                        f"{my_emoji} <b>Result vs {opp_name}</b>\n\n"
                        f"You: {my_choice.value}\n"
                        f"{opp_name}: {opp_choice.value}\n\n"
                        f"{result_line}"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=play_again_markup,
                )
            except TelegramError:
                pass

# ── Stats helpers ──────────────────────────────────────────────────────────────
def _build_stats_text(display: str, stats: dict | None) -> str:
    if not stats or (stats['total_games'] == 0 and stats['pvp_games'] == 0):
        return f"📊 <b>{display}'s Statistics</b>\n\nNo games played yet! 🎮"
    bot_total = stats['total_games']
    pvp_total = stats['pvp_games']
    bot_wr = (stats['wins']     / bot_total * 100) if bot_total > 0 else 0
    pvp_wr = (stats['pvp_wins'] / pvp_total * 100) if pvp_total > 0 else 0
    return (
        f"📊 <b>{display}'s Statistics</b>\n\n"
        f"<b>🤖 vs Bot ({bot_total} games)</b>\n"
        f"🎉 {stats['wins']}W  😔 {stats['losses']}L  🤝 {stats['draws']}D  📈 {bot_wr:.1f}%\n\n"
        f"<b>⚔️ vs Players ({pvp_total} games)</b>\n"
        f"🎉 {stats['pvp_wins']}W  😔 {stats['pvp_losses']}L  🤝 {stats['pvp_draws']}D  📈 {pvp_wr:.1f}%"
    )

def _build_leaderboard_text() -> str:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT * FROM users
                 WHERE total_games > 0 OR pvp_games > 0
                 ORDER BY (wins + pvp_wins) DESC LIMIT 10''')
    users = c.fetchall()
    conn.close()
    if not users:
        return "🏆 <b>Leaderboard</b>\n\nNo players yet! 🎮"
    text   = "🏆 <b>Top 10 Players</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for idx, u in enumerate(users, 1):
        medal = medals[idx - 1] if idx <= 3 else f"{idx}."
        name  = get_display_name_from_db(dict(u))
        tot_w = u['wins']     + u['pvp_wins']
        tot_l = u['losses']   + u['pvp_losses']
        tot_d = u['draws']    + u['pvp_draws']
        tot_g = u['total_games'] + u['pvp_games']
        wr    = (tot_w / tot_g * 100) if tot_g > 0 else 0
        text += f"{medal} <b>{name}</b>\n   {tot_w}W {tot_d}D {tot_l}L ({wr:.0f}%)\n\n"
    return text

# ── Stats ─────────────────────────────────────────────────────────────────────
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user    = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    stats = get_user_stats(user.id)
    text  = _build_stats_text(display, stats)
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Play",        callback_data="main_menu"),
        InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
    ]])
    await send_private_result(update, context, text, buttons)

# ── Leaderboard ───────────────────────────────────────────────────────────────
async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text    = _build_leaderboard_text()
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Play", callback_data="main_menu")]])
    await send_private_result(update, context, text, buttons)

# ── Rules ─────────────────────────────────────────────────────────────────────
async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 <b>How to Play</b>\n\n"
        "<b>Rules:</b>\n"
        "🪨 Rock beats ✂️ Scissors\n"
        "✂️ Scissors beats 📄 Paper\n"
        "📄 Paper beats 🪨 Rock\n\n"
        "<b>🤖 vs Bot:</b> Pick a move, bot picks randomly.\n\n"
        "<b>⚔️ vs Friend (group):</b>\n"
        "1. Press ⚔️ Challenge a Friend\n"
        "2. Anyone accepts within 60 seconds\n"
        "3. Each player picks privately via DM\n"
        "4. Result revealed in the group when both have chosen!\n\n"
        "<i>Both players must have started the bot in DM first!</i>\n\n"
        "<i>Good luck! 🍀</i>"
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Back", callback_data="main_menu")]])
    await send_private_result(update, context, text, buttons)

# ── Main menu ─────────────────────────────────────────────────────────────────
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        return
    try:
        await query.edit_message_text(
            "🎮 <b>Rock, Paper, Scissors</b>\n\nSelect your move to play!",
            parse_mode=ParseMode.HTML,
            reply_markup=create_game_buttons(),
        )
    except TelegramError:
        pass

# ── Error handler ─────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update caused error: %s", context.error, exc_info=context.error)

# ── Post init ─────────────────────────────────────────────────────────────────
async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start",       "Start the game 🎮"),
        BotCommand("stats",       "View your statistics 📊"),
        BotCommand("leaderboard", "View the leaderboard 🏆"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands set")
    except TelegramError as e:
        logger.warning("Could not set commands: %s", e)

# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    init_database()

    # Support both python-dotenv (local dev) and raw env vars (Railway)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN environment variable not set!")
        sys.exit(1)

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start",       start))
    application.add_handler(CommandHandler("stats",       stats_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))

    # vs Bot
    application.add_handler(CallbackQueryHandler(play_single,      pattern="^play_"))

    # PvP flow
    application.add_handler(CallbackQueryHandler(pvp_challenge,    pattern="^pvp_challenge$"))
    application.add_handler(CallbackQueryHandler(pvp_accept,       pattern="^pvp_accept_"))
    application.add_handler(CallbackQueryHandler(pvp_cancel,       pattern="^pvp_cancel_"))
    application.add_handler(CallbackQueryHandler(pvp_pick,         pattern="^pvp_pick_"))

    # Navigation
    application.add_handler(CallbackQueryHandler(show_stats,       pattern="^show_stats$"))
    application.add_handler(CallbackQueryHandler(show_leaderboard, pattern="^show_leaderboard$"))
    application.add_handler(CallbackQueryHandler(show_rules,       pattern="^rules$"))
    application.add_handler(CallbackQueryHandler(main_menu,        pattern="^main_menu$"))

    application.add_error_handler(error_handler)

    logger.info("Bot starting (polling)…")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == '__main__':
    main()
