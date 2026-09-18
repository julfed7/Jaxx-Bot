import os
import json
import re
import sqlite3
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GIGA_KEY = os.getenv("GIGACHAT_CREDENTIALS")
GIGA_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
DB_PATH = os.getenv("DB_PATH", "characters.db")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------- GigaChat ----------
giga = None
if GIGA_KEY:
    try:
        giga = GigaChat(
            credentials=GIGA_KEY,
            model=GIGA_MODEL,
            verify_ssl_certs=False,
        )
        print(f"✅ GigaChat инициализирован, модель: {GIGA_MODEL}")
    except Exception as e:
        print(f"❌ Ошибка GigaChat: {e}")


async def giga_chat_async(messages, max_tokens=400, temperature=0.4):
    if not giga:
        return None

    def _call():
        return giga.chat(Chat(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ))

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call),
            timeout=40.0,
        )
        return response.choices[0].message.content
    except asyncio.TimeoutError:
        print("⚠️ GigaChat timeout")
        return None
    except Exception as e:
        print(f"⚠️ GigaChat error: {e}")
        return None


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
        "history":      "TEXT DEFAULT ''",
        "health":       "INTEGER DEFAULT 100",
        "max_health":   "INTEGER DEFAULT 100",
        "regen":        "INTEGER DEFAULT 100",
        "inventory":    "TEXT DEFAULT '[]'",
        "skills":       "TEXT DEFAULT '[]'",
        "personality":  "TEXT DEFAULT ''",
        "is_bot":       "INTEGER DEFAULT 0",
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
    elif name and guild_id:
        c.execute(
            "SELECT * FROM characters WHERE guild_id=? AND name=? COLLATE NOCASE",
            (guild_id, name),
        )
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


def get_recent_log(guild_id, channel_id, limit=10):
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


# ---------- GigaChat: генерация персонажа ----------
async def generate_character_stats(name, history):
    if not giga:
        return {"health": 500, "regen": 100, "inventory": [], "skills": [], "personality": "спокойный"}

    system = (
        "Ты — генератор RPG-характеристик. Отвечай ТОЛЬКО валидным JSON. "
        "Никаких пояснений, только JSON-объект."
    )
    user = (
        f"Персонаж: {name}\nИстория: {history}\n\n"
        "Учитывай историю: если предмет сломан или утерян — не включай в инвентарь. "
        "Если персонаж сильный — дай больше HP. "
        "Опиши характер персонажа в personality — одной фразой, "
        "например: 'дерзкий и саркастичный', 'холодный и немногословный', "
        "'весёлый и болтливый'. Характер должен вытекать из истории.\n\n"
        "Сгенерируй:\n"
        "{\n"
        '  "health": <100-5000>,\n'
        '  "inventory": ["предмет1", ...],\n'
        '  "skills": ["навык1", ...],\n'
        '  "personality": "описание характера"\n'
        "}\n\nОтветь ТОЛЬКО JSON."
    )
    raw = await giga_chat_async(
        [
            Messages(role=MessagesRole.SYSTEM, content=system),
            Messages(role=MessagesRole.USER, content=user),
        ],
        max_tokens=350,
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


# ---------- GigaChat: оценка действия ----------
async def resolve_action(actor_name, actor_history, actor_personality,
                         actor_skills, actor_inventory,
                         action_text, context_log, targets_info):
    if not giga:
        return {"is_action": False, "narration": "", "damage": 0, "target": None, "success": True}

    skills_str = ", ".join(actor_skills) if actor_skills else "нет"
    inv_str = ", ".join(actor_inventory) if actor_inventory else "пусто"

    context_str = "\n".join(
        f"{a}: {c} → {r}" for a, c, r in context_log
    ) if context_log else "нет недавних событий"

    targets_str = "\n".join(targets_info) if targets_info else "нет других персонажей"

    system = (
        "Ты — RPG-мастер в Discord. Ты оцениваешь, что делает персонаж. "
        "Отвечай ТОЛЬКО валидным JSON, без пояснений и markdown.\n\n"
        "ПРАВИЛА:\n"
        "1. Речь (приветствие, реплика) → is_action=false, narration=\"\".\n"
        "2. Действие → is_action=true, опиши результат.\n"
        "3. Использует навык/предмет, которых нет → success=false, урон 0, "
        "придумай смешное/логичное последствие.\n"
        "4. Если есть цель (имя другого персонажа) → target.\n"
        "5. Урон 0-500. Сильная подача («со всей мощи») → больше.\n"
        "6. Провал может ударить самого персонажа.\n"
        "7. Описание веди в стиле характера персонажа, но от лица мастера.\n"
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
        '  "narration": "описание результата от лица мастера"\n'
        "}"
    )

    raw = await giga_chat_async(
        [
            Messages(role=MessagesRole.SYSTEM, content=system),
            Messages(role=MessagesRole.USER, content=user),
        ],
        max_tokens=350,
        temperature=0.5,
    )
    print("=== ACTION RAW ===", repr(raw))
    return parse_json_safe(raw, {
        "is_action": False, "target": None, "success": True, "damage": 0, "narration": ""
    })


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
@app_commands.describe(prefix="Префикс персонажа, которого хочешь удалить")
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


# ---------- Обработка сообщения ----------
async def send_as_character(channel, name, avatar_url, text):
    webhook = await get_webhook(channel)
    await webhook.send(content=text, username=name, avatar_url=avatar_url)


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

    # Публикуем от имени персонажа
    try:
        await send_as_character(message.channel, char_name, avatar_url, text)
    except Exception as e:
        print(f"⚠️ Ошибка вебхука: {e}")
        return

    # Удаляем исходное сообщение
    try:
        await message.delete()
    except Exception as e:
        print(f"⚠️ Ошибка удаления: {e}")

    # Если это действие — публикуем описание от бота
    if is_action and narration:
        embed = discord.Embed(
            description=f"🎲 **{narration}**",
            color=0xE67E22 if success else 0xE74C3C,
        )
        embed.set_author(name=f"Мастер: {char_name}")
        await message.channel.send(embed=embed)

        # Если была цель и урон — применяем к цели
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

    # Логируем событие для памяти канала
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
    if not GIGA_KEY:
        print("⚠️ GIGACHAT_CREDENTIALS не задан.")
    bot.run(TOKEN)