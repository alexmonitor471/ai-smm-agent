import logging
import json
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from aiogram import Bot
import database as db
from publisher import publish_post

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Europe/Andorra")


async def process_scheduled_posts(bot: Bot):
    """Запускается каждую минуту, публикует посты у которых пришло время."""
    now = datetime.now(timezone.utc)
    posts = await db.get_scheduled_posts(before=now)

    for post in posts:
        media_raw = post.get("media") or []
        media_list = []

        for m in media_raw:
            if m and m.get("file_id"):
                media_list.append({
                    "type": m["file_type"],
                    "file_id": m["file_id"],
                    "as_document": m.get("as_document", False),
                })

        success = await publish_post(
            bot=bot,
            platform_key=post["platform"],
            brand_key=post["brand"],
            text=post["text"],
            media_list=media_list,
            post_id=post["id"],
        )

        if success:
            logger.info(f"✅ Scheduled post {post['id']} published to {post['platform']}")
        else:
            logger.warning(f"⚠️ Failed to publish scheduled post {post['id']}")


def start_scheduler(bot: Bot):
    scheduler.add_job(
        process_scheduled_posts,
        trigger=IntervalTrigger(minutes=1),
        args=[bot],
        id="scheduled_posts",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started (checking every minute)")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
