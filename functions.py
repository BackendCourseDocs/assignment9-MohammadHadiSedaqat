from fastapi import FastAPI, Query, Form, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
import requests
from pydantic import BaseModel, Field
from typing import Optional
import os
import shutil
import uuid
import psycopg2
import redis
import json
import hashlib

redis_client = redis.Redis(host="localhost", port=6379, db=0, socket_connect_timeout=1)


conn = psycopg2.connect(
    dbname="books", user="hadisedaghat", password="", host="localhost", port="5432"
)
cursor = conn.cursor()

CACHE_TTL_BOOKS = 60
CACHE_TTL_AUTHORS = 120


os.makedirs("images", exist_ok=True)
app = FastAPI()
app.mount("/images", StaticFiles(directory="images"), name="images")
books = []
size = 0



class BookValidation(BaseModel):
    title: str = Field(..., min_length=3, max_length=100)
    author: str = Field(..., min_length=3, max_length=100)
    publisher: str = Field(..., min_length=3, max_length=100)
    first_publish_year: int = Field(..., ge=0)
    image_url: Optional[str] = None


def make_cache_key(prefix: str, payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{h}"


def cache_get_json(key: str):
    try:
        val = redis_client.get(key)
        if not val:
            return None
        return json.loads(val)
    except Exception:
        return None


def cache_set_json(key: str, obj, ttl: int):
    try:
        redis_client.setex(key, ttl, json.dumps(obj, ensure_ascii=False))
    except Exception:
        pass


def cache_invalidate_prefix(prefix: str):
    try:
        keys = redis_client.keys(f"{prefix}:*")
        if keys:
            redis_client.delete(*keys)
    except Exception:
        pass


def load_initial_data():
    global books, size
    url = "https://openlibrary.org/search.json"
    params = {"q": "python", "limit": 58}
    response = requests.get(url, params=params)
    data = response.json()

    for index, book in enumerate(data.get("docs", [])):
        books.append(
            {
                "id": 999 + index,
                "title": book.get("title", "Unknown"),
                "author": (
                    book.get("author_name", ["Unknown"])[0]
                    if book.get("author_name")
                    else "Unknown"
                ),
                "publisher": (
                    book.get("publisher", ["Unknown"])[0]
                    if book.get("publisher")
                    else "Unknown"
                ),
                "first_publish_year": book.get("first_publish_year", 0),
                "image_url": None,
                "source": "OpenLibrary",
            }
        )
        size += 1
