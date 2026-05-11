import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from anthropic import AsyncAnthropic

from config import (
    BOT_TOKEN, ALLOWED_USER_IDS, ANTHROPIC_API_KEY,
    BRANDS, PLATFORMS,
)
from media import (
    classify_message_media, select_media_for_platform,
    generate_ai_photo, generate_image_prompt_for_brand,
)
from publisher import publish_post
from scheduler import start_scheduler, stop_scheduler
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
anthropic = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ─── In-memory sessions ──────────────────────────────────────
# { user_id: { brand, platforms, post_type, raw_content, media, generated_posts,
#              awaiting_edit, awaiting_schedule_dt } }
sessions: dict[int, dict] = {}


def session(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = _empty_session()
    return sessions[uid]


def _empty_session() -> dict:
    return {
        "brand": None,
        "platforms": [],
        "post_type": None,
        "raw_content": None,
        "media": [],          # list of {type, file_id, as_document}
        "collecting_media": False,
        "generated_posts": {},   # { platform: text }
        "post_ids": {},          # { platform: db_id }
        "awaiting_edit": None,
        "awaiting_schedule": None,   # platform waiting for datetime input
        "publish_mode": None,        # "now" | "scheduled" | "draft"
    }


# ─── Keyboards ───────────────────────────────────────────────

def kb_brands():
    b = InlineKeyboardBuilder()
    for k, v in BRANDS.items():
        b.button(text=v["name"], callback_data=f"brand:{k}")
    b.adjust(1)
    return b.as_markup()


def kb_platforms(selected: list):
    b = InlineKeyboardBuilder()
    for k, v in PLATFORMS.items():
        check = "✅ " if k in selected else ""
        b.button(text=f"{check}{v['emoji']} {v['name']}", callback_data=f"plat:{k}")
    b.button(text="➡️ Продолжить", callback_data="plat:done")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def kb_post_type(platform_key: str):
    b = InlineKeyboardBuilder()
    types = PLATFORMS[platform_key]["post_types"]
    for t in types:
        b.button(text=t.capitalize(), callback_data=f"ptype:{t}")
    b.adjust(3)
    return b.as_markup()


def kb_media():
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Сгенерировать AI-фото",        callback_data="media:ai_photo")
    b.button(text="🎬 AI-видео (скоро)",              callback_data="media:ai_video")
    b.button(text="🎞 Видео из 3 фото (скоро)",      callback_data="media:ai_slides")
    b.button(text="📷 Уже прикрепил фото/видео",     callback_data="media:own")
    b.button(text="📝 Без медиа",                     callback_data="media:none")
    b.adjust(1)
    return b.as_markup()


def kb_approve(platform: str, idx: int, total: int):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Одобрить",          callback_data=f"ok:{platform}")
    b.button(text="✏️ Изменить",          callback_data=f"edit:{platform}")
    b.button(text="🔄 Перегенерировать",  callback_data=f"regen:{platform}")
    b.button(text="⏭ Пропустить",        callback_data=f"skip:{platform}")
    b.adjust(2, 2)
    return b.as_markup()


def kb_publish(platform: str):
    b = InlineKeyboardBuilder()
    b.button(text="🟢 Опубликовать сейчас",   callback_data=f"pub:now:{platform}")
    b.button(text="🕐 Запланировать",          callback_data=f"pub:schedule:{platform}")
    b.button(text="📋 Сохранить черновик",    callback_data=f"pub:draft:{platform}")
    b.adjust(1)
    return b.as_markup()


# ─── Helpers ─────────────────────────────────────────────────

def guard(uid: int) -> bool:
    return not ALLOWED_USER_IDS or uid in ALLOWED_USER_IDS


async def generate_post_text(brand_key: str, platform_key: str, post_type: str, raw: str) -> str:
    plat = PLATFORMS[platform_key]
    brand = BRANDS[brand_key]

    platform_rules = {
        "instagram": "До 300 слов. Добавь 6–8 тематических хэштегов в конце.",
        "facebook":  "До 400 слов. Живой и информативный текст.",
        "tiktok":    "До 100 слов. Короткий, динамичный, с призывом смотреть до конца.",
        "threads":   "До 250 слов. Лаконично, с хэштегами.",
        "telegram":  "До 500 слов. Допустимо HTML: <b>жирный</b>, <i>курсив</i>.",
    }.get(platform_key, "")

    prompt = (
        f"Создай {post_type} для {plat['name']}.\n\n"
        f"Тема и идея:\n{raw}\n\n"
        f"Требования к платформе: {platform_rules}\n\n"
        f"Верни ТОЛЬКО готовый текст поста. Без комментариев."
    )

    r = await anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=brand["system_prompt"],
        messages=[{"role": "user", "content": prompt}],
    )
    return r.content[0].text


async def show_approval(message: Message, s: dict, platform: str):
    platforms = s["platforms"]
    idx = platforms.index(platform)
    total = len(platforms)
    plat = PLATFORMS[platform]
    text = s["generated_posts"][platform]

    header = f"{plat['emoji']} *{plat['name']}* ({idx+1}/{total})\n{'─'*28}\n\n"
    await message.answer(
        header + text,
        parse_mode="Markdown",
        reply_markup=kb_approve(platform, idx, total),
    )


async def next_platform(message: Message, s: dict, current: str):
    platforms = s["platforms"]
    idx = platforms.index(current)
    if idx + 1 < len(platforms):
        await show_approval(message, s, platforms[idx + 1])
    else:
        brand = BRANDS[s["brand"]]
        await message.answer(
            f"🎉 Все посты для *{brand['name']}* готовы!\n\n"
            f"Для нового поста — /new",
            parse_mode="Markdown",
        )


# ─── Handlers: start / new ───────────────────────────────────

@dp.message(CommandStart())
@dp.message(Command("new"))
async def cmd_start(message: Message):
    if not guard(message.from_user.id):
        return await message.answer("⛔️ Доступ запрещён.")
    sessions[message.from_user.id] = _empty_session()
    await message.answer("👋 Выберите бренд:", reply_markup=kb_brands())


@dp.message(Command("history"))
async def cmd_history(message: Message):
    if not guard(message.from_user.id):
        return
    s = session(message.from_user.id)
    if not s["brand"]:
        return await message.answer("Сначала выберите бренд через /new")

    posts = await db.get_recent_posts(s["brand"], limit=10)
    if not posts:
        return await message.answer("История постов пуста.")

    lines = [f"*История — {BRANDS[s['brand']]['name']}*\n"]
    for p in posts:
        dt = p["created_at"].strftime("%d.%m %H:%M")
        plat = PLATFORMS.get(p["platform"], {}).get("emoji", "")
        status_icon = {"published": "✅", "scheduled": "🕐", "draft": "📋", "skipped": "⏭"}.get(p["status"], "❓")
        lines.append(f"{status_icon} {plat} `{p['platform']}` [{dt}]\n_{p['preview']}..._\n")

    await message.answer("\n".join(lines), parse_mode="Markdown")


# ─── Brand selection ─────────────────────────────────────────

@dp.callback_query(F.data.startswith("brand:"))
async def cb_brand(cb: CallbackQuery):
    brand_key = cb.data.split(":")[1]
    s = session(cb.from_user.id)
    s["brand"] = brand_key
    s["platforms"] = []
    await cb.message.edit_text(
        f"Бренд: *{BRANDS[brand_key]['name']}* ✓\n\nВыберите платформы:",
        parse_mode="Markdown",
        reply_markup=kb_platforms([]),
    )


# ─── Platform selection ──────────────────────────────────────

@dp.callback_query(F.data.startswith("plat:"))
async def cb_platform(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    s = session(cb.from_user.id)

    if key == "done":
        if not s["platforms"]:
            return await cb.answer("Выберите хотя бы одну платформу!", show_alert=True)
        plats = ", ".join(PLATFORMS[p]["emoji"] for p in s["platforms"])
        await cb.message.edit_text(
            f"Платформы: {plats} ✓\n\n"
            f"Шаг 3️⃣ — напишите мне:\n"
            f"• О чём пост (тема, идея)\n"
            f"• Тип: *пост / рилс / история / видео*\n"
            f"• Можно сразу прикрепить фото или видео",
            parse_mode="Markdown",
        )
        s["collecting_media"] = True
        return

    if key in s["platforms"]:
        s["platforms"].remove(key)
    else:
        s["platforms"].append(key)

    await cb.message.edit_reply_markup(reply_markup=kb_platforms(s["platforms"]))


# ─── Incoming media ──────────────────────────────────────────

async def _handle_media_message(message: Message):
    s = session(message.from_user.id)
    if not s["collecting_media"] and not s["brand"]:
        return await message.answer("Начните с /new")

    media = classify_message_media(message)
    if media:
        s["media"].append(media)

    # Подсказка о качестве при обычном фото
    if message.photo and not message.document:
        quality_hint = "\n\n💡 _Совет: для оригинального качества отправьте фото как файл (📎)_"
    else:
        quality_hint = ""

    text = message.caption or message.text or ""
    if text:
        s["raw_content"] = (s.get("raw_content") or "") + " " + text if s.get("raw_content") else text

    total_media = len(s["media"])
    if total_media > 0 and not text:
        await message.answer(
            f"{'📷' if media and media['type']=='photo' else '🎥'} Медиа принято ({total_media} файл(ов)){quality_hint}\n\n"
            f"Можно прикрепить ещё, или напишите тему/идею поста.",
            parse_mode="Markdown",
        )
    elif text and total_media > 0:
        await _ask_media_type(message, s)


async def _ask_media_type(message: Message, s: dict):
    if s["media"]:
        await _start_generation(message, s)
    else:
        await message.answer("Выберите медиа для постов:", reply_markup=kb_media())


@dp.message(F.photo)
async def on_photo(message: Message):
    await _handle_media_message(message)


@dp.message(F.video)
async def on_video(message: Message):
    await _handle_media_message(message)


@dp.message(F.document)
async def on_document(message: Message):
    await _handle_media_message(message)


# ─── Text input ──────────────────────────────────────────────

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    if not guard(message.from_user.id):
        return
    s = session(message.from_user.id)

    if not s["brand"]:
        return await message.answer("Начните с /new")

    # Ожидаем правки
    if s["awaiting_edit"]:
        return await _apply_edit(message, s)

    # Ожидаем дату/время для расписания
    if s["awaiting_schedule"]:
        return await _apply_schedule(message, s)

    # Собираем контент
    s["raw_content"] = message.text
    s["collecting_media"] = True

    if s["media"]:
        await _start_generation(message, s)
    else:
        await message.answer("Выберите медиа для постов:", reply_markup=kb_media())


# ─── Media choice ─────────────────────────────────────────────

@dp.callback_query(F.data.startswith("media:"))
async def cb_media(cb: CallbackQuery):
    action = cb.data.split(":")[1]
    s = session(cb.from_user.id)

    if action in ("ai_video", "ai_slides"):
        await cb.answer("🚧 Скоро будет доступно!", show_alert=True)
        return

    if action == "ai_photo":
        await cb.message.edit_text("🖼 Генерирую AI-фото и тексты постов...")
        prompt = await generate_image_prompt_for_brand(s["brand"], s["raw_content"], anthropic)
        url = await generate_ai_photo(prompt)
        if url:
            s["media"].append({"type": "photo", "file_id": url, "as_document": False, "is_url": True})
            await cb.message.answer(f"✅ AI-фото создано!")
        else:
            await cb.message.answer("⚠️ Не удалось сгенерировать фото. Продолжаем без медиа.")

    elif action == "own":
        await cb.message.edit_text(
            "📎 Хорошо! Отправьте фото или видео.\n\n"
            "Для оригинального качества → отправляйте как *файл* (📎), не как фото.",
            parse_mode="Markdown",
        )
        return

    await cb.message.edit_text("⏳ Генерирую посты...")
    await _start_generation(cb.message, s)


# ─── Generation ──────────────────────────────────────────────

async def _start_generation(message: Message, s: dict):
    brand_key = s["brand"]
    platforms = s["platforms"]

    wait = await message.answer(
        f"⏳ Генерирую {len(platforms)} пост(ов) для *{BRANDS[brand_key]['name']}*...",
        parse_mode="Markdown",
    )

    tasks = [
        generate_post_text(brand_key, p, s.get("post_type") or "пост", s["raw_content"])
        for p in platforms
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, plat in enumerate(platforms):
        if isinstance(results[i], Exception):
            s["generated_posts"][plat] = f"Ошибка: {results[i]}"
        else:
            s["generated_posts"][plat] = results[i]

    await wait.delete()
    await show_approval(message, s, platforms[0])


# ─── Approval flow ───────────────────────────────────────────

@dp.callback_query(F.data.startswith("ok:"))
async def cb_ok(cb: CallbackQuery):
    platform = cb.data[3:]
    s = session(cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    plat = PLATFORMS[platform]
    await cb.message.answer(
        f"✅ {plat['emoji']} {plat['name']} одобрен.\n\nКак публикуем?",
        reply_markup=kb_publish(platform),
    )


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery):
    platform = cb.data[5:]
    s = session(cb.from_user.id)
    s["awaiting_edit"] = platform
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("✏️ Напишите правки — что изменить, добавить или убрать:")


@dp.callback_query(F.data.startswith("regen:"))
async def cb_regen(cb: CallbackQuery):
    platform = cb.data[6:]
    s = session(cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    wait = await cb.message.answer("🔄 Перегенерирую...")
    new_text = await generate_post_text(s["brand"], platform, s.get("post_type") or "пост", s["raw_content"])
    s["generated_posts"][platform] = new_text
    await wait.delete()
    await show_approval(cb.message, s, platform)


@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery):
    platform = cb.data[5:]
    s = session(cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    plat = PLATFORMS[platform]

    post_id = await db.save_post(
        brand=s["brand"], platform=platform,
        post_type=s.get("post_type"), text=s["generated_posts"].get(platform, ""),
        media_type="none", status="skipped", raw_content=s["raw_content"],
    )

    await cb.message.answer(f"⏭ {plat['name']} пропущен.")
    await next_platform(cb.message, s, platform)


async def _apply_edit(message: Message, s: dict):
    platform = s["awaiting_edit"]
    s["awaiting_edit"] = None
    wait = await message.answer("✏️ Применяю правки...")
    brand = BRANDS[s["brand"]]
    plat = PLATFORMS[platform]

    r = await anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=brand["system_prompt"],
        messages=[{
            "role": "user",
            "content": (
                f"Текущий пост для {plat['name']}:\n\n{s['generated_posts'][platform]}\n\n"
                f"Правки: {message.text}\n\n"
                f"Обнови пост с учётом правок. Верни ТОЛЬКО готовый текст."
            ),
        }],
    )
    s["generated_posts"][platform] = r.content[0].text
    await wait.delete()
    await show_approval(message, s, platform)


# ─── Publish flow ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("pub:"))
async def cb_publish(cb: CallbackQuery):
    parts = cb.data.split(":")
    mode = parts[1]       # now | schedule | draft
    platform = parts[2]
    s = session(cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)

    if mode == "now":
        post_id = await db.save_post(
            brand=s["brand"], platform=platform,
            post_type=s.get("post_type"), text=s["generated_posts"][platform],
            media_type=s["media"][0]["type"] if s["media"] else "none",
            status="published", raw_content=s["raw_content"],
            scheduled_at=None,
        )
        s["post_ids"][platform] = post_id

        success = await publish_post(
            bot=bot, platform_key=platform, brand_key=s["brand"],
            text=s["generated_posts"][platform],
            media_list=s["media"], post_id=post_id,
        )

        plat = PLATFORMS[platform]
        if success:
            await cb.message.answer(f"🟢 Опубликовано в {plat['emoji']} {plat['name']}!")
        else:
            await cb.message.answer(
                f"📋 Пост сохранён. Автопубликация в {plat['name']} пока недоступна — опубликуйте вручную.\n\n"
                f"Текст уже скопирован выше 👆"
            )
        await next_platform(cb.message, s, platform)

    elif mode == "schedule":
        s["awaiting_schedule"] = platform
        now = datetime.now(timezone.utc) + timedelta(hours=2)  # +2 подсказка
        await cb.message.answer(
            f"🕐 Введите дату и время публикации (часовой пояс Андорры UTC+2):\n\n"
            f"Формат: `ДД.ММ.ГГГГ ЧЧ:ММ`\n"
            f"Например: `{now.strftime('%d.%m.%Y %H:%M')}`",
            parse_mode="Markdown",
        )

    elif mode == "draft":
        post_id = await db.save_post(
            brand=s["brand"], platform=platform,
            post_type=s.get("post_type"), text=s["generated_posts"][platform],
            media_type=s["media"][0]["type"] if s["media"] else "none",
            status="draft", raw_content=s["raw_content"],
        )
        plat = PLATFORMS[platform]
        await cb.message.answer(f"📋 Черновик сохранён для {plat['emoji']} {plat['name']}.")
        await next_platform(cb.message, s, platform)


async def _apply_schedule(message: Message, s: dict):
    platform = s["awaiting_schedule"]
    s["awaiting_schedule"] = None

    try:
        dt_naive = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        # Андорра UTC+2
        dt_utc = dt_naive.replace(tzinfo=timezone(timedelta(hours=2))).astimezone(timezone.utc)
    except ValueError:
        s["awaiting_schedule"] = platform
        return await message.answer(
            "⚠️ Неверный формат. Попробуйте ещё раз:\n`ДД.ММ.ГГГГ ЧЧ:ММ`",
            parse_mode="Markdown",
        )

    post_id = await db.save_post(
        brand=s["brand"], platform=platform,
        post_type=s.get("post_type"), text=s["generated_posts"][platform],
        media_type=s["media"][0]["type"] if s["media"] else "none",
        status="scheduled", raw_content=s["raw_content"],
        scheduled_at=dt_utc,
    )
    s["post_ids"][platform] = post_id

    # Сохраняем медиа-файлы в БД
    for i, m in enumerate(s["media"]):
        await db.save_media(post_id, m["file_id"], m["type"], m.get("as_document", False), i)

    plat = PLATFORMS[platform]
    local_dt = dt_naive.strftime("%d.%m.%Y в %H:%M")
    await message.answer(
        f"🕐 Запланировано!\n{plat['emoji']} {plat['name']} — {local_dt} (Андорра)",
    )
    await next_platform(message, s, platform)


# ─── Startup / shutdown ──────────────────────────────────────

async def on_startup():
    await db.init_db()
    start_scheduler(bot)
    logger.info("✅ Bot started")


async def on_shutdown():
    stop_scheduler()
    logger.info("Bot stopped")


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
