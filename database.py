import asyncpg
import json
from datetime import datetime
from config import DATABASE_URL


pool = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id          SERIAL PRIMARY KEY,
                brand       TEXT NOT NULL,
                platform    TEXT NOT NULL,
                post_type   TEXT,
                text        TEXT,
                media_type  TEXT,
                media_ids   JSONB DEFAULT '[]',
                status      TEXT DEFAULT 'draft',
                -- draft | scheduled | published | skipped
                scheduled_at TIMESTAMPTZ,
                published_at TIMESTAMPTZ,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                raw_content TEXT
            );

            CREATE TABLE IF NOT EXISTS media_files (
                id          SERIAL PRIMARY KEY,
                post_id     INT REFERENCES posts(id) ON DELETE CASCADE,
                file_id     TEXT,
                file_type   TEXT,  -- photo | video | document | ai_photo
                as_document BOOLEAN DEFAULT FALSE,
                position    INT DEFAULT 0
            );
        """)


async def save_post(
    brand: str,
    platform: str,
    post_type: str,
    text: str,
    media_type: str,
    status: str,
    raw_content: str,
    scheduled_at: datetime = None,
) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO posts
               (brand, platform, post_type, text, media_type, status, raw_content, scheduled_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               RETURNING id""",
            brand, platform, post_type, text, media_type, status, raw_content, scheduled_at,
        )
        return row["id"]


async def save_media(post_id: int, file_id: str, file_type: str, as_document: bool, position: int):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO media_files (post_id, file_id, file_type, as_document, position)
               VALUES ($1,$2,$3,$4,$5)""",
            post_id, file_id, file_type, as_document, position,
        )


async def update_post_status(post_id: int, status: str, published_at: datetime = None):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE posts SET status=$1, published_at=$2 WHERE id=$3",
            status, published_at, post_id,
        )


async def get_scheduled_posts(before: datetime):
    """Все посты со статусом scheduled у которых scheduled_at <= before"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.*, json_agg(mf ORDER BY mf.position) AS media
               FROM posts p
               LEFT JOIN media_files mf ON mf.post_id = p.id
               WHERE p.status = 'scheduled' AND p.scheduled_at <= $1
               GROUP BY p.id
               ORDER BY p.scheduled_at""",
            before,
        )
        return rows


async def get_recent_posts(brand: str, limit: int = 10):
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT id, platform, post_type, status, scheduled_at, published_at, created_at,
                      LEFT(text, 80) AS preview
               FROM posts WHERE brand=$1
               ORDER BY created_at DESC LIMIT $2""",
            brand, limit,
        )
