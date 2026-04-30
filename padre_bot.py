#!/usr/bin/env python3
"""
Telegram Chatbot - "Irmão Francisco"
Versão para Render.com - SEM .env
"""

import os
import logging
import random
import asyncio
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from telegram import Update, Chat
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from openai import AsyncOpenAI

# ==================== CONFIGURAÇÃO ====================
# PEGA O TOKEN DAS VARIÁVEIS DE AMBIENTE DO RENDER
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# SE NÃO TIVER TOKEN, MOSTRA ERRO CLARO
if not TELEGRAM_TOKEN:
    print("ERRO: TELEGRAM_BOT_TOKEN não configurado no Render!")
    print("Vá em Environment -> Add Environment Variable")
    print("Key: TELEGRAM_BOT_TOKEN")
    print("Value: seu token")
    exit(1)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Modelos gratuitos
OPENROUTER_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "microsoft/phi-3-mini-128k:free",
]
GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# Comportamento
AUTONOMOUS_PROBABILITY = 0.05
COOLDOWN_SECONDS_MENTION = 5
COOLDOWN_SECONDS_AUTO = 30
MAX_CONTEXT_MESSAGES = 20

SYSTEM_PROMPT = """Você é o Irmão Francisco, um frade franciscano simples, alegre e profundamente espiritual. 
Responda em português, com tom acolhedor e fraterno. Seja breve e natural."""

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

    @asynccontextmanager
    async def _get_connection(self):
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            yield db

    async def store_message(self, chat_id: int, message_id: int, user_id: int,
                            username: str, first_name: str, text: str, is_bot: bool = False):
        async with self._get_connection() as db:
            await db.execute(
                "INSERT INTO messages (chat_id, message_id, user_id, username, first_name, text, is_bot, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, user_id, username or "", first_name or "", text or "", 1 if is_bot else 0, datetime.now().timestamp())
            )
            await db.commit()

    async def get_recent_messages(self, chat_id: int, limit: int = 50) -> List[Dict]:
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT user_id, username, first_name, text, is_bot, timestamp FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
                (chat_id, limit)
            )
            rows = await cursor.fetchall()
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
                    "user_id": user_id
                })
            return messages

    async def set_user_memory(self, user_id: int, key: str, value: str):
        async with self._get_connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO user_memory (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, key.strip().lower(), value.strip(), datetime.now().timestamp())
            )
            await db.commit()

    async def get_user_memory(self, user_id: int, key: str = None) -> Dict[str, str]:
        async with self._get_connection() as db:
            if key:
                cursor = await db.execute(
                    "SELECT key, value FROM user_memory WHERE user_id = ? AND key = ?",
                    (user_id, key.strip().lower())
                )
                row = await cursor.fetchone()
                return {row[0]: row[1]} if row else {}
            else:
                cursor = await db.execute(
                    "SELECT key, value FROM user_memory WHERE user_id = ?",
                    (user_id,)
                )
                rows = await cursor.fetchall()
                return {row[0]: row[1] for row in rows}

    async def get_all_user_memory_text(self, user_id: int) -> str:
        mem = await self.get_user_memory(user_id)
        if not mem:
            return ""
        lines = [f"{k}: {v}" for k, v in mem.items()]
        return "Informações sobre o usuário: " + "; ".join(lines)

# ==================== CLIENTE LLM ====================
class LLMClient:
    def __init__(self):
        self.openrouter_client = None
        self.groq_client = None
        if OPENROUTER_API_KEY:
            self.openrouter_client = AsyncOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_API_KEY,
            )
        if GROQ_API_KEY:
            self.groq_client = AsyncOpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=GROQ_API_KEY,
            )

    async def generate_response(self, messages: List[Dict]) -> Optional[str]:
        if self.openrouter_client:
            for model in OPENROUTER_MODELS:
                resp = await self._call_openrouter(messages, model)
                if resp:
                    return resp
        if self.groq_client:
            for model in GROQ_MODELS:
                resp = await self._call_groq(messages, model)
                if resp:
                    return resp
        return "Paz e bem! Estou em oração agora. 🙏"

    async def _call_openrouter(self, messages: List[Dict], model: str) -> Optional[str]:
        try:
            response = await self.openrouter_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=500,
                temperature=0.7,
                timeout=30.0
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Erro OpenRouter ({model}): {e}")
            return None

    async def _call_groq(self, messages: List[Dict], model: str) -> Optional[str]:
        try:
            response = await self.groq_client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=500,
                temperature=0.7,
                timeout=30.0
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Erro Groq ({model}): {e}")
            return None

# ==================== BOT PRINCIPAL ====================
class IrmaoFranciscoBot:
    def __init__(self):
        self.db = Database()
        self.llm = LLMClient()
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

    async def _prepare_context(self, chat_id: int, current_user_id: int = None) -> List[Dict]:
        recent = await self.db.get_recent_messages(chat_id, MAX_CONTEXT_MESSAGES)
        context = [{"role": "system", "content": SYSTEM_PROMPT}]
        if current_user_id:
            user_memory_text = await self.db.get_all_user_memory_text(current_user_id)
            if user_memory_text:
                context.append({"role": "system", "content": user_memory_text})
        for msg in recent:
            role = msg["role"]
            content = f"{msg['name']}: {msg['content']}"
            context.append({"role": role, "content": content})
        return context

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

        context_messages = await self._prepare_context(chat_id, current_user_id=user_id)
        user = msg.from_user
        current_msg_text = msg.text
        if is_mention:
            bot_username = await self._get_bot_username(context)
            current_msg_text = current_msg_text.replace(f"@{bot_username}", "").strip()
        
        context_messages.append({
            "role": "user",
            "content": f"{user.first_name or user.username or 'Alguém'}: {current_msg_text}"
        })

        response = await self.llm.generate_response(context_messages)
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
            await update.message.reply_text("Uso: /remember chave valor")
            return
        key = context.args[0]
        value = " ".join(context.args[1:])
        await self.db.set_user_memory(update.effective_user.id, key, value)
        await update.message.reply_text(f"✅ Guardei: {key} = {value}")

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
            await update.message.reply_text(f"🔍 {key}: {mem[key]}")
        else:
            await update.message.reply_text(f"Não encontrei '{key}'")

    async def myinfo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use apenas no privado, irmão.")
            return
        mem = await self.db.get_user_memory(update.effective_user.id)
        if not mem:
            await update.message.reply_text("Você ainda não me contou nada.")
            return
        text = "📝 O que sei sobre você:\n" + "\n".join([f"• {k}: {v}" for k, v in mem.items()])
        await update.message.reply_text(text)

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
            f"Paz e bem, irmão! 🙏\n\nSou o Irmão Francisco.\n"
            f"Use /remember, /recall e /myinfo no privado.\n"
            f"Em grupos, me mencione com @{bot_name}"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🕊️ Ajuda:\n"
            "/remember chave valor - guarda info\n"
            "/recall chave - recupera info\n"
            "/myinfo - mostra tudo\n"
            "Em grupos: me mencione!"
        )

# ==================== MAIN ====================
async def main():
    print(f"Token configurado: {'SIM' if TELEGRAM_TOKEN else 'NAO'}")
    print("Iniciando Irmão Francisco...")
    bot = IrmaoFranciscoBot()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("remember", bot.remember_command))
    application.add_handler(CommandHandler("recall", bot.recall_command))
    application.add_handler(CommandHandler("myinfo", bot.myinfo_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    logger.info("Irmão Francisco está acordado!")
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
