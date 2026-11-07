import logging
from logging.handlers import RotatingFileHandler
import json
from html import escape
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode, ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated
import requests
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone

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

async def send_anime_result(message, anime):
    image_url = get_image_url(anime)
    info_text = format_anime_info(anime)
    
    keyboard = []
    post_url = get_post_url(anime)
    if post_url:
        keyboard.append([InlineKeyboardButton("🔗 View Full Post", url=post_url)])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    if image_url:
        try:
            logger.info(f"Sending image: {image_url}")
            await message.reply_photo(
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
    
    await message.reply_text(
        info_text,
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
            "• <code>/stats</code> — Bot statistics\n"
            "• <code>/broadcast</code> — Broadcast announcement"
        )
    
    text = (
        f"👋 <b>Hello {first_name}!</b>\n\n"
        f"Welcome to <b>{escape(BOT_NAME)}</b> 🎬\n\n"
        "🔍 <b>How to search anime:</b>\n"
        "• Send anime title directly in chat (e.g. <code>Naruto</code>)\n"
        "• Or use command: <code>/anime One Piece</code>\n\n"
        "⚡ <b>Commands:</b>\n"
        "• <code>/anime &lt;name&gt;</code> — Search anime database\n"
        f"• <code>/help</code> — Guide & features{admin_commands}"
    )
    
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_start_keyboard())


# ================= SEARCH HANDLERS (with force sub) ================= #

async def handle_search(message, query):
    # Force subscribe check
   # if not await check_force_sub(app, message):
    #    return
    
    # Save user activity
    '''
    await save_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )
    '''
    result = await asyncio.to_thread(search_anime, query)
    
    if not result or "results" not in result or not result["results"]:
        await message.reply_text(f"❌ No results found for '{query}'.")
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
    
    text = f"🔍 Found {len(results)} results for '{query}':\n\nSelect one:"
    keyboard = []
    
    for i, anime in enumerate(results[:10]):
        title = anime.get("title", "Unknown")
        anime_type = anime.get("type", "")
        label = f"[{anime_type}] {title}"
    
        if len(label) > 40:
            label = label[:37] + "..."
    
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"select_{i}")]
        )
    
    selection_msg = await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    app.search_results[selection_msg.id] = results
    
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
        "search", "stats", "broadcast"
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

@app.on_callback_query()
async def callback_handler(client, callback_query):
    data = callback_query.data

    if data.startswith("select_"):
        index = int(data.split("_")[1])
    
        results = app.search_results.get(
            callback_query.message.id,
            []
        )
    
        if 0 <= index < len(results):
    
            await callback_query.answer("Loading...")
    
            try:
                await callback_query.message.delete()
            except Exception:
                pass
    
            app.search_results.pop(
                callback_query.message.id,
                None
            )
    
            app.selection_messages.pop(
                callback_query.message.id,
                None
            )
    
            await send_anime_result(
                callback_query.message,
                results[index]
            )
    
        else:
            await callback_query.answer(
                "This menu expired.",
                show_alert=True
            )



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
            "• <code>/stats</code> — Bot statistics\n"
            "• <code>/broadcast</code> — Broadcast announcement"
        )
    
    text = (
        f"<b>🤖 How to use {escape(BOT_NAME)}:</b>\n\n"
        "• Type any anime title directly in private chat\n"
        "• Or use command: <code>/anime &lt;title&gt;</code> in groups\n\n"
        "✨ <b>Features:</b>\n"
        "🖼 Poster + English & Japanese titles + Synopsis\n"
        "🔗 Direct post links & download sources\n"
        "⚡ Fast interactive selection menu"
        f"{join_info}{admin_section}"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_start_keyboard())


# ================= STATS COMMAND (ADMIN ONLY) ================= #

@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def stats_command(client, message):
    total_users = await get_user_count()
    
    # Basic stats
    text = (
        "<b>📊 Bot Statistics</b>\n\n"
        f"👥 Total Users: <code>{total_users}</code>\n"
        f"🤖 Bot Name: <code>{(await app.get_me()).first_name}</code>\n"
        f"📅 Uptime: <code>Running</code>\n\n"
        f"🔗 Channel: {FORCE_SUB_CHANNEL}\n"
        f"👑 Admins: <code>{len(ADMIN_IDS)}</code>"
    )
    
    await message.reply_text(text, parse_mode=ParseMode.HTML)


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
    
    # Store broadcast data temporarily
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


@app.on_callback_query(filters.regex("^broadcast_"))
async def broadcast_callback(client, callback_query):
    action = callback_query.data.split("_")[1]
    
    if action == "cancel":
        await callback_query.answer("Broadcast cancelled")
        await callback_query.message.delete()
        if hasattr(app, "broadcast_data"):
            delattr(app, "broadcast_data")
        return
    
    if action == "confirm":
        if callback_query.from_user.id not in ADMIN_IDS:
            await callback_query.answer("⛔ Admins only!", show_alert=True)
            return
        
        await callback_query.answer("🚀 Broadcasting started...")
        status_msg = await callback_query.message.edit_text("🔄 Initializing broadcast batches...")
        
        broadcast_data = getattr(app, "broadcast_data", {})
        content = broadcast_data.get("content")
        send_method = broadcast_data.get("send_method")
        
        # Clear temp data
        if hasattr(app, "broadcast_data"):
            delattr(app, "broadcast_data")
        
        # Launch broadcast in background task to avoid blocking callback loop
        asyncio.create_task(
            execute_broadcast(client, status_msg, content, send_method)
        )


# ================= RUN ================= #

if __name__ == "__main__":
    logger.info("🤖 Anime Search Bot started with MongoDB + Force Sub + Broadcast + Group Support")
    app.run()



