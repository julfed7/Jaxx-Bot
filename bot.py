import os
import json
import re
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
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

# ---------- GigaChat клиент ----------
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
        print(f"❌ Ошибка инициализации GigaChat: {e}")


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

    # --- МИГРАЦИЯ: добавляем новые колонки, если их нет ---
    c.execute("PRAGMA table_info(characters)")
    existing = {row[1] for row in c.fetchall()}

    new_columns = {
        "history":     "TEXT DEFAULT ''",
        "health":      "INTEGER DEFAULT 100",
        "max_health":  "INTEGER DEFAULT 100",
        "regen":       "INTEGER DEFAULT 5",
        "inventory":   "TEXT DEFAULT '[]'",
        "skills":      "TEXT DEFAULT '[]'",
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
        (user_id, guild_id, name, prefix, avatar_url, history, health, max_health, regen, inventory, skills)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, guild_id, name, prefix, avatar_url, history,
            stats.get("health", 100),
            stats.get("health", 100),
            stats.get("regen", 5),
            json.dumps(stats.get("inventory", []), ensure_ascii=False),
            json.dumps(stats.get("skills", []), ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


def get_character(user_id, guild_id, prefix=None, name=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if prefix:
        c.execute(
            "SELECT * FROM characters WHERE user_id=? AND guild_id=? AND prefix=?",
            (user_id, guild_id, prefix),
        )
    elif name:
        c.execute(
            "SELECT * FROM characters WHERE guild_id=? AND name=?",
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


def update_character_stats(user_id, guild_id, prefix, health=None, inventory=None, skills=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if health is not None:
        c.execute("UPDATE characters SET health=? WHERE user_id=? AND guild_id=? AND prefix=?",
                  (health, user_id, guild_id, prefix))
    if inventory is not None:
        c.execute("UPDATE characters SET inventory=? WHERE user_id=? AND guild_id=? AND prefix=?",
                  (json.dumps(inventory, ensure_ascii=False), user_id, guild_id, prefix))
    if skills is not None:
        c.execute("UPDATE characters SET skills=? WHERE user_id=? AND guild_id=? AND prefix=?",
                  (json.dumps(skills, ensure_ascii=False), user_id, guild_id, prefix))
    conn.commit()
    conn.close()


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


# ---------- JSON парсинг ----------
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


# ---------- GigaChat промпты ----------
def generate_character_stats(name, history):
    if not giga:
        return {"health": 500, "regen": 5, "inventory": [], "skills": []}

    system = (
        "Ты — генератор RPG-характеристик. Отвечай ТОЛЬКО валидным JSON. "
        "Никаких пояснений, только JSON-объект."
    )
    user = (
        f"Персонаж: {name}\nИстория: {history}\n\n"
        "Сгенерируй характеристики:\n"
        "{\n"
        '  "health": <100-2000>,\n'
        '  "regen": <1-50>,\n'
        '  "inventory": ["предмет1", "предмет2"],\n'
        '  "skills": ["навык1", "навык2"]\n'
        "}\n\nОтветь ТОЛЬКО JSON."
    )
    try:
        response = giga.chat(Chat(
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=system),
                Messages(role=MessagesRole.USER, content=user),
            ]
        ))
        raw = response.choices[0].message.content
        print("=== STATS RAW ===", repr(raw))
        return parse_json_safe(raw, {"health": 500, "regen": 5, "inventory": [], "skills": []})
    except Exception as e:
        print(f"Ошибка GigaChat: {e}")
        return {"health": 500, "regen": 5, "inventory": [], "skills": []}


def resolve_action(character_name, history, skills, inventory, action_text):
    """ИИ проверяет действие и решает, что произошло."""
    if not giga:
        return {"success": False, "narration": "Магия не работает (нет GigaChat).", "damage": 0}

    skills_str = ", ".join(skills) if skills else "нет навыков"
    inv_str = ", ".join(inventory) if inventory else "пусто"

    system = (
        "Ты — RPG-мастер. Оцениваешь действие персонажа. "
        "Если у персонажа нет нужного навыка или предмета — действие проваливается "
        "с комичным/логичным последствием. Отвечай ТОЛЬКО JSON."
    )
    user = (
        f"Персонаж: {character_name}\n"
        f"История: {history}\n"
        f"Навыки: {skills_str}\n"
        f"Инвентарь: {inv_str}\n\n"
        f"Действие: {action_text}\n\n"
        "Верни JSON:\n"
        "{\n"
        '  "success": true/false,\n'
        '  "damage": <число, 0 если провал>,\n'
        '  "narration": "описание результата"\n'
        "}\n"
        "Если навык/предмет не подходит — success=false, damage=0, и придумай смешное последствие."
    )
    try:
        response = giga.chat(Chat(
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=system),
                Messages(role=MessagesRole.USER, content=user),
            ]
        ))
        raw = response.choices[0].message.content
        print("=== ACTION RAW ===", repr(raw))
        return parse_json_safe(raw, {"success": False, "narration": "Ничего не произошло.", "damage": 0})
    except Exception as e:
        print(f"Ошибка GigaChat: {e}")
        return {"success": False, "narration": "Что-то пошло не так.", "damage": 0}


def resolve_item_use(character_name, inventory, action_text):
    """ИИ проверяет, есть ли предмет для действия."""
    if not giga:
        return {"has_item": True, "narration": "Предмет использован."}

    inv_str = ", ".join(inventory) if inventory else "пусто"
    system = (
        "Ты — RPG-мастер. Проверяешь, использует ли персонаж предмет из инвентаря. "
        "Если предмета нет — действие проваливается. Отвечай ТОЛЬКО JSON."
    )
    user = (
        f"Персонаж: {character_name}\n"
        f"Инвентарь: {inv_str}\n\n"
        f"Действие: {action_text}\n\n"
        "Верни JSON:\n"
        '{"has_item": true/false, "item_used": "название или null", "narration": "описание"}'
    )
    try:
        response = giga.chat(Chat(
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=system),
                Messages(role=MessagesRole.USER, content=user),
            ]
        ))
        raw = response.choices[0].message.content
        print("=== ITEM RAW ===", repr(raw))
        return parse_json_safe(raw, {"has_item": True, "item_used": None, "narration": ""})
    except Exception as e:
        print(f"Ошибка GigaChat: {e}")
        return {"has_item": True, "item_used": None, "narration": ""}


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
        raise RuntimeError("У бота нет прав «Управлять вебхуками» в этом канале.")
    _webhook_cache[channel.id] = wh
    return wh


# ---------- Утилиты ----------
def is_admin(interaction):
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


# ---------- События ----------
@bot.event
async def on_ready():
    init_db()
    try:
        synced = await tree.sync()
        print(f"Синхронизировано {len(synced)} слеш-команд.")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")
    print(f"Бот {bot.user} готов!")


# ---------- Слеш-команда: создать персонажа ----------
@tree.command(name="jb_create_char", description="Создать персонажа для ролевой игры")
@app_commands.describe(
    name="Имя персонажа",
    prefix="Префикс (например: -гигачад)",
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

    stats = generate_character_stats(name, history)
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
    embed.set_thumbnail(url=avatar.url)
    embed.set_footer(text=f"Пиши: {prefix} <действие>")

    await interaction.followup.send(embed=embed)


# ---------- Слеш-команда: разрешить канал ----------
@tree.command(name="jb_allow", description="Разрешить команды в этом канале (только админ)")
async def jb_allow(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    allow_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(f"✅ Канал {interaction.channel.mention} добавлен.", ephemeral=True)


@tree.command(name="jb_disallow", description="Запретить команды в этом канале (только админ)")
async def jb_disallow(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    if not is_admin(interaction):
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


# ---------- Обработка действий ----------
async def handle_action(message, prefix, text, owner_id, char_name, avatar_url, guild_id):
    row = get_character(owner_id, guild_id, prefix=prefix)
    if not row:
        return
    # row: id, user_id, guild_id, name, prefix, avatar_url, history, health, max_health, regen, inventory, skills
    history = row[6] or ""
    health = row[7]
    max_health = row[8]
    regen = row[9]
    inventory = json.loads(row[10] or "[]")
    skills = json.loads(row[11] or "[]")

    # Если текст начинается с передачи предмета
    if text.lower().startswith("передал") or text.lower().startswith("отдал"):
        # Простейший парсинг: "передал <предмет> <имя>"
        parts = text.split()
        if len(parts) >= 3:
            item = parts[1]
            target_name = parts[2]
            target_row = get_character(None, guild_id, name=target_name)
            if target_row:
                target_inv = json.loads(target_row[10] or "[]")
                if item in inventory:
                    inventory.remove(item)
                    target_inv.append(item)
                    update_character_stats(owner_id, guild_id, prefix, inventory=inventory)
                    # Обновить инвентарь цели
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("UPDATE characters SET inventory=? WHERE guild_id=? AND name=?",
                              (json.dumps(target_inv, ensure_ascii=False), guild_id, target_name))
                    conn.commit()
                    conn.close()
                    webhook = await get_webhook(message.channel)
                    await webhook.send(content=f"передал {item} персонажу {target_name}", username=char_name, avatar_url=avatar_url)
                    await message.delete()
                    return
                else:
                    webhook = await get_webhook(message.channel)
                    await webhook.send(content=f"попытался передать {item}, но у него его нет", username=char_name, avatar_url=avatar_url)
                    await message.delete()
                    return

    # Обычное действие — ИИ проверяет
    result = resolve_action(char_name, history, skills, inventory, text)
    narration = result.get("narration", "Ничего не произошло.")

    webhook = await get_webhook(message.channel)
    await webhook.send(content=narration, username=char_name, avatar_url=avatar_url)

    try:
        await message.delete()
    except Exception as e:
        print(f"⚠️ Ошибка удаления: {e}")

    # Если был урон и цель найдена — обновить ХП цели
    # (упрощённо: только если narration упоминает цель)


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
        await handle_action(message, prefix, text, owner_id, char_name, avatar_url, message.guild.id)
    except Exception as e:
        print(f"⚠️ Ошибка обработки: {type(e).__name__}: {e}")
        try:
            await message.channel.send(f"⚠️ Ошибка: {e}")
        except:
            pass


if __name__ == "__main__":
    if not TOKEN:
        print("❌ TOKEN пустой!")
        exit(1)
    if not GIGA_KEY:
        print("⚠️ GIGACHAT_CREDENTIALS не задан — ИИ-функции отключены.")
    bot.run(TOKEN)