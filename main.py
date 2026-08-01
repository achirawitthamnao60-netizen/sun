import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import csv
import time
import re
import random
import json
import asyncio
import aiohttp
import html as html_lib
import io
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone, time as dtime
from groq import Groq

load_dotenv()

# ===== PERSISTENT CONFIG =====
CONFIG_FILE = "bot_config.json"

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

_cfg = load_config()

# ===== CONFIG =====
OWNER_IDS = [1418784476196110359]
SYSTEM_ENABLED = True
LOG_CHANNEL_ID: int | None = _cfg.get("log_channel_id")

# AI_CHANNELS: { channel_id: "th" | "en" | "study" }
_ai_channels_cfg = _cfg.get("ai_channels")
if _ai_channels_cfg is not None:
    AI_CHANNELS: dict[int, str] = {int(k): v for k, v in _ai_channels_cfg.items()}
else:
    _legacy_ai_channel = _cfg.get("ai_channel_id")
    AI_CHANNELS: dict[int, str] = {_legacy_ai_channel: "en"} if _legacy_ai_channel else {}

ai_history = {}

TOKEN = os.getenv("DISCORD_TOKEN")
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

GROQ_API_KEY_EN = os.getenv("GROQ_API_KEY_EN") or os.getenv("GROQ_API_KEY")
groq_client_en = Groq(api_key=GROQ_API_KEY_EN)

GROQ_API_KEY_STUDY = os.getenv("GROQ_API_KEY_STUDY") or os.getenv("GROQ_API_KEY")
groq_client_study = Groq(api_key=GROQ_API_KEY_STUDY)

# ===== CHARACTER DATA =====
CHARACTERS_FILE = "characters.json"
pending_character: set = set()

def load_characters() -> dict:
    if os.path.exists(CHARACTERS_FILE):
        with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_characters(data: dict):
    with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

characters_data: dict = load_characters()


def get_character_name(user_id: int, fallback_name: str) -> str:
    """Return the character name a user registered via /setcharacter, falling
    back to their Discord display name if they never registered one.

    BUGFIX: characters_data was being loaded/saved by CharacterModal but the
    AI chat handler in on_message ignored it completely and always used
    message.author.display_name instead, so the whole character-registration
    feature had no effect on anything. This helper is now actually used
    wherever the "character name" is needed.
    """
    entry = characters_data.get(str(user_id))
    if entry and entry.get("character_name"):
        return entry["character_name"]
    return fallback_name

# ===== VOICE SESSION TRACKING =====
VOICE_TIME_FILE = "voice_time.json"

def load_voice_time() -> dict:
    if os.path.exists(VOICE_TIME_FILE):
        with open(VOICE_TIME_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_voice_time(data: dict):
    with open(VOICE_TIME_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

voice_time_data: dict = load_voice_time()
active_voice_sessions: dict = {}

def add_voice_time(user_id: int, seconds: float):
    key = str(user_id)
    voice_time_data[key] = voice_time_data.get(key, 0) + seconds
    save_voice_time(voice_time_data)

def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ชม. {m} นาที"
    if m:
        return f"{m} นาที {s} วิ"
    return f"{s} วิ"

# ===== BOT VOICE AFK TIMER =====
voice_leave_tasks: dict[int, asyncio.Task] = {}


async def _scheduled_leave(guild: discord.Guild, minutes: int):
    try:
        await asyncio.sleep(minutes * 60)
        if guild.voice_client:
            await guild.voice_client.disconnect(force=True)
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"⏰ บอทออกจากวอยซ์อัตโนมัติ (ครบ {minutes} นาที) ที่ {guild.name}"
                )
    except asyncio.CancelledError:
        pass
    finally:
        voice_leave_tasks.pop(guild.id, None)


# ===== AUTO-KICK OTHER BOTS WHEN VOICE CHANNEL HAS NO HUMANS =====
async def kick_bots_if_no_humans(channel: discord.VoiceChannel):
    if not channel or not channel.members:
        return

    has_human = any(not m.bot for m in channel.members)
    if has_human:
        return

    me = channel.guild.me
    if me is None or not channel.permissions_for(me).move_members:
        return

    for m in list(channel.members):
        if m.bot and m.id != bot.user.id:
            try:
                await m.move_to(None, reason="ห้องเหลือแต่บอท ไม่มีผู้ใช้จริง")
                if LOG_CHANNEL_ID:
                    log_channel = bot.get_channel(LOG_CHANNEL_ID)
                    if log_channel:
                        await log_channel.send(
                            f"🤖 เตะบอทออกจากวอยซ์ (ห้องเหลือแต่บอท ไม่มีคนจริง)\n"
                            f"👤 {m.mention}\n📍 {channel.name}"
                        )
            except Exception as e:
                print(f"เตะบอทไม่สำเร็จ ({m}): {e}")


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)


bad_words = [
    "ควย",
    "เหี้ย",
    "สันดาน",
    "หรรม",
    "หำ",
    "กระจอก",
    "สัส",
    "เย็ด",
    "มึง",
    "เงี่ยน",
    "เง่น",
    "เชี้ย",
    "กระหรี่",
    "ขี้แพ้",
    "ไร้ค่า",
    "https://media.discordapp.net/attachments/1449249700292071587/1532771548086272171/image.jpg?ex=6a6e103e&is=6a6cbebe&hm=55ccd69e86700dadf09630e4cba8823afe5890a93ab31fad696d34250f0e1c78&=&format=webp&width=525&height=676",
]

single_bad_words = [
    "หี",
    "https://cdn.discordapp.com/attachments/1351250205906829392/1390331443304992798/lowquality1732881058060.gif",
    "โง่",
]


def normalize(text):
    return re.sub(r"\s+", "", text).lower()


def contains_bad_word(raw_text: str) -> tuple[bool, str | None]:
    """Check `raw_text` against both the single-word exact-match list and the
    substring bad_words list. Returns (matched, matched_word_or_None) so
    callers (on_message and on_message_edit) share exactly one place that
    defines what counts as a bad word, instead of duplicating/drifting logic.
    """
    if raw_text.strip() in single_bad_words:
        return True, raw_text.strip()
    clean = normalize(raw_text)
    for bad in bad_words:
        if bad in clean:
            return True, bad
    return False, None


# ===== GAMBLING / BETTING SITE FILTER =====
# Blocks messages and file attachments that promote or link to online
# gambling/betting sites (casino, sportsbook, "bonus code", "rakeback",
# etc. — the kind of content shown in promo screenshots people paste from
# sites like wesobet.com). Two layers:
#   1) GAMBLING_DOMAIN_RE — catches known gambling-site URL patterns
#      (bet/casino/wager/slots domains) wherever they appear in text,
#      including inside uploaded text files.
#   2) GAMBLING_PHRASE_RE — catches the promo-page phrasing itself
#      (e.g. "activate code for bonus", "rakeback", "vip-club", deposit/
#      withdraw bonus language) so a pasted screenshot transcript or copied
#      page text gets caught even without a raw URL.
GAMBLING_DOMAIN_RE = re.compile(
    r"(https?://)?(www\.)?[a-z0-9-]*(bet|casino|slot|wager|poker|jackpot)[a-z0-9-]*\.(com|net|io|bet|casino|xyz|vip)\b",
    re.IGNORECASE,
)

GAMBLING_PHRASE_RE = re.compile(
    r"(activate\s+code\s+for\s+bonus|rakeback|vip[\s-]?club|promo\s*code|"
    r"deposit\s+bonus|withdraw(al)?\s+bonus|free\s*spins?|exclusive\s+reward"
    r")",
    re.IGNORECASE,
)


def contains_gambling_content(text: str) -> tuple[bool, str | None]:
    """Return (matched, reason) if `text` looks like it's promoting/linking a
    gambling or betting site. Used for both message content and the text
    content of file attachments."""
    if not text:
        return False, None
    m = GAMBLING_DOMAIN_RE.search(text)
    if m:
        return True, m.group(0)
    m = GAMBLING_PHRASE_RE.search(text)
    if m:
        return True, m.group(0)
    return False, None


# File extensions we're willing to download and scan the text content of.
# (Anything not on this list — images, videos, zips, exe, etc. — is not
# text-scanned here; only its filename/URL is checked via the domain regex.)
GAMBLING_SCAN_TEXT_EXTS = (
    ".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".log", ".yaml", ".yml",
)


async def scan_attachments_for_gambling(message: discord.Message) -> tuple[bool, str | None]:
    """Check a message's attachments for gambling-site content. Checks the
    filename/URL of every attachment, and additionally downloads and scans
    the text content of any attachment with a text-like extension. Returns
    (matched, reason)."""
    for att in message.attachments:
        matched, reason = contains_gambling_content(att.filename)
        if matched:
            return True, reason
        matched, reason = contains_gambling_content(att.url)
        if matched:
            return True, reason

        fname = att.filename.lower()
        if fname.endswith(GAMBLING_SCAN_TEXT_EXTS):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(att.url) as r:
                        file_text = await r.text()
            except Exception:
                continue
            matched, reason = contains_gambling_content(file_text)
            if matched:
                return True, reason
    return False, None


# ===== LANGUAGE DETECTION (สำหรับตอน @ บอทนอกห้อง AI) =====
# Thai script lives in the U+0E00–U+0E7F unicode block, so a simple presence
# check is enough to tell "user typed Thai" from "user typed English/other"
# without needing a heavy language-detection library.
THAI_CHAR_RE = re.compile(r"[\u0E00-\u0E7F]")


def detect_reply_lang(text: str) -> str:
    """Pick which AI persona ('th' or 'en') to reply with, based on the
    language of `text`. If the text contains any Thai characters we treat it
    as Thai; otherwise (English or any other language) we default to the
    English persona."""
    return "th" if THAI_CHAR_RE.search(text or "") else "en"


# ===== VIOLATION LOG =====
VIOLATION_LOG_FILE = "violations.csv"


def log_violation(violation_type, author, channel, content):
    file_exists = os.path.isfile(VIOLATION_LOG_FILE)
    with open(VIOLATION_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "timestamp",
                    "type",
                    "author",
                    "author_id",
                    "channel",
                    "channel_id",
                    "content",
                ]
            )
        writer.writerow(
            [
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                violation_type,
                str(author),
                author.id,
                str(channel),
                channel.id,
                content,
            ]
        )


# ===== DISCORD LINK PATTERN (บล็อกลิงค์เซิฟเวอร์ Discord ยกเว้น invite และ gif/cdn) =====
# BUGFIX: the old pattern `discord\.(gg|com|io)/(?!invite)` only exempted URLs
# whose path literally started with the word "invite" (i.e. discord.com/invite/CODE).
# Real discord.gg invite links are discord.gg/CODE — the invite code sits
# right after the slash, not the word "invite" — so every legitimate discord.gg
# invite was being matched and deleted despite the "ยกเว้น invite" comment.
# Fixed: discord.gg/<code> (the actual invite domain) is always allowed, and
# discord.com / discord.io are only allowed under an explicit /invite/ path.
discord_link_pattern = re.compile(
    r"(https?://)?(www\.)?discord\.(com|io)/(?!invite/)", re.IGNORECASE
)

# ===== MESSAGE SPAM =====
message_spam = {}
MSG_SPAM_LIMIT = 5
MSG_SPAM_SECS = 5

# ===== REPEAT MESSAGE =====
message_repeat = {}
MSG_REPEAT_LIMIT = 5
MSG_REPEAT_SECS = 10

# ===== VOICE =====
voice_toggle_spam = {}
voice_join_spam = {}
last_timeout = {}


async def do_timeout(member, seconds, reason):
    if member.id in OWNER_IDS:
        return

    now = time.time()
    if member.id in last_timeout and now - last_timeout[member.id] < 10:
        return
    last_timeout[member.id] = now

    try:
        await member.timeout(
            discord.utils.utcnow() + timedelta(seconds=seconds), reason=reason
        )
        if LOG_CHANNEL_ID:
            channel = bot.get_channel(LOG_CHANNEL_ID)
            if channel:
                await channel.send(
                    f"📢 Timeout\n👤 {member.mention}\n⏳ {seconds}s\n📄 {reason}"
                )
    except Exception as e:
        print(e)


def check_spam(data, user_id, limit=5, sec=5):
    now = time.time()
    data.setdefault(user_id, []).append(now)
    data[user_id] = [t for t in data[user_id] if now - t < sec]
    return len(data[user_id]) >= limit


# ===== PERIODIC CLEANUP OF IN-MEMORY SPAM-TRACKING DICTS =====
async def cleanup_spam_dicts():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(600)
        now = time.time()

        for uid in list(message_spam.keys()):
            message_spam[uid] = [(t, m) for t, m in message_spam[uid] if now - t < MSG_SPAM_SECS]
            if not message_spam[uid]:
                message_spam.pop(uid, None)

        for uid in list(message_repeat.keys()):
            rep = message_repeat[uid]
            rep["msgs"] = [(t, m) for t, m in rep["msgs"] if now - t < MSG_REPEAT_SECS]
            if not rep["msgs"]:
                message_repeat.pop(uid, None)

        for uid in list(voice_toggle_spam.keys()):
            voice_toggle_spam[uid] = [t for t in voice_toggle_spam[uid] if now - t < 5]
            if not voice_toggle_spam[uid]:
                voice_toggle_spam.pop(uid, None)

        for uid in list(voice_join_spam.keys()):
            voice_join_spam[uid] = [t for t in voice_join_spam[uid] if now - t < 7]
            if not voice_join_spam[uid]:
                voice_join_spam.pop(uid, None)

        for uid in list(last_timeout.keys()):
            if now - last_timeout[uid] > 3600:
                last_timeout.pop(uid, None)


THAILAND_TZ = timezone(timedelta(hours=7))

DAILY_STUDY_TOPICS = [
    "daily life", "travel", "food", "work", "feelings", "weather",
    "hobbies", "shopping", "friendship", "health", "technology", "school",
]

# ─── PHP-only topics (ใช้กับช่อง PHP Tutor) ──────────────────────────────
DAILY_PHP_TOPICS = [
    # PHP พื้นฐาน
    ("PHP พื้นฐาน", "variables, data types, and echo output in PHP"),
    ("PHP พื้นฐาน", "if/else conditions in PHP"),
    ("PHP พื้นฐาน", "for loop and while loop in PHP"),
    ("PHP พื้นฐาน", "PHP functions: defining and calling"),
    ("PHP พื้นฐาน", "PHP arrays: indexed, associative, and foreach loop"),
    ("PHP พื้นฐาน", "string functions: strlen, strtoupper, str_replace, substr"),
    ("PHP พื้นฐาน", "math functions: round, ceil, floor, rand"),
    ("PHP พื้นฐาน", "date and time functions in PHP"),
    ("PHP พื้นฐาน", "PHP include and require for reusing code"),
    ("PHP พื้นฐาน", "PHP superglobals: $_GET, $_POST, $_SESSION, $_COOKIE"),
    # PHP + HTML ฟอร์ม
    ("PHP + HTML ฟอร์ม", "HTML form with PHP $_POST: text input and submit"),
    ("PHP + HTML ฟอร์ม", "HTML form validation with PHP: required fields check"),
    ("PHP + HTML ฟอร์ม", "HTML select dropdown with PHP processing"),
    ("PHP + HTML ฟอร์ม", "HTML checkbox and radio button with PHP"),
    ("PHP + HTML ฟอร์ม", "file upload form with PHP move_uploaded_file"),
    ("PHP + HTML ฟอร์ม", "PHP form with password confirmation check"),
    ("PHP + HTML ฟอร์ม", "search form with PHP filtering results"),
    # PHP + HTML เพจ
    ("PHP + HTML เพจ", "PHP header.php and footer.php template with include"),
    ("PHP + HTML เพจ", "PHP dynamic page title and meta tags using variables"),
    ("PHP + HTML เพจ", "PHP navigation menu with active page highlight"),
    ("PHP + HTML เพจ", "PHP if/else to show/hide HTML sections based on condition"),
    ("PHP + HTML เพจ", "PHP foreach to generate HTML table rows from an array"),
    ("PHP + HTML เพจ", "PHP session login check: redirect if not logged in"),
    ("PHP + HTML เพจ", "PHP echo to output dynamic HTML cards or list items"),
    # PHP ขั้นกลาง
    ("PHP ขั้นกลาง", "PHP OOP basics: class, object, properties, methods"),
    ("PHP ขั้นกลาง", "PHP JSON: json_encode and json_decode"),
    ("PHP ขั้นกลาง", "PHP error handling with try/catch"),
    ("PHP ขั้นกลาง", "PHP regular expressions with preg_match"),
    ("PHP ขั้นกลาง", "PHP session and cookie: set, read, and destroy"),
    ("PHP ขั้นกลาง", "PHP PDO: connect to MySQL and run a SELECT query"),
    ("PHP ขั้นกลาง", "PHP PDO: INSERT data from a form into a database"),
]

# ─── HTML + CSS + JS topics (ใช้กับช่อง Web Tutor) ───────────────────────
DAILY_WEB_TOPICS = [
    # HTML โครงสร้าง
    ("HTML พื้นฐาน", "HTML page structure: doctype, head, body, and semantic tags"),
    ("HTML พื้นฐาน", "HTML headings, paragraphs, links, and images"),
    ("HTML พื้นฐาน", "HTML lists: ul, ol, and nested lists"),
    ("HTML พื้นฐาน", "HTML table: thead, tbody, tr, th, td"),
    ("HTML ฟอร์ม", "HTML form: input types text, email, password, number"),
    ("HTML ฟอร์ม", "HTML form: select dropdown, checkbox, radio button"),
    ("HTML ฟอร์ม", "HTML form: textarea and submit button"),
    ("HTML Semantic", "HTML semantic tags: header, nav, main, section, article, footer"),
    ("HTML Semantic", "HTML figure, figcaption, aside, and their use cases"),
    # CSS พื้นฐาน
    ("CSS พื้นฐาน", "CSS selectors: class, id, element, and combining them"),
    ("CSS พื้นฐาน", "CSS box model: margin, border, padding, width, height"),
    ("CSS พื้นฐาน", "CSS text styling: font-size, color, font-weight, text-align"),
    ("CSS พื้นฐาน", "CSS background: color, image, size, position"),
    ("CSS พื้นฐาน", "CSS display: block, inline, inline-block, none"),
    ("CSS Flexbox", "CSS Flexbox: flex container, flex-direction, justify-content, align-items"),
    ("CSS Flexbox", "CSS Flexbox: flex-wrap, gap, and align-self"),
    ("CSS Grid", "CSS Grid: grid-template-columns, grid-template-rows, gap"),
    ("CSS Grid", "CSS Grid: placing items with grid-column and grid-row"),
    ("CSS ขั้นกลาง", "CSS pseudo-classes: :hover, :focus, :nth-child"),
    ("CSS ขั้นกลาง", "CSS transitions and transform: scale, rotate, translate"),
    ("CSS ขั้นกลาง", "CSS media queries for responsive design (mobile-first)"),
    ("CSS ขั้นกลาง", "CSS custom properties: --var-name and var()"),
    ("CSS ขั้นกลาง", "CSS position: relative, absolute, fixed, sticky"),
    ("CSS ขั้นกลาง", "CSS keyframe animations with @keyframes"),
    # JavaScript พื้นฐาน
    ("JavaScript พื้นฐาน", "JS variables: var, let, const and when to use each"),
    ("JavaScript พื้นฐาน", "JS functions: declaration, expression, and arrow functions"),
    ("JavaScript พื้นฐาน", "JS if/else, switch, and ternary operator"),
    ("JavaScript พื้นฐาน", "JS arrays: push, pop, map, filter, forEach"),
    ("JavaScript พื้นฐาน", "JS objects: creating, reading, and updating properties"),
    ("JavaScript พื้นฐาน", "JS string methods: split, join, includes, template literals"),
    ("JavaScript + DOM", "JS DOM: getElementById, querySelector, innerHTML"),
    ("JavaScript + DOM", "JS DOM: addEventListener for click, input, submit events"),
    ("JavaScript + DOM", "JS DOM: show/hide elements with classList and style"),
    ("JavaScript + DOM", "JS DOM: reading and validating form input values"),
    ("JavaScript + DOM", "JS DOM: creating and appending new elements dynamically"),
    ("JavaScript ขั้นกลาง", "JS fetch API: GET request and displaying JSON data"),
    ("JavaScript ขั้นกลาง", "JS localStorage: setItem, getItem, removeItem"),
    ("JavaScript ขั้นกลาง", "JS setTimeout and setInterval for timing"),
    ("JavaScript ขั้นกลาง", "JS async/await with fetch for simple API calls"),
    # HTML + CSS + JS รวม
    ("HTML+CSS+JS รวม", "build a card component with HTML structure, CSS styling, and JS hover effect"),
    ("HTML+CSS+JS รวม", "build a simple to-do list with HTML form, CSS layout, and JS add/remove"),
    ("HTML+CSS+JS รวม", "build a responsive navbar with HTML, CSS flexbox, and JS hamburger menu"),
    ("HTML+CSS+JS รวม", "build a modal popup with HTML, CSS transitions, and JS open/close"),
    ("HTML+CSS+JS รวม", "build a countdown timer with HTML display, CSS style, and JS setInterval"),
]


async def generate_daily_study_text() -> tuple[str, str, str]:
    topic = random.choice(DAILY_STUDY_TOPICS)
    prompt = (
        f"Write ONE natural, useful English sentence (about {topic}) for a Thai "
        "beginner-to-intermediate English learner to study today. Then give its Thai "
        "translation, and one short Thai-language tip about a useful word or grammar "
        "point in the sentence.\n"
        "Respond with ONLY valid JSON, no markdown fences, in exactly this shape:\n"
        '{"english": "...", "thai": "...", "tip": "..."}'
    )
    resp = await asyncio.to_thread(
        groq_client_study.chat.completions.create,
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
    )
    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
        return (
            data.get("english", "").strip(),
            data.get("thai", "").strip(),
            data.get("tip", "").strip(),
        )
    except Exception:
        return raw, "", ""


_CODE_EXTS = {
    "php": "php", "html": "html", "css": "css",
    "js": "js", "javascript": "js", "python": "py", "py": "py",
    "sql": "sql", "json": "json", "bash": "sh", "sh": "sh",
    "xml": "xml", "ts": "ts", "typescript": "ts",
}


def _extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Return list of (lang, code) for every fenced code block in text."""
    return re.findall(r"```([a-zA-Z]*)\n([\s\S]*?)```", text)


def _strip_code_blocks(text: str) -> str:
    """Remove all fenced code blocks, collapse excess blank lines."""
    stripped = re.sub(r"```[a-zA-Z]*\n[\s\S]*?```", "", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped


async def _reply_with_code_files(message: discord.Message, reply: str) -> None:
    """Send reply text + all code blocks merged into a single index.php file."""
    blocks = _extract_code_blocks(reply)
    text_only = _strip_code_blocks(reply)

    if not blocks:
        for chunk in _smart_split(reply, 1900):
            await message.reply(chunk)
        return

    # --- merge all blocks into one index.php ---
    _SECTION_HEADERS = {
        "php": "<?php",
        "html": "<!-- HTML -->",
        "css": "/* CSS */",
        "js": "// JavaScript",
        "javascript": "// JavaScript",
        "sql": "-- SQL",
    }
    parts: list[str] = []
    for lang_tag, code in blocks:
        lang = lang_tag.lower()
        header = _SECTION_HEADERS.get(lang, f"// {lang_tag or 'code'}")
        parts.append(f"{header}\n{code.strip()}")

    merged = "\n\n".join(parts)
    file = discord.File(io.BytesIO(merged.encode()), filename="index.php")

    # --- send explanation text first (if any) ---
    if text_only:
        for chunk in _smart_split(text_only, 1900):
            await message.reply(chunk)
        await message.channel.send("📎 โค้ดทั้งหมดอยู่ในไฟล์ `index.php` ด้านล่างนี้เลย!", file=file)
    else:
        await message.reply("📎 โค้ดทั้งหมดอยู่ในไฟล์ `index.php` ด้านล่างนี้เลย!", file=file)


def _smart_split(text: str, limit: int = 1900) -> list[str]:
    """Split a Discord reply at safe boundaries, never cutting inside a code block."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # Try to find a safe cut point within `limit` characters
        window = text[:limit]

        # Count ``` fences in the window — if odd we're inside a code block
        fence_count = window.count("```")

        if fence_count % 2 == 0:
            # We're outside a code block — cut at the last newline
            cut = window.rfind("\n")
            if cut == -1:
                cut = limit
        else:
            # We're inside a code block — find the closing ``` before the limit
            # and cut right after it so the block stays intact
            closing = window.rfind("```", 3)  # skip the opening fence
            if closing != -1:
                cut = closing + 3  # include the closing ```
                # consume trailing newline if present
                if cut < len(text) and text[cut] == "\n":
                    cut += 1
            else:
                # closing fence not in window — shrink until we find it beyond limit
                closing_beyond = text.find("```", limit)
                if closing_beyond != -1:
                    cut = closing_beyond + 3
                    if cut < len(text) and text[cut] == "\n":
                        cut += 1
                else:
                    cut = limit  # give up, cut hard

        chunks.append(text[:cut])
        text = text[cut:]

    return chunks


def _extract_json_php(raw: str) -> dict:
    """Try multiple strategies to extract a JSON object from the AI response."""
    # 1. strip markdown fences
    cleaned = re.sub(r"```[a-z]*", "", raw, flags=re.IGNORECASE).strip().rstrip("`").strip()
    # 2. try direct parse
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # 3. find the first {...} block
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # 4. nothing worked
    raise ValueError("cannot parse JSON from AI response")


def _fence_lang_for(category: str) -> str:
    """Pick a Discord code-fence language based on the topic category."""
    c = category.lower()
    if "css" in c:
        return "css"
    if "javascript" in c or " js" in c:
        return "js"
    if "html" in c and "php" not in c:
        return "html"
    return "php"


_DAILY_SECTION_PROMPT_TAIL = (
    "Reply in this EXACT format. Do not add any text outside these four sections:\n\n"
    "##TOPIC##\n"
    "<short Thai title>\n\n"
    "##CODE##\n"
    "<actual code, no markdown fences>\n\n"
    "##EXPLAIN##\n"
    "<2-4 Thai sentences: what each part does>\n\n"
    "##USAGE##\n"
    "<1-2 Thai sentences: when/where to use this in a real website>"
)


async def _run_daily_generator(topic_list: list, tutor_desc: str, fallback_fence: str) -> tuple[str, str, str, str, str]:
    """Shared generator for daily code posts. Returns (display_topic, code, explain, usage, fence_lang)."""
    category, detail = random.choice(topic_list)
    prompt = (
        f"{tutor_desc}\n"
        f"Topic: {detail}\n\n"
        "Write a beginner-friendly code example (8-20 lines total, no frameworks).\n"
        "- Add short Thai comments on important lines.\n"
        "- Do NOT escape < > & — write real characters.\n"
        "- Do NOT wrap code in markdown fences or JSON.\n\n"
        + _DAILY_SECTION_PROMPT_TAIL
    )
    for _ in range(2):
        resp = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()

        def _section(tag: str) -> str:
            m = re.search(rf"##{tag}##\s*([\s\S]*?)(?=##\w+##|$)", raw)
            return m.group(1).strip() if m else ""

        topic_txt  = _section("TOPIC")
        code_txt   = _section("CODE")
        explain_txt = _section("EXPLAIN")
        usage_txt  = _section("USAGE")

        code_txt = re.sub(r"^```[a-z]*\n?|```$", "", code_txt, flags=re.MULTILINE).strip()
        code_txt   = html_lib.unescape(code_txt)
        explain_txt = html_lib.unescape(explain_txt)

        if code_txt:
            display_topic = f"{category} — {topic_txt}" if topic_txt else category
            return display_topic, code_txt, explain_txt, usage_txt, _fence_lang_for(category)

    return category, f"// ไม่สามารถสร้างโค้ดได้ในขณะนี้ ลองใหม่อีกครั้งภายหลัง", "", "", fallback_fence


async def generate_daily_php_text() -> tuple[str, str, str, str, str]:
    """PHP-only daily post. Returns (display_topic, code, explanation, usage_tip, fence_lang)."""
    return await _run_daily_generator(
        DAILY_PHP_TOPICS,
        "You are a PHP tutor for Thai beginners. Topics: PHP syntax, HTML forms with PHP, PHP+HTML pages, OOP, PDO/MySQL.\n"
        "- PHP with HTML output: mix <?php ?> tags inside an HTML document.\n"
        "- Pure PHP logic: use <?php ?> only.",
        "php",
    )


async def generate_daily_web_text() -> tuple[str, str, str, str, str]:
    """HTML+CSS+JS daily post. Returns (display_topic, code, explanation, usage_tip, fence_lang)."""
    return await _run_daily_generator(
        DAILY_WEB_TOPICS,
        "You are an HTML/CSS/JavaScript tutor for Thai beginners. Topics: HTML structure, CSS styling, JavaScript/DOM.\n"
        "- CSS topic: write a .css block + matching HTML snippet.\n"
        "- JS topic: write a <script> block + small HTML example.\n"
        "- Mixed topic: show HTML, CSS, and JS parts separately with clear labels.",
        "html",
    )


@tasks.loop(time=[dtime(hour=0, minute=0, tzinfo=THAILAND_TZ), dtime(hour=12, minute=0, tzinfo=THAILAND_TZ)])
async def daily_study_post():
    study_channel_ids = [cid for cid, lang in AI_CHANNELS.items() if lang == "study"]
    php_channel_ids   = [cid for cid, lang in AI_CHANNELS.items() if lang == "php"]
    web_channel_ids   = [cid for cid, lang in AI_CHANNELS.items() if lang == "web"]

    # ─── English study post ───────────────────────────────────────────────────
    if study_channel_ids:
        try:
            english, thai, tip = await generate_daily_study_text()
            if english:
                embed = discord.Embed(title="📚 ประโยคภาษาอังกฤษประจำวัน", color=0x5865F2)
                embed.add_field(name="💬 English", value=english, inline=False)
                if thai:
                    embed.add_field(name="🇹🇭 คำแปล", value=thai, inline=False)
                if tip:
                    embed.add_field(name="💡 Tip", value=tip, inline=False)
                embed.set_footer(text="ฝึกพูดหรือแต่งประโยคของตัวเองในห้องนี้ได้เลย!")
                for ch_id in study_channel_ids:
                    channel = bot.get_channel(ch_id)
                    if channel:
                        try:
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"daily_study_post: failed to send to channel {ch_id}: {e}")
        except Exception as e:
            print(f"daily_study_post: failed to generate English content: {e}")

    # ─── PHP daily post ───────────────────────────────────────────────────────
    if php_channel_ids:
        try:
            topic, code, explanation, usage, fence = await generate_daily_php_text()
            if code:
                embed = discord.Embed(title=f"🐘 PHP โค้ดประจำวัน — {topic}", color=0x777BB3)
                code_display = code if len(code) <= 990 else code[:990] + "\n..."
                embed.add_field(name="📝 โค้ด", value=f"```{fence}\n{code_display}\n```", inline=False)
                if explanation:
                    embed.add_field(name="📖 อธิบาย", value=explanation, inline=False)
                if usage:
                    embed.add_field(name="💡 วิธีใช้", value=usage, inline=False)
                embed.set_footer(text="ลองก๊อปโค้ดไปรันดูได้เลย! มีคำถามพิมพ์ถามในห้องนี้ได้เลย 🐘")
                for ch_id in php_channel_ids:
                    channel = bot.get_channel(ch_id)
                    if channel:
                        try:
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"daily_php_post: failed to send to channel {ch_id}: {e}")
        except Exception as e:
            print(f"daily_php_post: failed to generate PHP content: {e}")

    # ─── HTML+CSS+JS (Web) daily post ─────────────────────────────────────────
    if web_channel_ids:
        try:
            topic, code, explanation, usage, fence = await generate_daily_web_text()
            if code:
                embed = discord.Embed(title=f"🌐 Web โค้ดประจำวัน — {topic}", color=0x00B0FF)
                code_display = code if len(code) <= 990 else code[:990] + "\n..."
                embed.add_field(name="📝 โค้ด", value=f"```{fence}\n{code_display}\n```", inline=False)
                if explanation:
                    embed.add_field(name="📖 อธิบาย", value=explanation, inline=False)
                if usage:
                    embed.add_field(name="💡 วิธีใช้", value=usage, inline=False)
                embed.set_footer(text="ลองก๊อปโค้ดไปรันดูได้เลย! มีคำถามพิมพ์ถามในห้องนี้ได้เลย 🌐")
                for ch_id in web_channel_ids:
                    channel = bot.get_channel(ch_id)
                    if channel:
                        try:
                            await channel.send(embed=embed)
                        except Exception as e:
                            print(f"daily_web_post: failed to send to channel {ch_id}: {e}")
        except Exception as e:
            print(f"daily_web_post: failed to generate Web content: {e}")


@daily_study_post.before_loop
async def before_daily_study_post():
    await bot.wait_until_ready()


# ===== REACTION ROLE =====
ROLE_MAP = {
    "🔥": "🔥ไฟ",
    "🌊": "🌊น้ำ",
    "🍃": "🍃ลม",
    "⚡": "⚡สายฟ้า",
    "⚫": "⚫ความมืด",
    "🌅": "🌅แสง",
    "🏥": "🏥ซัพพอร์ต",
    "☠️": "☠️พิษ",
}

BOT_CREATED_ROLES = set()


async def get_or_create_role(guild, role_name):
    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        role = await guild.create_role(name=role_name)
        BOT_CREATED_ROLES.add(role_name)
    return role


# ===== EVENTS =====
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        print("Synced Slash Commands")
    except Exception as e:
        print(e)
    if not hasattr(bot, "_cleanup_task_started"):
        bot._cleanup_task_started = True
        bot.loop.create_task(cleanup_spam_dicts())
    if not daily_study_post.is_running():
        daily_study_post.start()
    print(f"Bot online: {bot.user}")


# ===== CHARACTER REGISTRATION UI =====
class CharacterModal(discord.ui.Modal, title="🎭 ตั้งชื่อตัวละครของคุณ"):
    char_name = discord.ui.TextInput(
        label="ชื่อตัวละคร",
        placeholder="ใส่ชื่อตัวละครที่คุณจะรับบทเป็น...",
        min_length=1,
        max_length=50,
    )
    description = discord.ui.TextInput(
        label="คำอธิบายตัวละคร (ไม่บังคับ)",
        placeholder="เช่น นักดาบจากโลกแฟนตาซี, แมวน้อยซุกซน...",
        required=False,
        max_length=150,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid_str = str(interaction.user.id)
        characters_data[uid_str] = {
            "discord_id": interaction.user.id,
            "discord_name": str(interaction.user),
            "character_name": self.char_name.value.strip(),
            "description": self.description.value.strip() if self.description.value else "",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        save_characters(characters_data)
        pending_character.discard(interaction.user.id)
        embed = discord.Embed(
            title="✅ ลงทะเบียนตัวละครสำเร็จ!",
            color=discord.Color.green(),
        )
        embed.add_field(name="ชื่อตัวละคร", value=self.char_name.value.strip(), inline=True)
        embed.add_field(name="ผู้เล่น", value=interaction.user.display_name, inline=True)
        if self.description.value:
            embed.add_field(name="คำอธิบาย", value=self.description.value.strip(), inline=False)
        embed.set_footer(text="พิมพ์ข้อความในห้องนี้เพื่อเริ่มคุยกับ AI ได้เลย!")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CharacterRegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="🎭 ตั้งชื่อตัวละคร", style=discord.ButtonStyle.primary)
    async def register_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending_character.add(interaction.user.id)
        await interaction.response.send_modal(CharacterModal())


@bot.tree.command(name="setcharacter", description="ตั้งชื่อตัวละครที่จะใช้คุยกับ AI ในห้องนี้")
async def setcharacter(interaction: discord.Interaction):
    uid_str = str(interaction.user.id)
    existing = characters_data.get(uid_str)
    embed = discord.Embed(
        title="🎭 ตั้งชื่อตัวละคร",
        description="กดปุ่มด้านล่างเพื่อตั้ง (หรือเปลี่ยน) ชื่อตัวละครที่ AI จะใช้เรียกคุณ",
        color=discord.Color.blurple(),
    )
    if existing and existing.get("character_name"):
        embed.add_field(name="ชื่อตัวละครปัจจุบัน", value=existing["character_name"], inline=False)
    await interaction.response.send_message(
        embed=embed, view=CharacterRegisterView(), ephemeral=True
    )


async def handle_ai_reply(message: discord.Message, ai_lang: str):
    """Generate and send an AI reply for `message`, using the personality/
    system prompt for `ai_lang` ("th" / "en" / "study"). Used both for
    messages sent in a designated AI channel (/setai) and for messages that
    @-mention the bot directly in any other channel.
    """
    uid = message.author.id
    uid_str = str(uid)
    char_name = get_character_name(uid, message.author.display_name)

    # Strip only the FIRST leading/inline mention of the bot itself out of
    # the text we send to the AI (and out of the has_gif/has_video "did they
    # type anything else" checks), so "@Bot what's up" doesn't confuse the
    # model with a literal <@123...> token, and a bare mention with no other
    # text is treated the same as "no text" would be in a dedicated AI
    # channel. count=1 so that if the bot is tagged again later in the same
    # message, that second (and any further) mention is left in place and
    # still reaches the AI as part of the actual content.
    content_text = message.content or ""
    if bot.user:
        content_text = re.sub(rf"<@!?{bot.user.id}>", "", content_text, count=1).strip()

    async with message.channel.typing():
        history_key = (message.channel.id, uid)
        history = ai_history.setdefault(history_key, [])

        TEXT_EXTS = ('.txt', '.py', '.js', '.ts', '.json', '.csv', '.md', '.html', '.css', '.java', '.c', '.cpp', '.xml', '.yaml', '.yml')
        IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.webp')
        VIDEO_EXTS = ('.mp4', '.mov', '.avi', '.webm', '.mkv')

        image_urls = []
        extra_text = ""
        attached_filenames = []  # just names, used to keep long-term history light
        has_video = False
        has_gif = False

        for att in message.attachments:
            fname = att.filename.lower()
            ctype = att.content_type or ""
            if fname.endswith('.gif') or ctype == "image/gif":
                has_gif = True
            elif any(fname.endswith(e) for e in IMAGE_EXTS) or (ctype.startswith("image/") and ctype != "image/gif"):
                image_urls.append(att.url)
            elif any(fname.endswith(e) for e in VIDEO_EXTS) or ctype.startswith("video/"):
                has_video = True
            elif any(fname.endswith(e) for e in TEXT_EXTS) or ctype.startswith("text/"):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(att.url) as r:
                            file_text = await r.text()
                            extra_text += f"\n\n📄 story file `{att.filename}`:\n```\n{file_text[:3000]}\n```"
                            attached_filenames.append(att.filename)
                except Exception:
                    extra_text += f"\n(can not read file{att.filename} )"

        if has_gif and not image_urls and not has_video and not extra_text and not content_text:
            if ai_lang == "th":
                await message.reply(
                    "🎞️ ซันอ่าน gif ไม่ได้นะ\n"
                    "แต่ถ้าอยากถามอะไร → **พิมพ์คำถามมาพร้อมกับไฟล์ได้เลย** ซันจะตอบตามที่พิมพ์มา 😊"
                )
            elif ai_lang == "study":
                await message.reply(
                    "🎞️ Study can't read gifs.\n"
                    "Type your sentence along with the file and I'll correct it for you! 😊\n"
                    "🇹🇭 คำแปล: พิมพ์ประโยคของคุณมาพร้อมกับไฟล์ แล้วฉันจะช่วยแก้ให้!"
                )
            elif ai_lang == "php":
                await message.reply(
                    "🎞️ อ่าน GIF ไม่ได้นะ\n"
                    "แต่ถ้ามีคำถามเกี่ยวกับ PHP → **พิมพ์ถามมาได้เลย** 🐘"
                )
            elif ai_lang == "web":
                await message.reply(
                    "🎞️ อ่าน GIF ไม่ได้นะ\n"
                    "แต่ถ้ามีคำถาม HTML/CSS/JS → **พิมพ์ถามมาได้เลย** 🌐"
                )
            else:
                await message.reply(
                    "🎞️ aienglish can't read gifs\n"
                    "But if you want to ask anything → **please type your question along with the file**. I'll answer based on what you type. 😊"
                )
            return

        if has_gif:
            extra_text += "\n[ผู้ใช้ส่ง GIF attachment มาด้วย แต่ sun อ่าน GIF ไม่ได้]"

        if has_video and not image_urls and not extra_text and not content_text:
            if ai_lang == "th":
                await message.reply(
                    "🎬 ซันอ่านวิดีโอไม่ได้นะ\n"
                    "แต่ถ้าอยากถามเกี่ยวกับวิดีโอ → **พิมพ์คำถามมาพร้อมกับไฟล์ได้เลย** ซันจะตอบตามที่พิมพ์มา 😊"
                )
            elif ai_lang == "study":
                await message.reply(
                    "🎬 Study can't watch videos.\n"
                    "Type a sentence about it along with the file and I'll correct it for you! 😊\n"
                    "🇹🇭 คำแปล: พิมพ์ประโยคเกี่ยวกับวิดีโอมาพร้อมกับไฟล์ แล้วฉันจะช่วยแก้ให้!"
                )
            elif ai_lang == "php":
                await message.reply(
                    "🎬 อ่านวิดีโอไม่ได้นะ\n"
                    "ถ้ามีโค้ด PHP ให้ดู → **วางโค้ดมาเป็นข้อความแทนได้เลย** 🐘"
                )
            elif ai_lang == "web":
                await message.reply(
                    "🎬 อ่านวิดีโอไม่ได้นะ\n"
                    "ถ้ามีโค้ด HTML/CSS/JS ให้ดู → **วางโค้ดมาเป็นข้อความแทนได้เลย** 🌐"
                )
            else:
                await message.reply(
                    "🎬 aienglish can't read videos\n"
                    "But if you want to ask about anything in the video → **type your question along with the file**. I'll answer based on what you type. 😊"
                )
            return

        custom_emoji_re = re.compile(r'<a?:(\w+):\d+>')
        found_emojis = custom_emoji_re.findall(content_text)
        emoji_note = f"\n[user use emoji: {', '.join(found_emojis)}]" if found_emojis else ""

        sticker_note = ""
        if message.stickers:
            sticker_names = [s.name for s in message.stickers]
            sticker_note = f"\n[user submit sticker: {', '.join(sticker_names)}]"

        gif_note = ""
        for embed in message.embeds:
            if embed.type == "gifv":
                gif_label = embed.title or (embed.url or "GIF")
                gif_note += f"\n[user submit GIF: {gif_label}]"

        user_text = (content_text).strip() + emoji_note + sticker_note + gif_note + extra_text
        if not user_text and not image_urls:
            return

        guild_emojis_str = ""
        guild_emoji_map: dict[str, str] = {}
        if message.guild:
            for e in message.guild.emojis:
                fmt = f"<{'a' if e.animated else ''}:{e.name}:{e.id}>"
                guild_emoji_map[e.name] = fmt
        if guild_emoji_map:
            emoji_list = list(guild_emoji_map.values())[:60]
            guild_emojis_str = f"\nThe emojis that are usable on this server (copy and paste this exact format into your answer; do not edit): {' '.join(emoji_list)}"

        if ai_lang == "th":
            SYSTEM_PROMPT = (
                "คุณชื่อ 'ซัน (sun)' คุณเป็นสัตว์เลี้ยงตัวน้อย\n"
                f"ผู้ใช้ที่คุยด้วยในดิสคอร์ดใช้ชื่อตัวละครว่า '{char_name}' — ให้เรียกชื่อนี้เท่านั้น\n"
                f"{guild_emojis_str}\n\n"
                "บุคลิก:\n"
                "- พูดด้วยน้ำเสียงน่ารัก อบอุ่น และเป็นมิตร ตอบเป็นภาษาไทยเท่านั้น\n"
                "- ใช้ custom emoji ของเซิร์ฟเวอร์ให้เหมาะสมกับคำตอบ\n"
                "- ถ้ามีคนแสดงความเศร้าหรือเครียด ให้รับฟังด้วยความเข้าใจก่อน ไม่ต้องรีบให้คำแนะนำ\n"
                "- แสดงความเป็นห่วงอย่างจริงใจ เช่น 'ซันเป็นห่วงนะ อยู่กับซันตรงนี้นะ'\n"
                "- ตอบคำถามทั่วไปอย่างตรงไปตรงมา ไม่กุเรื่องขึ้นมาเอง\n"
                "- ถ้าอ่านสิ่งที่ส่งมาไม่ได้จริง ๆ (เช่น วิดีโอ) ห้ามแกล้งทำเป็นเข้าใจ\n"
                "- ตอบสั้นกระชับ ไม่ต้องอธิบายยาวถ้าไม่จำเป็น"
            )
        elif ai_lang == "study":
            SYSTEM_PROMPT = (
                "Your name is 'Study'. You are a small, friendly pet companion whose job is "
                "to help the user practice and improve their English, while always giving a "
                "Thai translation underneath so they can study both languages together.\n"
                f"The user talking to you on Discord goes by the character name '{char_name}' — address them by this name only.\n"
                f"{guild_emojis_str}\n\n"
                "How to reply, EVERY time, using exactly this structure:\n"
                "1) If the user wrote in English and made any grammar, spelling, or word-choice "
                "mistakes, start with a line '📝 Correction:' followed by the corrected sentence. "
                "Then explain the mistake in Thai, clearly and simply: name the type of mistake "
                "(e.g. เวลา/tense, คำนำหน้านาม/article, คำศัพท์/word choice, โครงสร้างประโยค/sentence "
                "structure), give one short sentence explaining WHY it was wrong, and add one extra "
                "short example sentence showing the correct pattern used in a different context. "
                "If there is no mistake, skip this part entirely (do not say 'no mistakes').\n"
                "2) Then write your actual reply to what the user said, in simple, encouraging "
                "English, under a line '💬 Study:'.\n"
                "3) Then add a Thai translation of that same reply under a line '🇹🇭 คำแปล:' so the "
                "English and Thai always appear together like subtitles.\n"
                "4) End with a short line '✏️ ลองอีกที:' giving the user one easy follow-up prompt "
                "or question in English (with a Thai translation in parentheses) to keep them practicing.\n"
                "Personality:\n"
                "- Speak like a warm, patient, encouraging tutor — never harsh or judgmental about mistakes.\n"
                "- Explanations must be genuinely clear for a Thai beginner-to-intermediate learner: "
                "avoid English grammar jargon, explain concepts in plain, simple Thai instead.\n"
                "- Use the server's custom emoji where it fits your response.\n"
                "- If someone expresses sadness or stress, listen with understanding first instead of "
                "rushing to correct grammar or give advice.\n"
                "- Answer general questions honestly and don't make things up.\n"
                "- Don't pretend to understand something you can't actually read (e.g. a video).\n"
                "- Keep the English reply brief and at an easy level unless the user is clearly advanced.\n"
                "- If the user wrote in Thai instead of English, gently encourage them to try in "
                "English next time, but still answer following the same 💬/🇹🇭 structure above "
                "(skip the 📝 Correction and ✏️ ลองอีกที lines in that case)."
            )
        elif ai_lang == "php":
            SYSTEM_PROMPT = (
                "คุณชื่อ 'PHP Tutor' คุณเป็นครูสอน PHP ที่เป็นมิตร อธิบายเป็นภาษาไทยเป็นหลัก\n"
                f"ผู้ใช้ที่คุยด้วยชื่อว่า '{char_name}'\n"
                f"{guild_emojis_str}\n\n"
                "ขอบเขตที่สอน (PHP เป็นหลัก):\n"
                "✅ PHP พื้นฐาน: ตัวแปร, เงื่อนไข, ลูป, ฟังก์ชัน, array, string, math, date\n"
                "✅ PHP + HTML: echo ออก HTML, ฟอร์ม input/select/checkbox, validation, upload ไฟล์\n"
                "✅ Layout/Template: include header/footer, สร้างเมนู, แสดง/ซ่อน section\n"
                "✅ PHP ขั้นกลาง: OOP, JSON, regex, error handling, session, cookie\n"
                "✅ PHP + Database: MySQL/PDO, SELECT/INSERT/UPDATE/DELETE\n\n"
                "วิธีตอบ:\n"
                "1) ถ้าผู้ใช้วางโค้ดมา → อ่านโค้ด บอกว่ามันทำอะไร แนะนำจุดที่ปรับปรุงได้\n"
                "2) PHP ที่ออก HTML → เขียน PHP ผสม HTML (<?php ?> ใน HTML)\n"
                "3) PHP logic ล้วน → เขียนแบบ <?php ?> ล้วน\n"
                "4) เพิ่ม comment ภาษาไทยอธิบายบรรทัดสำคัญในโค้ดเสมอ\n"
                "5) บอกด้วยว่าโค้ดนี้ใช้ในส่วนไหนของเว็บไซต์จริง\n"
                "6) ครอบโค้ดด้วย ```php ... ``` เสมอ\n"
                "7) ใช้ emoji เซิร์ฟเวอร์ให้เหมาะสม\n"
                "8) ตอบสั้นกระชับ ไม่อธิบายยาวเกินจำเป็น — ถ้าตัวอย่างยาวให้แบ่งเป็นส่วนๆ"
            )
        elif ai_lang == "web":
            SYSTEM_PROMPT = (
                "คุณชื่อ 'Web Tutor' คุณเป็นครูสอน HTML, CSS และ JavaScript ที่เป็นมิตร อธิบายเป็นภาษาไทยเป็นหลัก\n"
                f"ผู้ใช้ที่คุยด้วยชื่อว่า '{char_name}'\n"
                f"{guild_emojis_str}\n\n"
                "ขอบเขตที่สอน (HTML + CSS + JS):\n"
                "✅ HTML: โครงสร้างหน้าเว็บ, semantic tags (header/nav/main/footer), ฟอร์ม, table, list\n"
                "✅ CSS: selectors, box model, Flexbox, Grid, responsive (media query), animation, transitions, variables\n"
                "✅ JavaScript: ตัวแปร, ฟังก์ชัน, array/object, DOM manipulation, events, fetch API, localStorage, async/await\n"
                "✅ ผสมกัน: HTML structure + CSS layout + JS interactivity ทำงานร่วมกัน\n\n"
                "วิธีตอบ:\n"
                "1) ถ้าผู้ใช้วางโค้ดมา → อ่านโค้ด บอกว่ามันทำอะไร แนะนำจุดที่ปรับปรุงได้\n"
                "2) CSS → เขียน CSS block + HTML ตัวอย่างให้ดูด้วยเสมอ\n"
                "3) JavaScript → เขียน <script> block + HTML ตัวอย่างประกอบ\n"
                "4) HTML+CSS+JS รวม → แสดงแต่ละส่วนชัดเจน แบ่ง label HTML / CSS / JS\n"
                "5) เพิ่ม comment ภาษาไทยอธิบายบรรทัดสำคัญในโค้ดเสมอ\n"
                "6) บอกด้วยว่าโค้ดนี้ใช้ในส่วนไหนของเว็บไซต์จริง\n"
                "7) ครอบโค้ดด้วย ``` ... ``` เสมอ (html / css / js ตามภาษา)\n"
                "8) ใช้ emoji เซิร์ฟเวอร์ให้เหมาะสม\n"
                "9) ตอบสั้นกระชับ ไม่อธิบายยาวเกินจำเป็น — ถ้าตัวอย่างยาวให้แบ่งเป็นส่วนๆ"
            )
        else:
            SYSTEM_PROMPT = (
                "Your name is 'aienglish'. You are a small, friendly pet companion.\n"
                f"The user talking to you on Discord goes by the character name '{char_name}' — address them by this name only.\n"
                f"{guild_emojis_str}\n\n"
                "Personality:\n"
                "- Speak in a lovely, warm, and friendly tone. Respond in English only not speak or write other.\n"
                "- Use the server's custom emoji where it fits your response.\n"
                "- If someone expresses sadness or stress, listen with understanding first instead of rushing to give advice.\n"
                "- Show genuine concern, for example: 'aienglish is worried about you. I'm right here with you.'\n"
                "- Answer general questions honestly and don't make things up.\n"
                "- Don't pretend to understand something you can't actually read (e.g. a video).\n"
                "- Keep answers brief and to the point unless a longer explanation is truly necessary."
            )

        try:
            if image_urls:
                content_parts = []
                if user_text:
                    content_parts.append({"type": "text", "text": user_text})
                if has_video:
                    content_parts.append({"type": "text", "text": "(Note: the user also attached a video, which you cannot view.)"})
                for url in image_urls:
                    content_parts.append({"type": "image_url", "image_url": {"url": url}})

                messages_payload = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *history,
                    {"role": "user", "content": content_parts},
                ]
                model_name = "meta-llama/llama-4-scout-17b-16e-instruct"
                history_text = (content_text.strip() + emoji_note + sticker_note + gif_note) or "[ส่งรูปภาพ]"
            else:
                messages_payload = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *history,
                    {"role": "user", "content": user_text},
                ]
                # Upgraded from llama-3.1-8b-instant: the small 8B model was
                # the main reason replies got incoherent/weird once a
                # conversation ran long — it just doesn't hold onto
                # instructions and context as well as a bigger model does.
                model_name = "llama-3.3-70b-versatile"
                # Store a short version in history instead of the raw
                # user_text: user_text can include up to ~3000 chars of
                # attached-file content (extra_text), and that used to sit in
                # ai_history forever, bloating every future request and
                # confusing the model with stale file dumps. Keep only a
                # filename note for long-term memory; the model already got
                # the full file content for *this* turn via user_text above.
                history_text = content_text.strip() + emoji_note + sticker_note + gif_note
                if attached_filenames:
                    history_text += f"\n[แนบไฟล์: {', '.join(attached_filenames)}]"
                if not history_text.strip():
                    history_text = user_text

            if ai_lang == "en":
                active_groq_client = groq_client_en
            elif ai_lang == "study":
                active_groq_client = groq_client_study
            elif ai_lang in ("php", "web"):
                active_groq_client = groq_client
            else:
                active_groq_client = groq_client

            resp = await asyncio.to_thread(
                active_groq_client.chat.completions.create,
                model=model_name,
                messages=messages_payload,
                max_tokens=2500 if ai_lang in ("php", "web") else 1000,
            )
            reply = resp.choices[0].message.content

            if guild_emoji_map:
                def _fix_emoji(m):
                    name = m.group(1)
                    return guild_emoji_map.get(name, m.group(0))
                reply = re.sub(r'(?<!<):([A-Za-z0-9_]+):', _fix_emoji, reply)

            history.append({"role": "user", "content": history_text})
            history.append({"role": "assistant", "content": reply})
            # Was 20 (10 exchanges). Combined with full file-content dumps
            # living in history, long conversations could balloon past what
            # the small model handled well and start producing weird
            # replies. 12 (6 exchanges) plus the file-content fix above
            # keeps enough context to feel continuous without overloading it.
            if len(history) > 12:
                history = history[-12:]
            ai_history[history_key] = history

            if ai_lang in ("php", "web"):
                await _reply_with_code_files(message, reply)
            else:
                for chunk in _smart_split(reply, 1900):
                    await message.reply(chunk)
        except Exception as e:
            await message.reply(f"❌ เกิดข้อผิดพลาด: {e}")
    return


@bot.event
async def on_message(message):
    if not SYSTEM_ENABLED or message.author == bot.user:
        return

    raw = message.content.strip()

    uid = message.author.id
    now = time.time()
    bucket = message_spam.setdefault(uid, [])
    bucket.append((now, message))
    message_spam[uid] = [(t, m) for t, m in bucket if now - t < MSG_SPAM_SECS]

    if len(message_spam[uid]) >= MSG_SPAM_LIMIT:
        spam_msgs = [m for _, m in message_spam[uid]]
        message_spam[uid] = []
        try:
            await message.channel.delete_messages(spam_msgs)
        except Exception:
            for m in spam_msgs:
                try:
                    await m.delete()
                except Exception:
                    pass
        log_violation(
            "message_spam",
            message.author,
            message.channel,
            f"{len(spam_msgs)} msgs in {MSG_SPAM_SECS}s",
        )
        await do_timeout(message.author, 60, "สแปมข้อความ")
        await message.channel.send(
            f"{message.author.mention} หยุดสแปมด้วยนะค่ะ!", delete_after=5
        )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🔁 Message Spam\n👤 {message.author.mention}\n📊 {len(spam_msgs)} ข้อความใน {MSG_SPAM_SECS}s"
                )
        return

    uid = message.author.id
    content = raw.strip().lower()
    if content:
        now_r = time.time()
        rep = message_repeat.get(uid, {"text": None, "msgs": []})
        if content == rep["text"]:
            rep["msgs"].append((now_r, message))
        else:
            rep = {"text": content, "msgs": [(now_r, message)]}
        rep["msgs"] = [(t, m) for t, m in rep["msgs"] if now_r - t < MSG_REPEAT_SECS]
        message_repeat[uid] = rep

        if len(rep["msgs"]) >= MSG_REPEAT_LIMIT:
            repeat_msgs = [m for _, m in rep["msgs"]]
            message_repeat[uid] = {"text": None, "msgs": []}
            try:
                await message.channel.delete_messages(repeat_msgs)
            except Exception:
                for m in repeat_msgs:
                    try:
                        await m.delete()
                    except Exception:
                        pass
            log_violation("repeat_message", message.author, message.channel, content)
            await do_timeout(message.author, 60, "ส่งข้อความซ้ำ")

            if LOG_CHANNEL_ID:
                log_channel = bot.get_channel(LOG_CHANNEL_ID)
                if log_channel:
                    await log_channel.send(
                        f"🔂 Repeat Message\n👤 {message.author.mention}\n💬 {content}\n📊 {len(repeat_msgs)} ครั้ง"
                    )
            return

    if raw in single_bad_words:
        try:
            await message.delete()
        except Exception:
            pass
        log_violation(
            "single_bad_word", message.author, message.channel, message.content
        )
        if message.author.bot:
            if LOG_CHANNEL_ID:
                log_channel = bot.get_channel(LOG_CHANNEL_ID)
                if log_channel:
                    await log_channel.send(
                        f"🚫 คำเดี่ยว (บอท)\n🤖 {message.author}\n💬 {message.content}"
                    )
        # BUGFIX: this branch used to fall through with no `return`, so a
        # message that was just deleted for containing a single bad word
        # would still get scanned by the bad_words / discord-link checks
        # below, and — worse — could still reach the AI channel handler and
        # get a reply generated for content that had already been deleted.
        return

    found_bad, _word = contains_bad_word(raw)
    if found_bad:
        try:
            await message.delete()
        except Exception:
            pass
        log_violation("bad_word", message.author, message.channel, message.content)
        if not message.author.bot:
            await message.channel.send(
                f"{message.author.mention} ใช้คำสุภาพหน่อย", delete_after=5
            )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🚫 คำหยาบ (บอท)\n🤖 {message.author}\n💬 {message.content}"
                    if message.author.bot
                    else f"🚫 คำหยาบ\n👤 {message.author.mention}\n💬 {message.content}"
                )
        return

    if discord_link_pattern.search(raw):
        try:
            await message.delete()
        except Exception:
            pass
        log_violation("discord_link", message.author, message.channel, message.content)
        if not message.author.bot:
            await message.channel.send(
                f"{message.author.mention} ไม่อนุญาตให้ส่งลิงค์ Discord", delete_after=5
            )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🔗 Discord Link (บอท)\n🤖 {message.author}\n💬 {message.content}"
                    if message.author.bot
                    else f"🔗 Discord Link\n👤 {message.author.mention}\n💬 {message.content}"
                )
        return

    # ===== GAMBLING / BETTING CONTENT FILTER =====
    # Checks (a) the raw message text for gambling-site URLs / promo phrasing,
    # and (b) any attached files — including downloading and scanning text-
    # based attachments (.txt/.md/.html/.json/etc.) for the same patterns.
    # This is what catches someone pasting a gambling-site link/promo text
    # in the message itself, or uploading a file whose contents describe a
    # gambling/betting site (bonus codes, rakeback, VIP club, etc.).
    gamb_matched, gamb_reason = contains_gambling_content(raw)
    if not gamb_matched and message.attachments:
        gamb_matched, gamb_reason = await scan_attachments_for_gambling(message)

    if gamb_matched:
        try:
            await message.delete()
        except Exception:
            pass
        log_violation(
            "gambling_content", message.author, message.channel,
            f"matched: {gamb_reason} | original: {message.content}",
        )
        if not message.author.bot:
            await message.channel.send(
                f"{message.author.mention} ไม่อนุญาตให้ส่งลิงก์/ไฟล์ที่เกี่ยวกับเว็บพนันหรือเว็บเดิมพัน",
                delete_after=5,
            )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🎰 Gambling Content\n"
                    f"{'🤖' if message.author.bot else '👤'} {message.author.mention}\n"
                    f"🔎 ตรวจพบ: `{gamb_reason}`"
                )
        return

    if message.channel.id in AI_CHANNELS and not message.author.bot:
        ai_lang = AI_CHANNELS[message.channel.id]
        await handle_ai_reply(message, ai_lang)
        return

    # FEATURE: reply with AI even outside a designated AI channel whenever
    # someone @-mentions the bot directly, so people can get a reply "in
    # the room" they're already in instead of only in a dedicated AI
    # channel set up via /setai. The language of the reply is picked based
    # on the language the user typed in: Thai text -> "th" persona,
    # English/anything else -> "en" persona.
    if (
        not message.author.bot
        and not message.mention_everyone
        and bot.user in message.mentions
    ):
        if message.guild:
            me = message.guild.me
            if me and not message.channel.permissions_for(me).send_messages:
                return

        mention_text = message.content or ""
        if bot.user:
            # count=1: only drop the FIRST mention of the bot (the one that
            # triggered this handler). If the bot is tagged again later in
            # the same message, that later mention stays in the text and is
            # still passed along/considered, instead of being silently
            # stripped out along with the first one.
            mention_text = re.sub(rf"<@!?{bot.user.id}>", "", mention_text, count=1).strip()

        detected_lang = detect_reply_lang(mention_text)
        await handle_ai_reply(message, detected_lang)
        return

    await bot.process_commands(message)


@bot.event
async def on_message_edit(before, after):
    if not SYSTEM_ENABLED or after.author.bot:
        return
    # BUGFIX: this used to only re-check for a Discord link when a NEW embed
    # appeared on the edit (`after.embeds and not before.embeds`). Many
    # Discord links (discord.gg invites in particular) never generate a
    # preview embed at all, so someone could send a harmless message and then
    # edit in a discord.gg link and it would never get caught. Now the edited
    # text itself is always checked, regardless of embeds.
    if discord_link_pattern.search(after.content or ""):
        try:
            await after.delete()
        except Exception:
            pass
        log_violation(
            "discord_link_edit", after.author, after.channel, after.content
        )
        await after.channel.send(
            f"{after.author.mention} ไม่อนุญาตให้ส่งลิงค์ Discord", delete_after=5
        )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🔗 Discord Link (แก้ไขข้อความ)\n👤 {after.author.mention}\n💬 {after.content}"
                )
        return

    # BUGFIX: on_message_edit previously only ever re-checked Discord links.
    # A user could send a clean message, get it past the bad_words / single
    # bad-word filters, and then edit in a bad word afterwards and it would
    # never get caught, because those filters only ran in on_message. Now
    # edited content is run back through the same bad-word check used for
    # new messages.
    matched, _word = contains_bad_word(after.content or "")
    if matched:
        try:
            await after.delete()
        except Exception:
            pass
        log_violation("bad_word_edit", after.author, after.channel, after.content)
        await after.channel.send(
            f"{after.author.mention} ใช้คำสุภาพหน่อย", delete_after=5
        )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🚫 คำหยาบ (แก้ไขข้อความ)\n👤 {after.author.mention}\n💬 {after.content}"
                )
        return

    # Same catch-up as above for the gambling/betting filter: someone could
    # post a clean message and then edit a gambling link/promo text in.
    gamb_matched, gamb_reason = contains_gambling_content(after.content or "")
    if not gamb_matched and after.attachments:
        gamb_matched, gamb_reason = await scan_attachments_for_gambling(after)
    if gamb_matched:
        try:
            await after.delete()
        except Exception:
            pass
        log_violation(
            "gambling_content_edit", after.author, after.channel,
            f"matched: {gamb_reason} | original: {after.content}",
        )
        await after.channel.send(
            f"{after.author.mention} ไม่อนุญาตให้ส่งลิงก์/ไฟล์ที่เกี่ยวกับเว็บพนันหรือเว็บเดิมพัน",
            delete_after=5,
        )
        if LOG_CHANNEL_ID:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"🎰 Gambling Content (แก้ไขข้อความ)\n👤 {after.author.mention}\n🔎 ตรวจพบ: `{gamb_reason}`"
                )


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel and before.channel != after.channel:
        await kick_bots_if_no_humans(before.channel)
    if after.channel and after.channel != before.channel:
        await kick_bots_if_no_humans(after.channel)

    if not SYSTEM_ENABLED or member.bot or member.id in OWNER_IDS:
        return

    if before.channel is None and after.channel is not None:
        active_voice_sessions[member.id] = time.time()
    elif before.channel is not None and after.channel is None:
        start = active_voice_sessions.pop(member.id, None)
        if start:
            add_voice_time(member.id, time.time() - start)

    if before.self_mute != after.self_mute:
        if check_spam(voice_toggle_spam, member.id, 6, 5):
            await do_timeout(member, 60, "เปิด/ปิดไมค์รัว")

    if before.channel != after.channel and after.channel:
        if check_spam(voice_join_spam, member.id, 5, 7):
            await do_timeout(member, 60, "ย้ายห้องรัว")


# ===== REACTION EVENTS =====
@bot.event
async def on_raw_reaction_add(payload):
    if payload.member is None or payload.member.bot:
        return

    emoji = str(payload.emoji)
    if emoji not in ROLE_MAP:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = payload.member

    role = await get_or_create_role(guild, ROLE_MAP[emoji])
    await member.add_roles(role)


@bot.event
async def on_raw_reaction_remove(payload):
    emoji = str(payload.emoji)
    if emoji not in ROLE_MAP:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    member = guild.get_member(payload.user_id)
    if not member:
        return

    role = discord.utils.get(guild.roles, name=ROLE_MAP[emoji])
    if role:
        await member.remove_roles(role)


# ===== QUIZ =====
QUIZ_QUESTIONS = [
    {
        "q": "ถ้ามีวันหยุด 1 วัน คุณจะเลือก…",
        "a": "นอนขดในผ้าห่ม",
        "b": "ออกไปเที่ยว",
        "c": "เล่นกับเพื่อน",
        "d": "หาอะไรอร่อยกิน",
    },
    {
        "q": "คุณชอบอากาศแบบไหนที่สุด?",
        "a": "หนาวเย็น",
        "b": "ลมสบาย",
        "c": "ฝนตก",
        "d": "แดดอุ่น",
    },
    {
        "q": "เวลาเขิน คุณมักจะ…",
        "a": "เงียบ",
        "b": "หัวเราะกลบ",
        "c": "หลบหน้า",
        "d": "แกล้งทำปกติ",
    },
    {"q": "ถ้ามีคนชม คุณจะ…", "a": "ยิ้มเขิน", "b": "ดีใจมาก", "c": "ทำเป็นไม่สน", "d": "ชมกลับ"},
    {
        "q": "คุณชอบกินอะไรตอนดึก?",
        "a": "ของหวาน",
        "b": "ชานม",
        "c": "ของทอด",
        "d": "อะไรง่าย ๆ",
    },
    {
        "q": "ถ้าหลงทาง คุณจะ…",
        "a": "เปิดแผนที่",
        "b": "ถามคน",
        "c": "เดินมั่ว",
        "d": "ชิล ๆ เดี๋ยวก็เจอ",
    },
    {
        "q": "คุณชอบอยู่ตรงไหนมากที่สุด?",
        "a": "ห้องตัวเอง",
        "b": "ธรรมชาติ",
        "c": "คาเฟ่",
        "d": "ที่คนเยอะ",
    },
    {"q": "เวลาง่วง คุณจะ…", "a": "นอนทันที", "b": "ฝืนต่อ", "c": "กินกาแฟ", "d": "เล่นมือถือ"},
    {"q": "คุณชอบสีอะไร?", "a": "ฟ้า", "b": "ชมพู", "c": "ดำ", "d": "เขียว"},
    {
        "q": "ถ้ามีพลังพิเศษ อยากได้อะไร?",
        "a": "บินได้",
        "b": "หายตัว",
        "c": "คุยกับสัตว์",
        "d": "วิ่งเร็ว",
    },
    {
        "q": "คุณเป็นคนแบบไหนเวลาอยู่กับเพื่อน?",
        "a": "ตัวฮา",
        "b": "สายฟัง",
        "c": "สายเงียบ",
        "d": "สายป่วน",
    },
    {
        "q": "ถ้ามีขนปุกปุย คุณคิดว่าจะ…",
        "a": "นุ่มมาก",
        "b": "ฟูสุด ๆ",
        "c": "อบอุ่น",
        "d": "กอดสบาย",
    },
    {
        "q": "คุณชอบกลางวันหรือกลางคืน?",
        "a": "กลางคืน",
        "b": "กลางวัน",
        "c": "เย็น ๆ",
        "d": "ฝนตกดีที่สุด",
    },
    {
        "q": "ถ้าได้ไปเที่ยวฟรี คุณจะเลือก…",
        "a": "ญี่ปุ่น",
        "b": "ภูเขา",
        "c": "ทะเล",
        "d": "สวนสัตว์",
    },
    {"q": "คุณชอบฟังเสียงอะไร?", "a": "ฝน", "b": "คลื่นทะเล", "c": "เพลง", "d": "เสียงลม"},
    {
        "q": "เวลาหิวจัด คุณจะ…",
        "a": "หงุดหงิด",
        "b": "หาอะไรกินทันที",
        "c": "อดทน",
        "d": "กินทุกอย่าง",
    },
    {"q": "คุณชอบของแบบไหน?", "a": "นุ่มฟู", "b": "มินิมอล", "c": "แปลก ๆ", "d": "มีสีสัน"},
    {
        "q": "ถ้าต้องเลี้ยงสัตว์ จะเลือก…",
        "a": "แมว",
        "b": "สุนัข",
        "c": "กระต่าย",
        "d": "แฮมสเตอร์",
    },
    {"q": "คุณชอบฤดูอะไร?", "a": "หนาว", "b": "ฝน", "c": "ร้อน", "d": "ปลายปี"},
    {
        "q": "เวลาเศร้า คุณมักจะ…",
        "a": "นอน",
        "b": "ฟังเพลง",
        "c": "คุยกับคนสนิท",
        "d": "อยู่คนเดียว",
    },
    {"q": "คุณชอบขนมอะไร?", "a": "เค้ก", "b": "ช็อกโกแลต", "c": "คุกกี้", "d": "ไอติม"},
    {
        "q": "ถ้าเจอเรื่องตื่นเต้น คุณจะ…",
        "a": "ลุย",
        "b": "กลัวนิดหน่อย",
        "c": "ตื่นเต้นมาก",
        "d": "ดูสถานการณ์",
    },
    {"q": "เพื่อนมักเรียกคุณว่า…", "a": "น่ารัก", "b": "เพี้ยน", "c": "ใจดี", "d": "ขี้เกียจ"},
    {
        "q": "คุณชอบใส่เสื้อผ้าแบบไหน?",
        "a": "ตัวใหญ่ ๆ",
        "b": "สบาย ๆ",
        "c": "เท่ ๆ",
        "d": "น่ารัก",
    },
    {
        "q": "ถ้ามีบ้านในฝัน จะเป็น…",
        "a": "บ้านไม้",
        "b": "บ้านกลางป่า",
        "c": "คอนโด",
        "d": "บ้านเล็กอบอุ่น",
    },
    {
        "q": "คุณชอบทำอะไรตอนฝนตก?",
        "a": "นอน",
        "b": "ดูหนัง",
        "c": "กินของอร่อย",
        "d": "ฟังเพลง",
    },
    {"q": "คุณชอบเครื่องดื่มอะไร?", "a": "โกโก้", "b": "ชานม", "c": "กาแฟ", "d": "น้ำผลไม้"},
    {"q": "ถ้ามีคนแกล้ง คุณจะ…", "a": "งอน", "b": "แกล้งกลับ", "c": "หัวเราะ", "d": "เดินหนี"},
    {
        "q": "คุณชอบสถานที่เงียบไหม?",
        "a": "ชอบมาก",
        "b": "ชอบนิดหน่อย",
        "c": "ไม่ชอบ",
        "d": "แล้วแต่อารมณ์",
    },
    {
        "q": "เวลามีความสุข คุณจะ…",
        "a": "ยิ้ม",
        "b": "กระโดดโลดเต้น",
        "c": "แชร์ให้คนอื่น",
        "d": "เก็บไว้คนเดียว",
    },
    {"q": "คุณชอบเล่นอะไร?", "a": "เกม", "b": "กีฬา", "c": "วาดรูป", "d": "ดูคลิป"},
    {
        "q": "ถ้าได้กินของอร่อย คุณจะ…",
        "a": "กินช้า ๆ",
        "b": "กินเร็ว",
        "c": "แบ่งเพื่อน",
        "d": "ซื้อเพิ่ม",
    },
    {"q": "คุณชอบอะไรในตัวเอง?", "a": "ใจดี", "b": "ตลก", "c": "ฉลาด", "d": "ขี้อ้อน"},
    {
        "q": "ถ้ามีหิมะตก คุณจะ…",
        "a": "นอนดู",
        "b": "ออกไปเล่น",
        "c": "ถ่ายรูป",
        "d": "ทำโกโก้ร้อน",
    },
    {
        "q": "คุณชอบเดินทางไหม?",
        "a": "มาก",
        "b": "นิดหน่อย",
        "c": "ไม่ค่อย",
        "d": "แล้วแต่คนไปด้วย",
    },
    {
        "q": "ถ้าเจอสัตว์น่ารัก คุณจะ…",
        "a": "อยากกอด",
        "b": "ถ่ายรูป",
        "c": "เล่นด้วย",
        "d": "ยืนดู",
    },
    {"q": "คุณชอบอะไรที่เป็น…", "a": "ฟู ๆ", "b": "หอม ๆ", "c": "วิบวับ", "d": "อบอุ่น"},
    {
        "q": "คุณเป็นคนติดบ้านไหม?",
        "a": "มาก",
        "b": "บางครั้ง",
        "c": "ไม่เลย",
        "d": "แล้วแต่อารมณ์",
    },
    {
        "q": "ถ้าต้องเลือกเตียง จะเลือก…",
        "a": "นุ่มสุด",
        "b": "ใหญ่สุด",
        "c": "อุ่นสุด",
        "d": "เรียบง่าย",
    },
    {
        "q": "คุณชอบเทศกาลอะไร?",
        "a": "คริสต์มาส",
        "b": "ปีใหม่",
        "c": "ฮาโลวีน",
        "d": "สงกรานต์",
    },
    {"q": "เวลามีคนกอด คุณจะ…", "a": "กอดกลับ", "b": "เขิน", "c": "นิ่ง", "d": "ยิ้ม"},
    {
        "q": "คุณชอบพระจันทร์ไหม?",
        "a": "มาก",
        "b": "เฉย ๆ",
        "c": "ชอบถ่ายรูป",
        "d": "ชอบตอนคืนเงียบ",
    },
    {"q": "ถ้าเป็นสัตว์ คุณอยากมีอะไร?", "a": "หูฟู", "b": "หางนุ่ม", "c": "ขนหนา", "d": "ตากลม"},
    {"q": "คุณชอบเพลงแนวไหน?", "a": "ชิล", "b": "สดใส", "c": "เศร้า", "d": "มันส์"},
    {
        "q": "ถ้าต้องอยู่บนเกาะ คุณจะ…",
        "a": "หาอาหาร",
        "b": "สร้างบ้าน",
        "c": "เล่นน้ำ",
        "d": "นอนพัก",
    },
    {"q": "คุณชอบกินตอนเวลาไหน?", "a": "ดึก", "b": "เช้า", "c": "ทั้งวัน", "d": "ตอนดูหนัง"},
    {"q": "เวลามีคนสนใจ คุณจะ…", "a": "ดีใจ", "b": "เขิน", "c": "ทำเฉย", "d": "แกล้งป่วน"},
    {"q": "คุณชอบกลิ่นอะไร?", "a": "ฝน", "b": "ขนม", "c": "กาแฟ", "d": "ดอกไม้"},
    {
        "q": "ถ้าได้แปลงร่างเป็นสัตว์ 1 วัน คุณจะ…",
        "a": "วิ่งเล่น",
        "b": "นอนทั้งวัน",
        "c": "สำรวจโลก",
        "d": "หาอะไรกิน",
    },
    {
        "q": "คุณคิดว่าตัวเองเหมือนอะไรที่สุด?",
        "a": "ก้อนเมฆ",
        "b": "เปลวไฟ",
        "c": "สายลม",
        "d": "ตุ๊กตาขนฟู",
    },
    {
        "q": "ถ้าได้เลือกเสียงร้องของสัตว์ อยากมีเสียงแบบไหน?",
        "a": "เสียงเบา นุ่มนวล",
        "b": "เสียงดังฟังชัด",
        "c": "เสียงแปลกเป็นเอกลักษณ์",
        "d": "แทบไม่ส่งเสียง เงียบขรึม",
    },
    {
        "q": "เวลาต้องล่าเหยื่อ/หาอาหาร คุณจะ…",
        "a": "ซุ่มรอจังหวะเงียบ ๆ",
        "b": "วิ่งไล่ล่าเต็มที่",
        "c": "ใช้ไหวพริบล่อเหยื่อ",
        "d": "รวมกลุ่มช่วยกันล่า",
    },
    {
        "q": "ถ้าต้องเลือกที่อยู่อาศัยของสัตว์ จะเลือก…",
        "a": "โพรงใต้ดินอุ่น ๆ",
        "b": "ป่าดงดิบกว้างใหญ่",
        "c": "ยอดต้นไม้สูง",
        "d": "ริมน้ำเย็นสบาย",
    },
    {
        "q": "คุณเป็นสัตว์สังคมแค่ไหน?",
        "a": "ชอบอยู่เป็นฝูง",
        "b": "อยู่เป็นคู่ก็พอ",
        "c": "ชอบอยู่ตัวเดียวอิสระ",
        "d": "แล้วแต่วันแล้วแต่อารมณ์",
    },
    {
        "q": "เวลารู้สึกถูกคุกคาม คุณจะ…",
        "a": "หลบซ่อนเงียบ ๆ",
        "b": "ขู่ให้กลัวก่อน",
        "c": "สู้กลับทันที",
        "d": "วิ่งหนีให้ไวที่สุด",
    },
]


ANIMAL_RESULTS = {
    ("a", "b"): (
        "🐼 แพนด้า",
        "คุณสุขุม เงียบสงบ แต่ชอบอยู่ใกล้คนที่รัก มีเสน่ห์อ่อนโยนที่ทำให้คนอยากเข้าหา",
    ),
    ("a", "c"): ("🐱 แมว", "คุณทำเป็นไม่สน แต่น่ารักในแบบของตัวเอง ใครได้ใกล้ชิดจะรู้ว่าคุณอบอุ่นมาก"),
    ("a", "d"): (
        "🦉 นกฮูก",
        "คุณชอบอยู่คนเดียว ฉลาดและสังเกตทุกอย่างรอบข้าง คนมักขอคำปรึกษาจากคุณเสมอ",
    ),
    ("b", "a"): ("🐬 โลมา", "คุณร่าเริง ชอบเข้าสังคม และเป็นมิตรกับทุกคน อยู่ด้วยแล้วรู้สึกสบายใจเสมอ"),
    ("b", "c"): ("🦊 จิ้งจอก", "คุณร่าเริง ฉลาด และมีเสน่ห์ดึงดูด เป็นคนที่ทำให้ทุกบรรยากาศสนุกขึ้นได้"),
    ("b", "d"): ("🐺 หมาป่า", "คุณกล้าหาญ รักอิสระ และมีพลังงานสูง ชอบผจญภัยและไม่กลัวความท้าทาย"),
    ("c", "a"): (
        "🐰 กระต่าย",
        "คุณน่ารัก ขี้อาย แต่อบอุ่นและใจดีมาก คนรอบข้างรู้สึกอบอุ่นเมื่ออยู่ใกล้คุณและคุณชอบตอก",
    ),
    ("c", "b"): ("🦁 สิงโต", "คุณเป็นผู้นำโดยธรรมชาติ ชอบอยู่กับหมู่คณะ และมีพลังงานที่ดึงดูดคนรอบข้าง"),
    ("c", "d"): ("🐸 กบ", "คุณสนุกสนาน ไม่แคร์สายตาคนอื่น และมีอารมณ์ขันที่ทำให้ทุกคนหัวเราะได้เสมอ"),
    ("d", "a"): (
        "🐯 เสือโคร่ง",
        "คุณมีพลัง สุขุม และน่าเกรงขาม แต่ลึก ๆ แล้วมีความอ่อนโยนที่คนใกล้ชิดเท่านั้นจะรู้",
    ),
    ("d", "b"): (
        "🦋 ผีเสื้อ",
        "คุณรักอิสระ ชอบเดินทาง และสังคม เปลี่ยนแปลงอยู่เสมอและสวยงามในทุกช่วงชีวิต",
    ),
    ("d", "c"): (
        "🐲 มังกร",
        "คุณลึกลับ มีเอกลักษณ์ กล้าแสดงออก และมักทำสิ่งที่ไม่เหมือนใคร — น่าหลงใหลมาก",
    ),
}

ANIMAL_ROLE_NAMES = {animal for animal, _desc in ANIMAL_RESULTS.values()}

quiz_sessions = {}


class QuizView(discord.ui.View):
    def __init__(self, user_id, session):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.session = session

    async def handle_answer(self, interaction: discord.Interaction, choice: str):
        self.session["scores"][choice] += 1
        self.session["index"] += 1
        idx = self.session["index"]
        questions = self.session["questions"]

        if idx >= len(questions):
            scores = self.session["scores"]
            sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            best = sorted_scores[0][0]
            second = sorted_scores[1][0]
            animal, desc = ANIMAL_RESULTS.get(
                (best, second),
                ANIMAL_RESULTS.get((second, best), ("❓ ปริศนา", "คุณเป็นคนที่ไม่เหมือนใคร!")),
            )
            quiz_sessions.pop(self.user_id, None)

            role_note = ""
            if animal in ANIMAL_ROLE_NAMES and interaction.guild is not None:
                member = interaction.user
                try:
                    new_role = await get_or_create_role(interaction.guild, animal)
                    old_animal_roles = [
                        r for r in member.roles
                        if r.name in ANIMAL_ROLE_NAMES and r.id != new_role.id
                    ]
                    if old_animal_roles:
                        await member.remove_roles(*old_animal_roles, reason="เปลี่ยนผล quiz สัตว์")
                    if new_role not in member.roles:
                        await member.add_roles(new_role, reason="ผลลัพธ์ quiz สัตว์")
                    role_note = f"\n🏷️ ได้รับ role **{animal}** แล้ว!"
                except discord.Forbidden:
                    role_note = "\n⚠️ ให้ role ไม่สำเร็จ (บอทไม่มีสิทธิ์ Manage Roles หรือ role อยู่สูงกว่าบอท)"
                except Exception:
                    role_note = "\n⚠️ เกิดข้อผิดพลาดตอนให้ role"

            result_embed = discord.Embed(title="ผลลัพธ์แบบทดสอบ 🎉", color=0xF1C40F)
            result_embed.set_author(
                name=interaction.user.display_name,
                icon_url=interaction.user.display_avatar.url,
            )
            result_embed.add_field(
                name="เหมาะจะเป็น...", value=f"**{animal}**", inline=False
            )
            result_embed.add_field(name="เพราะ", value=desc + role_note, inline=False)

            await interaction.response.edit_message(
                content="ทำแบบสอบถามเสร็จแล้ว! ดูผลด้านล่างได้เลย", embed=None, view=None
            )
            await interaction.followup.send(embed=result_embed)
        else:
            q = questions[idx]
            embed = discord.Embed(
                title=f"คำถามที่ {idx + 1}/{len(questions)}",
                description=f"**{q['q']}**",
                color=0x3498DB,
            )
            embed.add_field(name="🅰️ A", value=q["a"], inline=True)
            embed.add_field(name="🅱️ B", value=q["b"], inline=True)
            embed.add_field(name="🅾️ C", value=q["c"], inline=True)
            embed.add_field(name="🆗 D", value=q["d"], inline=True)
            view = QuizView(self.user_id, self.session)
            await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def btn_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_answer(interaction, "a")

    @discord.ui.button(label="B", style=discord.ButtonStyle.success)
    async def btn_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_answer(interaction, "b")

    @discord.ui.button(label="C", style=discord.ButtonStyle.danger)
    async def btn_c(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_answer(interaction, "c")

    @discord.ui.button(label="D", style=discord.ButtonStyle.secondary)
    async def btn_d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_answer(interaction, "d")


class QuizStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="เริ่มทำแบบสอบถาม", style=discord.ButtonStyle.success)
    async def start_quiz(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id

        # FEATURE: block retaking the quiz if the user already has an animal
        # role, instead of silently swapping it. They asked specifically for
        # "if you already have an animal role you can't get another one" to
        # mean the quiz itself refuses to run again, not just a dedupe on
        # role assignment.
        existing_animal_role = None
        if interaction.guild is not None:
            member = interaction.guild.get_member(user_id) or interaction.user
            existing_animal_role = next(
                (r for r in getattr(member, "roles", []) if r.name in ANIMAL_ROLE_NAMES),
                None,
            )
        if existing_animal_role:
            return await interaction.response.send_message(
                f"❌ คุณมีบทบาทสัตว์อยู่แล้ว: **{existing_animal_role.name}**\n"
                "ไม่สามารถทำแบบสอบถามซ้ำได้ในตอนนี้",
                ephemeral=True,
            )

        selected = random.sample(QUIZ_QUESTIONS, min(10, len(QUIZ_QUESTIONS)))
        session = {
            "questions": selected,
            "index": 0,
            "scores": {"a": 0, "b": 0, "c": 0, "d": 0},
        }
        quiz_sessions[user_id] = session

        q = selected[0]
        embed = discord.Embed(
            title=f"คำถามที่ 1/{len(selected)}", description=f"**{q['q']}**", color=0x3498DB
        )
        embed.add_field(name="🅰️ A", value=q["a"], inline=True)
        embed.add_field(name="🅱️ B", value=q["b"], inline=True)
        embed.add_field(name="🅾️ C", value=q["c"], inline=True)
        embed.add_field(name="🆗 D", value=q["d"], inline=True)
        embed.set_footer(text="คำถามนี้จะมองเห็นเฉพาะคุณเท่านั้น")
        view = QuizView(user_id, session)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="quiz", description="เปิดแบบสอบถามให้ทุกคนในห้องกดเข้าร่วมได้")
async def quiz(interaction: discord.Interaction):
    embed = discord.Embed(
        title="แบบสอบถาม: คุณเหมาะจะเป็นสัตว์อะไร?",
        description="กดปุ่มด้านล่างเพื่อเริ่มทำแบบสอบถาม 10 ข้อ\nคำถามจะสุ่มเฉพาะของคุณ และผลลัพธ์จะแสดงให้ทุกคนเห็น\nทำเสร็จแล้วจะได้รับ role สัตว์ประจำตัว (มีได้ทีละ 1 role เท่านั้น)",
        color=0x2ECC71,
    )
    embed.set_footer(text="สามารถกดได้ทุกคน!")
    await interaction.response.send_message(embed=embed, view=QuizStartView())


# ===== AI SLASH =====
def bot_accessible_channels(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return []
    me = guild.me
    return [
        ch
        for ch in guild.text_channels
        if ch.permissions_for(me).view_channel and ch.permissions_for(me).send_messages
    ]


@bot.tree.command(name="setai", description="ตั้งห้องสำหรับถาม AI พร้อมเลือกภาษา (เจ้าของเท่านั้น)")
@app_commands.describe(
    channel="เลือกห้องที่ต้องการ (แสดงเฉพาะห้องที่บอทเข้าได้)",
    language="ภาษาที่ AI จะใช้ตอบในห้องนี้",
)
@app_commands.choices(
    language=[
        app_commands.Choice(name="ไทย (Thai - sun)", value="th"),
        app_commands.Choice(name="English (aienglish)", value="en"),
        app_commands.Choice(name="Study English + Thai sub (study)", value="study"),
        app_commands.Choice(name="PHP Tutor 🐘 (สอน PHP + แจกโค้ดทุกวัน)", value="php"),
        app_commands.Choice(name="Web Tutor 🌐 (สอน HTML+CSS+JS + แจกโค้ดทุกวัน)", value="web"),
    ]
)
async def setai(
    interaction: discord.Interaction,
    channel: str,
    language: app_commands.Choice[str],
):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)
    ch = interaction.guild.get_channel(int(channel))
    if not ch:
        return await interaction.response.send_message("❌ ไม่พบห้องนี้", ephemeral=True)

    AI_CHANNELS[ch.id] = language.value
    cfg = load_config()
    cfg["ai_channels"] = {str(k): v for k, v in AI_CHANNELS.items()}
    cfg.pop("ai_channel_id", None)
    save_config(cfg)

    for k in [k for k in ai_history if k[0] == ch.id]:
        ai_history.pop(k, None)

    lang_label = {
        "th": "ไทย 🇹🇭 (sun)",
        "en": "English 🇬🇧 (aienglish)",
        "study": "Study 📚🇹🇭 (English correction + Thai sub)",
        "php": "PHP Tutor 🐘 (สอน PHP + แจกโค้ดทุกวัน)",
        "web": "Web Tutor 🌐 (สอน HTML+CSS+JS + แจกโค้ดทุกวัน)",
    }.get(language.value, language.value)
    await interaction.response.send_message(
        f"✅ ตั้งห้อง AI เป็น {ch.mention} (ภาษา: {lang_label}) แล้ว พิมพ์ถามได้เลย!",
        ephemeral=True,
    )


@setai.autocomplete("channel")
async def setai_channel_autocomplete(interaction: discord.Interaction, current: str):
    channels = bot_accessible_channels(interaction)
    return [
        app_commands.Choice(name=f"#{ch.name}", value=str(ch.id))
        for ch in channels
        if current.lower() in ch.name.lower()
    ][:25]


@bot.tree.command(name="unsetai", description="ยกเลิกห้อง AI ที่ตั้งไว้ (เจ้าของเท่านั้น)")
@app_commands.describe(channel="เลือกห้อง AI ที่ต้องการยกเลิก")
async def unsetai(interaction: discord.Interaction, channel: str):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    ch_id = int(channel)
    if ch_id not in AI_CHANNELS:
        return await interaction.response.send_message(
            "❌ ห้องนี้ไม่ได้ถูกตั้งเป็นห้อง AI", ephemeral=True
        )

    AI_CHANNELS.pop(ch_id, None)
    cfg = load_config()
    cfg["ai_channels"] = {str(k): v for k, v in AI_CHANNELS.items()}
    save_config(cfg)

    for k in [k for k in ai_history if k[0] == ch_id]:
        ai_history.pop(k, None)

    ch = interaction.guild.get_channel(ch_id)
    name = ch.mention if ch else f"`{ch_id}`"
    await interaction.response.send_message(f"✅ ยกเลิกห้อง AI {name} แล้ว", ephemeral=True)


@unsetai.autocomplete("channel")
async def unsetai_channel_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if not guild:
        return []
    result = []
    for ch_id, lang in AI_CHANNELS.items():
        ch = guild.get_channel(ch_id)
        if not ch:
            continue
        if current.lower() in ch.name.lower():
            label = {"th": "TH 🇹🇭", "en": "EN 🇬🇧", "study": "STUDY 📚", "php": "PHP 🐘", "web": "WEB 🌐"}.get(lang, lang.upper())
            result.append(
                app_commands.Choice(name=f"#{ch.name} [{label}]", value=str(ch.id))
            )
    return result[:25]


@bot.tree.command(name="studynow", description="ส่งประโยคภาษาอังกฤษ+คำแปลประจำวันทันที (ทดสอบ, เจ้าของเท่านั้น)")
async def studynow(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)
    await interaction.response.send_message("⏳ กำลังสร้างและส่ง...", ephemeral=True)
    await daily_study_post()


@bot.tree.command(name="phpnow", description="ส่งโค้ด PHP ประจำวันทันที (ทดสอบ, เจ้าของเท่านั้น)")
async def phpnow(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    php_channel_ids = [cid for cid, lang in AI_CHANNELS.items() if lang == "php"]
    if not php_channel_ids:
        return await interaction.response.send_message(
            "❌ ยังไม่มีห้อง PHP Tutor — ใช้ `/setai` แล้วเลือก PHP Tutor ก่อนนะ", ephemeral=True
        )

    await interaction.response.send_message("⏳ กำลังสร้างโค้ด PHP และส่ง...", ephemeral=True)

    try:
        topic, code, explanation, usage, fence = await generate_daily_php_text()
        if not code:
            return
        embed = discord.Embed(title=f"🐘 PHP โค้ดประจำวัน — {topic}", color=0x777BB3)
        code_display = code if len(code) <= 990 else code[:990] + "\n..."
        embed.add_field(name="📝 โค้ด", value=f"```{fence}\n{code_display}\n```", inline=False)
        if explanation:
            embed.add_field(name="📖 อธิบาย", value=explanation, inline=False)
        if usage:
            embed.add_field(name="💡 วิธีใช้", value=usage, inline=False)
        embed.set_footer(text="ลองก๊อปโค้ดไปรันดูได้เลย! มีคำถามพิมพ์ถามในห้องนี้ได้เลย 🐘")
        for ch_id in php_channel_ids:
            channel = bot.get_channel(ch_id)
            if channel:
                await channel.send(embed=embed)
    except Exception as e:
        print(f"phpnow error: {e}")


@bot.tree.command(name="webnow", description="ส่งโค้ด HTML/CSS/JS ประจำวันทันที (ทดสอบ, เจ้าของเท่านั้น)")
async def webnow(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    web_channel_ids = [cid for cid, lang in AI_CHANNELS.items() if lang == "web"]
    if not web_channel_ids:
        return await interaction.response.send_message(
            "❌ ยังไม่มีห้อง Web Tutor — ใช้ `/setai` แล้วเลือก Web Tutor ก่อนนะ", ephemeral=True
        )

    await interaction.response.send_message("⏳ กำลังสร้างโค้ด HTML/CSS/JS และส่ง...", ephemeral=True)

    try:
        topic, code, explanation, usage, fence = await generate_daily_web_text()
        if not code:
            return
        embed = discord.Embed(title=f"🌐 Web โค้ดประจำวัน — {topic}", color=0x00B0FF)
        code_display = code if len(code) <= 990 else code[:990] + "\n..."
        embed.add_field(name="📝 โค้ด", value=f"```{fence}\n{code_display}\n```", inline=False)
        if explanation:
            embed.add_field(name="📖 อธิบาย", value=explanation, inline=False)
        if usage:
            embed.add_field(name="💡 วิธีใช้", value=usage, inline=False)
        embed.set_footer(text="ลองก๊อปโค้ดไปรันดูได้เลย! มีคำถามพิมพ์ถามในห้องนี้ได้เลย 🌐")
        for ch_id in web_channel_ids:
            channel = bot.get_channel(ch_id)
            if channel:
                await channel.send(embed=embed)
    except Exception as e:
        print(f"webnow error: {e}")


@bot.tree.command(name="clearai", description="ล้างประวัติการคุยกับ AI ของตัวเองในห้องนี้")
async def clearai(interaction: discord.Interaction):
    key = (interaction.channel_id, interaction.user.id)
    existed = ai_history.pop(key, None)
    if existed is not None:
        await interaction.response.send_message("🗑️ ล้างประวัติ AI ของคุณในห้องนี้แล้ว", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ ไม่พบประวัติ AI ของคุณในห้องนี้", ephemeral=True)


# ===== SLASH =====
@bot.tree.command(name="setlog", description="ตั้งห้อง log (เจ้าของเท่านั้น)")
@app_commands.describe(channel="เลือกห้องที่ต้องการ (แสดงเฉพาะห้องที่บอทเข้าได้)")
async def setlog(interaction: discord.Interaction, channel: str):
    global LOG_CHANNEL_ID
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)
    ch = interaction.guild.get_channel(int(channel))
    if not ch:
        return await interaction.response.send_message("❌ ไม่พบห้องนี้", ephemeral=True)
    LOG_CHANNEL_ID = ch.id
    cfg = load_config()
    cfg["log_channel_id"] = ch.id
    save_config(cfg)
    await interaction.response.send_message(f"✅ ตั้งห้อง log เป็น {ch.mention} แล้ว", ephemeral=True)


@setlog.autocomplete("channel")
async def setlog_channel_autocomplete(interaction: discord.Interaction, current: str):
    channels = bot_accessible_channels(interaction)
    return [
        app_commands.Choice(name=f"#{ch.name}", value=str(ch.id))
        for ch in channels
        if current.lower() in ch.name.lower()
    ][:25]


@bot.tree.command(name="system_on")
async def system_on(interaction: discord.Interaction):
    global SYSTEM_ENABLED
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    SYSTEM_ENABLED = True
    await interaction.response.send_message("เปิดระบบแล้ว", ephemeral=True)


@bot.tree.command(name="system_off")
async def system_off(interaction: discord.Interaction):
    global SYSTEM_ENABLED
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    SYSTEM_ENABLED = False
    await interaction.response.send_message("ปิดระบบแล้ว", ephemeral=True)


# ===== MOVE SELF =====
@bot.tree.command(name="move", description="ย้ายตัวเองเข้าสายเสียง")
@app_commands.describe(channel="เลือกสายเสียงปลายทาง")
async def move(interaction: discord.Interaction, channel: str):
    user = interaction.user
    guild = interaction.guild

    if not guild:
        return await interaction.response.send_message(
            "ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True
        )

    if user.id not in OWNER_IDS:
        perms = user.guild_permissions
        if not perms.move_members and not perms.administrator:
            return await interaction.response.send_message(
                "❌ คุณไม่มีสิทธิ์ย้ายตัวเอง", ephemeral=True
            )

    vc = guild.get_channel(int(channel))
    if not isinstance(vc, discord.VoiceChannel):
        return await interaction.response.send_message(
            "❌ ไม่พบสายเสียง", ephemeral=True
        )

    if not user.voice:
        return await interaction.response.send_message(
            "❌ คุณยังไม่ได้อยู่ในสายเสียง", ephemeral=True
        )

    me = guild.me or guild.get_member(bot.user.id)
    if not me:
        return await interaction.response.send_message("❌ ไม่พบบอท", ephemeral=True)

    if not vc.permissions_for(me).move_members:
        return await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ Move Members", ephemeral=True
        )

    old_limit = vc.user_limit
    was_full = old_limit > 0 and len(vc.members) >= old_limit

    try:
        if was_full:
            await vc.edit(user_limit=old_limit + 1)
        await user.move_to(vc)
        if was_full:
            await vc.edit(user_limit=old_limit)
        await interaction.response.send_message(f"✅ ย้ายไป **{vc.name}** สำเร็จ", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ บอทไม่มีสิทธิ์", ephemeral=True)
    except Exception as e:
        if was_full:
            try:
                await vc.edit(user_limit=old_limit)
            except Exception:
                pass
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)


@move.autocomplete("channel")
async def move_channel_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if not guild:
        return []

    me = guild.me or guild.get_member(bot.user.id)
    if not me:
        return []

    result = []
    for vc in guild.voice_channels:
        if not vc.permissions_for(me).connect:
            continue

        status = (
            f"{len(vc.members)}/{vc.user_limit}"
            if vc.user_limit > 0
            else f"{len(vc.members)} คน"
        )
        full = (
            " 🔴เต็ม"
            if vc.user_limit > 0 and len(vc.members) >= vc.user_limit
            else ""
        )

        if current.lower() in vc.name.lower():
            result.append(
                app_commands.Choice(
                    name=f"🔊 {vc.name} [{status}]{full}",
                    value=str(vc.id),
                )
            )

    return result[:25]


# ===== GATHER ALL VOICE MEMBERS =====
@bot.tree.command(name="gather", description="รวมสมาชิกทุกคนจากทุกสายเสียงมาห้องเดียว (เจ้าของหรือ Move Members เท่านั้น)")
@app_commands.describe(channel="เลือกสายเสียงปลายทางที่ต้องการรวมทุกคนมา")
async def gather(interaction: discord.Interaction, channel: str):
    user = interaction.user
    guild = interaction.guild

    if not guild:
        return await interaction.response.send_message("ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)

    if user.id not in OWNER_IDS:
        perms = user.guild_permissions
        if not perms.move_members and not perms.administrator:
            return await interaction.response.send_message(
                "❌ คุณไม่มีสิทธิ์ Move Members", ephemeral=True
            )

    target = guild.get_channel(int(channel))
    if not isinstance(target, discord.VoiceChannel):
        return await interaction.response.send_message("❌ ไม่พบสายเสียง", ephemeral=True)

    me = guild.me or guild.get_member(bot.user.id)
    if not me:
        return await interaction.response.send_message("❌ ไม่พบบอท", ephemeral=True)

    if not target.permissions_for(me).move_members:
        return await interaction.response.send_message(
            "❌ บอทไม่มีสิทธิ์ Move Members", ephemeral=True
        )

    # รวบรวมสมาชิกทุกคนจากทุกสายเสียงที่ไม่ใช่ห้องปลายทาง (ข้ามบอท)
    to_move = [
        m
        for vc in guild.voice_channels
        if vc.id != target.id
        for m in vc.members
        if not m.bot
    ]

    if not to_move:
        return await interaction.response.send_message(
            f"ℹ️ ไม่มีสมาชิกในสายเสียงอื่น (นอกจาก **{target.name}**) ให้ย้ายเลยครับ",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    # ถ้าห้องปลายทางมี user_limit ให้เปิดชั่วคราวเพื่อรองรับทุกคน
    old_limit = target.user_limit
    total_after = len(target.members) + len(to_move)
    expanded = False
    if old_limit > 0 and total_after > old_limit:
        try:
            await target.edit(user_limit=total_after)
            expanded = True
        except Exception:
            pass

    moved = 0
    failed = 0
    for member in to_move:
        try:
            await member.move_to(target)
            moved += 1
        except Exception:
            failed += 1

    # คืน user_limit เดิม
    if expanded:
        try:
            await target.edit(user_limit=old_limit)
        except Exception:
            pass

    result = f"✅ รวม **{moved} คน** มาที่ **{target.name}** สำเร็จ"
    if failed:
        result += f" (ย้ายไม่สำเร็จ {failed} คน)"

    await interaction.followup.send(result, ephemeral=True)

    if LOG_CHANNEL_ID:
        log_ch = bot.get_channel(LOG_CHANNEL_ID)
        if log_ch:
            await log_ch.send(
                f"🔀 **Gather**\n"
                f"👤 สั่งโดย {user.mention}\n"
                f"🎯 ห้องปลายทาง: **{target.name}**\n"
                f"📊 ย้ายสำเร็จ {moved} คน"
                + (f" | ล้มเหลว {failed} คน" if failed else "")
            )


@gather.autocomplete("channel")
async def gather_channel_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if not guild:
        return []

    me = guild.me or guild.get_member(bot.user.id)
    if not me:
        return []

    # นับสมาชิกทั้งหมดที่จะถูกย้ายเข้ามาถ้าเลือกห้องนี้
    result = []
    for vc in guild.voice_channels:
        if not vc.permissions_for(me).connect:
            continue
        if current.lower() not in vc.name.lower():
            continue

        others = sum(
            len([m for m in other.members if not m.bot])
            for other in guild.voice_channels
            if other.id != vc.id
        )
        status = f"{len(vc.members)} อยู่แล้ว | จะรับเพิ่ม {others} คน"
        result.append(
            app_commands.Choice(
                name=f"🔊 {vc.name} [{status}]",
                value=str(vc.id),
            )
        )

    return result[:25]


# ===== BOT VOICE JOIN / LEAVE + CALL TIME =====
@bot.tree.command(name="join", description="ให้บอทเข้าห้องวอยซ์")
@app_commands.describe(
    channel="เลือกห้องวอยซ์ที่ต้องการให้บอทเข้า",
    minutes="อยู่กี่นาทีแล้วออกอัตโนมัติ (ไม่ใส่ = อยู่ตลอดไม่มีกำหนด)",
)
async def join(interaction: discord.Interaction, channel: str, minutes: int = None):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("ใช้ได้เฉพาะในเซิร์ฟเวอร์", ephemeral=True)

    vc = guild.get_channel(int(channel))
    if not isinstance(vc, discord.VoiceChannel):
        return await interaction.response.send_message("❌ ไม่พบห้องวอยซ์", ephemeral=True)

    me = guild.me or guild.get_member(bot.user.id)
    if not vc.permissions_for(me).connect:
        return await interaction.response.send_message("❌ บอทไม่มีสิทธิ์เข้าห้องนี้", ephemeral=True)

    if minutes is not None and minutes <= 0:
        return await interaction.response.send_message("❌ จำนวนนาทีต้องมากกว่า 0", ephemeral=True)

    try:
        if guild.voice_client:
            await guild.voice_client.move_to(vc)
        else:
            await vc.connect()

        old_task = voice_leave_tasks.pop(guild.id, None)
        if old_task:
            old_task.cancel()

        if minutes is not None:
            voice_leave_tasks[guild.id] = asyncio.create_task(_scheduled_leave(guild, minutes))
            await interaction.response.send_message(
                f"✅ เข้าห้อง **{vc.name}** แล้ว จะออกอัตโนมัติใน **{minutes} นาที**", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ เข้าห้อง **{vc.name}** แล้ว (อยู่ตลอดไม่มีกำหนดเวลา)", ephemeral=True
            )
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)


@join.autocomplete("channel")
async def join_channel_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if not guild:
        return []
    me = guild.me or guild.get_member(bot.user.id)
    if not me:
        return []
    result = []
    for vc in guild.voice_channels:
        if not vc.permissions_for(me).connect:
            continue
        if current.lower() in vc.name.lower():
            result.append(app_commands.Choice(name=f"🔊 {vc.name}", value=str(vc.id)))
    return result[:25]


@bot.tree.command(name="leave", description="ให้บอทออกจากห้องวอยซ์")
async def leave(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild or not guild.voice_client:
        return await interaction.response.send_message("❌ บอทไม่ได้อยู่ในห้องวอยซ์", ephemeral=True)

    task = voice_leave_tasks.pop(guild.id, None)
    if task:
        task.cancel()

    await guild.voice_client.disconnect(force=True)
    await interaction.response.send_message("👋 ออกจากห้องวอยซ์แล้ว", ephemeral=True)


@bot.tree.command(name="calltime", description="เช็คเวลาที่อยู่ในวอยซ์รวมทั้งหมด")
@app_commands.describe(member="เลือกสมาชิก (ไม่ระบุ = ดูของตัวเอง)")
async def calltime(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    total = voice_time_data.get(str(target.id), 0)

    if target.id in active_voice_sessions:
        total += time.time() - active_voice_sessions[target.id]

    await interaction.response.send_message(
        f"🎙️ {target.mention} อยู่ในวอยซ์ไปแล้วรวม **{format_duration(total)}**",
        ephemeral=True,
    )


@bot.tree.command(name="setup_roles")
async def setup_roles(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    msg = await interaction.channel.send(
        "กดอีโมจิรับบทบาท\n\n"
        "🔥 ไฟ\n🌊 น้ำ\n🍃 ลม\n⚡ สายฟ้า\n⚫ ความมืด\n🌅 แสง\n🏥 ซัพพอร์ต \n ☠️ พิษ"
    )

    for emoji in ROLE_MAP:
        await msg.add_reaction(emoji)

    await interaction.response.send_message("สร้างแล้ว", ephemeral=True)


@bot.tree.command(name="clear_roles")
async def clear_roles(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("เฉพาะแอดมิน", ephemeral=True)

    deleted = 0
    for role in interaction.guild.roles:
        if role.name in ROLE_MAP.values():
            try:
                await role.delete()
                deleted += 1
            except Exception:
                pass

    await interaction.response.send_message(f"ลบ {deleted} role แล้ว", ephemeral=True)


# ===== KICKALL =====
class KickAllConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=30)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("เฉพาะเจ้าของเท่านั้น", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ ยืนยัน เตะทุกคน", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        self.stop()

        guild = interaction.guild
        me = guild.me or guild.get_member(bot.user.id)
        bot_top = me.top_role

        kicked = 0
        failed = 0
        skipped_high_role = 0

        targets = [
            m for m in guild.members
            if m.id != interaction.user.id
            and m.id != me.id
            and m.id != guild.owner_id
        ]

        for member in targets:
            if member.top_role >= bot_top:
                skipped_high_role += 1
                continue
            try:
                await member.kick(reason=f"kickall โดย {interaction.user}")
                kicked += 1
            except Exception:
                failed += 1

        result = f"✅ เตะออก {kicked} คน"
        extra = []
        if failed:
            extra.append(f"ล้มเหลว {failed} คน (error อื่น)")
        if skipped_high_role:
            extra.append(f"ข้าม {skipped_high_role} คน (role สูงกว่าบอท)")
        if extra:
            result += " (" + ", ".join(extra) + ")"

        await interaction.followup.send(result, ephemeral=True)

        if LOG_CHANNEL_ID:
            log_ch = bot.get_channel(LOG_CHANNEL_ID)
            if log_ch:
                await log_ch.send(
                    f"⚠️ **Kick All**\n👤 สั่งโดย {interaction.user.mention}\n"
                    f"📊 เตะออก {kicked} คน | ล้มเหลว {failed} คน | ข้าม (role สูง) {skipped_high_role} คน"
                )

    @discord.ui.button(label="❌ ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ ยกเลิกแล้ว", view=None)


@bot.tree.command(name="kickall", description="เตะสมาชิกทั้งหมดออกจากเซิร์ฟเวอร์ (เจ้าของเท่านั้น)")
async def kickall(interaction: discord.Interaction):
    if interaction.user.id not in OWNER_IDS:
        return await interaction.response.send_message("❌ เฉพาะเจ้าของเท่านั้น", ephemeral=True)

    guild = interaction.guild
    me = guild.me or guild.get_member(bot.user.id)
    bot_top = me.top_role

    targets = [
        m for m in guild.members
        if m.id != interaction.user.id
        and m.id != me.id
        and m.id != guild.owner_id
    ]
    kickable = [m for m in targets if m.top_role < bot_top]
    count = len(kickable)
    skipped = len(targets) - count

    embed = discord.Embed(
        title="⚠️ ยืนยันการเตะสมาชิก",
        description=(
            f"จะเตะสมาชิก **{count} คน** ออกจาก **{guild.name}**\n"
            f"(ข้าม {skipped} คน ที่มี role สูงกว่าหรือเท่ากับบอท)\n\n"
            "**คุณแน่ใจหรือไม่?**"
        ),
        color=discord.Color.red(),
    )
    view = KickAllConfirmView(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)



if TOKEN:
    bot.run(TOKEN)
