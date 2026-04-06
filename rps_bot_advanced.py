"""
Rock, Paper, Scissors Telegram Bot
Features: Database, Stats, Leaderboard, Groups & Topics Support
Fixes: Display name, Railway compatibility, Per-user game isolation
"""

import logging
import random
import sqlite3
import os
import signal
import sys
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

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('rps_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Game choices
class Choice(Enum):
    ROCK = "🪨 Rock"
    PAPER = "📄 Paper"
    SCISSORS = "✂️ Scissors"

# Database setup
DB_PATH = "rps_stats.db"

def init_database():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  wins INTEGER DEFAULT 0,
                  losses INTEGER DEFAULT 0,
                  draws INTEGER DEFAULT 0,
                  total_games INTEGER DEFAULT 0,
                  last_played TIMESTAMP,
                  joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS game_history
                 (id INTEGER PRIMARY KEY,
                  user_id INTEGER,
                  opponent_id INTEGER,
                  user_choice TEXT,
                  bot_choice TEXT,
                  result TEXT,
                  played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY(user_id) REFERENCES users(user_id))''')
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_display_name(user) -> str:
    """Best display name from a live Telegram user object."""
    if user.first_name:
        full = user.first_name
        if user.last_name:
            full += f" {user.last_name}"
        return full
    if user.username:
        return f"@{user.username}"
    return "Player"

def get_display_name_from_db(row: dict) -> str:
    """Best display name from a DB row — never returns Unknown."""
    if row.get('first_name'):
        return row['first_name']
    if row.get('username'):
        return row['username']
    return "Player"

def get_or_create_user(user_id: int, username: str, display_name: str):
    """Upsert user, always refreshing their display name."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    exists = c.fetchone()
    if not exists:
        c.execute('''INSERT INTO users (user_id, username, first_name)
                     VALUES (?, ?, ?)''', (user_id, username, display_name))
    else:
        c.execute('''UPDATE users SET username = ?, first_name = ?
                     WHERE user_id = ?''', (username, display_name, user_id))
    conn.commit()
    conn.close()

def get_user_stats(user_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = c.fetchone()
    conn.close()
    return dict(user) if user else None

def update_game_result(user_id: int, user_choice: str, bot_choice: str, result: str):
    conn = get_db_connection()
    c = conn.cursor()
    if result == "win":
        c.execute('UPDATE users SET wins = wins + 1 WHERE user_id = ?', (user_id,))
    elif result == "loss":
        c.execute('UPDATE users SET losses = losses + 1 WHERE user_id = ?', (user_id,))
    else:
        c.execute('UPDATE users SET draws = draws + 1 WHERE user_id = ?', (user_id,))
    c.execute('''UPDATE users SET total_games = total_games + 1,
                 last_played = CURRENT_TIMESTAMP WHERE user_id = ?''', (user_id,))
    c.execute('''INSERT INTO game_history (user_id, opponent_id, user_choice, bot_choice, result)
                 VALUES (?, ?, ?, ?, ?)''', (user_id, 0, user_choice, bot_choice, result))
    conn.commit()
    conn.close()

def get_winner(choice1: Choice, choice2: Choice) -> int:
    if choice1 == choice2:
        return 0
    wins = {
        (Choice.ROCK, Choice.SCISSORS): 1,
        (Choice.SCISSORS, Choice.PAPER): 1,
        (Choice.PAPER, Choice.ROCK): 1,
    }
    return wins.get((choice1, choice2), -1)

def create_game_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪨 Rock", callback_data="play_rock"),
            InlineKeyboardButton("📄 Paper", callback_data="play_paper"),
            InlineKeyboardButton("✂️ Scissors", callback_data="play_scissors"),
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="show_stats"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
        ],
        [InlineKeyboardButton("ℹ️ Rules", callback_data="rules")]
    ])

def create_rematch_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎮 Play Again", callback_data="main_menu"),
            InlineKeyboardButton("📊 My Stats", callback_data="show_stats"),
        ]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", display)

    await update.message.reply_text(
        f"🎮 <b>Welcome to Rock, Paper, Scissors!</b>\n\n"
        f"Hi {display}! 👋\n\n"
        f"Choose your move below and battle the bot.\n\n"
        f"<b>✨ Features:</b>\n"
        f"🤖 Play against the bot\n"
        f"📊 Track your statistics\n"
        f"🏆 View the leaderboard\n"
        f"✅ Works in group topics\n\n"
        f"<i>Let's play!</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=create_game_buttons(),
    )

async def play_single(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fully isolated per-user game.
    No shared state — every user's button press is independent.
    """
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        return

    user = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", display)

    choice_map = {
        "play_rock": Choice.ROCK,
        "play_paper": Choice.PAPER,
        "play_scissors": Choice.SCISSORS,
    }
    user_choice = choice_map.get(query.data)
    if not user_choice:
        return

    # Independent random choice per user call
    bot_choice = random.choice(list(Choice))
    result = get_winner(user_choice, bot_choice)

    if result == 1:
        result_text, result_emoji, result_type = "🎉 <b>YOU WIN!</b>", "✨", "win"
    elif result == -1:
        result_text, result_emoji, result_type = "😔 <b>YOU LOSE!</b>", "💔", "loss"
    else:
        result_text, result_emoji, result_type = "🤝 <b>DRAW!</b>", "⚖️", "draw"

    # Only THIS user's DB row is touched
    update_game_result(user.id, user_choice.name, bot_choice.name, result_type)
    stats = get_user_stats(user.id)
    total = stats['total_games']
    win_rate = (stats['wins'] / total * 100) if total > 0 else 0

    try:
        await query.edit_message_text(
            f"{result_emoji} {result_text}\n\n"
            f"<b>Your choice:</b> {user_choice.value}\n"
            f"<b>Bot choice:</b> {bot_choice.value}\n\n"
            f"<b>📊 Your Stats:</b>\n"
            f"🎉 Wins: {stats['wins']}\n"
            f"😔 Losses: {stats['losses']}\n"
            f"🤝 Draws: {stats['draws']}\n"
            f"📈 Win Rate: {win_rate:.1f}%",
            parse_mode=ParseMode.HTML,
            reply_markup=create_rematch_buttons(),
        )
    except TelegramError:
        pass

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows stats only for the user who pressed the button."""
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        return

    user = update.effective_user
    display = get_display_name(user)
    get_or_create_user(user.id, user.username or "", display)
    stats = get_user_stats(user.id)

    if not stats or stats['total_games'] == 0:
        stats_text = (
            f"📊 <b>Your Statistics</b>\n\n"
            f"No games played yet! 🎮\n"
            f"Start your first game now!"
        )
    else:
        total = stats['total_games']
        win_rate = (stats['wins'] / total * 100) if total > 0 else 0
        stats_text = (
            f"📊 <b>{display}'s Statistics</b>\n\n"
            f"<b>Results:</b>\n"
            f"🎉 Wins: {stats['wins']}\n"
            f"😔 Losses: {stats['losses']}\n"
            f"🤝 Draws: {stats['draws']}\n"
            f"📊 Total: {total}\n\n"
            f"<b>Performance:</b>\n"
            f"📈 Win Rate: {win_rate:.1f}%\n"
            f"🎯 Record: {stats['wins']}W-{stats['losses']}L-{stats['draws']}D"
        )

    try:
        await query.edit_message_text(
            stats_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 Play", callback_data="main_menu"),
                InlineKeyboardButton("🏆 Leaderboard", callback_data="show_leaderboard"),
            ]]),
        )
    except TelegramError:
        pass

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leaderboard with proper display names — no Unknown."""
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE total_games > 0 ORDER BY wins DESC LIMIT 10')
    users = c.fetchall()
    conn.close()

    if not users:
        leaderboard_text = "🏆 <b>Leaderboard</b>\n\nNo players yet! 🎮"
    else:
        leaderboard_text = "🏆 <b>Top 10 Players</b>\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for idx, user in enumerate(users, 1):
            medal = medals[idx - 1] if idx <= 3 else f"{idx}."
            win_rate = (user['wins'] / user['total_games'] * 100) if user['total_games'] > 0 else 0
            name = get_display_name_from_db(dict(user))  # Never "Unknown"
            leaderboard_text += (
                f"{medal} <b>{name}</b>\n"
                f"   {user['wins']}W {user['draws']}D {user['losses']}L "
                f"({win_rate:.0f}%)\n\n"
            )

    try:
        await query.edit_message_text(
            leaderboard_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 Play", callback_data="main_menu")
            ]]),
        )
    except TelegramError:
        pass

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        return

    try:
        await query.edit_message_text(
            "📖 <b>How to Play</b>\n\n"
            "<b>The Rules:</b>\n"
            "🪨 Rock beats ✂️ Scissors\n"
            "✂️ Scissors beats 📄 Paper\n"
            "📄 Paper beats 🪨 Rock\n\n"
            "<b>Game Features:</b>\n"
            "🤖 Challenge the bot anytime\n"
            "📊 Automatic stats tracking\n"
            "🏆 Global leaderboard\n"
            "✅ Works everywhere!\n\n"
            "<i>Good luck! 🍀</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 Back to Game", callback_data="main_menu")
            ]]),
        )
    except TelegramError:
        pass

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

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Swallow errors so Railway never sees an unhandled exception crash."""
    logger.error(f"Update caused error: {context.error}", exc_info=context.error)

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Start the game 🎮"),
        BotCommand("stats", "View your statistics 📊"),
        BotCommand("leaderboard", "View the leaderboard 🏆"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands set")
    except TelegramError as e:
        logger.warning(f"Could not set commands: {e}")

def main() -> None:
    init_database()
    logger.info("Database initialized")

    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.critical("BOT_TOKEN not found!")
        sys.exit(1)

    application = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(play_single, pattern="^play_"))
    application.add_handler(CallbackQueryHandler(show_stats, pattern="^show_stats$"))
    application.add_handler(CallbackQueryHandler(show_leaderboard, pattern="^show_leaderboard$"))
    application.add_handler(CallbackQueryHandler(show_rules, pattern="^rules$"))
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    application.add_error_handler(error_handler)
    application.post_init = post_init

    logger.info("Bot starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,     # Skip stale updates after Railway restart
        close_loop=False,              # Avoid asyncio conflicts on Railway
        stop_signals=[signal.SIGINT, signal.SIGTERM],
    )

if __name__ == '__main__':
    main()
