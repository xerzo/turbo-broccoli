import logging
from logging.handlers import RotatingFileHandler
import json
from html import escape
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from pyrogram.enums import ParseMode, ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated
import requests
import asyncio
import os
import time
import random
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone

BOT_START_TIME = time.time()

# High-performance event loop (uvloop)
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except (ImportError, AttributeError):
    pass

# ================= LOGGING ================= #
os.makedirs("logs", exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

_file_handler = RotatingFileHandler(
    "logs/anisearch.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)
logger.addHandler(_stream_handler)

# ================= CONFIG ================= #

def load_config(filename="config.json"):
    """Load config from JSON file, with env var fallback."""
    try:
        with open(filename) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}

    return cfg

cfg = load_config()

try:
    API_ID = int(cfg.get("api_id") or os.getenv("API_ID") or 0)
except (ValueError, TypeError):
    API_ID = 0
API_HASH = cfg.get("api_hash", os.getenv("API_HASH", ""))
BOT_TOKEN = cfg.get("bot_token", os.getenv("BOT_TOKEN", ""))
MONGODB_URI = cfg.get("mongodb_uri", os.getenv("MONGODB_URI", ""))

# Bot config
API_BASE_URL = cfg.get("api_base_url", os.getenv("API_BASE_URL", "https://xapi/public/search-anime"))
VIEW_BASE_URL = cfg.get("view_base_url", os.getenv("VIEW_BASE_URL", "https://xxxxx/"))
FORCE_SUB_CHANNEL = cfg.get("force_sub_channel", os.getenv("FORCE_SUB_CHANNEL", ""))
# Admin IDs (normalized to integers)
raw_admin_ids = cfg.get("admin_ids", [])
ADMIN_IDS = []
for aid in raw_admin_ids:
    try:
        ADMIN_IDS.append(int(aid))
    except (ValueError, TypeError):
        pass

# Channel subscription settings
ENABLE_FORCE_SUB = cfg.get("enable_force_sub", False)
FORCE_SUB_IN_GROUPS = cfg.get("force_sub_in_groups", False)
ENABLE_TEXT_SEARCH_IN_GROUPS = cfg.get("enable_text_search_in_groups", False)

# Forum topic settings
FORUM_TOPIC_CHAT_ID = int(cfg.get("forum_topic_chat_id", 0) or 0)
FORUM_TOPIC_THREAD_ID = int(cfg.get("forum_topic_thread_id", 0) or 0)

# Community links & branding (configurable via config.json)
BOT_NAME = cfg.get("bot_name", "Anime Search Bot")
WEBSITE_URL = cfg.get("website_url", "")
UPDATES_CHANNEL_URL = cfg.get("updates_channel_url", "")
COMMUNITY_CHAT_URL = cfg.get("community_chat_url", "")
START_BUTTONS = cfg.get("start_buttons")


def get_start_keyboard():
    """Build inline buttons for /start command based on configuration."""
    if START_BUTTONS:
        keyboard = []
        for row in START_BUTTONS:
            btn_row = [InlineKeyboardButton(btn["text"], url=btn["url"]) for btn in row]
            keyboard.append(btn_row)
        return InlineKeyboardMarkup(keyboard)

    # Default button layout from config links
    rows = []
    if WEBSITE_URL:
        rows.append([InlineKeyboardButton("🌐 Website", url=WEBSITE_URL)])
    community_row = []
    if UPDATES_CHANNEL_URL:
        community_row.append(InlineKeyboardButton("📢 Updates Channel", url=UPDATES_CHANNEL_URL))
    if COMMUNITY_CHAT_URL:
        community_row.append(InlineKeyboardButton("💬 Community Chat", url=COMMUNITY_CHAT_URL))
    if community_row:
        rows.append(community_row)

    return InlineKeyboardMarkup(rows) if rows else None

# ========================================== #

# MongoDB Setup
if MONGODB_URI:
    mongo_client = AsyncIOMotorClient(MONGODB_URI)
    db = mongo_client["anime_bot_db"]
    users_collection = db["users"]
else:
    logger.warning("MONGODB_URI not set. MongoDB operations will be skipped.")
    mongo_client = None
    db = None
    users_collection = None

SESSION_DIR = "session"
os.makedirs(SESSION_DIR, exist_ok=True)

app = Client(
    "anime_search_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=SESSION_DIR
)
app.search_results = {}

app.selection_messages = {}

async def auto_delete_selection(chat_id, message_id):
    await asyncio.sleep(300)  # 5 minutes

    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        pass

    app.selection_messages.pop(message_id, None)
    app.search_results.pop(message_id, None)
# ================= USER DB HELPERS ================= #

async def save_user(user_id: int, username: str = None, first_name: str = None):
    """Save or update user in MongoDB"""
    if users_collection is None:
        return
    await users_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username,
                "first_name": first_name,
                "last_seen": datetime.now(timezone.utc)
            },
            "$setOnInsert": {
                "joined_at": datetime.now(timezone.utc),
                "is_banned": False
            }
        },
        upsert=True
    )

async def get_user_count():
    """Get total registered users"""
    if users_collection is None:
        return 0
    return await users_collection.count_documents({})

async def get_all_user_ids():
    """Get all user IDs for broadcast (excluding banned users and admins)"""
    if users_collection is None:
        return []
    admin_set = set(ADMIN_IDS)
    query = {"is_banned": False}
    if ADMIN_IDS:
        query["user_id"] = {"$nin": ADMIN_IDS}
    cursor = users_collection.find(query, {"user_id": 1})
    return [
        doc["user_id"]
        async for doc in cursor
        if doc.get("user_id") is not None
        and int(doc.get("user_id", 0)) not in admin_set
    ]

async def is_user_banned(user_id: int) -> bool:
    """Check if user is banned"""
    if users_collection is None:
        return False
    user = await users_collection.find_one({"user_id": user_id})
    return user.get("is_banned", False) if user else False


# ================= FORCE SUBSCRIBE CHECK ================= #

async def is_user_subscribed(user_id: int) -> bool:
    """Check if user has joined the force-sub channel."""
    if not ENABLE_FORCE_SUB or not FORCE_SUB_CHANNEL or FORCE_SUB_CHANNEL in ["@xxx", "@channel_username"]:
        return True
    try:
        member = await app.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status in [ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER]
    except Exception as e:
        logger.error(f"Force sub check error: {e}")
        return False  # Fail-safe: deny access if check fails


async def check_force_sub(client, message):
    """Middleware to enforce channel subscription if enabled in config."""
    # Skip if force subscription is disabled or no channel is configured
    if not ENABLE_FORCE_SUB or not FORCE_SUB_CHANNEL or FORCE_SUB_CHANNEL in ["@xxx", "@channel_username"]:
        return True

    user_id = message.from_user.id
    
    # Skip for admins
    if user_id in ADMIN_IDS:
        return True
    
    # Skip force-sub in groups if disabled in config
    if message.chat.type != ChatType.PRIVATE and not FORCE_SUB_IN_GROUPS:
        return True
    
    # Check subscription
    if not await is_user_subscribed(user_id):
        join_url = f"https://t.me/{FORCE_SUB_CHANNEL.lstrip('@')}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔔 Join Channel to Use Bot", url=join_url)]]
        )
        await message.reply_text(
            "❌ <b>Access Denied!</b>\n\n"
            f"You must join our channel to use this bot:\n"
            f"➤ @{FORCE_SUB_CHANNEL.lstrip('@')}\n\n"
            "After joining, click /start again.",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        return False
    return True


# ================= API SEARCH ================= #

def search_anime(query: str):
    try:
        params = {"query": query}
        response = requests.get(API_BASE_URL, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"API error: {e}")
        return None


# ================= IMAGE & URL HELPERS ================= #

def get_nested_key(data, *keys):
    """Safely get nested dictionary keys"""
    for key in keys:
        try:
            if isinstance(data, dict):
                data = data.get(key)
            else:
                return None
        except (KeyError, TypeError):
            return None
    return data

def get_image_url(anime):
    """Get image URL from anime data - handles both nested and flat structures"""
    # Try nested structure first: images.jpg.image_url
    image_url = get_nested_key(anime, "images", "jpg", "image_url")
    if image_url:
        return image_url
    
    # Try webp format
    image_url = get_nested_key(anime, "images", "webp", "image_url")
    if image_url:
        return image_url
    
    # Try flat key structure (with dots in key name)
    if isinstance(anime, dict):
        if "images.jpg.image_url" in anime:
            return anime["images.jpg.image_url"]
        if "images.webp.image_url" in anime:
            return anime["images.webp.image_url"]
        if "image_url" in anime:
            return anime["image_url"]
        if "imageUrl" in anime:
            return anime["imageUrl"]
        if "poster" in anime:
            return anime["poster"]
        if "cover" in anime:
            return anime["cover"]
    
    return None

def get_post_url(anime):
    post_ids = anime.get("post_ids", [])
    if post_ids and len(post_ids) > 0:
        return f"{VIEW_BASE_URL}{post_ids[0]}"
    return None


# ================= FORMAT MESSAGE ================= #

def format_anime_info(anime):
    title = anime.get("title", "Unknown Title")
    title_en = anime.get("title_english")
    title_jp = anime.get("title_japanese")
    anime_type = anime.get("type", "Unknown")
    mal_id = anime.get("mal_id", "N/A")
    synopsis = anime.get("synopsis", "No synopsis available")
    
    if len(synopsis) > 500:
        synopsis = synopsis[:500] + "..."
    
    text = f"<b>📺 {title}</b>\n━━━━━━━━━━━━━━━━\n"
    if title_en and title_en != title:
        text += f"<b>📝 English:</b> {title_en}\n"
    if title_jp:
        text += f"<b>🇯🇵 Japanese:</b> {title_jp}\n"
    text += f"<b>📌 Type:</b> {anime_type}\n<b>🆔 MAL ID:</b> {mal_id}\n"
    text += "━━━━━━━━━━━━━━━━\n"
    text += f"<b>📖 Synopsis:</b>\n<blockquote>{synopsis}</blockquote>"
    return text


# ================= SEND RESULT ================= #

async def send_anime_result(message, anime, is_random=False):
    image_url = get_image_url(anime)
    info_text = format_anime_info(anime)
    
    keyboard = []
    post_url = get_post_url(anime)
    if post_url:
        keyboard.append([InlineKeyboardButton("🔗 View Full Post", url=post_url)])
    if is_random:
        keyboard.append([InlineKeyboardButton("🎲 Roll Another Anime", callback_data="random_anime")])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    chat_id = message.chat.id
    
    if image_url:
        try:
            logger.info(f"Sending image: {image_url}")
            await app.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=info_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return
        except Exception as e:
            logger.error(f"Photo failed: {e}")
            # Fallback: send as text with image URL
            info_text += f"\n\n🖼️ <a href='{image_url}'>View Image</a>"
    
    await app.send_message(
        chat_id=chat_id,
        text=info_text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=False
    )


# ================= START COMMAND ================= #

@app.on_message(filters.command("start"))
async def start_command(client, message):
    user_id = message.from_user.id
    
    # Save user to DB
    await save_user(
        user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    
    # Force sub check (honors enable_force_sub and group settings)
    if not await check_force_sub(client, message):
        return
    
    first_name = escape(message.from_user.first_name or "there")
    
    admin_commands = ""
    if user_id in ADMIN_IDS:
        admin_commands = (
            "\n\n👑 <b>Admin Commands:</b>\n"
            "• <code>/admin</code> or <code>/stats</code> — Interactive dashboard\n"
            "• <code>/broadcast</code> — Broadcast announcement"
        )
    
    bot_user = app.me if app.me else await app.get_me()
    bot_username = bot_user.username or ""
    inline_hint = f"\n• Inline in any chat: <code>@{bot_username} Naruto</code>" if bot_username else ""

    text = (
        f"👋 <b>Hello {first_name}!</b>\n\n"
        f"Welcome to <b>{escape(BOT_NAME)}</b> 🎬\n\n"
        "🔍 <b>How to search anime:</b>\n"
        "• Send anime title directly in chat (e.g. <code>Naruto</code>)\n"
        f"• Or use command: <code>/anime One Piece</code>{inline_hint}\n\n"
        "⚡ <b>Commands:</b>\n"
        "• <code>/anime &lt;name&gt;</code> — Search anime database\n"
        "• <code>/random</code> — Surprise me with a random anime\n"
        f"• <code>/help</code> — Guide & features{admin_commands}"
    )
    
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_start_keyboard())


# ================= SEARCH PAGINATION HELPERS ================= #

PAGE_SIZE = 5

def build_search_keyboard(results, page, query):
    total = len(results)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total)
    
    keyboard = []
    for i in range(start_idx, end_idx):
        anime = results[i]
        title = anime.get("title", "Unknown")
        anime_type = anime.get("type", "")
        label = f"[{anime_type}] {title}" if anime_type else title
        if len(label) > 40:
            label = label[:37] + "..."
        keyboard.append([InlineKeyboardButton(label, callback_data=f"select_{i}")])
    
    # Navigation row if more than 1 page
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"page_{page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏹", callback_data="noop"))
            
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{page + 1}"))
        else:
            nav_row.append(InlineKeyboardButton("⏹", callback_data="noop"))
        keyboard.append(nav_row)
        
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_search")])
    return InlineKeyboardMarkup(keyboard)


# ================= SEARCH HANDLERS (with force sub) ================= #

async def handle_search(message, query):
    result = await asyncio.to_thread(search_anime, query)
    
    if not result or "results" not in result or not result["results"]:
        await message.reply_text(f"❌ No results found for '<code>{escape(query)}</code>'.", parse_mode=ParseMode.HTML)
        return
    
    results = result["results"]
    
    # Debug: Log first result structure
    if results:
        logger.info(f"API Response keys: {results[0].keys()}")
        if "images" in results[0]:
            logger.info(f"Images structure: {results[0]['images']}")
    
    if len(results) == 1:
        await send_anime_result(message, results[0])
        return
    
    total = len(results)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page_text = f" (Page 1/{total_pages})" if total_pages > 1 else ""
    text = f"🔍 Found <b>{total}</b> results for '<code>{escape(query)}</code>'{page_text}:\n\nSelect an anime below:"
    
    selection_msg = await message.reply_text(
        text,
        reply_markup=build_search_keyboard(results, 0, query),
        parse_mode=ParseMode.HTML
    )
    
    app.search_results[selection_msg.id] = {
        "query": query,
        "results": results,
        "page": 0
    }
    
    app.selection_messages[selection_msg.id] = {
        "chat_id": selection_msg.chat.id
    }
    
    asyncio.create_task(
        auto_delete_selection(
            selection_msg.chat.id,
            selection_msg.id
        )
    )


# Search commands - FIXED: Works in groups with proper closure handling
for cmd in ["anime", "anidl", "search"]:
    @app.on_message(filters.command(cmd))  # Works in private + groups by default
    async def search_cmd(client, message, cmd=cmd):  # ← Fixed closure with default arg
        if len(message.command) < 2:
            await message.reply_text(
                f"❌ Please provide an anime name.\n\nExample:\n<code>/{cmd} One Piece</code>",
                parse_mode=ParseMode.HTML
            )
            return
        query = " ".join(message.command[1:])
        await handle_search(message, query)


# Text search handler - Private chats, dedicated forum topic, and group mentions/text
@app.on_message(
    filters.text
    & ~filters.command([
        "start", "help", "anime", "anidl",
        "search", "stats", "admin", "broadcast", "random"
    ])
)
async def text_search_handler(client, message):

    # 1. Private chats: always allow plain text search
    if message.chat.type == ChatType.PRIVATE:
        query = message.text.strip()
        if query:
            await handle_search(message, query)
        return

    # 2. Dedicated forum topic: allow direct plain text search inside the configured topic thread
    if (
        FORUM_TOPIC_CHAT_ID
        and message.chat.id == FORUM_TOPIC_CHAT_ID
        and getattr(message, "message_thread_id", None) == FORUM_TOPIC_THREAD_ID
    ):
        query = message.text.strip()
        if query:
            await handle_search(message, query)
        return

    # 3. Mention in groups: e.g. "@bot_username Naruto"
    bot_user = app.me if app.me else await app.get_me()
    bot_username = (bot_user.username or "").lower()
    text = message.text.strip()
    mention_prefix = f"@{bot_username}"

    if bot_username and text.lower().startswith(mention_prefix):
        query = text[len(mention_prefix):].strip()
        if query:
            await handle_search(message, query)
        else:
            await message.reply_text(
                f"🔍 Please provide an anime name after the mention.\nExample: <code>@{bot_username} Naruto</code>",
                parse_mode=ParseMode.HTML
            )
        return

    # 4. Reply to bot in groups
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_user.id
    ):
        query = text
        if query:
            await handle_search(message, query)
        return

    # 5. Global plain text search in groups (if enable_text_search_in_groups is true)
    if ENABLE_TEXT_SEARCH_IN_GROUPS:
        query = text
        if query:
            await handle_search(message, query)

# ================= CALLBACK HANDLER ================= #

@app.on_callback_query(filters.regex(r"^select_"))
async def callback_handler(client, callback_query):
    data = callback_query.data
    index = int(data.split("_")[1])

    search_data = app.search_results.get(callback_query.message.id)
    if isinstance(search_data, dict):
        results = search_data.get("results", [])
    elif isinstance(search_data, list):
        results = search_data
    else:
        results = []

    if 0 <= index < len(results):
        await callback_query.answer("Loading...")

        try:
            await callback_query.message.delete()
        except Exception:
            pass

        app.search_results.pop(callback_query.message.id, None)
        app.selection_messages.pop(callback_query.message.id, None)

        await send_anime_result(callback_query.message, results[index])
    else:
        await callback_query.answer("This menu has expired.", show_alert=True)


# ================= SEARCH PAGINATION CALLBACKS ================= #

@app.on_callback_query(filters.regex(r"^page_"))
async def page_callback_handler(client, callback_query):
    new_page = int(callback_query.data.split("_")[1])
    search_data = app.search_results.get(callback_query.message.id)
    if not search_data:
        await callback_query.answer("⚠️ This search menu has expired.", show_alert=True)
        return

    results = search_data.get("results", []) if isinstance(search_data, dict) else search_data
    query = search_data.get("query", "") if isinstance(search_data, dict) else ""
    total = len(results)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if isinstance(search_data, dict):
        search_data["page"] = new_page

    markup = build_search_keyboard(results, new_page, query)
    text = (
        f"🔍 Found <b>{total}</b> results for '<code>{escape(query)}</code>' "
        f"(Page {new_page + 1}/{total_pages}):\n\nSelect an anime below:"
    )

    try:
        await callback_query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    await callback_query.answer()


@app.on_callback_query(filters.regex(r"^close_search$"))
async def close_search_handler(client, callback_query):
    app.search_results.pop(callback_query.message.id, None)
    app.selection_messages.pop(callback_query.message.id, None)
    try:
        await callback_query.message.delete()
    except Exception:
        pass
    await callback_query.answer("Closed")


@app.on_callback_query(filters.regex(r"^noop$"))
async def noop_handler(client, callback_query):
    await callback_query.answer()


# ================= SURPRISE ME / RANDOM ANIME ================= #

RANDOM_SEEDS = [
    "Naruto", "One Piece", "Bleach", "Attack on Titan", "Jujutsu Kaisen",
    "Demon Slayer", "Fullmetal Alchemist", "Death Note", "Hunter x Hunter",
    "Steins;Gate", "Frieren", "Spy x Family", "Chainsaw Man", "Vinland Saga",
    "Solo Leveling", "Bocchi the Rock", "Mob Psycho 100", "Haikyuu",
    "Sword Art Online", "Black Clover", "Overlord", "Re:Zero", "Konosuba",
    "Dr. Stone", "Tokyo Ghoul", "Cowboy Bebop", "Gintama", "Fate",
    "Oshi no Ko", "Mushoku Tensei", "Cyberpunk", "Classroom of the Elite"
]

async def fetch_random_anime():
    """Fetch a random anime recommendation from the API."""
    seeds = random.sample(RANDOM_SEEDS, min(4, len(RANDOM_SEEDS)))
    for seed in seeds:
        data = await asyncio.to_thread(search_anime, seed)
        if data and data.get("results"):
            return random.choice(data["results"])
    return None


@app.on_message(filters.command("random"))
async def random_anime_command(client, message):
    if not await check_force_sub(client, message):
        return
    status = await message.reply_text("🎲 <i>Rolling a random anime recommendation...</i>", parse_mode=ParseMode.HTML)
    anime = await fetch_random_anime()
    if not anime:
        await status.edit_text("❌ Could not fetch a random anime. Please try again.")
        return
    try:
        await status.delete()
    except Exception:
        pass
    await send_anime_result(message, anime, is_random=True)


@app.on_callback_query(filters.regex(r"^random_anime$"))
async def random_anime_callback(client, callback_query):
    await callback_query.answer("🎲 Finding another anime...")
    anime = await fetch_random_anime()
    if not anime:
        await callback_query.answer("❌ Could not fetch another anime. Please try again.", show_alert=True)
        return
    try:
        await callback_query.message.delete()
    except Exception:
        pass
    await send_anime_result(callback_query.message, anime, is_random=True)


# ================= TELEGRAM INLINE MODE ================= #

@app.on_inline_query()
async def inline_query_handler(client, inline_query):
    query = inline_query.query.strip()
    if not query:
        await inline_query.answer(
            results=[],
            switch_pm_text="🔍 Type an anime title to search...",
            switch_pm_parameter="help",
            cache_time=5
        )
        return

    result = await asyncio.to_thread(search_anime, query)
    if not result or not result.get("results"):
        await inline_query.answer(
            results=[],
            switch_pm_text=f"❌ No anime found for '{query}'",
            switch_pm_parameter="search",
            cache_time=5
        )
        return

    articles = []
    for i, anime in enumerate(result["results"][:15]):
        title = anime.get("title", "Unknown Title")
        anime_type = anime.get("type", "Anime")
        synopsis = anime.get("synopsis", "No synopsis available.") or "No synopsis available."
        image_url = get_image_url(anime)
        post_url = get_post_url(anime)
        info_text = format_anime_info(anime)

        keyboard = []
        if post_url:
            keyboard.append([InlineKeyboardButton("🔗 View Full Post", url=post_url)])
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        desc = (synopsis[:110] + "...") if len(synopsis) > 110 else synopsis
        articles.append(
            InlineQueryResultArticle(
                id=str(i),
                title=f"[{anime_type}] {title}" if anime_type else title,
                description=desc,
                thumb_url=image_url if image_url else None,
                input_message_content=InputTextMessageContent(
                    message_text=info_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                ),
                reply_markup=reply_markup
            )
        )

    await inline_query.answer(
        results=articles,
        cache_time=60
    )


# ================= HELP COMMAND ================= #

@app.on_message(filters.command("help"))
async def help_command(client, message):
    if not await check_force_sub(app, message):
        return
    
    channel_display = f"@{FORCE_SUB_CHANNEL.lstrip('@')}" if FORCE_SUB_CHANNEL else ""
    join_info = f"\n\n📢 <b>Official Channel:</b> {channel_display}" if channel_display else ""
    
    admin_section = ""
    if message.from_user and message.from_user.id in ADMIN_IDS:
        admin_section = (
            "\n\n👑 <b>Admin Tools:</b>\n"
            "• <code>/admin</code> or <code>/stats</code> — Interactive dashboard\n"
            "• <code>/broadcast</code> — Broadcast announcement"
        )
    
    bot_user = app.me if app.me else await app.get_me()
    bot_username = bot_user.username or ""
    inline_guide = f"\n• <b>Inline Search:</b> Type <code>@{bot_username} Naruto</code> in any chat" if bot_username else ""

    text = (
        f"<b>🤖 How to use {escape(BOT_NAME)}:</b>\n\n"
        "• Type any anime title directly in private chat\n"
        "• Or use command: <code>/anime &lt;title&gt;</code> in groups\n"
        "• Surprise recommendation: <code>/random</code>"
        f"{inline_guide}\n\n"
        "✨ <b>Features:</b>\n"
        "🖼 Poster + English & Japanese titles + Synopsis\n"
        "📑 Interactive page-by-page search results\n"
        "🎲 Surprise me random anime button\n"
        "🔗 Direct post links & download sources\n"
        "⚡ Fast interactive selection menu"
        f"{join_info}{admin_section}"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_start_keyboard())


# ================= STATS & ADMIN DASHBOARD ================= #

def format_timespan(seconds: float) -> str:
    """Format seconds into human-readable duration like '1d 2h 30m 15s'."""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def get_system_uptime() -> str:
    """Read machine/host uptime from /proc/uptime if available."""
    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.readline().split()[0])
            return format_timespan(uptime_seconds)
    except Exception:
        return "N/A"


def build_stats_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh Stats", callback_data="admin_refresh_stats"),
            InlineKeyboardButton("⚙️ Live Settings", callback_data="admin_settings")
        ]
    ])


def build_settings_keyboard():
    fs_status = "✅ ON" if ENABLE_FORCE_SUB else "❌ OFF"
    gt_status = "✅ ON" if ENABLE_TEXT_SEARCH_IN_GROUPS else "❌ OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Force Sub: {fs_status}", callback_data="admin_toggle_forcesub")
        ],
        [
            InlineKeyboardButton(f"Group Text Search: {gt_status}", callback_data="admin_toggle_groupsearch")
        ],
        [
            InlineKeyboardButton("🔙 Back to Stats", callback_data="admin_refresh_stats")
        ]
    ])


async def build_stats_text():
    total_users = await get_user_count()
    bot_uptime = format_timespan(time.time() - BOT_START_TIME)
    system_uptime = get_system_uptime()
    bot_user = app.me if app.me else await app.get_me()

    channel_info = FORCE_SUB_CHANNEL if ENABLE_FORCE_SUB and FORCE_SUB_CHANNEL else "Disabled"

    return (
        "<b>📊 Bot & System Statistics</b>\n\n"
        f"🤖 <b>Bot Name:</b> <code>{escape(bot_user.first_name)}</code>\n"
        f"⏱ <b>Bot Uptime:</b> <code>{bot_uptime}</code>\n"
        f"🖥 <b>Host Uptime:</b> <code>{system_uptime}</code>\n"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
        f"👑 <b>Admins:</b> <code>{len(ADMIN_IDS)}</code>\n"
        f"📢 <b>Force Sub:</b> <code>{channel_info}</code>\n"
        f"💬 <b>Group Search:</b> <code>{'Enabled' if ENABLE_TEXT_SEARCH_IN_GROUPS else 'Mention Only'}</code>"
    )


@app.on_message(filters.command(["stats", "admin"]) & filters.user(ADMIN_IDS))
async def stats_command(client, message):
    text = await build_stats_text()
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=build_stats_keyboard())


@app.on_callback_query(filters.regex(r"^admin_"))
async def admin_callback_handler(client, callback_query):
    user_id = callback_query.from_user.id if callback_query.from_user else 0
    if user_id not in ADMIN_IDS:
        await callback_query.answer("⛔ Admins only!", show_alert=True)
        return

    data = callback_query.data
    global ENABLE_FORCE_SUB, ENABLE_TEXT_SEARCH_IN_GROUPS

    if data == "admin_refresh_stats":
        text = await build_stats_text()
        try:
            await callback_query.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_stats_keyboard()
            )
        except Exception:
            pass
        await callback_query.answer("✅ Stats refreshed!")

    elif data == "admin_settings":
        settings_text = (
            "<b>⚙️ Live Bot Controls</b>\n\n"
            "Toggle runtime settings on the fly without restarting the bot:\n\n"
            f"• <b>Force Sub:</b> {'Enabled' if ENABLE_FORCE_SUB else 'Disabled'}\n"
            f"• <b>Group Plain Text Search:</b> {'Enabled' if ENABLE_TEXT_SEARCH_IN_GROUPS else 'Disabled (Mention/Reply only)'}"
        )
        try:
            await callback_query.message.edit_text(
                settings_text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_settings_keyboard()
            )
        except Exception:
            pass
        await callback_query.answer()

    elif data == "admin_toggle_forcesub":
        ENABLE_FORCE_SUB = not ENABLE_FORCE_SUB
        await callback_query.answer(f"Force Sub set to {'ON' if ENABLE_FORCE_SUB else 'OFF'}")
        settings_text = (
            "<b>⚙️ Live Bot Controls</b>\n\n"
            "Toggle runtime settings on the fly without restarting the bot:\n\n"
            f"• <b>Force Sub:</b> {'Enabled' if ENABLE_FORCE_SUB else 'Disabled'}\n"
            f"• <b>Group Plain Text Search:</b> {'Enabled' if ENABLE_TEXT_SEARCH_IN_GROUPS else 'Disabled (Mention/Reply only)'}"
        )
        try:
            await callback_query.message.edit_text(
                settings_text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_settings_keyboard()
            )
        except Exception:
            pass

    elif data == "admin_toggle_groupsearch":
        ENABLE_TEXT_SEARCH_IN_GROUPS = not ENABLE_TEXT_SEARCH_IN_GROUPS
        await callback_query.answer(f"Group Text Search set to {'ON' if ENABLE_TEXT_SEARCH_IN_GROUPS else 'OFF'}")
        settings_text = (
            "<b>⚙️ Live Bot Controls</b>\n\n"
            "Toggle runtime settings on the fly without restarting the bot:\n\n"
            f"• <b>Force Sub:</b> {'Enabled' if ENABLE_FORCE_SUB else 'Disabled'}\n"
            f"• <b>Group Plain Text Search:</b> {'Enabled' if ENABLE_TEXT_SEARCH_IN_GROUPS else 'Disabled (Mention/Reply only)'}"
        )
        try:
            await callback_query.message.edit_text(
                settings_text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_settings_keyboard()
            )
        except Exception:
            pass


# ================= BROADCAST COMMAND (ADMIN ONLY) ================= #

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_IDS))
async def broadcast_command(client, message):
    if len(message.command) < 2 and not message.reply_to_message:
        await message.reply_text(
            "❌ <b>Usage:</b>\n"
            "• Reply to a message + <code>/broadcast</code>\n"
            "• Or: <code>/broadcast Your message here</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Get broadcast content
    if message.reply_to_message:
        content = message.reply_to_message
        send_method = "reply_copy"
    else:
        content = " ".join(message.command[1:])
        send_method = "text"
    
    # Confirmation
    target_users = await get_all_user_ids()
    if not target_users:
        await message.reply_text(
            "ℹ️ No eligible non-admin users found in database to broadcast to.",
            parse_mode=ParseMode.HTML
        )
        return

    confirm_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, Broadcast", callback_data="broadcast_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel")
        ]
    ])
    
    confirm_msg = await message.reply_text(
        f"⚠️ <b>Confirm Broadcast</b>\n\n"
        f"📤 Send to: <code>{len(target_users)}</code> users (excluding admins)\n"
        f"📝 Content: <i>{content if isinstance(content, str) else 'Reply message'}</i>",
        reply_markup=confirm_kb,
        parse_mode=ParseMode.HTML
    )
    
    # Store broadcast data temporarily (by message ID for concurrency safety)
    if not hasattr(app, "broadcast_sessions"):
        app.broadcast_sessions = {}
    app.broadcast_sessions[confirm_msg.id] = {
        "admin_id": message.from_user.id,
        "content": content,
        "send_method": send_method,
    }
    app.broadcast_data = {
        "admin_id": message.from_user.id,
        "content": content,
        "send_method": send_method,
        "confirm_msg_id": confirm_msg.id
    }


# ================= BROADCAST RUNNER (BATCHING & FLOOD SAFE) ================= #

BATCH_SIZE = 25          # Users per batch
BATCH_DELAY = 1.0        # Seconds between batches
INTER_MSG_DELAY = 0.05   # Seconds between individual sends inside a batch


async def send_broadcast_message(client, user_id, content, send_method):
    """Send broadcast content to a single user with automatic FloodWait retry."""
    while True:
        try:
            if send_method == "reply_copy":
                await content.copy(chat_id=user_id)
            else:
                await client.send_message(user_id, content, parse_mode=ParseMode.HTML)
            return "success"
        except FloodWait as e:
            logger.warning(f"FloodWait hit during broadcast: sleeping {e.value}s before retrying user {user_id}")
            await asyncio.sleep(e.value + 1)
            continue
        except (UserIsBlocked, InputUserDeactivated):
            return "blocked"
        except Exception as e:
            logger.error(f"Broadcast to {user_id} failed: {e}")
            return "failed"


async def execute_broadcast(client, status_message, content, send_method):
    """Execute batched broadcast with live progress updates."""
    user_ids = await get_all_user_ids()
    total = len(user_ids)
    if total == 0:
        try:
            await status_message.edit_text("ℹ️ No eligible non-admin users found to broadcast to.")
        except Exception:
            pass
        return

    success = 0
    blocked = 0
    failed = 0

    last_edit = asyncio.get_event_loop().time()

    for i in range(0, total, BATCH_SIZE):
        batch = user_ids[i:i + BATCH_SIZE]
        for uid in batch:
            res = await send_broadcast_message(client, uid, content, send_method)
            if res == "success":
                success += 1
            elif res == "blocked":
                blocked += 1
                # Mark as inactive/banned in MongoDB so future broadcasts skip them
                if users_collection is not None:
                    try:
                        await users_collection.update_one(
                            {"user_id": uid},
                            {"$set": {"is_banned": True}}
                        )
                    except Exception:
                        pass
            else:
                failed += 1

            await asyncio.sleep(INTER_MSG_DELAY)

        # Update progress every ~3 seconds or on the final batch
        now = asyncio.get_event_loop().time()
        processed = success + blocked + failed
        if (now - last_edit >= 3.0) or (processed >= total):
            pct = min(100, int((processed / total) * 100)) if total > 0 else 100
            try:
                await status_message.edit_text(
                    f"🔄 <b>Broadcasting in progress...</b>\n\n"
                    f"📤 Sent: <code>{success}</code> / <code>{total}</code>\n"
                    f"🚫 Blocked/Inactive: <code>{blocked}</code>\n"
                    f"❌ Failed: <code>{failed}</code>\n"
                    f"📊 Progress: <code>{pct}%</code>",
                    parse_mode=ParseMode.HTML
                )
                last_edit = now
            except Exception:
                pass

        # Pause between batches to respect Telegram global flood limits
        if (i + BATCH_SIZE) < total:
            await asyncio.sleep(BATCH_DELAY)

    # Final summary message
    try:
        await status_message.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"📤 Successfully Sent: <code>{success}</code>\n"
            f"🚫 Blocked / Deleted: <code>{blocked}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            f"👥 Total Targeted: <code>{total}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^broadcast_"))
async def broadcast_callback(client, callback_query):
    action = callback_query.data.split("_")[1]
    msg_id = callback_query.message.id if callback_query.message else None
    
    if action == "cancel":
        try:
            await callback_query.answer("Broadcast cancelled")
        except Exception:
            pass
        try:
            await callback_query.message.delete()
        except Exception:
            pass
        if hasattr(app, "broadcast_sessions") and msg_id:
            app.broadcast_sessions.pop(msg_id, None)
        if hasattr(app, "broadcast_data"):
            delattr(app, "broadcast_data")
        return
    
    if action == "confirm":
        user_id = callback_query.from_user.id if callback_query.from_user else 0
        if user_id not in ADMIN_IDS:
            try:
                await callback_query.answer("⛔ Admins only!", show_alert=True)
            except Exception:
                pass
            return
        
        broadcast_sessions = getattr(app, "broadcast_sessions", {})
        session = broadcast_sessions.pop(msg_id, None) if msg_id else None
        if not session and hasattr(app, "broadcast_data"):
            session = getattr(app, "broadcast_data", None)
            if session and session.get("confirm_msg_id") == msg_id:
                delattr(app, "broadcast_data")
            elif not session or "content" not in session:
                session = None

        if not session or not session.get("content"):
            try:
                await callback_query.answer("⚠️ Broadcast session expired or not found.", show_alert=True)
            except Exception:
                pass
            try:
                await callback_query.message.delete()
            except Exception:
                pass
            return
        
        try:
            await callback_query.answer("🚀 Broadcasting started...")
        except Exception:
            pass
        
        content = session.get("content")
        send_method = session.get("send_method")
        
        try:
            status_msg = await callback_query.message.edit_text("🔄 Initializing broadcast batches...")
        except Exception:
            status_msg = callback_query.message
        
        # Launch broadcast in background task to avoid blocking callback loop
        asyncio.create_task(
            execute_broadcast(client, status_msg, content, send_method)
        )


# ================= RUN ================= #

if __name__ == "__main__":
    logger.info("🤖 Anime Search Bot started with MongoDB + Force Sub + Broadcast + Group Support")
    app.run()



