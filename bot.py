"""
aurora vpn — backend.

Three tiers are planned:
    01 · FREE   — one free key per user, valid forever (the only tier live today)
    02 · PRO    — paid subscription (coming soon)
    03 · ATLAS  — expanded paid subscription (coming soon)

This file implements the FREE tier. The cooldown is configured to be
effectively forever ('one key per user') — when paid tiers come online,
that constant becomes tier-aware.

Architecture:
    - aiohttp HTTP server: REST API + Telegram webhook
    - aiogram (webhook mode): bot commands
    - asyncpg pool: Postgres (Neon / Supabase / any)
    - HMAC-SHA256: every API call from the WebApp is verified using
      the bot token as the shared secret — the browser cannot forge a request.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import parse_qsl

import asyncpg
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from dotenv import load_dotenv

# =============================================================================
# CONFIGURATION
# =============================================================================

load_dotenv()


def _env(name: str, default: Optional[str] = None, *, required: bool = False) -> str:
    """Read an env var; raise if required and missing. Empty string counts as missing."""
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required but missing.")
    return value or ""


# --- Secrets (set in Render dashboard) --------------------------------------
BOT_TOKEN: str = _env("BOT_TOKEN", required=True)
DATABASE_URL: str = _env("DATABASE_URL", required=True)
PUBLIC_URL: str = _env("PUBLIC_URL", required=True).rstrip("/")
WEBHOOK_SECRET: str = _env("WEBHOOK_SECRET", required=True)

# --- Identifiers ------------------------------------------------------------
WEBAPP_URL: str = _env("WEBAPP_URL", required=True).rstrip("/") + "/"
CORS_ORIGIN: str = _env("CORS_ORIGIN", required=True).rstrip("/")
ADMIN_IDS: list[int] = [
    int(uid) for uid in _env("ADMIN_IDS", "").split(",") if uid.strip().isdigit()
]

# --- Tier policy ------------------------------------------------------------
BRAND_NAME: str = "aurora"

# FREE tier: one key per user, forever. We implement this by setting the
# cooldown so high that no one will ever wait it out (~100 years). When we
# introduce paid tiers, the constant becomes a per-tier dict and the API picks
# the right value based on the requesting user's subscription.
FREE_TIER_REISSUE_HOURS: int = 365 * 100 * 24  # ~876,000 hours = "forever"
COOLDOWN_HOURS_BETWEEN_CODES: int = FREE_TIER_REISSUE_HOURS

LOW_STOCK_WARNING_THRESHOLD: int = 5
INIT_DATA_MAX_AGE_SECONDS: int = 86_400  # WebApp initData is valid for 24h

ADMIN_ADD_COMMAND_PREFIX: str = "/add"

# --- Server -----------------------------------------------------------------
HTTP_PORT: int = int(os.environ.get("PORT", "8080"))
HTTP_HOST: str = "0.0.0.0"
WEBHOOK_PATH: str = "/webhook"
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("aurora")


# =============================================================================
# DATABASE
# =============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vpn_codes (
    id        SERIAL PRIMARY KEY,
    code      TEXT UNIQUE NOT NULL,
    is_used   BOOLEAN NOT NULL DEFAULT FALSE,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    used_at   TIMESTAMPTZ,
    used_by   BIGINT
);

CREATE TABLE IF NOT EXISTS user_requests (
    id           SERIAL PRIMARY KEY,
    telegram_id  BIGINT NOT NULL,
    username     TEXT,
    code_id      INTEGER NOT NULL REFERENCES vpn_codes(id),
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_requests_tg ON user_requests(telegram_id);
CREATE INDEX IF NOT EXISTS idx_vpn_codes_unused ON vpn_codes(id) WHERE NOT is_used;
"""


async def init_database_pool() -> asyncpg.Pool:
    """Open the asyncpg pool and run the schema migration."""
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=20,
        ssl="require",
    )
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    logger.info("Database pool ready.")
    return pool


@asynccontextmanager
async def db_tx(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a pooled connection inside a transaction. Auto-commits on exit."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            yield conn


# =============================================================================
# TELEGRAM initData VALIDATION
# =============================================================================

def validate_init_data(init_data: str) -> Optional[dict[str, Any]]:
    """
    Verify Telegram WebApp `initData` and return the user dict if valid.

    Algorithm (per Telegram docs):
        1. parse query string, pop `hash`
        2. data_check_string = sorted "key=value" pairs joined by '\\n'
        3. secret_key = HMAC-SHA256("WebAppData", bot_token)
        4. expected  = HMAC-SHA256(secret_key, data_check_string)
        5. constant-time compare expected vs hash
        6. reject if auth_date is too old

    This is the only thing standing between our API and a forged request.
    The browser cannot produce `hash` without the bot token.
    """
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    except Exception:
        return None

    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    try:
        auth_date = int(params.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date == 0:
        return None

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if now_ts - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        return None

    user_json = params.get("user")
    if not user_json:
        return None
    try:
        user = json.loads(user_json)
    except json.JSONDecodeError:
        return None

    if "id" not in user:
        return None
    return user


# =============================================================================
# REPOSITORY (every SQL statement lives here)
# =============================================================================

async def repo_get_last_issue(
    conn: asyncpg.Connection, telegram_id: int
) -> Optional[asyncpg.Record]:
    """Return the most recent (issued_at, code) for this user, or None."""
    return await conn.fetchrow(
        """
        SELECT ur.issued_at, v.code
          FROM user_requests ur
          JOIN vpn_codes v ON v.id = ur.code_id
         WHERE ur.telegram_id = $1
         ORDER BY ur.issued_at DESC
         LIMIT 1
        """,
        telegram_id,
    )


async def repo_count_available(conn: asyncpg.Connection) -> int:
    """Return the number of unused codes in the pool."""
    value = await conn.fetchval("SELECT COUNT(*) FROM vpn_codes WHERE NOT is_used")
    return int(value or 0)


async def repo_issue_code(
    conn: asyncpg.Connection, telegram_id: int, username: Optional[str]
) -> Optional[str]:
    """
    Atomically pick the next available code and mark it used.

    `FOR UPDATE SKIP LOCKED` ensures concurrent calls never hand out the same
    row — each transaction grabs one that no one else holds.
    """
    row = await conn.fetchrow(
        """
        SELECT id, code FROM vpn_codes
         WHERE NOT is_used
         ORDER BY id ASC
         LIMIT 1
         FOR UPDATE SKIP LOCKED
        """
    )
    if row is None:
        return None

    code_id = row["id"]
    code_value = row["code"]
    now = datetime.now(timezone.utc)

    await conn.execute(
        "UPDATE vpn_codes SET is_used = TRUE, used_at = $1, used_by = $2 WHERE id = $3",
        now,
        telegram_id,
        code_id,
    )
    await conn.execute(
        """
        INSERT INTO user_requests (telegram_id, username, code_id, issued_at)
        VALUES ($1, $2, $3, $4)
        """,
        telegram_id,
        username,
        code_id,
        now,
    )
    logger.info("Issued code id=%s to user=%s (@%s)", code_id, telegram_id, username)
    return code_value


async def repo_add_codes(conn: asyncpg.Connection, raw_codes: list[str]) -> int:
    """Insert new codes, skipping duplicates and blanks. Returns count inserted."""
    inserted = 0
    for raw in raw_codes:
        code = raw.strip()
        if not code:
            continue
        result = await conn.fetchval(
            "INSERT INTO vpn_codes (code) VALUES ($1) "
            "ON CONFLICT (code) DO NOTHING RETURNING id",
            code,
        )
        if result is not None:
            inserted += 1
    logger.info("Codes added: %d new (of %d submitted)", inserted, len(raw_codes))
    return inserted


async def repo_stats(conn: asyncpg.Connection) -> dict[str, int]:
    """Return pool size and totals."""
    available = await conn.fetchval(
        "SELECT COUNT(*) FROM vpn_codes WHERE NOT is_used"
    )
    issued = await conn.fetchval("SELECT COUNT(*) FROM vpn_codes WHERE is_used")
    unique_users = await conn.fetchval(
        "SELECT COUNT(DISTINCT telegram_id) FROM user_requests"
    )
    return {
        "available": int(available or 0),
        "issued": int(issued or 0),
        "unique_users": int(unique_users or 0),
    }


# =============================================================================
# BUSINESS LOGIC
# =============================================================================

def cooldown_remaining(last_issued: datetime) -> timedelta:
    """Time left until next code can be requested. Zero if cooldown passed."""
    elapsed = datetime.now(timezone.utc) - last_issued
    remaining = timedelta(hours=COOLDOWN_HOURS_BETWEEN_CODES) - elapsed
    return remaining if remaining.total_seconds() > 0 else timedelta(0)


async def notify_admins_low_stock(bot: Bot, pool: asyncpg.Pool) -> None:
    """DM every admin a low-stock warning. Errors are logged, never raised."""
    async with db_tx(pool) as conn:
        available = await repo_count_available(conn)
    if available > LOW_STOCK_WARNING_THRESHOLD:
        return

    text = (
        f"<b>запас кодов на исходе</b>\n"
        f"\n"
        f"осталось: <b>{available}</b>\n"
        f"пополните пул командой <code>/add</code>."
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.warning("Could not notify admin %s: %s", admin_id, exc)


# =============================================================================
# HTTP API
# =============================================================================

def _cors_headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """CORS headers for the single allowed origin (GitHub Pages domain)."""
    h = {
        "Access-Control-Allow-Origin": CORS_ORIGIN,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }
    if extra:
        h.update(extra)
    return h


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Add CORS headers to every response. Answer preflights inline."""
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        return web.Response(status=204, headers=_cors_headers())

    try:
        response = await handler(request)
    except web.HTTPException as exc:
        for k, v in _cors_headers().items():
            exc.headers[k] = v
        raise

    for k, v in _cors_headers().items():
        response.headers[k] = v
    return response


async def _authenticate(request: web.Request) -> Optional[dict[str, Any]]:
    """Read the 'Authorization: tma <initData>' header and validate it."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("tma "):
        return None
    init_data = auth[4:].strip()
    if not init_data:
        return None
    return validate_init_data(init_data)


async def api_status(request: web.Request) -> web.Response:
    """
    GET /api/status

    Returns the user's current state — used by the WebApp on load to render
    the right view (idle / active / empty).
    """
    user = await _authenticate(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    pool: asyncpg.Pool = request.app["db_pool"]
    user_id = int(user["id"])

    async with db_tx(pool) as conn:
        last = await repo_get_last_issue(conn, user_id)
        available = await repo_count_available(conn)

    last_code: Optional[str] = None
    next_available_at: Optional[str] = None
    can_request_now = True

    if last:
        last_issued: datetime = last["issued_at"]
        last_code = last["code"]
        remaining = cooldown_remaining(last_issued)
        if remaining.total_seconds() > 0:
            can_request_now = False
            next_available_at = (datetime.now(timezone.utc) + remaining).isoformat()

    return web.json_response(
        {
            "user_id": user_id,
            "first_name": user.get("first_name", ""),
            "tier": "free",  # the only live tier today
            "last_code": last_code,
            "next_available_at": next_available_at,
            "can_request_now": can_request_now and available > 0,
            "pool_available": available,
        }
    )


async def api_issue_code(request: web.Request) -> web.Response:
    """
    POST /api/issue-code

    Atomically issues the next available code to the authenticated user,
    respecting the FREE-tier 'one key per user, forever' policy.

    Responses:
        200 {"code": "...", "issued_at": "..."}
        401 {"error": "unauthorized"}
        429 {"error": "already_issued", "code": "...prev..."}
        503 {"error": "no_codes_available"}
    """
    user = await _authenticate(request)
    if user is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    pool: asyncpg.Pool = request.app["db_pool"]
    bot: Bot = request.app["bot"]
    user_id = int(user["id"])
    username = user.get("username")

    async with db_tx(pool) as conn:
        last = await repo_get_last_issue(conn, user_id)
        if last:
            remaining = cooldown_remaining(last["issued_at"])
            if remaining.total_seconds() > 0:
                # For FREE tier this means "user already has their key" —
                # the cooldown is effectively forever. Return their existing
                # code so the WebApp can re-display it.
                return web.json_response(
                    {
                        "error": "already_issued",
                        "code": last["code"],
                    },
                    status=429,
                )

        code = await repo_issue_code(conn, user_id, username)

    if code is None:
        asyncio.create_task(notify_admins_low_stock(bot, pool))
        return web.json_response({"error": "no_codes_available"}, status=503)

    asyncio.create_task(notify_admins_low_stock(bot, pool))

    return web.json_response(
        {
            "code": code,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
    )


async def health(request: web.Request) -> web.Response:
    """Liveness probe — used by Render and external keep-alive pings."""
    return web.Response(text="ok")


# =============================================================================
# BOT HANDLERS
# =============================================================================

router = Router(name="aurora")


def _is_admin(telegram_id: int) -> bool:
    """Whitelist check used by admin commands."""
    return telegram_id in ADMIN_IDS


def _welcome_keyboard() -> InlineKeyboardMarkup:
    """The 'open aurora' WebApp button below the /start message."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="открыть aurora",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    """/start — editorial welcome card with the WebApp button."""
    welcome_text = (
        "<b>aurora vpn</b>\n"
        "<i>приватный доступ к свободному интернету</i>\n"
        "\n"
        "<b>free</b>  ·  ключ навсегда\n"
        "\n"
        "безлимит  ·  скорость  ·  приватность\n"
        "\n"
        "<i>один код — ваш ключ.</i>\n"
        "\n"
        "откройте приложение."
    )
    try:
        await message.answer(welcome_text, reply_markup=_welcome_keyboard())
    except Exception:
        logger.exception("Failed to send welcome message")


@router.message(Command("add"))
async def handle_add_codes(message: Message) -> None:
    """
    Admin: /add  CODE1  CODE2 ...
    
    Newlines or spaces work. Duplicates are silently skipped.
    """
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    if not message.text:
        return

    payload = message.text[len(ADMIN_ADD_COMMAND_PREFIX):].strip()
    if not payload:
        await message.answer(
            "отправьте коды после команды, по одному на строке:\n"
            "<code>/add\nCODE-1\nCODE-2</code>"
        )
        return

    candidate_codes = payload.split()
    pool: asyncpg.Pool = message.bot.workflow_data["db_pool"]  # type: ignore[attr-defined]

    try:
        async with db_tx(pool) as conn:
            newly_added = await repo_add_codes(conn, candidate_codes)
            available = await repo_count_available(conn)
    except Exception:
        logger.exception("Failed to add codes")
        await message.answer("не удалось добавить коды. загляните в логи.")
        return

    await message.answer(
        f"добавлено новых кодов: <b>{newly_added}</b>\n"
        f"всего доступно: <b>{available}</b>"
    )


@router.message(Command("stats"))
async def handle_stats(message: Message) -> None:
    """Admin: /stats — pool size and totals."""
    if message.from_user is None or not _is_admin(message.from_user.id):
        return

    pool: asyncpg.Pool = message.bot.workflow_data["db_pool"]  # type: ignore[attr-defined]
    try:
        async with db_tx(pool) as conn:
            stats = await repo_stats(conn)
    except Exception:
        logger.exception("Failed to fetch stats")
        await message.answer("не удалось получить статистику.")
        return

    await message.answer(
        f"<b>статистика</b>\n"
        f"\n"
        f"свободных кодов: <b>{stats['available']}</b>\n"
        f"выдано всего: <b>{stats['issued']}</b>\n"
        f"уникальных пользователей: <b>{stats['unique_users']}</b>"
    )


# =============================================================================
# LIFECYCLE
# =============================================================================

async def on_startup(app: web.Application) -> None:
    """Open the DB pool and install the Telegram webhook."""
    app["db_pool"] = await init_database_pool()

    bot: Bot = app["bot"]
    app["dp"].workflow_data["db_pool"] = app["db_pool"]
    bot.workflow_data = app["dp"].workflow_data  # type: ignore[attr-defined]

    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message"],
    )
    logger.info("Webhook installed at %s", webhook_url)


async def on_shutdown(app: web.Application) -> None:
    """Tear down the webhook, close bot session and DB pool."""
    bot: Bot = app["bot"]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        logger.exception("Failed to delete webhook on shutdown")
    try:
        await bot.session.close()
    except Exception:
        pass
    pool: Optional[asyncpg.Pool] = app.get("db_pool")
    if pool:
        await pool.close()


def create_app() -> web.Application:
    """Compose the aiohttp application with all routes and middlewares."""
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    app = web.Application(middlewares=[cors_middleware])
    app["bot"] = bot
    app["dp"] = dispatcher

    # Health and API routes
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/issue-code", api_issue_code)

    # Webhook route — aiogram verifies X-Telegram-Bot-Api-Secret-Token
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    """Entry point — boot the HTTP server."""
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty — nobody will be able to add codes.")
    logger.info("Starting %s on %s:%d", BRAND_NAME, HTTP_HOST, HTTP_PORT)
    web.run_app(create_app(), host=HTTP_HOST, port=HTTP_PORT, print=None)


if __name__ == "__main__":
    main()
