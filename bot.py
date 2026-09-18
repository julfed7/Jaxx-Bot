import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "characters.db")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ---------- База данных ----------
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
    # Новая таблица — белый список каналов
    c.execute("""CREATE TABLE IF NOT EXISTS allowed_channels (
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    )""")
    conn.commit()
    conn.close()


def is_channel_allowed(guild_id: int, channel_id: int) -> bool:
    """Если список пуст — разрешено всё. Иначе — только каналы из списка."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM allowed_channels WHERE guild_id = ?", (guild_id,))
    total = c.fetchone()[0]
    if total == 0:
        conn.close()
        return True
    c.execute(
        "SELECT 1 FROM allowed_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    row = c.fetchone()
    conn.close()
    return row is not None


def allow_channel(guild_id: int, channel_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)",
        (guild_id, channel_id),
    )
    conn.commit()
    conn.close()


def disallow_channel(guild_id: int, channel_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM allowed_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    conn.commit()
    conn.close()


def list_allowed_channels(guild_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM allowed_channels WHERE guild_id = ?", (guild_id,))
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def add_character(user_id, guild_id, name, prefix, avatar_url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO characters (user_id, guild_id, name, prefix, avatar_url) VALUES (?, ?, ?, ?, ?)",
        (user_id, guild_id, name, prefix, avatar_url),
    )
    conn.commit()
    conn.close()


def get_chars_for_guild(guild_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT prefix, user_id, name, avatar_url FROM characters WHERE guild_id = ?",
        (guild_id,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def find_by_prefix(content, guild_id):
    """Ищет самого подходящего персонажа по началу сообщения."""
    rows = get_chars_for_guild(guild_id)
    # Сортируем по длине префикса (убывание), чтобы длинные матчились первыми
    rows.sort(key=lambda r: len(r[0]), reverse=True)
    for prefix, user_id, name, avatar_url in rows:
        if content == prefix:
            return prefix, "", user_id, name, avatar_url
        if content.startswith(prefix + " "):
            return prefix, content[len(prefix):].strip(), user_id, name, avatar_url
    return None


# ---------- Вебхуки ----------
_webhook_cache = {}  # channel_id -> Webhook


async def get_webhook(channel: discord.TextChannel) -> discord.Webhook:
    if channel.id in _webhook_cache:
        return _webhook_cache[channel.id]

    # Пробуем найти существующий вебхук нашего бота
    try:
        for wh in await channel.webhooks():
            if wh.name == "RP Bot" and wh.user and wh.user.id == bot.user.id:
                _webhook_cache[channel.id] = wh
                return wh
        wh = await channel.create_webhook(name="RP Bot")
    except discord.Forbidden:
        raise RuntimeError(
            "У бота нет прав «Управлять вебхуками» в этом канале."
        )

    _webhook_cache[channel.id] = wh
    return wh


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


# ---------- Слеш-команда ----------
@tree.command(name="jb_create_char", description="Создать персонажа для ролевой игры")
@app_commands.describe(
    name="Имя персонажа",
    prefix="Префикс (например: -гигачад)",
    avatar="Картинка-аватар персонажа",
)
async def create_char(interaction, name, prefix, avatar):
    if not interaction.guild:
        await interaction.response.send_message(
            "Команда работает только на сервере.", ephemeral=True
        )
        return

    # Проверка белого списка каналов
    if not is_channel_allowed(interaction.guild.id, interaction.channel.id):
        await interaction.response.send_message(
            "❌ В этом канале команды бота отключены.", ephemeral=True
        )
        return

    # ... остальной код

    if not (avatar.content_type or "").startswith("image/"):
        await interaction.response.send_message(
            "Прикрепи, пожалуйста, изображение.", ephemeral=True
        )
        return

    if " " in prefix:
        await interaction.response.send_message(
            "Префикс не должен содержать пробелов.", ephemeral=True
        )
        return

    add_character(
        interaction.user.id,
        interaction.guild.id,
        name,
        prefix,
        avatar.url,
    )

    embed = discord.Embed(title="✅ Персонаж создан", color=0x57F287)
    embed.add_field(name="Имя", value=name, inline=True)
    embed.add_field(name="Префикс", value=f"`{prefix}`", inline=True)
    embed.set_thumbnail(url=avatar.url)
    embed.set_footer(text=f"Пиши: {prefix} <твой текст>")

    await interaction.response.send_message(embed=embed)

def is_admin(interaction: discord.Interaction) -> bool:
    """Проверка: админ ли пользователь. Используется для команд управления каналами."""
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


@tree.command(name="jb_allow", description="Разрешить команды бота в этом канале (только админ)")
async def jb_allow(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Нужны права администратора.", ephemeral=True
        )
        return

    allow_channel(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(
        f"✅ Канал {interaction.channel.mention} добавлен в белый список.\n"
        f"Теперь команды бота работают **только** в каналах из списка.",
        ephemeral=True,
    )


@tree.command(name="jb_disallow", description="Запретить команды бота в этом канале (только админ)")
async def jb_disallow(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ Нужны права администратора.", ephemeral=True
        )
        return

    disallow_channel(interaction.guild.id, interaction.channel.id)
    remaining = list_allowed_channels(interaction.guild.id)
    msg = f"✅ Канал {interaction.channel.mention} убран из белого списка."
    if not remaining:
        msg += "\n⚠️ Список пуст — команды снова работают **во всех** каналах."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="jb_channels", description="Показать, в каких каналах разрешены команды")
async def jb_channels(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Только на сервере.", ephemeral=True)
        return

    ids = list_allowed_channels(interaction.guild.id)

    if not ids:
        await interaction.response.send_message(
            "📋 Белый список пуст — команды бота работают **во всех** каналах.",
            ephemeral=True,
        )
        return

    lines = []
    for cid in ids:
        ch = interaction.guild.get_channel(cid)
        lines.append(ch.mention if ch else f"❓ `{cid}` (канал удалён)")

    embed = discord.Embed(
        title="📋 Каналы, где разрешены команды",
        description="\n".join(lines),
        color=0x5865F2,
    )
    embed.set_footer(text=f"Всего: {len(ids)}. Добавить — /jb_allow, убрать — /jb_disallow")
    await interaction.response.send_message(embed=embed, ephemeral=True)



# ---------- Ловим сообщения с префиксом ----------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild or not message.content:
        return

    # Проверка белого списка каналов
    if not is_channel_allowed(message.guild.id, message.channel.id):
        return

    match = find_by_prefix(message.content, message.guild.id)
    # ... остальной код
    if not match:
        return

    prefix, text, owner_id, char_name, avatar_url = match

    # Ограничение: только владелец персонажа может за него говорить.
    # Если хочешь разрешить всем — закомментируй эти 2 строки.
    if message.author.id != owner_id:
        return

    try:
        webhook = await get_webhook(message.channel)
    except RuntimeError as e:
        await message.channel.send(f"⚠️ {e}")
        return

    # Удаляем оригинальное сообщение
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass

    # Отправляем от имени персонажа через вебхук
    if text:
        await webhook.send(content=text, username=char_name, avatar_url=avatar_url)
    else:
        # Пустой текст — отправим одну картинку-аватар или что-то ещё, если нужно
        await webhook.send(content="…", username=char_name, avatar_url=avatar_url)


if __name__ == "__main__":
    print("=== ПРОВЕРКА ТОКЕНА ===")
    if not TOKEN:
        print("❌ TOKEN = None или пустая строка!")
        print("Значит Railway не видит переменную DISCORD_BOT_TOKEN.")
        exit(1)

    print(f"✅ Длина токена: {len(TOKEN)}")
    print(f"✅ Первые 8 символов: {TOKEN[:8]}")
    print(f"✅ Последние 4 символа: {TOKEN[-4:]}")
    print(f"✅ Содержит пробелы: {' ' in TOKEN}")
    print(f"✅ Содержит кавычки: {'\"' in TOKEN or chr(39) in TOKEN}")
    print("=======================")

    try:
        bot.run(TOKEN)
    except discord.errors.LoginFailure:
        print("❌ Discord отклонил токен. Он недействителен — сбрось в Developer Portal.")
    except Exception as e:
        print(f"❌ Ошибка: {type(e).__name__}: {e}")