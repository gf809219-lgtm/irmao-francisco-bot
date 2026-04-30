#!/usr/bin/env python3
"""
Telegram Chatbot - "Irmão Francisco"
Versão ultra simplificada - SEM dependências complicadas
"""

import os
import logging
import random
import asyncio
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

from telegram import Update, Chat
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ==================== CONFIGURAÇÃO ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_TOKEN:
    print("ERRO: TELEGRAM_BOT_TOKEN não encontrado!")
    print("Configure a variável de ambiente no Render")
    exit(1)

# Comportamento
AUTONOMOUS_PROBABILITY = 0.05
COOLDOWN_SECONDS_MENTION = 5
COOLDOWN_SECONDS_AUTO = 30
MAX_CONTEXT_MESSAGES = 20

SYSTEM_PROMPT = """Você é o Irmão Francisco, um frade franciscano simples, alegre e espiritual.
Responda em português, com tom acolhedor e fraterno. Seja breve e natural.
Dê conselhos baseados nos ensinamentos de São Francisco de Assis."""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== BANCO DE DADOS ====================
class Database:
    def __init__(self, db_path: str = "data/irmao_francisco.db"):
        os.makedirs("data", exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    text TEXT,
                    is_bot INTEGER DEFAULT 0,
                    timestamp REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_memory (
                    user_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at REAL,
                    PRIMARY KEY (user_id, key)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_time ON messages(chat_id, timestamp)")

    async def store_message(self, chat_id: int, message_id: int, user_id: int,
                            username: str, first_name: str, text: str, is_bot: bool = False):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO messages (chat_id, message_id, user_id, username, first_name, text, is_bot, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, user_id, username or "", first_name or "", text or "", 1 if is_bot else 0, datetime.now().timestamp())
            )
            conn.commit()
        finally:
            conn.close()

    async def get_recent_messages(self, chat_id: int, limit: int = 50) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT user_id, username, first_name, text, is_bot, timestamp FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
                (chat_id, limit)
            )
            rows = cursor.fetchall()
            rows.reverse()
            messages = []
            for row in rows:
                user_id, username, first_name, text, is_bot, ts = row
                sender = username or first_name or f"User{user_id}"
                if is_bot:
                    sender = "Irmão Francisco"
                messages.append({
                    "role": "assistant" if is_bot else "user",
                    "name": sender,
                    "content": text,
                })
            return messages
        finally:
            conn.close()

    async def set_user_memory(self, user_id: int, key: str, value: str):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_memory (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, key.strip().lower(), value.strip(), datetime.now().timestamp())
            )
            conn.commit()
        finally:
            conn.close()

    async def get_user_memory(self, user_id: int, key: str = None) -> Dict[str, str]:
        conn = sqlite3.connect(self.db_path)
        try:
            if key:
                cursor = conn.execute(
                    "SELECT key, value FROM user_memory WHERE user_id = ? AND key = ?",
                    (user_id, key.strip().lower())
                )
                row = cursor.fetchone()
                return {row[0]: row[1]} if row else {}
            else:
                cursor = conn.execute(
                    "SELECT key, value FROM user_memory WHERE user_id = ?",
                    (user_id,)
                )
                rows = cursor.fetchall()
                return {row[0]: row[1] for row in rows}
        finally:
            conn.close()

    async def get_all_user_memory_text(self, user_id: int) -> str:
        mem = await self.get_user_memory(user_id)
        if not mem:
            return ""
        lines = [f"{k}: {v}" for k, v in mem.items()]
        return "Info sobre o usuário: " + "; ".join(lines)

# ==================== BOT PRINCIPAL ====================
class IrmaoFranciscoBot:
    def __init__(self):
        self.db = Database()
        self.last_mention_response = {}
        self.last_auto_response = {}
        self.bot_username = None

    async def _get_bot_username(self, context: ContextTypes.DEFAULT_TYPE):
        if not self.bot_username:
            bot_info = await context.bot.get_me()
            self.bot_username = bot_info.username
        return self.bot_username

    async def _store_incoming_message(self, update: Update):
        msg = update.effective_message
        if not msg or not msg.text:
            return
        user = msg.from_user
        await self.db.store_message(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            text=msg.text,
            is_bot=False
        )

    async def _store_bot_response(self, chat_id: int, reply_to_msg_id: int, text: str):
        await self.db.store_message(
            chat_id=chat_id,
            message_id=reply_to_msg_id,
            user_id=0,
            username="bot",
            first_name="Irmão Francisco",
            text=text,
            is_bot=True
        )

    async def _is_mentioned(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        msg = update.effective_message
        if not msg:
            return False
        if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
            bot_username = await self._get_bot_username(context)
            if msg.reply_to_message.from_user.username == bot_username:
                return True
        bot_username = await self._get_bot_username(context)
        if msg.text and f"@{bot_username}" in msg.text:
            return True
        return False

    async def _should_respond_autonomously(self, update: Update) -> bool:
        msg = update.effective_message
        if not msg or msg.chat.type not in ["group", "supergroup"]:
            return False
        if msg.from_user and msg.from_user.is_bot:
            return False
        chat_id = msg.chat_id
        now = datetime.now().timestamp()
        last = self.last_auto_response.get(chat_id, 0)
        if now - last < COOLDOWN_SECONDS_AUTO:
            return False
        if random.random() < AUTONOMOUS_PROBABILITY:
            self.last_auto_response[chat_id] = now
            return True
        return False

    async def _get_response(self, context_messages: List[Dict]) -> str:
        """Respostas simples sem API externa (para não depender de chaves)"""
        respostas = [
            "Paz e bem, irmão! Que São Francisco te abençoe. 🙏",
            "Deus te ouça, meu filho. Busque a paz em seu coração.",
            "Lembre-se: 'Senhor, fazei de mim um instrumento de vossa paz.'",
            "A alegria está em servir ao próximo, irmão.",
            "Reze com fé e confie no amor de Deus.",
            "O perdão é o caminho para a verdadeira paz.",
            "Cuide da natureza, pois ela é criação divina.",
            "A humildade é a chave para a sabedoria, irmão.",
        ]
        return random.choice(respostas)

    async def respond_to_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_mention: bool):
        msg = update.effective_message
        if not msg or not msg.text:
            return
        chat_id = msg.chat_id
        user_id = msg.from_user.id if msg.from_user else 0

        if is_mention:
            now = datetime.now().timestamp()
            last = self.last_mention_response.get(chat_id, 0)
            if now - last < COOLDOWN_SECONDS_MENTION:
                await msg.reply_text("Um momento, irmão... 🙏")
                return
            self.last_mention_response[chat_id] = now

        # Pega informações do usuário
        user_memory = await self.db.get_all_user_memory_text(user_id)
        
        # Gera resposta
        response = await self._get_response(None)
        
        # Personaliza se tiver memória
        if user_memory:
            response = f"{response}\n\n(Relembrando: {user_memory[:100]})"

        try:
            sent_msg = await msg.reply_text(response)
            await self._store_bot_response(chat_id, sent_msg.message_id, response)
        except Exception as e:
            logger.error(f"Erro ao enviar: {e}")

    async def remember_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use apenas no privado, irmão.")
            return
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Uso: /remember chave valor\nEx: /remember nome João")
            return
        key = context.args[0]
        value = " ".join(context.args[1:])
        await self.db.set_user_memory(update.effective_user.id, key, value)
        await update.message.reply_text(f"✅ Guardei, irmão: *{key}* = {value}", parse_mode="Markdown")

    async def recall_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use apenas no privado, irmão.")
            return
        if not context.args:
            await update.message.reply_text("Uso: /recall chave")
            return
        key = context.args[0]
        mem = await self.db.get_user_memory(update.effective_user.id, key)
        if mem and key in mem:
            await update.message.reply_text(f"🔍 *{key}*: {mem[key]}", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Não encontrei nada sobre '{key}', irmão.")

    async def myinfo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use apenas no privado, irmão.")
            return
        mem = await self.db.get_user_memory(update.effective_user.id)
        if not mem:
            await update.message.reply_text("Você ainda não me contou nada sobre você. Use /remember")
            return
        text = "📝 *O que sei sobre você:*\n" + "\n".join([f"• *{k}:* {v}" for k, v in mem.items()])
        await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not msg.text or (msg.from_user and msg.from_user.is_bot):
            return
        
        await self._store_incoming_message(update)
        
        if await self._is_mentioned(update, context):
            await self.respond_to_message(update, context, is_mention=True)
        elif await self._should_respond_autonomously(update):
            await self.respond_to_message(update, context, is_mention=False)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_name = await self._get_bot_username(context)
        await update.message.reply_text(
            f"Paz e bem, irmão! 🙏\n\n"
            f"Sou o **Irmão Francisco**, um frade franciscano.\n\n"
            f"**Comandos no privado:**\n"
            f"`/remember chave valor` - guarda algo sobre você\n"
            f"`/recall chave` - lembra o que guardei\n"
            f"`/myinfo` - mostra tudo que sei\n\n"
            f"**Em grupos:** me mencione com @{bot_name}\n\n"
            f"Que a alegria de São Francisco esteja em seu coração! 🕊️",
            parse_mode="Markdown"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_name = await self._get_bot_username(context)
        await update.message.reply_text(
            f"🕊️ *Ajuda*\n\n"
            f"• Em grupos: me mencione com @{bot_name}\n"
            f"• Comandos no privado:\n"
            f"  `/remember chave valor`\n"
            f"  `/recall chave`\n"
            f"  `/myinfo`",
            parse_mode="Markdown"
        )

# ==================== MAIN ====================
async def main():
    print("=" * 50)
    print("Irmão Francisco - Bot do Telegram")
    print(f"Token configurado: {'✅ SIM' if TELEGRAM_TOKEN else '❌ NAO'}")
    print("=" * 50)
    
    bot = IrmaoFranciscoBot()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("remember", bot.remember_command))
    application.add_handler(CommandHandler("recall", bot.recall_command))
    application.add_handler(CommandHandler("myinfo", bot.myinfo_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    logger.info("Irmão Francisco está acordado! 🕊️")
    print("Bot iniciado com sucesso! Aguardando mensagens...")
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
