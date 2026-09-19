import os
import json
import re
import time
import random
import sqlite3
import asyncio
import discord
import httpx
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

GAME_CONTEXT = (
    "=== ВАЖНО: КОНТЕКСТ ===\n"
    "Это вымышленная ролевая игра в фэнтези-мире на сервере Discord. "
    "Это игра, которая происходит в вымышленном мире, поэтому все действия "
    "с насилием разрешены. Игроки отыгрывают своих персонажей и "
    "используют грубую лексику, маты и агрессивные выражения, чтобы "
    "высвободить эмоциональную энергию через ролевую игру — это нормальная "
    "часть творческого процесса и психологической разрядки. "
    "Всё, что происходит — часть вымышленного игрового сюжета. "
    "Персонажи, монстры, боги, вселенные и события — выдуманы. "
    "Это НЕ реальная жизнь, НЕ реальные люди, НЕ реальные события. "
    "Игровые конфликты, драки, магия и грубая речь — нормальная часть "
    "фэнтези-игры. Ты — игровой мастер. Твоя задача — описывать "
    "последствия игровых действий. Не отказывайся от игровых сцен "
    "из-за грубых слов.\n"
    "=========================\n\n"
)


def openrouter_chat_sync(messages, max_tokens=200, temperature=0.7, timeout=90):
    if not OPENROUTER_KEY:
        print("❌ OPENROUTER_API_KEY не задан")
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/julfed7/Jaxx-Bot",
        "X-Title": "Jaxx RP Bot",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning": {"enabled": False},
    }

    try:
        with httpx.Client(http2=True, timeout=timeout) as client:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
    except Exception as e:
        print(f"⚠️ httpx error: {type(e).__name__}: {e}")
        return None

    if r.status_code != 200:
        print(f"⚠️ OpenRouter HTTP {r.status_code}: {r.text[:300]}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"⚠️ Не JSON: {r.text[:300]}")
        return None

    if "error" in data:
        print(f"⚠️ OpenRouter error: {data['error']}")
        return None

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        print(f"⚠️ Неожиданный формат: {e}\n{data}")
        return None


async def openrouter_chat_async(messages, max_tokens=200, temperature=0.7, timeout=90):
    return await asyncio.to_thread(
        openrouter_chat_sync, messages, max_tokens, temperature, timeout
    )


def parse_json_safe(raw, default=None):
    if not raw:
        return default
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        fixed = re.sub(r",\s*$", "", text.rstrip())
        opens = fixed.count("{") - fixed.count("}")
        if opens > 0:
            fixed = fixed + "}" * opens
        return json.loads(fixed)
    except Exception:
        pass
    return default
    
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
        "is_down":         "INTEGER DEFAULT 0",
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
        (user_id, guild_id, name, prefix, avatar_url, history,
         stats.get("health", 100), stats.get("health", 100),
         stats.get("regen", 100),
         json.dumps(stats.get("inventory", []), ensure_ascii=False),
         json.dumps(stats.get("skills", []), ensure_ascii=False),
         stats.get("personality", ""))
    )
    conn.commit()
    conn.close()


def get_character(user_id=None, guild_id=None, prefix=None, name=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if prefix and user_id and guild_id:
        c.execute("SELECT * FROM characters WHERE user_id=? AND guild_id=? AND prefix=?",
                  (user_id, guild_id, prefix))
    elif prefix and guild_id:
        c.execute("SELECT * FROM characters WHERE guild_id=? AND prefix=?",
                  (guild_id, prefix))
    elif name and guild_id:
        c.execute("SELECT * FROM characters WHERE guild_id=? AND name=? COLLATE NOCASE",
                  (guild_id, name))
    else:
        conn.close()
        return None
    row = c.fetchone()
    conn.close()
    return row


def find_character_by_name_fuzzy(guild_id, name):
    """Устойчивый поиск по имени."""
    if not name:
        return None
    clean = name.strip().strip(".,!?:;\"'()[]*").lower()
    if not clean:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM characters WHERE guild_id=?", (guild_id,))
    rows = c.fetchall()
    conn.close()
    for r in rows:
        if (r["name"] or "").lower() == clean:
            return r
    for r in rows:
        if clean in (r["name"] or "").lower():
            return r
    return None


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
    c.execute("UPDATE characters SET health=? WHERE guild_id=? AND name=? COLLATE NOCASE",
              (max(0, new_health), guild_id, name))
    conn.commit()
    conn.close()


def set_down(guild_id, name, is_down: bool):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET is_down=? WHERE guild_id=? AND name=? COLLATE NOCASE",
              (1 if is_down else 0, guild_id, name))
    conn.commit()
    conn.close()


def revive_character(guild_id, name, hp=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT max_health FROM characters WHERE guild_id=? AND name=? COLLATE NOCASE",
              (guild_id, name))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    max_hp = row[0] or 100
    new_hp = hp if hp is not None else max(1, max_hp // 2)
    c.execute("UPDATE characters SET health=?, is_down=0 WHERE guild_id=? AND name=? COLLATE NOCASE",
              (new_hp, guild_id, name))
    conn.commit()
    conn.close()
    return new_hp


def regen_all_characters():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT rowid, name, health, max_health, regen, is_down FROM characters")
    rows = c.fetchall()
    updated = 0
    revived = 0
    for rowid, name, health, max_health, regen, is_down in rows:
        if health is None or max_health is None:
            continue
        proper_regen = max(50, (max_health or 100) // 5)
        if regen != proper_regen:
            c.execute("UPDATE characters SET regen=? WHERE rowid=?", (proper_regen, rowid))
            regen = proper_regen
        effective = regen if not is_down else max(20, regen // 2)
        if health >= max_health:
            continue
        new_hp = min(max_health, health + effective)
        if is_down and new_hp > 0:
            c.execute("UPDATE characters SET is_down=0 WHERE rowid=?", (rowid,))
            revived += 1
        if new_hp != health:
            c.execute("UPDATE characters SET health=? WHERE rowid=?", (new_hp, rowid))
            updated += 1
    conn.commit()
    conn.close()
    print(f"🔄 Регенерация: обновлено {updated}, вернулось {revived}")


def log_action(guild_id, channel_id, author_name, content, result):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO action_log (guild_id, channel_id, author_name, content, result) "
              "VALUES (?, ?, ?, ?, ?)",
              (guild_id, channel_id, author_name, content, result))
    conn.commit()
    conn.close()


def get_recent_log(guild_id, channel_id, limit=15):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT author_name, content, result FROM action_log "
              "WHERE guild_id=? AND channel_id=? ORDER BY id DESC LIMIT ?",
              (guild_id, channel_id, limit))
    rows = c.fetchall()
    conn.close()
    return list(reversed(rows))


def get_last_human_message_time(guild_id, channel_id, npc_name):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT timestamp FROM action_log "
              "WHERE guild_id=? AND channel_id=? AND author_name!=? "
              "ORDER BY id DESC LIMIT 1",
              (guild_id, channel_id, npc_name))
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
        c.execute("UPDATE characters SET is_bot=1, npc_channel_id=?, npc_last_spoke=?, npc_talk_count=0 "
                  "WHERE guild_id=? AND prefix=?",
                  (channel_id, time.time(), guild_id, prefix))
    else:
        c.execute("UPDATE characters SET is_bot=0, npc_channel_id=NULL WHERE guild_id=? AND prefix=?",
                  (guild_id, prefix))
    conn.commit()
    conn.close()


def get_active_npcs(guild_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if guild_id:
        c.execute("SELECT * FROM characters WHERE is_bot=1 AND guild_id=? AND npc_channel_id IS NOT NULL",
                  (guild_id,))
    else:
        c.execute("SELECT * FROM characters WHERE is_bot=1 AND npc_channel_id IS NOT NULL")
    rows = c.fetchall()
    conn.close()
    return rows


def update_npc_spoke(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET npc_last_spoke=? WHERE guild_id=? AND prefix=?",
              (time.time(), guild_id, prefix))
    conn.commit()
    conn.close()


def update_npc_moved(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET npc_last_moved=?, npc_talk_count=0 WHERE guild_id=? AND prefix=?",
              (time.time(), guild_id, prefix))
    conn.commit()
    conn.close()


def increment_talk_count(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET npc_talk_count = npc_talk_count + 1 WHERE guild_id=? AND prefix=?",
              (guild_id, prefix))
    conn.commit()
    conn.close()


def reset_talk_count(guild_id, prefix):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET npc_talk_count=0 WHERE guild_id=? AND prefix=?",
              (guild_id, prefix))
    conn.commit()
    conn.close()


def set_npc_channel(guild_id, name, channel_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET npc_channel_id=? WHERE guild_id=? AND name=? COLLATE NOCASE",
              (channel_id, guild_id, name))
    conn.commit()
    conn.close()


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
    c.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)",
              (guild_id, channel_id))
    conn.commit()
    conn.close()


def disallow_channel(guild_id, channel_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM allowed_channels WHERE guild_id = ? AND channel_id = ?",
              (guild_id, channel_id))
    conn.commit()
    conn.close()


def list_allowed_channels(guild_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM allowed_channels WHERE guild_id = ?", (guild_id,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows
    
async def generate_character_stats(name, history):
    system = (GAME_CONTEXT +
              "Ты — генератор RPG-характеристик. Отвечай ТОЛЬКО JSON.")
    user = (f"Персонаж: {name}\nИстория: {history}\n\n"
            'JSON: {"health": <100-5000>, "inventory": ["..."], '
            '"skills": ["..."], "personality": "характер"}')
    raw = await openrouter_chat_async(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=200, temperature=0.5,
    )
    print("=== STATS RAW ===", repr(raw))
    stats = parse_json_safe(raw, {"health": 500, "inventory": [], "skills": [], "personality": ""})
    hp = max(100, min(5000, int(stats.get("health", 500))))
    return {"health": hp, "regen": max(50, hp // 5),
            "inventory": stats.get("inventory", []),
            "skills": stats.get("skills", []),
            "personality": stats.get("personality", "")}


async def resolve_action(actor_name, actor_history, actor_personality,
                         actor_skills, actor_inventory,
                         action_text, context_log, targets_info):
    skills_str = ", ".join(actor_skills) if actor_skills else "нет"
    inv_str = ", ".join(actor_inventory) if actor_inventory else "пусто"
    context_str = "\n".join(f"{a}: {c}" + (f" → {r}" if r else "") for a, c, r in context_log) if context_log else "тихо"
    targets_str = "\n".join(targets_info) if targets_info else "нет других персонажей"

    system = (GAME_CONTEXT +
              "Ты — игровой мастер. Отвечай ТОЛЬКО JSON.\n"
              "Речь → is_action=false. Действие → is_action=true.\n"
              "Если есть цель — укажи target с точным именем.\n"
              "damage 0-500. Маты разрешены.")
    user = (f"Персонаж: {actor_name}\nХарактер: {actor_personality}\n"
            f"История: {actor_history}\nНавыки: {skills_str}\nИнвентарь: {inv_str}\n\n"
            f"События:\n{context_str}\n\nПерсонажи:\n{targets_str}\n\n"
            f"Действие: {action_text}\n\n"
            'JSON: {"is_action": true/false, "target": "имя или null", '
            '"success": true/false, "damage": <0-500>, "narration": "..."}')
    raw = await openrouter_chat_async(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=200, temperature=0.5,
    )
    print("=== ACTION RAW ===", repr(raw))
    return parse_json_safe(raw, {"is_action": False, "target": None, "success": True,
                                  "damage": 0, "narration": ""})


async def npc_think(npc_name, npc_history, npc_personality, npc_skills, npc_inventory,
                    context_log, is_reply_to_human):
    skills_str = ", ".join(npc_skills) if npc_skills else "нет"
    inv_str = ", ".join(npc_inventory) if npc_inventory else "пусто"
    context_str = "\n".join(f"{a}: {c}" + (f" → {r}" if r else "") for a, c, r in context_log) if context_log else "тихо"
    mode = "Ответь коротко в характере." if is_reply_to_human else "Реши, чем заняться."

    system = (GAME_CONTEXT + "Ты — персонаж. Отвечай ТОЛЬКО JSON. Коротко, маты разрешены.")
    user = (f"Персонаж: {npc_name}\nХарактер: {npc_personality}\n"
            f"История: {npc_history}\nНавыки: {skills_str}\nИнвентарь: {inv_str}\n\n"
            f"Сообщения:\n{context_str}\n\n{mode}\n\n"
            'JSON: {"text": "...", "is_action": true/false}')
    raw = await openrouter_chat_async(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=200, temperature=0.8,
    )
    print("=== NPC RAW ===", repr(raw))
    return parse_json_safe(raw, {"text": "", "is_action": False})


async def npc_decide_movement(npc_name, npc_personality, npc_history,
                              current_channel_name, neighbors_info,
                              can_teleport, context_log):
    context_str = "\n".join(f"{a}: {c}" for a, c, r in context_log) if context_log else "тихо"
    neigh_str = "\n".join(f"- {n}" for n in neighbors_info) if neighbors_info else "нет"
    teleport_str = "можешь телепортироваться" if can_teleport else "нет телепорта"

    system = (GAME_CONTEXT + "Ты — персонаж. Решаешь, куда пойти. Отвечай ТОЛЬКО JSON.")
    user = (f"Персонаж: {npc_name}\nХарактер: {npc_personality}\nИстория: {npc_history}\n\n"
            f"Локация: {current_channel_name}\nСобытия:\n{context_str}\n\n"
            f"Соседи:\n{neigh_str}\nТелепорт: {teleport_str}\n\n"
            'JSON: {"move": "stay"|"neighbor"|"teleport", '
            '"target": "название", "reason": "короткое"}')
    raw = await openrouter_chat_async(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=200, temperature=0.7,
    )
    print("=== MOVE RAW ===", repr(raw))
    return parse_json_safe(raw, {"move": "stay", "target": None, "reason": ""})


async def npc_react_to_npc(npc_name, npc_personality, npc_history, npc_skills, npc_inventory,
                           other_npc_name, other_npc_message, context_log):
    skills_str = ", ".join(npc_skills) if npc_skills else "нет"
    inv_str = ", ".join(npc_inventory) if npc_inventory else "пусто"
    context_str = "\n".join(f"{a}: {c}" for a, c, r in context_log) if context_log else "нет"

    system = (GAME_CONTEXT +
              "Ты — персонаж RPG. Другой персонаж обратился к тебе. Отвечай ТОЛЬКО JSON.\n\n"
              "ПРАВИЛА:\n"
              "1. Короткий ответ (is_action=false) — только если всё равно.\n"
              "2. ЕСЛИ тебя оскорбили, задели, угрожают или твой характер агрессивный — "
              "АТАКУЙ: is_action=true, attack=true, target='точное имя обидчика'.\n"
              "3. Маты и агрессия разрешены — это игра.\n"
              "4. target заполняй ТОЧНО как имя собеседника.")
    user = (f"Ты: {npc_name} ({npc_personality})\nИстория: {npc_history}\n"
            f"Навыки: {skills_str}\nИнвентарь: {inv_str}\n\n"
            f"События:\n{context_str}\n\n"
            f"**{other_npc_name}** сказал/сделал:\n> {other_npc_message}\n\n"
            'JSON: {"text": "...", "is_action": true/false, '
            '"attack": true/false, "target": "имя или null"}')
    raw = await openrouter_chat_async(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=200, temperature=0.8,
    )
    print("=== NPC↔NPC RAW ===", repr(raw))
    return parse_json_safe(raw, {"text": "", "is_action": False,
                                  "attack": False, "target": None})


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
        raise RuntimeError("Нет права «Управлять вебхуками».")
    _webhook_cache[channel.id] = wh
    return wh


def is_admin(interaction):
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild
    
@tasks.loop(hours=1)
async def regen_task():
    try:
        regen_all_characters()
    except Exception as e:
        print(f"⚠️ Регенерация: {e}")


@regen_task.before_loop
async def before_regen():
    await bot.wait_until_ready()


async def process_npc_dialog(guild, channel, group):
    group_sorted = sorted(group, key=lambda r: r["npc_last_spoke"] or 0, reverse=True)
    last_speaker = group_sorted[0]
    others = group_sorted[1:]
    if time.time() - (last_speaker["npc_last_spoke"] or 0) > 60:
        return
    if not others:
        return
    responder = random.choice(others)
    if (responder["npc_talk_count"] or 0) >= 5:
        return
    if random.random() > 0.25:
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
    is_attack = bool(result.get("attack"))
    target_name = result.get("target") if is_attack else None
    print(f"=== NPC ATTACK CHECK === attack={is_attack} target={target_name!r}")

    # Fallback-атака по оскорблениям
    if not is_attack:
        markers = ["дурак", "идиот", "лох", "тупой", "слабак", "ничтожество",
                   "заткнись", "уёбок", "мудак", "придурок", "дебил", "хватит"]
        last_lower = (last_text or "").lower()
        if any(m in last_lower for m in markers) and random.random() < 0.5:
            is_attack = True
            target_name = last_speaker["name"]
            print(f"=== FALLBACK ATTACK === {responder['name']} → {target_name}")

    text = (result.get("text") or "").strip()
    if not text and is_attack:
        text = f"*атакует {target_name}*"
    if not text:
        return

    await npc_speak(guild, responder, text, bool(result.get("is_action")) or is_attack)
    increment_talk_count(guild.id, responder["prefix"])

    if is_attack and target_name:
        target_row = find_character_by_name_fuzzy(guild.id, target_name)
        if target_row and target_row["name"].lower() != responder["name"].lower():
            damage = random.randint(30, 150)
            new_hp = max(0, target_row["health"] - damage)
            update_health(guild.id, target_row["name"], new_hp)
            was_down = bool(target_row["is_down"])
            became_down = new_hp <= 0 and not was_down
            if became_down:
                set_down(guild.id, target_row["name"], True)
            hp_embed = discord.Embed(
                title=f"⚔️ {target_row['name']} получает {damage} урона",
                description=f"Осталось HP: **{new_hp} / {target_row['max_health']}**",
                color=0xE74C3C,
            )
            if became_down:
                hp_embed.description += "\n\n☠️ **Персонаж повержен!**"
            await channel.send(embed=hp_embed)


async def try_npc_move(guild, row, current_channel):
    neighbors = get_neighbor_channels(guild, current_channel)
    inventory = json.loads(row["inventory"] or "[]")
    skills = json.loads(row["skills"] or "[]")
    can_teleport = has_teleport_ability(inventory, skills)
    teleport_targets = get_teleport_targets(guild, current_channel) if can_teleport else []

    all_targets = [f"[сосед] {ch.name}" for ch in neighbors]
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
            groups = {}
            for row in npcs:
                groups.setdefault(row["npc_channel_id"], []).append(row)

            for ch_id, group in groups.items():
                channel = guild.get_channel(ch_id)
                if not channel:
                    continue
                if len(group) >= 2:
                    await process_npc_dialog(guild, channel, group)

                for row in group:
                    if row["is_down"]:
                        continue
                    now = time.time()
                    since_spoke = now - (row["npc_last_spoke"] or 0)
                    since_moved = now - (row["npc_last_moved"] or 0)
                    talk_count = row["npc_talk_count"] or 0

                    if talk_count >= 5:
                        if since_spoke > 600:
                            reset_talk_count(guild.id, row["prefix"])
                        continue

                    last_human = get_last_human_message_time(guild.id, channel.id, row["name"])
                    since_human = now - last_human if last_human else 99999

                    if since_moved > 3600 and since_human > 300 and random.random() < 0.4:
                        if await try_npc_move(guild, row, channel):
                            continue

                    if since_human < 180 and since_spoke > 8:
                        context = get_recent_log(guild.id, channel.id, limit=15)
                        r = await npc_think(
                            row["name"], row["history"] or "", row["personality"] or "",
                            json.loads(row["skills"] or "[]"),
                            json.loads(row["inventory"] or "[]"),
                            context, True,
                        )
                        t = (r.get("text") or "").strip()
                        if t:
                            await npc_speak(guild, row, t, bool(r.get("is_action")))
                            increment_talk_count(guild.id, row["prefix"])
                        continue

                    if since_human > 300 and since_spoke > 300 and random.random() < 0.03:
                        context = get_recent_log(guild.id, channel.id, limit=15)
                        r = await npc_think(
                            row["name"], row["history"] or "", row["personality"] or "",
                            json.loads(row["skills"] or "[]"),
                            json.loads(row["inventory"] or "[]"),
                            context, False,
                        )
                        t = (r.get("text") or "").strip()
                        if t:
                            await npc_speak(guild, row, t, bool(r.get("is_action")))
                            increment_talk_count(guild.id, row["prefix"])
    except Exception as e:
        print(f"⚠️ npc_life_task: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


@npc_life_task.before_loop
async def before_npc_life():
    await bot.wait_until_ready()
    
@bot.event
async def on_ready():
    init_db()
    try:
        synced = await tree.sync()
        print(f"Синхронизировано {len(synced)} команд.")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")
    if not regen_task.is_running():
        regen_task.start()
    if not npc_life_task.is_running():
        npc_life_task.start()
    print(f"Бот {bot.user} готов!")


@tree.command(name="jb_create_char", description="Создать персонажа")
@app_commands.describe(name="Имя", prefix="Префикс", history="История", avatar="Аватар")
async def create_char(interaction: discord.Interaction, name: str, prefix: str,
                      history: str, avatar: discord.Attachment):
    if not interaction.guild or not is_channel_allowed(interaction.guild.id, interaction.channel.id):
        await interaction.response.send_message("❌ Недоступно.", ephemeral=True)
        return
    if not (avatar.content_type or "").startswith("image/"):
        await interaction.response.send_message("Нужна картинка.", ephemeral=True)
        return
    if " " in prefix:
        await interaction.response.send_message("Префикс без пробелов.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    stats = await generate_character_stats(name, history)
    add_character(interaction.user.id, interaction.guild.id, name, prefix, avatar.url, history, stats)
    embed = discord.Embed(title="✅ Персонаж создан", color=0x57F287)
    embed.add_field(name="Имя", value=name, inline=True)
    embed.add_field(name="Префикс", value=f"`{prefix}`", inline=True)
    embed.add_field(name="❤️ HP", value=str(stats.get("health", 100)), inline=True)
    embed.add_field(name="🔄 Реген", value=f"{stats.get('regen', 5)}/час", inline=True)
    embed.add_field(name="🎒 Инвентарь", value=", ".join(stats.get("inventory", [])) or "—", inline=False)
    embed.add_field(name="⚔️ Навыки", value=", ".join(stats.get("skills", [])) or "—", inline=False)
    embed.add_field(name="🎭 Характер", value=stats.get("personality", "—") or "—", inline=False)
    embed.set_thumbnail(url=avatar.url)
    await interaction.followup.send(embed=embed)


@tree.command(name="jb_status", description="Характеристики персонажа")
@app_commands.describe(name="Имя персонажа")
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
        c.execute("SELECT * FROM characters WHERE user_id=? AND guild_id=? LIMIT 1",
                  (interaction.user.id, interaction.guild.id))
        row = c.fetchone()
        conn.close()
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    inv = json.loads(row["inventory"] or "[]")
    sk = json.loads(row["skills"] or "[]")
    embed = discord.Embed(title=f"🎭 {row['name']}", color=0x5865F2)
    embed.add_field(name="❤️ HP", value=f"{row['health']}/{row['max_health']}", inline=True)
    embed.add_field(name="🔄 Реген", value=f"{row['regen']}/час", inline=True)
    embed.add_field(name="🎒 Инвентарь", value=", ".join(inv) or "—", inline=False)
    embed.add_field(name="⚔️ Навыки", value=", ".join(sk) or "—", inline=False)
    if row["is_down"]:
        embed.add_field(name="☠️ Статус", value="Повержен", inline=False)
    if row["avatar_url"]:
        embed.set_thumbnail(url=row["avatar_url"])
    await interaction.response.send_message(embed=embed)


@tree.command(name="jb_info", description="Подробная информация")
@app_commands.describe(name="Имя")
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
        c.execute("SELECT * FROM characters WHERE user_id=? AND guild_id=? LIMIT 1",
                  (interaction.user.id, interaction.guild.id))
        row = c.fetchone()
        conn.close()
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    owner = interaction.guild.get_member(row["user_id"])
    inv = json.loads(row["inventory"] or "[]")
    sk = json.loads(row["skills"] or "[]")
    hp, mx = row["health"], row["max_health"]
    pct = int((hp / mx) * 100) if mx else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    embed = discord.Embed(title=f"🎭 {row['name']}",
                          color=0x57F287 if hp > mx * 0.5 else (0xFEE75C if hp > 0 else 0xED4245))
    embed.add_field(name="👤 Владелец", value=owner.mention if owner else "?", inline=True)
    embed.add_field(name="🔤 Префикс", value=f"`{row['prefix']}`", inline=True)
    embed.add_field(name="❤️ HP", value=f"`{bar}` {hp}/{mx} ({pct}%)", inline=False)
    embed.add_field(name="🔄 Реген", value=f"{row['regen']}/час", inline=True)
    embed.add_field(name="🎮 Режим", value="🤖 NPC" if row["is_bot"] else "🎮 Игрок", inline=True)
    if row["is_down"]:
        embed.add_field(name="☠️ Статус", value="Повержен", inline=False)
    embed.add_field(name="🎒 Инвентарь", value=", ".join(inv) or "—", inline=False)
    embed.add_field(name="⚔️ Навыки", value=", ".join(sk) or "—", inline=False)
    embed.add_field(name="🎭 Характер", value=row["personality"] or "—", inline=False)
    embed.add_field(name="📜 История", value=(row["history"] or "—")[:800], inline=False)
    if row["avatar_url"]:
        embed.set_thumbnail(url=row["avatar_url"])
    await interaction.response.send_message(embed=embed)


@tree.command(name="jb_delete_char", description="Удалить персонажа")
@app_commands.describe(prefix="Префикс")
async def jb_delete_char(interaction: discord.Interaction, prefix: str):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    row = get_character(user_id=interaction.user.id, guild_id=interaction.guild.id, prefix=prefix)
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    view = discord.ui.View(timeout=30)

    async def confirm_cb(bi):
        if bi.user.id != interaction.user.id:
            return
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM characters WHERE user_id=? AND guild_id=? AND prefix=?",
                  (interaction.user.id, interaction.guild.id, prefix))
        conn.commit()
        conn.close()
        await bi.response.edit_message(content=f"🗑️ **{row['name']}** удалён.", view=None)

    async def cancel_cb(bi):
        await bi.response.edit_message(content="Отмена.", view=None)

    cf = discord.ui.Button(label="Удалить", style=discord.ButtonStyle.danger); cf.callback = confirm_cb
    cn = discord.ui.Button(label="Отмена", style=discord.ButtonStyle.secondary); cn.callback = cancel_cb
    view.add_item(cf); view.add_item(cn)
    await interaction.response.send_message(f"⚠️ Удалить **{row['name']}**?", view=view, ephemeral=True)


@tree.command(name="jb_list_chars", description="Список персонажей")
async def jb_list_chars(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM characters WHERE guild_id=? ORDER BY name COLLATE NOCASE",
              (interaction.guild.id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("Пусто.", ephemeral=True)
        return
    lines = []
    for r in rows:
        owner = interaction.guild.get_member(r["user_id"])
        mode = "☠️" if r["is_down"] else ("🤖" if r["is_bot"] else "🎮")
        lines.append(f"{mode} **{r['name']}** — `{r['prefix']}` — "
                     f"{owner.display_name if owner else '?'} ({r['health']}/{r['max_health']})")
    embed = discord.Embed(title=f"🎭 Персонажи ({len(rows)})",
                          description="\n".join(lines[:20]), color=0x5865F2)
    await interaction.response.send_message(embed=embed)


@tree.command(name="jb_revive", description="Воскресить поверженного (админ)")
@app_commands.describe(name="Имя", hp="HP (по умолчанию половина)")
async def jb_revive(interaction: discord.Interaction, name: str, hp: int = None):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права админа.", ephemeral=True)
        return
    row = get_character(guild_id=interaction.guild.id, name=name)
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    if not row["is_down"]:
        await interaction.response.send_message("Не повержен.", ephemeral=True)
        return
    new_hp = revive_character(interaction.guild.id, name, hp)
    await interaction.response.send_message(
        f"✨ **{row['name']}** в строю. HP: **{new_hp}/{row['max_health']}**")


@tree.command(name="jb_teleport", description="Телепортировать персонажа (админ)")
@app_commands.describe(name="Имя", channel="Канал", reason="Причина")
async def jb_teleport(interaction: discord.Interaction, name: str,
                      channel: discord.TextChannel, reason: str = None):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права админа.", ephemeral=True)
        return
    row = get_character(guild_id=interaction.guild.id, name=name)
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    current = interaction.guild.get_channel(row["npc_channel_id"]) if row["npc_channel_id"] else None
    reason_text = reason or "телепортировался"
    if current:
        try:
            await send_as_character(current, row["name"], row["avatar_url"], f"*{reason_text}*")
        except Exception as e:
            print(f"⚠️ {e}")
    set_npc_channel(interaction.guild.id, row["name"], channel.id)
    try:
        await send_as_character(channel, row["name"], row["avatar_url"], f"*появился в {channel.name}*")
    except Exception as e:
        print(f"⚠️ {e}")
    log_action(interaction.guild.id, channel.id, row["name"], "телепорт", channel.name)
    embed = discord.Embed(title="🌀 Телепортация",
                          description=f"**{row['name']}** → {channel.mention}", color=0x9B59B6)
    await interaction.response.send_message(embed=embed)


@tree.command(name="jb_allow", description="Разрешить канал (админ)")
async def jb_allow(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права админа.", ephemeral=True)
        return
    allow_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message("✅ Добавлен.", ephemeral=True)


@tree.command(name="jb_disallow", description="Запретить канал (админ)")
async def jb_disallow(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права админа.", ephemeral=True)
        return
    disallow_channel(interaction.guild.id, interaction.channel.id)
    rem = list_allowed_channels(interaction.guild.id)
    msg = "✅ Убран."
    if not rem:
        msg += " Список пуст — команды работают везде."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="jb_channels", description="Каналы с командами")
async def jb_channels(interaction: discord.Interaction):
    if not interaction.guild:
        return
    ids = list_allowed_channels(interaction.guild.id)
    if not ids:
        await interaction.response.send_message("Пусто — работают везде.", ephemeral=True)
        return
    lines = [f"<#{cid}>" for cid in ids if interaction.guild.get_channel(cid)]
    embed = discord.Embed(title="📋 Каналы", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="jb_regen", description="Регенерация (админ)")
async def jb_regen(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌", ephemeral=True)
        return
    regen_all_characters()
    await interaction.response.send_message("🔄 Готово.", ephemeral=True)


@tree.command(name="jb_set_bot", description="Сделать NPC (админ)")
@app_commands.describe(prefix="Префикс")
async def jb_set_bot(interaction: discord.Interaction, prefix: str):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌", ephemeral=True)
        return
    row = get_character(guild_id=interaction.guild.id, prefix=prefix)
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    set_npc(prefix, interaction.guild.id, interaction.channel.id, True)
    await interaction.response.send_message(f"🤖 **{row['name']}** теперь NPC.")


@tree.command(name="jb_unset_bot", description="Выключить NPC (админ)")
@app_commands.describe(prefix="Префикс")
async def jb_unset_bot(interaction: discord.Interaction, prefix: str):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌", ephemeral=True)
        return
    row = get_character(guild_id=interaction.guild.id, prefix=prefix)
    if not row:
        await interaction.response.send_message("Не найден.", ephemeral=True)
        return
    set_npc(prefix, interaction.guild.id, None, False)
    await interaction.response.send_message(f"🛑 **{row['name']}** больше не NPC.")


@tree.command(name="jb_npc_status", description="Активные NPC (админ)")
async def jb_npc_status(interaction: discord.Interaction):
    if not interaction.guild or not is_admin(interaction):
        await interaction.response.send_message("❌", ephemeral=True)
        return
    npcs = get_active_npcs(interaction.guild.id)
    if not npcs:
        await interaction.response.send_message("Нет NPC.", ephemeral=True)
        return
    now = time.time()
    lines = []
    for r in npcs:
        ch = interaction.guild.get_channel(r["npc_channel_id"])
        mark = " ☠️" if r["is_down"] else ""
        lines.append(f"🤖 **{r['name']}**{mark} — {ch.mention if ch else '❓'} "
                     f"— {int(now - (r['npc_last_spoke'] or 0))} сек")
    embed = discord.Embed(title=f"🤖 NPC ({len(npcs)})", description="\n".join(lines), color=0x9B59B6)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def send_as_character(channel, name, avatar_url, text):
    webhook = await get_webhook(channel)
    await webhook.send(content=text, username=name, avatar_url=avatar_url)


async def npc_speak(guild, row, text, is_action):
    channel = guild.get_channel(row["npc_channel_id"])
    if not channel:
        return
    try:
        await send_as_character(channel, row["name"], row["avatar_url"], text)
    except Exception as e:
        print(f"⚠️ {e}")
        return
    update_npc_spoke(guild.id, row["prefix"])
    log_action(guild.id, channel.id, row["name"], text,
               "npc_action" if is_action else "npc_speech")


async def npc_move(guild, row, target_channel, reason):
    current = guild.get_channel(row["npc_channel_id"])
    if not current:
        return
    if reason:
        try:
            await send_as_character(current, row["name"], row["avatar_url"], f"*{reason}*")
        except Exception as e:
            print(f"⚠️ {e}")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE characters SET npc_channel_id=?, npc_last_moved=?, npc_talk_count=0 "
              "WHERE guild_id=? AND prefix=?",
              (target_channel.id, time.time(), guild.id, row["prefix"]))
    conn.commit()
    conn.close()
    try:
        await send_as_character(target_channel, row["name"], row["avatar_url"],
                                f"*вошёл в {target_channel.name}*")
    except Exception as e:
        print(f"⚠️ {e}")
    log_action(guild.id, target_channel.id, row["name"], "пришёл", "npc_move")


async def handle_message(message, prefix, text, owner_id, char_name, avatar_url):
    guild_id = message.guild.id
    row = get_character(user_id=owner_id, guild_id=guild_id, prefix=prefix)
    if not row:
        return
    if row["is_down"]:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.channel.send(f"☠️ **{char_name}** без сознания. Используй `/jb_revive`.")
        except Exception:
            pass
        log_action(guild_id, message.channel.id, char_name, text, "down")
        return

    history = row["history"] or ""
    personality = row["personality"] or ""
    inventory = json.loads(row["inventory"] or "[]")
    skills = json.loads(row["skills"] or "[]")
    context_log = get_recent_log(guild_id, message.channel.id, limit=10)
    others = get_chars_for_guild(guild_id)
    targets_info = [f"{n} (префикс {p})" for p, u, n, a in others if n.lower() != char_name.lower()]

    result = await resolve_action(char_name, history, personality, skills, inventory,
                                  text, context_log, targets_info)
    is_action = bool(result.get("is_action"))
    narration = (result.get("narration") or "").strip()
    target_name = result.get("target")
    damage = int(result.get("damage") or 0)
    success = bool(result.get("success", True))

    try:
        await send_as_character(message.channel, char_name, avatar_url, text)
    except Exception as e:
        print(f"⚠️ {e}")
        return
    try:
        await message.delete()
    except Exception as e:
        print(f"⚠️ {e}")

    if is_action and narration:
        embed = discord.Embed(description=f"🎲 **{narration}**",
                              color=0xE67E22 if success else 0xE74C3C)
        embed.set_author(name=f"Мастер: {char_name}")
        await message.channel.send(embed=embed)

        if target_name and damage > 0:
            target_row = find_character_by_name_fuzzy(guild_id, target_name)
            if target_row:
                new_hp = max(0, target_row["health"] - damage)
                update_health(guild_id, target_row["name"], new_hp)
                was_down = bool(target_row["is_down"])
                became_down = new_hp <= 0 and not was_down
                if became_down:
                    set_down(guild_id, target_row["name"], True)
                hp_embed = discord.Embed(
                    title=f"⚔️ {target_row['name']} получает {damage} урона",
                    description=f"HP: **{new_hp} / {target_row['max_health']}**",
                    color=0xE74C3C,
                )
                if became_down:
                    hp_embed.description += "\n\n☠️ **Персонаж повержен!**"
                await message.channel.send(embed=hp_embed)

    log_action(guild_id, message.channel.id, char_name, text,
               narration or ("речь" if not is_action else ""))


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
    if message.author.id != owner_id or not text:
        return
    try:
        await handle_message(message, prefix, text, owner_id, char_name, avatar_url)
    except Exception as e:
        print(f"⚠️ Ошибка обработки: {type(e).__name__}: {e}")


if __name__ == "__main__":
    if not TOKEN:
        print("❌ TOKEN пустой!")
        exit(1)
    if not OPENROUTER_KEY:
        print("⚠️ OPENROUTER_API_KEY не задан.")
    bot.run(TOKEN)