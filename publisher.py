import logging
from datetime import datetime
from aiogram import Bot
from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from config import TELEGRAM_CHANNELS, PLATFORMS
from media import build_input_media, select_media_for_platform
import database as db

logger = logging.getLogger(__name__)


async def publish_to_telegram(
    bot: Bot,
    brand_key: str,
    text: str,
    media_list: list[dict],
    post_id: int,
) -> bool:
    """
    Публикует пост в Telegram-канал бренда.
    Фото отправленные как документ → оригинальное качество.
    """
    channel = TELEGRAM_CHANNELS.get(brand_key)
    if not channel:
        logger.warning(f"Telegram channel not configured for brand: {brand_key}")
        return False

    selected = select_media_for_platform(media_list, "telegram")

    try:
        if not selected:
            # Только текст
            await bot.send_message(channel, text, parse_mode="HTML")

        elif len(selected) == 1:
            m = selected[0]
            if m["type"] == "video":
                if m.get("as_document"):
                    await bot.send_document(channel, m["file_id"], caption=text, parse_mode="HTML")
                else:
                    await bot.send_video(channel, m["file_id"], caption=text, parse_mode="HTML")
            elif m["type"] == "document":
                await bot.send_document(channel, m["file_id"], caption=text, parse_mode="HTML")
            else:
                if m.get("as_document"):
                    # Фото в оригинальном качестве
                    await bot.send_document(channel, m["file_id"], caption=text, parse_mode="HTML")
                else:
                    await bot.send_photo(channel, m["file_id"], caption=text, parse_mode="HTML")

        else:
            # Медиагруппа (несколько файлов)
            media_group = []
            for i, m in enumerate(selected):
                caption = text if i == 0 else None
                media_group.append(build_input_media(m, caption=caption, platform_key="telegram"))
            await bot.send_media_group(channel, media_group)

        await db.update_post_status(post_id, "published", datetime.utcnow())
        logger.info(f"Published post {post_id} to Telegram channel {channel}")
        return True

    except Exception as e:
        logger.error(f"Failed to publish post {post_id} to Telegram: {e}")
        return False


# ─── Stubs for future platforms ──────────────────────────────

async def publish_to_instagram(brand_key: str, text: str, media_list: list, post_id: int) -> bool:
    """
    TODO: Meta Graph API
    Требует: Instagram Business Account + Facebook App
    Docs: https://developers.facebook.com/docs/instagram-api
    """
    logger.info(f"[STUB] Instagram publish for post {post_id}")
    return False


async def publish_to_facebook(brand_key: str, text: str, media_list: list, post_id: int) -> bool:
    """
    TODO: Meta Graph API (Pages)
    Docs: https://developers.facebook.com/docs/pages-api
    """
    logger.info(f"[STUB] Facebook publish for post {post_id}")
    return False


async def publish_to_tiktok(brand_key: str, text: str, media_list: list, post_id: int) -> bool:
    """
    TODO: TikTok for Developers
    Docs: https://developers.tiktok.com/doc/content-posting-api-get-started
    """
    logger.info(f"[STUB] TikTok publish for post {post_id}")
    return False


async def publish_to_threads(brand_key: str, text: str, media_list: list, post_id: int) -> bool:
    """
    TODO: Threads API (Meta)
    Docs: https://developers.facebook.com/docs/threads
    """
    logger.info(f"[STUB] Threads publish for post {post_id}")
    return False


# ─── Dispatcher ──────────────────────────────────────────────

PUBLISHERS = {
    "telegram":  publish_to_telegram,
    "instagram": publish_to_instagram,
    "facebook":  publish_to_facebook,
    "tiktok":    publish_to_tiktok,
    "threads":   publish_to_threads,
}


async def publish_post(
    bot: Bot,
    platform_key: str,
    brand_key: str,
    text: str,
    media_list: list,
    post_id: int,
) -> bool:
    publisher = PUBLISHERS.get(platform_key)
    if not publisher:
        return False

    if platform_key == "telegram":
        return await publisher(bot, brand_key, text, media_list, post_id)
    else:
        return await publisher(brand_key, text, media_list, post_id)
