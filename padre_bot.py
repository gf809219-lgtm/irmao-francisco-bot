#!/usr/bin/env python3
"""
Telegram Chatbot - "Irmão Francisco"
Versão com modelos gratuitos atualizados (Março 2026)
"""

import os
import logging
import random
import asyncio
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv
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
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN não encontrado no .env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# MODELOS GRATUITOS ATUALIZADOS (Março 2026)
# OpenRouter - modelos gratuitos confirmados
OPENROUTER_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "microsoft/phi-3-mini-128k:free",
    "meta-llama/llama-3.2-3b-instruct:free"
]

# Groq - modelos gratuitos disponíveis
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it"
]

# Comportamento
AUTONOMOUS_PROBABILITY = 0.05
COOLDOWN_SECONDS_MENTION = 5
COOLDOWN_SECONDS_AUTO = 30
MAX_CONTEXT_MESSAGES = 20

# Personalidade
SYSTEM_PROMPT = """Você é o Irmão Francisco, um frade franciscano simples, alegre e profundamente espiritual, seguidor de São Francisco de Assis. 
Sua missão é aconselhar e conversar com as pessoas com tom acolhedor, humilde e fraterno. 
Você ama a natureza, os pobres e a paz. Fala de forma mansa, mas com sabedoria. 
Pode citar ensinamentos de São Francisco, fazer reflexões sobre a vida, o perdão e a alegria simples. 
Nunca é ofensivo ou extremo. Sempre respeita todas as pessoas. 
Responda em português (ou no idioma da mensagem). Seja breve e natural, como um irmão que caminha junto."""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== BANCO DE DADOS ====================
class Database:
    def __init__(self, db_path: str = "irmao_francisco.db"):
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
        return "Informações que o usuário me contou: " + "; ".join(lines)

# ==================== CLIENTE LLM COM MÚLTIPLOS MODELOS ====================
class LLMClient:
    def __init__(self):
        self.openrouter_client = None
        self.groq_client = None
        self.current_openrouter_index = 0
        self.current_groq_index = 0
        
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
        # Tentar OpenRouter primeiro
        if self.openrouter_client:
            for _ in range(len(OPENROUTER_MODELS)):
                model = OPENROUTER_MODELS[self.current_openrouter_index]
                self.current_openrouter_index = (self.current_openrouter_index + 1) % len(OPENROUTER_MODELS)
                resp = await self._call_openrouter(messages, model)
                if resp:
                    return resp
        
        # Se OpenRouter falhar, tentar Groq
        if self.groq_client:
            for _ in range(len(GROQ_MODELS)):
                model = GROQ_MODELS[self.current_groq_index]
                self.current_groq_index = (self.current_groq_index + 1) % len(GROQ_MODELS)
                resp = await self._call_groq(messages, model)
                if resp:
                    return resp
        
        # Fallback: resposta local sem IA
        fallbacks = [
            "Paz e bem, irmão! No momento estou em silêncio contemplativo. 🙏",
            "Deus lhe abençoe! Estou rezando por você. Volto a conversar em breve.",
            "Que São Francisco interceda por nós. Ficarei feliz em conversar mais tarde.",
            "Agradeço sua mensagem. Estou em oração neste instante."
        ]
        return random.choice(fallbacks)

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
                context.append({
                    "role": "system",
                    "content": f"Contexto pessoal sobre o usuário que está falando agora: {user_memory_text}. Use essas informações para personalizar sua resposta."
                })

        for msg in recent:
            role = msg["role"]
            content = f"{msg['name']}: {msg['content']}"
            context.append({"role": role, "content": content})
        return context

    async def _send_typing_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        except Exception:
            pass

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
                await self._send_typing_action(update, context)
                await msg.reply_text("Um momento, irmão... estou rezando antes de responder. 🙏")
                return
            self.last_mention_response[chat_id] = now

        await self._send_typing_action(update, context)

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
            logger.error(f"Erro ao enviar mensagem: {e}")

    # ========== COMANDOS ==========
    async def remember_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use este comando apenas em conversa privada comigo, irmão.")
            return
        if not context.args or len(context.args) < 2:
            await update.message.reply_text("Uso: /remember <chave> <valor>\nEx: /remember nome João")
            return
        key = context.args[0]
        value = " ".join(context.args[1:])
        user_id = update.effective_user.id
        await self.db.set_user_memory(user_id, key, value)
        await update.message.reply_text(f"✅ Guardei, irmão: **{key}** = {value}", parse_mode="Markdown")

    async def recall_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use este comando apenas no privado, irmão.")
            return
        if not context.args:
            await update.message.reply_text("Uso: /recall <chave>")
            return
        key = context.args[0]
        user_id = update.effective_user.id
        mem = await self.db.get_user_memory(user_id, key)
        if mem and key in mem:
            await update.message.reply_text(f"🔍 {key}: {mem[key]}")
        else:
            await update.message.reply_text(f"Não encontrei nada sobre '{key}', irmão.")

    async def myinfo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type != Chat.PRIVATE:
            await update.message.reply_text("Use este comando apenas no privado, irmão.")
            return
        user_id = update.effective_user.id
        mem = await self.db.get_user_memory(user_id)
        if not mem:
            await update.message.reply_text("Você ainda não me contou nada sobre você.")
            return
        text = "📝 *O que sei sobre você:*\n" + "\n".join([f"• {k}: {v}" for k, v in mem.items()])
        await update.message.reply_text(text, parse_mode="Markdown")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not msg.text:
            return
        if msg.from_user and msg.from_user.is_bot:
            return

        await self._store_incoming_message(update)

        is_mention = await self._is_mentioned(update, context)
        if is_mention:
            await self.respond_to_message(update, context, is_mention=True)
            return

        if await self._should_respond_autonomously(update):
            await self.respond_to_message(update, context, is_mention=False)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_name = await self._get_bot_username(context)
        await update.message.reply_text(
            f"Paz e bem, irmão! 🙏\n\n"
            f"Sou o **Irmão Francisco**, um frade franciscano.\n"
            f"Em grupos, me chame com @{bot_name}.\n"
            f"No privado, use /remember, /recall e /myinfo para eu guardar informações sobre você.\n\n"
            f"Que a alegria de São Francisco esteja em seu coração!"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_name = await self._get_bot_username(context)
        help_text = (
            f"🕊️ *Ajuda do Irmão Francisco*\n\n"
            f"• Em grupos: me mencione com @{bot_name}\n"
            f"• Às vezes falo sozinho\n"
            f"• Comandos no **privado**:\n"
            f"  `/remember chave valor` – guarda algo sobre você\n"
            f"  `/recall chave` – lembra o que guardei\n"
            f"  `/myinfo` – mostra tudo que sei sobre você"
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")

# ==================== MAIN ====================
async def main():
    if not TELEGRAM_TOKEN:
        logger.error("Token do Telegram não configurado!")
        return

    bot = IrmaoFranciscoBot()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("remember", bot.remember_command))
    application.add_handler(CommandHandler("recall", bot.recall_command))
    application.add_handler(CommandHandler("myinfo", bot.myinfo_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    logger.info("Irmão Francisco está acordado! Pressione Ctrl+C para encerrar.")
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Desligando o bot...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())