import os
import httpx
import asyncio
from aiogram import Bot
from aiogram.types import (
    Message, InputMediaPhoto, InputMediaVideo,
    InputMediaDocument, FSInputFile,
)
from config import PLATFORMS, REPLICATE_API_KEY


# ─── Collect media from incoming Telegram messages ───────────

def classify_message_media(message: Message) -> dict | None:
    """Определяет тип медиа входящего сообщения."""
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "as_document": False}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id, "as_document": False}
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("image/"):
            return {"type": "photo", "file_id": message.document.file_id, "as_document": True}
        if mime.startswith("video/"):
            return {"type": "video", "file_id": message.document.file_id, "as_document": True}
        return {"type": "document", "file_id": message.document.file_id, "as_document": True}
    return None


def select_media_for_platform(media_list: list[dict], platform_key: str) -> list[dict]:
    """
    Выбирает оптимальный набор медиа для платформы.
    Сохраняет флаг as_document для Telegram.
    """
    cfg = PLATFORMS[platform_key]
    photos = [m for m in media_list if m["type"] == "photo"]
    videos = [m for m in media_list if m["type"] == "video"]
    docs   = [m for m in media_list if m["type"] == "document"]

    result = []

    if platform_key == "tiktok":
        # TikTok: только видео, или одно фото как обложка
        if videos:
            result = [videos[0]]
        elif photos:
            result = [photos[0]]

    elif platform_key == "telegram":
        # Telegram: документы в оригинальном качестве
        # Фото как документ → оригинальное разрешение
        tg_photos = photos[:cfg["max_photos"]]
        tg_videos = videos[:cfg["max_videos"]]
        result = tg_photos + tg_videos + docs[:2]

    else:
        # Instagram, Facebook, Threads
        result = photos[:cfg["max_photos"]]
        if not result and videos:
            result = [videos[0]]
        elif videos and len(result) < cfg["max_photos"]:
            pass  # фото приоритетнее для карусели

    return result


# ─── AI Image Generation (Replicate / Flux) ──────────────────

async def generate_ai_photo(prompt: str) -> str | None:
    """
    Генерирует фото через Replicate (Flux Schnell).
    Возвращает URL изображения или None при ошибке.
    """
    if not REPLICATE_API_KEY:
        return None

    headers = {
        "Authorization": f"Token {REPLICATE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "version": "black-forest-labs/flux-schnell",
        "input": {
            "prompt": prompt,
            "num_outputs": 1,
            "aspect_ratio": "1:1",
            "output_format": "jpg",
            "output_quality": 90,
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        # Создаём задачу
        r = await client.post(
            "https://api.replicate.com/v1/predictions",
            headers=headers,
            json=payload,
        )
        prediction = r.json()
        pred_id = prediction.get("id")
        if not pred_id:
            return None

        # Ждём результата (polling)
        for _ in range(30):
            await asyncio.sleep(4)
            r = await client.get(
                f"https://api.replicate.com/v1/predictions/{pred_id}",
                headers=headers,
            )
            data = r.json()
            if data.get("status") == "succeeded":
                output = data.get("output")
                return output[0] if output else None
            if data.get("status") == "failed":
                return None

    return None


async def generate_image_prompt_for_brand(brand_key: str, raw_content: str, anthropic) -> str:
    """Просит Claude создать промпт для генерации изображения."""
    response = await anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=150,
        system="Создавай промпты для генерации фотографий. Отвечай ТОЛЬКО промптом на английском языке. Без пояснений.",
        messages=[{
            "role": "user",
            "content": (
                f"Создай промпт для реалистичного фото к посту о горнолыжном отдыхе в Андорре. "
                f"Тема поста: {raw_content[:200]}. "
                f"Стиль: профессиональная фотография, яркие цвета, горный пейзаж Андорры, "
                f"солнечная погода, люди в горнолыжной экипировке."
            ),
        }],
    )
    return response.content[0].text


# ─── AI Video (placeholder — Kling AI / Runway) ──────────────

async def generate_ai_video(prompt: str) -> str | None:
    """
    Заглушка для генерации видео.
    Будет подключён Kling AI или Runway Gen-4.
    """
    # TODO: интеграция с Kling AI API
    # https://klingai.com/api
    return None


async def generate_slideshow_video(photo_urls: list[str]) -> str | None:
    """
    Заглушка: видео из 3 фото.
    Будет реализовано через FFmpeg или Kling AI image-to-video.
    """
    # TODO: FFmpeg slideshow или Kling AI
    return None


# ─── Build Telegram InputMedia for sending ───────────────────

def build_input_media(media_item: dict, caption: str = None, platform_key: str = "telegram"):
    """
    Создаёт InputMedia объект для отправки через Telegram Bot API.
    Для Telegram: фото как документ = оригинальное качество.
    """
    file_id = media_item["file_id"]
    as_doc  = media_item.get("as_document", False)
    mtype   = media_item["type"]

    if platform_key == "telegram" and as_doc and mtype in ("photo", "video"):
        return InputMediaDocument(media=file_id, caption=caption)

    if mtype == "video":
        return InputMediaVideo(media=file_id, caption=caption)

    if mtype == "document":
        return InputMediaDocument(media=file_id, caption=caption)

    return InputMediaPhoto(media=file_id, caption=caption)
