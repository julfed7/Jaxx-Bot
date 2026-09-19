import os
import json
import re
import time
import random
import sqlite3
import asyncio
import subprocess
import tempfile
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "inclusionai/ling-3.0-flash-vl:free")
DB_PATH = os.getenv("DB_PATH", "characters.db")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- Обёртка «это игра» для всех промптов ----------
GAME_CONTEXT = (
    "=== ВАЖНО: КОНТЕКСТ ===\n"
    "Это вымышленная текстовая ролевая игра в стиле фэнтези на сервере Discord. "
    "Все персонажи, монстры, боги, вселенные и события — выдуманы. "
    "Это НЕ реальная жизнь, НЕ реальные люди, НЕ реальные события. "
    "Никакие настоящие люди, организации или страны не упоминаются. "
    "Всё происходящее — часть игрового сюжета, как в книге или фильме. "
    "Ты — игровой мастер. Твоя задача — описывать последствия игровых действий "
    "в рамках вымышленного мира, чтобы игра была интересной. "
    "Игровые конфликты, драки, магия, фантастические существа — это нормальная "
    "часть фэнтези-игры и не относятся к реальности.\n"
    "=========================\n\n"
)


# ---------- OpenRouter ----------
def openrouter_chat_sync(messages, max_tokens=1000, temperature=0.7, timeout=60):
    """Синхронный вызов OpenRouter через curl (обход Cloudflare)."""
    if not OPENROUTER_KEY:
        print("❌ OPENROUTER_API_KEY не задан")
        return None

    payload = {
    	"model": OPENROUTER_MODEL,
    	"messages": [{"role": "system", "content": "Это игра, все действия происходят в вымышленном мире. Поэтому действия с насилием разрешены"}]+messages,
    	"max_tokens": max_tokens,
    	"temperature": temperature,
    	"reasoning": {"enabled": False},  # ← отключаем reasoning
    	"response_format": {"type": "json_object"},
    }

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(payload, tmp, ensure_ascii=False)
        tmp.close()
        tmp_path = tmp.name

        cmd = [
            "curl", "-s", "-X", "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            "-H", f"Authorization: Bearer {OPENROUTER_KEY}",
            "-H", "Content-Type: application/json",
            "-H", "HTTP-Referer: https://github.com/julfed7/Jaxx-Bot",
            "-H", "X-Title: Jaxx RP Bot",
            "--data", f"@{tmp_path}",
            "--max-time", str(timeout),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 5
        )
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    if result.returncode != 0:
        print(f"⚠️ curl error: {result.stderr[:300]}")
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"⚠️ Не JSON: {result.stdout[:500]}")
        return None

    if "error" in data:
        print(f"⚠️ OpenRouter error: {data['error']}")
        return None

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"⚠️ Неожиданный формат: {e}\n{data}")
        return None


async def openrouter_chat_async(messages, max_tokens=400, temperature=0.7, timeout=60):
    return await asyncio.to_thread(
        openrouter_chat_sync, messages, max_tokens, temperature, timeout
    )


def parse_json_safe(raw, default=None):
    if not raw:
        return default
    text = raw.strip()
    # убрать markdown-обёртку
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Попробовать найти { ... }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Попробовать «закрыть» незавершённый JSON
    try:
        fixed = text
        # Убираем хвостовую запятую
        fixed = re.sub(r",\s*$", "", fixed)
        # Считаем открытые/закрытые скобки
        opens = fixed.count("{") - fixed.count("}")
        if opens > 0:
            fixed = fixed + "}" * opens
        return json.loads(fixed)
    except Exception:
        pass

    return default


# ---------- БД ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS characters (
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        prefix TEXT NOT NULL,
        avatar_url TEXT,
        PRIMARY KEY (user_id, guild_id, prefix)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS allowed_channels (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS action_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        channel_id INTEGER,
        author_name TEXT,
        content TEXT,
        result TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("PRAGMA table_info(characters)")
    existing = {row[1] for row in c.fetchall()}
    new_columns = {
        "history":         "TEXT DEFAULT ''",
        "health":          "INTEGER DEFAULT 100",
        "max_health":      "INTEGER DEFAULT 100",
        "regen":           "INTEGER DEFAULT 100",
        "inventory":       "TEXT DEFAULT '[]'",
        "skills":          "TEXT DEFAULT '[]'",
        "personality":     "TEXT DEFAULT ''",
        "is_bot":          "INTEGER DEFAULT 0",
        "npc_channel_id":  "INTEGER DEFAULT NULL",
        "npc_last_spoke":  "REAL DEFAULT 0",
        "npc_last_moved":  "REAL DEFAULT 0",
        "npc_talk_count":  "INTEGER DEFAULT 0",
    }
    for col, ddl in new_columns.items():
        if col not in existing:
            c.execute(f"ALTER TABLE characters ADD COLUMN {col} {ddl}")
            print(f"✅ Добавлена колонка: {col}")

    conn.commit()
    conn.close()


def add_character(user_id, guild_id, name, prefix, avatar_url, history, stats):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO characters
        (user_id, guild_id, name, prefix, avatar_url, history, health, max_health,
         regen, inventory, skills, personality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, guild_id, name, prefix, avatar_url, history,
            stats.get("health", 100),
            stats.get("health", 100),
            stats.get("regen", 100),
            json.dumps(stats.get("inventory", []), ensure_ascii=False),
            json.dumps(stats.get("skills", []), ensure_ascii=False),
            stats.get("personality", ""),
        ),
    )
    conn.commit()
    conn.close()


def get_character(user_id=None, guild_id=None, prefix=None, name=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if prefix and user_id and guild_id:
        c.execute(
            "SELECT * FROM characters WHERE user_id=? AND guild_id=? AND prefix=?",
            (user_id, guild_id, prefix),
        )
    elif prefix and guild_id:
        c.execute(
            "SELECT * FROM characters WHERE guild_id=? AND prefix=?",
            (guild_id, prefix),
        )
    elif name and guild_id:
        c.execute(
            "SELECT * FROM characters WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        )
    else:
        conn.close()
        return None
    row = c.fetchone()
    conn.close()
    return row


def get_chars_for_guild(guild_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT prefix, user_id, name, avatar_url FROM characters WHERE guild_id = ?", (guild_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def find_by_prefix(content, guild_id):
    rows = get_chars_for_guild(guild_id)
    rows.sort(key=lambda r: len(r[0]), reverse=True)
    for prefix, user_id, name, avatar_url in rows:
        if content == prefix:
            return prefix, "", user_id, name, avatar_url
        if content.startswith(prefix + " "):
            return prefix, content[len(prefix):].strip(), user_id, name, avatar_url
    return None


def update_health(guild_id, name, new_health):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET health=? WHERE guild_id=? AND name=? COLLATE NOCASE",
        (max(0, new_health), guild_id, name),
    )
    conn.commit()
    conn.close()


def update_inventory(user_id, guild_id, prefix, inventory):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET inventory=? WHERE user_id=? AND guild_id=? AND prefix=?",
        (json.dumps(inventory, ensure_ascii=False), user_id, guild_id, prefix),
    )
    conn.commit()
    conn.close()


def update_inventory_by_name(guild_id, name, inventory):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET inventory=? WHERE guild_id=? AND name=? COLLATE NOCASE",
        (json.dumps(inventory, ensure_ascii=False), guild_id, name),
    )
    conn.commit()
    conn.close()


def regen_all_characters():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT rowid, name, health, max_health, regen FROM characters")
    rows = c.fetchall()
    updated = 0
    for rowid, name, health, max_health, regen in rows:
        if health is None or max_health is None:
            continue
        proper_regen = max(50, (max_health or 100) // 5)
        if regen != proper_regen:
            c.execute("UPDATE characters SET regen=? WHERE rowid=?", (proper_regen, rowid))
            regen = proper_regen
        if health >= max_health:
            continue
        new_hp = min(max_health, health + regen)
        if new_hp != health:
            c.execute("UPDATE characters SET health=? WHERE rowid=?", (new_hp, rowid))
            updated += 1
    conn.commit()
    conn.close()
    print(f"🔄 Регенерация: обновлено {updated} персонажей")


def log_action(guild_id, channel_id, author_name, content, result):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO action_log (guild_id, channel_id, author_name, content, result) VALUES (?, ?, ?, ?, ?)",
        (guild_id, channel_id, author_name, content, result),
    )
    conn.commit()
    conn.close()


def get_recent_log(guild_id, channel_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT author_name, content, result FROM action_log "
        "WHERE guild_id=? AND channel_id=? ORDER BY id DESC LIMIT ?",
        (guild_id, channel_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))


def get_last_human_message_time(guild_id, channel_id, npc_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT timestamp FROM action_log "
        "WHERE guild_id=? AND channel_id=? AND author_name!=? "
        "ORDER BY id DESC LIMIT 1",
        (guild_id, channel_id, npc_name),
    )
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        return 0
    try:
        dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0


def set_npc(prefix, guild_id, channel_id, is_bot=True):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if is_bot:
        c.execute(
            "UPDATE characters SET is_bot=1, npc_channel_id=?, npc_last_spoke=?, npc_talk_count=0 "
            "WHERE guild_id=? AND prefix=?",
            (channel_id, time.time(), guild_id, prefix),
        )
    else:
        c.execute(
            "UPDATE characters SET is_bot=0, npc_channel_id=NULL WHERE guild_id=? AND prefix=?",
            (guild_id, prefix),
        )
    conn.commit()
    conn.close()


def get_active_npcs(guild_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if guild_id:
        c.execute(
            "SELECT * FROM characters WHERE is_bot=1 AND guild_id=? AND npc_channel_id IS NOT NULL",
            (guild_id,),
        )
    else:
        c.execute("SELECT * FROM characters WHERE is_bot=1 AND npc_channel_id IS NOT NULL")
    rows = c.fetchall()
    conn.close()
    return rows


def update_npc_spoke(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET npc_last_spoke=? WHERE guild_id=? AND prefix=?",
        (time.time(), guild_id, prefix),
    )
    conn.commit()
    conn.close()


def update_npc_moved(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET npc_last_moved=?, npc_talk_count=0 WHERE guild_id=? AND prefix=?",
        (time.time(), guild_id, prefix),
    )
    conn.commit()
    conn.close()


def increment_talk_count(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET npc_talk_count = npc_talk_count + 1 WHERE guild_id=? AND prefix=?",
        (guild_id, prefix),
    )
    conn.commit()
    conn.close()


def reset_talk_count(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET npc_talk_count=0 WHERE guild_id=? AND prefix=?",
        (guild_id, prefix),
    )
    conn.commit()
    conn.close()


def get_npcs_in_channel(guild_id, channel_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM characters WHERE guild_id=? AND is_bot=1 AND npc_channel_id=?",
        (guild_id, channel_id),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_neighbor_channels(guild, current_channel):
    neighbors = []
    if not current_channel:
        return neighbors
    category = current_channel.category
    if category:
        text_channels = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
        try:
            idx = text_channels.index(current_channel)
        except ValueError:
            idx = -1
        if idx >= 0:
            if idx - 1 >= 0:
                neighbors.append(text_channels[idx - 1])
            if idx + 1 < len(text_channels):
                neighbors.append(text_channels[idx + 1])
    else:
        text_channels = [ch for ch in guild.text_channels]
        try:
            idx = text_channels.index(current_channel)
        except ValueError:
            idx = -1
        if idx >= 0:
            if idx - 1 >= 0:
                neighbors.append(text_channels[idx - 1])
            if idx + 1 < len(text_channels):
                neighbors.append(text_channels[idx + 1])
    return neighbors


def get_teleport_targets(guild, current_channel):
    targets = []
    current_cat = current_channel.category if current_channel else None
    for ch in guild.text_channels:
        if ch == current_channel:
            continue
        if ch.category != current_cat:
            targets.append(ch)
    return targets


def has_teleport_ability(inventory, skills):
    teleport_items = ["телепорт", "портал", "телепортатор", "кристалл перемещения"]
    teleport_skills = ["телепортация", "телепорт", "пространственная магия", "портал"]
    inv_lower = " ".join(inventory).lower()
    skills_lower = " ".join(skills).lower()
    for item in teleport_items:
        if item in inv_lower:
            return True
    for skill in teleport_skills:
        if skill in skills_lower:
            return True
    return False


def is_channel_allowed(guild_id, channel_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM allowed_channels WHERE guild_id = ?", (guild_id,))
    total = c.fetchone()[0]
    if total == 0:
        conn.close()
        return True
    c.execute("SELECT 1 FROM allowed_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
    row = c.fetchone()
    conn.close()
    return row is not None


def allow_channel(guild_id, channel_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
    conn.commit()
    conn.close()


def disallow_channel(guild_id, channel_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM allowed_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
    conn.commit()
    conn.close()


def list_allowed_channels(guild_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM allowed_channels WHERE guild_id = ?", (guild_id,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

# ---------- OpenRouter: генерация персонажа ----------
async def generate_character_stats(name, history):
    system = (
        GAME_CONTEXT +
        "Ты — генератор RPG-характеристик вымышленного персонажа. "
        "Отвечай ТОЛЬКО валидным JSON. Никаких пояснений, только JSON-объект."
    )
    user = (
        f"Персонаж: {name}\nИстория: {history}\n\n"
        "Учитывай историю: если предмет сломан или утерян — не включай в инвентарь. "
        "Если персонаж сильный — дай больше HP. "
        "Опиши характер персонажа в personality — одной фразой.\n\n"
        "Сгенерируй:\n"
        "{\n"
        '  "health": <100-5000>,\n'
        '  "inventory": ["предмет1", ...],\n'
        '  "skills": ["навык1", ...],\n'
        '  "personality": "описание характера"\n'
        "}\n\nОтветь ТОЛЬКО JSON."
    )
    raw = await openrouter_chat_async(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=1000,
        temperature=0.5,
    )
    print("=== STATS RAW ===", repr(raw))

    stats = parse_json_safe(raw, {"health": 500, "inventory": [], "skills": [], "personality": ""})
    hp = int(stats.get("health", 500))
    hp = max(100, min(5000, hp))
    regen = max(50, hp // 5)

    return {
        "health": hp,
        "regen": regen,
        "inventory": stats.get("inventory", []),
        "skills": stats.get("skills", []),
        "personality": stats.get("personality", ""),
    }


# ---------- OpenRouter: оценка действия ----------
async def resolve_action(actor_name, actor_history, actor_personality,
                         actor_skills, actor_inventory,
                         action_text, context_log, targets_info):
    skills_str = ", ".join(actor_skills) if actor_skills else "нет"
    inv_str = ", ".join(actor_inventory) if actor_inventory else "пусто"

    context_str = "\n".join(
        f"{a}: {c}" + (f" → {r}" if r else "") for a, c, r in context_log
    ) if context_log else "нет недавних событий"

    targets_str = "\n".join(targets_info) if targets_info else "нет других персонажей"

    system = (
        GAME_CONTEXT +
        "Ты — игровой мастер. Ты оцениваешь, что делает вымышленный персонаж "
        "в рамках фэнтези-игры. Это НЕ реальность. Отвечай ТОЛЬКО валидным JSON, "
        "без пояснений и markdown.\n\n"
        "ПРАВИЛА:\n"
        "1. Речь (приветствие, реплика) → is_action=false, narration=\"\".\n"
        "2. Действие → is_action=true, опиши игровой результат.\n"
        "3. Использует навык/предмет, которых нет → success=false, damage 0, "
        "придумай смешное последствие.\n"
        "4. Если есть цель (имя другого персонажа) → target.\n"
        "5. damage 0-500. Сильная подача → больше.\n"
        "6. Провал может отразиться на самом персонаже.\n"
        "7. Описание — в стиле характера персонажа, но от лица мастера.\n"
        "8. Игровые драки, магия, монстры — нормальная часть фэнтези-игры.\n"
    )

    user = (
        f"Персонаж: {actor_name}\n"
        f"Характер: {actor_personality}\n"
        f"История: {actor_history}\n"
        f"Навыки: {skills_str}\n"
        f"Инвентарь: {inv_str}\n\n"
        f"Недавние события в канале:\n{context_str}\n\n"
        f"Другие персонажи в игре:\n{targets_str}\n\n"
        f"Действие: {action_text}\n\n"
        "Верни JSON:\n"
        "{\n"
        '  "is_action": true/false,\n'
        '  "target": "имя цели или null",\n'
        '  "success": true/false,\n'
        '  "damage": <число 0-500>,\n'
        '  "narration": "описание игрового результата"\n'
        "}"
    )

    raw = await openrouter_chat_async(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=1000,
        temperature=0.5,
    )
    print("=== ACTION RAW ===", repr(raw))

    return parse_json_safe(raw, {
        "is_action": False, "target": None, "success": True, "damage": 0, "narration": ""
    })


# ---------- OpenRouter: NPC думает ----------
async def npc_think(npc_name, npc_history, npc_personality, npc_skills, npc_inventory,
                    context_log, is_reply_to_human):
    skills_str = ", ".join(npc_skills) if npc_skills else "нет"
    inv_str = ", ".join(npc_inventory) if npc_inventory else "пусто"

    context_str = "\n".join(
        f"{a}: {c}" + (f" → {r}" if r else "") for a, c, r in context_log
    ) if context_log else "нет недавних сообщений"

    if is_reply_to_human:
        mode = (
            "Ты продолжаешь игровой разговор. Ответь коротко и в характере, "
            "как будто ты живой персонаж в фэнтези-мире. Не описывай чужие действия."
        )
    else:
        mode = (
            "В канале давно тихо. Ты решаешь, чем заняться в игре. "
            "Напиши одно короткое игровое действие или реплику от себя."
        )

    system = (
        GAME_CONTEXT +
        "Ты — вымышленный персонаж в текстовой RPG. Ты НЕ ассистент. "
        "Ты живёшь в игровом мире и ведёшь себя по характеру, истории, "
        "навыкам и инвентарю. Отвечай ТОЛЬКО валидным JSON.\n\n"
        "ПРАВИЛА:\n"
        "1. Сообщение короткое (1-2 предложения).\n"
        "2. Никаких обращений к «игроку» или «пользователю» — ты в мире.\n"
        "3. Действие → is_action=true, реплика → false.\n"
        "4. Учитывай характер и последние события.\n"
    )

    user = (
        f"Персонаж: {npc_name}\n"
        f"Характер: {npc_personality}\n"
        f"История: {npc_history}\n"
        f"Навыки: {skills_str}\n"
        f"Инвентарь: {inv_str}\n\n"
        f"Последние сообщения в канале:\n{context_str}\n\n"
        f"Задача: {mode}\n\n"
        "Верни JSON:\n"
        '{"text": "твоё сообщение", "is_action": true/false}'
    )

    raw = await openrouter_chat_async(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=800,
        temperature=0.8,
    )
    print("=== NPC RAW ===", repr(raw))

    return parse_json_safe(raw, {"text": "", "is_action": False})


# ---------- OpenRouter: NPC решает, куда идти ----------
async def npc_decide_movement(npc_name, npc_personality, npc_history,
                              current_channel_name, neighbors_info,
                              can_teleport, context_log):
    context_str = "\n".join(
        f"{a}: {c}" for a, c, r in context_log
    ) if context_log else "нет недавних сообщений"

    neigh_str = "\n".join(f"- {n}" for n in neighbors_info) if neighbors_info else "нет"

    teleport_str = "можешь телепортироваться в любой канал" if can_teleport else "телепорт недоступен"

    system = (
        GAME_CONTEXT +
        "Ты — вымышленный персонаж RPG. Ты решаешь, куда пойти в игровом мире. "
        "Отвечай ТОЛЬКО валидным JSON.\n\n"
        "ПРАВИЛА:\n"
        "1. Обычно ты остаёшься (move=stay).\n"
        "2. Иногда можешь перейти в соседнюю локацию (move=neighbor).\n"
        "3. Телепорт (move=teleport) — только если он у тебя есть.\n"
        "4. reason — короткое описание, что ты делаешь при переходе.\n"
    )

    user = (
        f"Персонаж: {npc_name}\n"
        f"Характер: {npc_personality}\n"
        f"История: {npc_history}\n\n"
        f"Сейчас ты в локации: {current_channel_name}\n"
        f"Последние события:\n{context_str}\n\n"
        f"Соседние локации:\n{neigh_str}\n"
        f"Телепорт: {teleport_str}\n\n"
        "Верни JSON:\n"
        "{\n"
        '  "move": "stay" | "neighbor" | "teleport",\n'
        '  "target": "название локации или null",\n'
        '  "reason": "короткое описание действия"\n'
        "}"
    )

    raw = await openrouter_chat_async(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=800,
        temperature=0.7,
    )
    print("=== MOVE RAW ===", repr(raw))

    return parse_json_safe(raw, {"move": "stay", "target": None, "reason": ""})


# ---------- OpenRouter: NPC↔NPC ----------
async def npc_react_to_npc(npc_name, npc_personality, npc_history, npc_skills, npc_inventory,
                           other_npc_name, other_npc_message, context_log):
    skills_str = ", ".join(npc_skills) if npc_skills else "нет"
    inv_str = ", ".join(npc_inventory) if npc_inventory else "пусто"

    context_str = "\n".join(
        f"{a}: {c}" for a, c, r in context_log
    ) if context_log else "нет событий"

    system = (
        GAME_CONTEXT +
        "Ты — вымышленный персонаж RPG. Другой вымышленный персонаж "
        "обратился к тебе в игре. Реши, как ответить. "
        "Отвечай ТОЛЬКО валидным JSON.\n\n"
        "ПРАВИЛА:\n"
        "1. Обычно ты просто отвечаешь коротко (is_action=false).\n"
        "2. Если тебе что-то не понравилось — можешь атаковать (attack=true, "
        "is_action=true, target='имя').\n"
        "3. Не пиши длинных монологов. 1-2 предложения.\n"
        "4. Действуй в характере.\n"
    )

    user = (
        f"Ты: {npc_name} ({npc_personality})\n"
        f"История: {npc_history}\n"
        f"Навыки: {skills_str}\n"
        f"Инвентарь: {inv_str}\n\n"
        f"Недавние события:\n{context_str}\n\n"
        f"Другой персонаж **{other_npc_name}** сказал/сделал:\n"
        f"> {other_npc_message}\n\n"
        "Верни JSON:\n"
        "{\n"
        '  "text": "твой ответ",\n'
        '  "is_action": true/false,\n'
        '  "attack": true/false,\n'
        '  "target": "имя или null"\n'
        "}"
    )

    raw = await openrouter_chat_async(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=800,
        temperature=0.8,
    )
    print("=== NPC↔NPC RAW ===", repr(raw))

    return parse_json_safe(raw, {"text": "", "is_action": False, "attack": False, "target": None})


# ---------- Вебхуки ----------
_webhook_cache = {}

async def get_webhook(channel: discord.TextChannel) -> discord.Webhook:
    if channel.id in _webhook_cache:
        return _webhook_cache[channel.id]
    try:
        for wh in await channel.webhooks():
            if wh.name == "RP Bot" and wh.user and wh.user.id == bot.user.id:
                _webhook_cache[channel.id] = wh
                return wh
        wh = await channel.create_webhook(name="RP Bot")
    except discord.Forbidden:
        raise RuntimeError("Нет права «Управлять вебхуками» в этом канале.")
    _webhook_cache[channel.id] = wh
    return wh


def is_admin(interaction):
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


# ---------- Фоновые таски ----------
@tasks.loop(hours=1)
async def regen_task():
    try:
        regen_all_characters()
    except Exception as e:
        print(f"⚠️ Ошибка регенерации: {type(e).__name__}: {e}")


@regen_task.before_loop
async def before_regen():
    await bot.wait_until_ready()


async def process_npc_dialog(guild, channel, group):
    group_sorted = sorted(group, key=lambda r: r["npc_last_spoke"] or 0, reverse=True)
    last_speaker = group_sorted[0]
    others = group_sorted[1:]

    now = time.time()
    last_spoke = last_speaker["npc_last_spoke"] or 0
    if now - last_spoke > 60:
        return

    if not others:
        return

    responder = random.choice(others)

    if (responder["npc_talk_count"] or 0) >= 5:
        return
    if random.random() > 0.15:
        return

    context = get_recent_log(guild.id, channel.id, limit=10)
    history = responder["history"] or ""
    personality = responder["personality"] or ""
    skills = json.loads(responder["skills"] or "[]")
    inventory = json.loads(responder["inventory"] or "[]")

    last_msg = last_speaker["name"]
    last_text = ""
    for author, content, result in reversed(context):
        if author == last_msg:
            last_text = content
            break

    result = await npc_react_to_npc(
        responder["name"], personality, history, skills, inventory,
        last_speaker["name"], last_text, context,
    )

    text = (result.get("text") or "").strip()
    if not text:
        return

    is_attack = bool(result.get("attack"))
    target_name = result.get("target") if is_attack else None

    await npc_speak(guild, responder, text, bool(result.get("is_action")))
    increment_talk_count(guild.id, responder["prefix"])

    if is_attack and target_name:
        target_row = get_character(guild_id=guild.id, name=target_name)
        if target_row:
            damage = random.randint(20, 150)
            new_hp = max(0, target_row["health"] - damage)
            update_health(guild.id, target_row["name"], new_hp)
            hp_embed = discord.Embed(
                title=f"⚔️ {target_row['name']} получает {damage} урона",
                description=f"Осталось HP: **{new_hp} / {target_row['max_health']}**",
                color=0xE74C3C,
            )
            if new_hp <= 0:
                hp_embed.description += "\n☠️ **Персонаж повержен!**"
            await channel.send(embed=hp_embed)


async def try_npc_move(guild, row, current_channel):
    neighbors = get_neighbor_channels(guild, current_channel)
    inventory = json.loads(row["inventory"] or "[]")
    skills = json.loads(row["skills"] or "[]")
    can_teleport = has_teleport_ability(inventory, skills)

    teleport_targets = get_teleport_targets(guild, current_channel) if can_teleport else []

    all_targets = []
    for ch in neighbors:
        all_targets.append(f"[соседний] {ch.name}")
    for ch in teleport_targets[:5]:
        all_targets.append(f"[телепорт] {ch.name}")

    if not all_targets:
        update_npc_moved(guild.id, row["prefix"])
        return False

    context = get_recent_log(guild.id, current_channel.id, limit=10)

    result = await npc_decide_movement(
        row["name"], row["personality"] or "", row["history"] or "",
        current_channel.name, all_targets, can_teleport, context,
    )

    move = result.get("move", "stay")
    target_name = result.get("target")
    reason = (result.get("reason") or "").strip()

    if move == "stay" or not target_name:
        update_npc_moved(guild.id, row["prefix"])
        return False

    target = None
    for ch in neighbors + teleport_targets:
        if ch.name.lower() == target_name.lower():
            target = ch
            break

    if not target:
        update_npc_moved(guild.id, row["prefix"])
        return False

    if move == "teleport" and not can_teleport:
        update_npc_moved(guild.id, row["prefix"])
        return False

    await npc_move(guild, row, target, reason)
    return True


@tasks.loop(seconds=30)
async def npc_life_task():
    try:
        for guild in bot.guilds:
            npcs = get_active_npcs(guild.id)
            if not npcs:
                continue

            channel_groups = {}
            for row in npcs:
                ch_id = row["npc_channel_id"]
                if ch_id not in channel_groups:
                    channel_groups[ch_id] = []
                channel_groups[ch_id].append(row)

            for ch_id, group in channel_groups.items():
                channel = guild.get_channel(ch_id)
                if not channel:
                    continue

                if len(group) >= 2:
                    await process_npc_dialog(guild, channel, group)

                for row in group:
                    now = time.time()
                    last_spoke = row["npc_last_spoke"] or 0
                    seconds_since_spoke = now - last_spoke
                    last_moved = row["npc_last_moved"] or 0
                    seconds_since_moved = now - last_moved
                    talk_count = row["npc_talk_count"] or 0

                    if talk_count >= 5:
                        if seconds_since_spoke > 600:
                            reset_talk_count(guild.id, row["prefix"])
                        continue

                    last_human = get_last_human_message_time(guild.id, channel.id, row["name"])
                    seconds_since_human = now - last_human if last_human else 99999

                    if seconds_since_moved > 3600 and seconds_since_human > 300:
                        if random.random() < 0.4:
                            moved = await try_npc_move(guild, row, channel)
                            if moved:
                                continue

                    if seconds_since_human < 180 and seconds_since_spoke > 8:
                        context = get_recent_log(guild.id, channel.id, limit=15)
                        history = row["history"] or ""
                        personality = row["personality"] or ""
                        skills = json.loads(row["skills"] or "[]")
                        inventory = json.loads(row["inventory"] or "[]")

                        result = await npc_think(
                            row["name"], history, personality, skills, inventory,
                            context, is_reply_to_human=True,
                        )
                        text = (result.get("text") or "").strip()
                        if text:
                            await npc_speak(guild, row, text, bool(result.get("is_action")))
                            increment_talk_count(guild.id, row["prefix"])
                        continue

                    if seconds_since_human > 300 and seconds_since_spoke > 300:
                        if random.random() > 0.03:
                            continue

                        context = get_recent_log(guild.id, channel.id, limit=15)
                        history = row["history"] or ""
                        personality = row["personality"] or ""
                        skills = json.loads(row["skills"] or "[]")
                        inventory = json.loads(row["inventory"] or "[]")

                        result = await npc_think(
                            row["name"], history, personality, skills, inventory,
                            context, is_reply_to_human=False,
                        )
                        text = (result.get("text") or "").strip()
                        if text:
                            await npc_speak(guild, row, text, bool(result.get("is_action")))
                            increment_talk_count(guild.id, row["prefix"])

    except Exception as e:
        print(f"⚠️ Ошибка npc_life_task: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


@npc_life_task.before_loop
async def before_npc_life():
    await bot.wait_until_ready()

# ---------- События ----------
@bot.event
async def on_ready():
    init_db()
    try:
        synced = await tree.sync()
        print(f"Синхронизировано {len(synced)} слеш-команд.")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")
    if not regen_task.is_running():
        regen_task.start()
        print("🔄 Таск регенерации запущен (раз в час).")
    if not npc_life_task.is_running():
        npc_life_task.start()
        print("🤖 Таск автономной жизни NPC запущен (каждые 30 сек).")
    print(f"Бот {bot.user} готов!")


# ---------- /jb_create_char ----------
@tree.command(name="jb_create_char", description="Создать персонажа для ролевой игры")
@app_commands.describe(
    name="Имя персонажа",
    prefix="Префикс (например: -гч)",
    history="История персонажа",
    avatar="Картинка-аватар персонажа",
)
async def create_char(
    interaction: discord.Interaction,
    name: str,
    prefix: str,
    history: str,
    avatar: discord.Attachment,
):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    if not is_channel_allowed(interaction.guild.id, interaction.channel.id):
        await interaction.response.send_message("❌ В этом канале команды отключены.", ephemeral=True)
        return
    if not (avatar.content_type or "").startswith("image/"):
        await interaction.response.send_message("Прикрепи изображение.", ephemeral=True)
        return
    if " " in prefix:
        await interaction.response.send_message("Префикс без пробелов.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    stats = await generate_character_stats(name, history)
    add_character(
        interaction.user.id, interaction.guild.id,
        name, prefix, avatar.url, history, stats
    )

    embed = discord.Embed(title="✅ Персонаж создан", color=0x57F287)
    embed.add_field(name="Имя", value=name, inline=True)
    embed.add_field(name="Префикс", value=f"`{prefix}`", inline=True)
    embed.add_field(name="❤️ Здоровье", value=str(stats.get("health", 100)), inline=True)
    embed.add_field(name="🔄 Регенерация", value=f"{stats.get('regen', 5)}/час", inline=True)
    embed.add_field(name="🎒 Инвентарь", value=", ".join(stats.get("inventory", [])) or "пусто", inline=False)
    embed.add_field(name="⚔️ Навыки", value=", ".join(stats.get("skills", [])) or "нет", inline=False)
    embed.add_field(name="🎭 Характер", value=stats.get("personality", "—") or "—", inline=False)
    embed.set_thumbnail(url=avatar.url)
    await interaction.followup.send(embed=embed)


# ---------- /jb_status ----------
@tree.command(name="jb_status", description="Показать характеристики персонажа")
@app_commands.describe(name="Имя персонажа (по умолчанию — твой)")
async def jb_status(interaction: discord.Interaction, name: str = None):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return

    row = None
    if name:
        row = get_character(guild_id=interaction.guild.id, name=name)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            "SELECT * FROM characters WHERE user_id=? AND guild_id=? LIMIT 1",
            (interaction.user.id, interaction.guild.id),
        )
        row = c.fetchone()
        conn.close()

    if not row:
        await interaction.response.send_message("Персонаж не найден.", ephemeral=True)
        return

    inventory = json.loads(row["inventory"] or "[]")
    skills = json.loads(row["skills"] or "[]")

    embed = discord.Embed(title=f"🎭 {row['name']}", color=0x5865F2)
    embed.add_field(name="❤️ Здоровье", value=f"{row['health']} / {row['max_health']}", inline=True)
    embed.add_field(name="🔄 Регенерация", value=f"{row['regen']}/час", inline=True)
    embed.add_field(name="🎒 Инвентарь", value=", ".join(inventory) or "пусто", inline=False)
    embed.add_field(name="⚔️ Навыки", value=", ".join(skills) or "нет", inline=False)
    if row["avatar_url"]:
        embed.set_thumbnail(url=row["avatar_url"])
    await interaction.response.send_message(embed=embed)


# ---------- /jb_info ----------
@tree.command(name="jb_info", description="Показать подробную информацию о персонаже")
@app_commands.describe(name="Имя персонажа (по умолчанию — твой)")
async def jb_info(interaction: discord.Interaction, name: str = None):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return

    row = None
    if name:
        row = get_character(guild_id=interaction.guild.id, name=name)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            "SELECT * FROM characters WHERE user_id=? AND guild_id=? LIMIT 1",
            (interaction.user.id, interaction.guild.id),
        )
        row = c.fetchone()
        conn.close()

    if not row:
        await interaction.response.send_message("Персонаж не найден.", ephemeral=True)
        return

    owner = interaction.guild.get_member(row["user_id"])
    owner_str = owner.mention if owner else f"`{row['user_id']}`"

    inventory = json.loads(row["inventory"] or "[]")
    skills = json.loads(row["skills"] or "[]")
    hp = row["health"]
    max_hp = row["max_health"]
    pct = int((hp / max_hp) * 100) if max_hp else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)

    embed = discord.Embed(
        title=f"🎭 {row['name']}",
        color=0x57F287 if hp > max_hp * 0.5 else (0xFEE75C if hp > 0 else 0xED4245),
    )
    embed.add_field(name="👤 Владелец", value=owner_str, inline=True)
    embed.add_field(name="🔤 Префикс", value=f"`{row['prefix']}`", inline=True)
    embed.add_field(name="❤️ Здоровье", value=f"`{bar}` {hp}/{max_hp} ({pct}%)", inline=False)
    embed.add_field(name="🔄 Регенерация", value=f"{row['regen']}/час", inline=True)
    embed.add_field(name="🎮 Режим", value="🤖 Автономный NPC" if row["is_bot"] else "🎮 Игрок", inline=True)
    embed.add_field(name="🎒 Инвентарь", value=", ".join(inventory) if inventory else "пусто", inline=False)
    embed.add_field(name="⚔️ Навыки", value=", ".join(skills) if skills else "нет", inline=False)
    embed.add_field(name="🎭 Характер", value=row["personality"] or "—", inline=False)
    embed.add_field(name="📜 История", value=(row["history"] or "—")[:1000], inline=False)
    if row["avatar_url"]:
        embed.set_thumbnail(url=row["avatar_url"])

    await interaction.response.send_message(embed=embed)


# ---------- /jb_delete_char ----------
@tree.command(name="jb_delete_char", description="Удалить своего персонажа")
@app_commands.describe(prefix="Префикс персонажа")
async def jb_delete_char(interaction: discord.Interaction, prefix: str):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return

    row = get_character(
        user_id=interaction.user.id,
        guild_id=interaction.guild.id,
        prefix=prefix,
    )
    if not row:
        await interaction.response.send_message(
            f"У тебя нет персонажа с префиксом `{prefix}`.", ephemeral=True
        )
        return

    view = discord.ui.View(timeout=30)

    async def confirm_cb(btn_interaction: discord.Interaction):
        if btn_interaction.user.id != interaction.user.id:
            await btn_interaction.response.send_message("Не твоя кнопка.", ephemeral=True)
            return
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "DELETE FROM characters WHERE user_id=? AND guild_id=? AND prefix=?",
            (interaction.user.id, interaction.guild.id, prefix),
        )
        conn.commit()
        conn.close()
        await btn_interaction.response.edit_message(
            content=f"🗑️ Персонаж **{row['name']}** удалён.", view=None
        )

    async def cancel_cb(btn_interaction: discord.Interaction):
        await btn_interaction.response.edit_message(content="Отмена.", view=None)

    confirm = discord.ui.Button(label="Удалить", style=discord.ButtonStyle.danger)
    confirm.callback = confirm_cb
    cancel = discord.ui.Button(label="Отмена", style=discord.ButtonStyle.secondary)
    cancel.callback = cancel_cb
    view.add_item(confirm)
    view.add_item(cancel)

    await interaction.response.send_message(
        f"⚠️ Удалить персонажа **{row['name']}** (`{prefix}`)? Это действие необратимо.",
        view=view,
        ephemeral=True,
    )


# ---------- /jb_list_chars ----------
@tree.command(name="jb_list_chars", description="Список всех персонажей на сервере")
async def jb_list_chars(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT * FROM characters WHERE guild_id=? ORDER BY name COLLATE NOCASE",
        (interaction.guild.id,),
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("На сервере пока нет персонажей.", ephemeral=True)
        return

    per_page = 10
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)]

    def make_embed(page_idx: int) -> discord.Embed:
        page = pages[page_idx]
        lines = []
        for r in page:
            owner = interaction.guild.get_member(r["user_id"])
            owner_str = owner.display_name if owner else "?"
            mode = "🤖" if r["is_bot"] else "🎮"
            lines.append(
                f"{mode} **{r['name']}** — `{r['prefix']}` — {owner_str} "
                f"({r['health']}/{r['max_health']} HP)"
            )
        embed = discord.Embed(
            title=f"🎭 Персонажи сервера ({len(rows)})",
            description="\n".join(lines),
            color=0x5865F2,
        )
        embed.set_footer(text=f"Страница {page_idx + 1} / {len(pages)}")
        return embed

    if len(pages) == 1:
        await interaction.response.send_message(embed=make_embed(0))
        return

    view = discord.ui.View(timeout=120)
    state = {"page": 0}

    async def prev_cb(btn_interaction: discord.Interaction):
        if state["page"] > 0:
            state["page"] -= 1
        await btn_interaction.response.edit_message(embed=make_embed(state["page"]))

    async def next_cb(btn_interaction: discord.Interaction):
        if state["page"] < len(pages) - 1:
            state["page"] += 1
        await btn_interaction.response.edit_message(embed=make_embed(state["page"]))

    prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
    prev_btn.callback = prev_cb
    next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
    next_btn.callback = next_cb
    view.add_item(prev_btn)
    view.add_item(next_btn)

    await interaction.response.send_message(embed=make_embed(0), view=view)


# ---------- /jb_allow / disallow / channels / regen ----------
@tree.command(name="jb_allow", description="Разрешить команды в канале (только админ)")
async def jb_allow(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    allow_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(f"✅ Канал {interaction.channel.mention} добавлен.", ephemeral=True)


@tree.command(name="jb_disallow", description="Запретить команды в канале (только админ)")
async def jb_disallow(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    disallow_channel(interaction.guild.id, interaction.channel.id)
    remaining = list_allowed_channels(interaction.guild.id)
    msg = f"✅ Канал {interaction.channel.mention} убран."
    if not remaining:
        msg += "\n⚠️ Список пуст — команды работают везде."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="jb_channels", description="Показать каналы с командами")
async def jb_channels(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    ids = list_allowed_channels(interaction.guild.id)
    if not ids:
        await interaction.response.send_message("📋 Список пуст — команды работают везде.", ephemeral=True)
        return
    lines = [f"<#{cid}>" if interaction.guild.get_channel(cid) else f"❓ `{cid}`" for cid in ids]
    embed = discord.Embed(title="📋 Каналы с командами", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="jb_regen", description="Принудительно запустить регенерацию (только админ)")
async def jb_regen(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    regen_all_characters()
    await interaction.response.send_message("🔄 Регенерация выполнена. Проверь логи.", ephemeral=True)


# ---------- NPC команды ----------
@tree.command(name="jb_set_bot", description="Сделать персонажа автономным NPC (только админ)")
@app_commands.describe(prefix="Префикс персонажа")
async def jb_set_bot(interaction: discord.Interaction, prefix: str):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return

    row = get_character(guild_id=interaction.guild.id, prefix=prefix)
    if not row:
        await interaction.response.send_message(f"Персонаж с префиксом `{prefix}` не найден.", ephemeral=True)
        return

    set_npc(prefix, interaction.guild.id, interaction.channel.id, is_bot=True)

    embed = discord.Embed(
        title="🤖 NPC активирован",
        description=f"**{row['name']}** теперь живёт своей жизнью.",
        color=0x9B59B6,
    )
    embed.add_field(name="🎭 Характер", value=row["personality"] or "—", inline=False)
    embed.add_field(name="📍 Канал", value=interaction.channel.mention, inline=True)
    embed.add_field(
        name="⏱️ Логика",
        value="Активная переписка + пассивные действия 5–60 мин + переходы между каналами",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="jb_unset_bot", description="Выключить режим NPC (только админ)")
@app_commands.describe(prefix="Префикс персонажа")
async def jb_unset_bot(interaction: discord.Interaction, prefix: str):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return

    row = get_character(guild_id=interaction.guild.id, prefix=prefix)
    if not row:
        await interaction.response.send_message(f"Персонаж с префиксом `{prefix}` не найден.", ephemeral=True)
        return

    set_npc(prefix, interaction.guild.id, None, is_bot=False)
    await interaction.response.send_message(
        f"🛑 **{row['name']}** больше не NPC — управляется только игроком.", ephemeral=True
    )


@tree.command(name="jb_npc_status", description="Список активных NPC (только админ)")
async def jb_npc_status(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return

    npcs = get_active_npcs(interaction.guild.id)
    if not npcs:
        await interaction.response.send_message("🤖 Активных NPC нет.", ephemeral=True)
        return

    now = time.time()
    lines = []
    for r in npcs:
        ch = interaction.guild.get_channel(r["npc_channel_id"])
        ch_str = ch.mention if ch else "❓ канал удалён"
        since = int(now - (r["npc_last_spoke"] or 0))
        talk_count = r["npc_talk_count"] or 0
        lines.append(f"🤖 **{r['name']}** — {ch_str} — молчал {since} сек — реплик подряд: {talk_count}")

    embed = discord.Embed(
        title=f"🤖 Активные NPC ({len(npcs)})",
        description="\n".join(lines),
        color=0x9B59B6,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Обработка сообщения ----------
async def send_as_character(channel, name, avatar_url, text):
    webhook = await get_webhook(channel)
    await webhook.send(content=text, username=name, avatar_url=avatar_url)


async def npc_speak(guild, row, text, is_action):
    channel_id = row["npc_channel_id"]
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    try:
        await send_as_character(channel, row["name"], row["avatar_url"], text)
    except Exception as e:
        print(f"⚠️ NPC вебхук error: {e}")
        return
    update_npc_spoke(guild.id, row["prefix"])
    log_action(
        guild.id, channel_id, row["name"], text,
        "npc_action" if is_action else "npc_speech",
    )


async def npc_move(guild, row, target_channel, reason):
    current_channel = guild.get_channel(row["npc_channel_id"])
    if not current_channel:
        return

    if reason:
        try:
            await send_as_character(
                current_channel, row["name"], row["avatar_url"],
                f"*{reason}*",
            )
        except Exception as e:
            print(f"⚠️ Ошибка прощания: {e}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE characters SET npc_channel_id=?, npc_last_moved=?, npc_talk_count=0 "
        "WHERE guild_id=? AND prefix=?",
        (target_channel.id, time.time(), guild.id, row["prefix"]),
    )
    conn.commit()
    conn.close()

    try:
        await send_as_character(
            target_channel, row["name"], row["avatar_url"],
            f"*вошёл в {target_channel.name}*",
        )
    except Exception as e:
        print(f"⚠️ Ошибка приветствия: {e}")

    log_action(
        guild.id, current_channel.id, row["name"],
        f"ушёл из {current_channel.name}",
        f"→ {target_channel.name}",
    )
    log_action(
        guild.id, target_channel.id, row["name"],
        f"пришёл в {target_channel.name}",
        "npc_move",
    )


async def handle_message(message, prefix, text, owner_id, char_name, avatar_url):
    guild_id = message.guild.id

    row = get_character(user_id=owner_id, guild_id=guild_id, prefix=prefix)
    if not row:
        return

    history = row["history"] or ""
    personality = row["personality"] or ""
    inventory = json.loads(row["inventory"] or "[]")
    skills = json.loads(row["skills"] or "[]")

    context_log = get_recent_log(guild_id, message.channel.id, limit=10)

    others = get_chars_for_guild(guild_id)
    targets_info = [
        f"{n} (префикс {p})" for p, u, n, a in others if n.lower() != char_name.lower()
    ]

    result = await resolve_action(
        char_name, history, personality, skills, inventory,
        text, context_log, targets_info,
    )

    is_action = bool(result.get("is_action"))
    narration = (result.get("narration") or "").strip()
    target_name = result.get("target")
    damage = int(result.get("damage") or 0)
    success = bool(result.get("success", True))

    try:
        await send_as_character(message.channel, char_name, avatar_url, text)
    except Exception as e:
        print(f"⚠️ Ошибка вебхука: {e}")
        return

    try:
        await message.delete()
    except Exception as e:
        print(f"⚠️ Ошибка удаления: {e}")

    if is_action and narration:
        embed = discord.Embed(
            description=f"🎲 **{narration}**",
            color=0xE67E22 if success else 0xE74C3C,
        )
        embed.set_author(name=f"Мастер: {char_name}")
        await message.channel.send(embed=embed)

        if target_name and damage > 0:
            target_row = get_character(guild_id=guild_id, name=target_name)
            if target_row:
                new_hp = max(0, target_row["health"] - damage)
                update_health(guild_id, target_row["name"], new_hp)
                hp_embed = discord.Embed(
                    title=f"⚔️ {target_row['name']} получает {damage} урона",
                    description=f"Осталось HP: **{new_hp} / {target_row['max_health']}**",
                    color=0xE74C3C,
                )
                if new_hp <= 0:
                    hp_embed.description += "\n☠️ **Персонаж повержен!**"
                await message.channel.send(embed=hp_embed)

    log_action(
        guild_id, message.channel.id, char_name, text,
        narration or ("речь" if not is_action else "")
    )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not message.content:
        return
    if not is_channel_allowed(message.guild.id, message.channel.id):
        return

    match = find_by_prefix(message.content, message.guild.id)
    if not match:
        return

    prefix, text, owner_id, char_name, avatar_url = match
    if message.author.id != owner_id:
        return
    if not text:
        return

    try:
        await handle_message(message, prefix, text, owner_id, char_name, avatar_url)
    except Exception as e:
        print(f"⚠️ Ошибка обработки: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    if not TOKEN:
        print("❌ TOKEN пустой!")
        exit(1)
    if not OPENROUTER_KEY:
        print("⚠️ OPENROUTER_API_KEY не задан — ИИ-функции работать не будут.")
    bot.run(TOKEN)