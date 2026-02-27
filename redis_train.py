from functions import *

@app.on_event("startup")
async def startup_event():
    load_initial_data()


@app.get("/books")
async def search_books(
    q: str = Query(..., min_length=3, max_length=100, description="Search query"),
    skip: Optional[int] = Query(0, ge=0),
    limit: Optional[int] = Query(10, ge=0),
):
    cache_key = make_cache_key("search_books", {"q": q, "skip": skip, "limit": limit})

    cached = cache_get_json(cache_key)

    if cached is not None:
        cached["cache"] = "HIT"
        return cached

    sql = "SELECT id, title, author, publisher, first_publish_year, image_url FROM books WHERE 1=1"
    db_params = []

    if q:
        query_like = f"%{q.lower()}%"
        sql += " AND (LOWER(title) LIKE %s OR LOWER(author) LIKE %s OR LOWER(publisher) LIKE %s OR CAST(first_publish_year AS TEXT) LIKE %s)"
        db_params.extend([query_like, query_like, query_like, query_like])

    try:
        cursor.execute(sql, tuple(db_params))
        rows = cursor.fetchall()
    except Exception:
        raise HTTPException(status_code=500, detail="Database query failed")

    db_results = [
        {
            "id": row[0],
            "title": row[1],
            "author": row[2],
            "publisher": row[3],
            "first_publish_year": row[4],
            "image_url": f"http://127.0.0.1:8000/images/{row[5]}" if row[5] else None,
            "source": "Database",
        }
        for row in rows
    ]

    query_lower = q.lower()

    ext_results = [
        book
        for book in books
        if query_lower in book["title"].lower()
        or query_lower in book["author"].lower()
        or query_lower in book["publisher"].lower()
        or query_lower in str(book["first_publish_year"])
    ]

    all_result = db_results + ext_results
    total_count = len(all_result)
    end = (skip + limit) if limit is not None else total_count
    final_result = all_result[skip:end]

    result = {
        "query": q,
        "all counts": total_count,
        "results": final_result,
        "skip": skip,
        "limit": limit,
        "cache": "MISS",
    }

    cache_set_json(cache_key, result, CACHE_TTL_BOOKS)
    return result


@app.get("/authors")
async def search_authors(
    q: str = Query(
        ..., min_length=1, max_length=100, description="Search query for authors"
    )
):
    cache_key = make_cache_key("authors_search", {"q": q})

    cached = cache_get_json(cache_key)

    if cached is not None:
        cached["cache"] = "HIT"
        return cached

    sql = """
        SELECT author, COUNT(*) AS book_count
        FROM books
        WHERE LOWER(author) LIKE %s
        GROUP BY author
    """

    cursor.execute(sql, (f"%{q.lower()}%",))
    db_results = [{"author": row[0], "book_count": row[1]} for row in cursor.fetchall()]
    merged_authors = {}

    for row in db_results:
        merged_authors[row["author"]] = row["book_count"]

    query_lower = q.lower()

    for book in books:
        author_name = book["author"]
        if query_lower in author_name.lower():
            merged_authors[author_name] = merged_authors.get(author_name, 0) + 1

    if not merged_authors:
        raise HTTPException(
            status_code=404, detail="No authors found matching the query"
        )

    final_results = [{"author": a, "book_count": c} for a, c in merged_authors.items()]

    result = {"query": q, "results": final_results, "cache": "MISS"}

    cache_set_json(cache_key, result, CACHE_TTL_AUTHORS)
    return result


@app.post("/books")
async def add_book(
    title: str = Form(..., min_length=3, max_length=100),
    author: str = Form(..., min_length=3, max_length=100),
    publisher: str = Form(..., min_length=3, max_length=100),
    first_publish_year: int = Form(..., ge=0),
    image: Optional[UploadFile] = File(None),
):
    image_file_name = None
    if image:
        extension = os.path.splitext(image.filename)[1]
        image_file_name = f"{uuid.uuid4()}{extension}"
        image_path = os.path.join("images", image_file_name)
        with open(image_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)

    try:
        cursor.execute(
            """
            INSERT INTO books (title, author, publisher, first_publish_year, image_url)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (title, author, publisher, first_publish_year, image_file_name),
        )

        new_id = cursor.fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        if image_file_name:
            delete_path = os.path.join("images", image_file_name)
            if os.path.exists(delete_path):
                os.remove(delete_path)
        raise HTTPException(status_code=500, detail="Failed to add book to database")

    cache_invalidate_prefix("books_search")
    cache_invalidate_prefix("authors_search")

    image_url = (
        f"http://127.0.0.1:8000/images/{image_file_name}" if image_file_name else None
    )
    return {
        "id": new_id,
        "title": title,
        "author": author,
        "publisher": publisher,
        "first_publish_year": first_publish_year,
        "image_url": image_url,
    }


@app.delete("/books/{id}")
async def delete_book(id: int):
    try:
        cursor.execute(
            """
            DELETE FROM books WHERE id = %s 
            RETURNING id, title, author, publisher, first_publish_year, image_url
            """,
            (id,),
        )

        deleted_book = cursor.fetchone()
        if not deleted_book:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Book not found")

        image_file_name = deleted_book[5]
        if image_file_name:
            delete_path = os.path.join("images", image_file_name)
            if os.path.exists(delete_path):
                os.remove(delete_path)
        conn.commit()
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(
            status_code=500, detail="Internal server error while deleting book"
        )

    cache_invalidate_prefix("books_search")
    cache_invalidate_prefix("authors_search")

    return {
        "message": "Book deleted successfully",
        "book": {
            "id": deleted_book[0],
            "title": deleted_book[1],
            "author": deleted_book[2],
            "publisher": deleted_book[3],
            "first_publish_year": deleted_book[4],
            "image_url": deleted_book[5],
        },
    }


@app.put("/books/{id}")
async def update_fully_book(
    id: int,
    title: str = Form(..., min_length=3, max_length=100),
    author: str = Form(..., min_length=3, max_length=100),
    publisher: str = Form(..., min_length=3, max_length=100),
    first_publish_year: int = Form(..., ge=0),
    image: Optional[UploadFile] = File(None),
):
    if id >= 999:
        raise HTTPException(
            status_code=403, detail="Cannot update books from external source"
        )

    cursor.execute("SELECT image_url FROM books WHERE id = %s", (id,))
    existing = cursor.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Book not found")

    old_image_name = existing[0]
    new_image_name = old_image_name

    if image:
        ext = os.path.splitext(image.filename)[1]
        new_image_name = f"{uuid.uuid4()}{ext}"
        new_path = os.path.join("images", new_image_name)
        with open(new_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        if old_image_name:
            old_path = os.path.join("images", old_image_name)
            if os.path.exists(old_path):
                os.remove(old_path)

    try:
        cursor.execute(
            """
            UPDATE books
            SET title = %s,
                author = %s,
                publisher = %s,
                first_publish_year = %s,
                image_url = %s
            WHERE id = %s RETURNING id, title, author, publisher, first_publish_year, image_url
            """,
            (title, author, publisher, first_publish_year, new_image_name, id),
        )
        updated_book = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Update failed")

    cache_invalidate_prefix("books_search")
    cache_invalidate_prefix("authors_search")

    return {
        "message": "Book fully updated",
        "book": {
            "id": updated_book[0],
            "title": updated_book[1],
            "author": updated_book[2],
            "publisher": updated_book[3],
            "first_publish_year": updated_book[4],
            "image_url": (
                f"http://127.0.0.1:8000/images/{updated_book[5]}"
                if updated_book[5]
                else None
            ),
        },
    }


@app.patch("/books/{id}")
async def update_book_part(
    id: int,
    title: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
    publisher: Optional[str] = Form(None),
    first_publish_year: Optional[int] = Form(None),
    image: Optional[UploadFile] = File(None),
):
    if id >= 999:
        raise HTTPException(status_code=403, detail="Cannot update external API data")

    cursor.execute(
        """
        SELECT title, author, publisher, first_publish_year, image_url
        FROM books WHERE id = %s
        """,
        (id,),
    )
    existing = cursor.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Book not found")

    new_title = title if title is not None else existing[0]
    new_author = author if author is not None else existing[1]
    new_publisher = publisher if publisher is not None else existing[2]
    new_year = first_publish_year if first_publish_year is not None else existing[3]
    new_image_name = existing[4]

    if image:
        ext = os.path.splitext(image.filename)[1]
        new_image_name = f"{uuid.uuid4()}{ext}"
        new_path = os.path.join("images", new_image_name)
        with open(new_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        if existing[4]:
            old_path = os.path.join("images", existing[4])
            if os.path.exists(old_path):
                os.remove(old_path)

    try:
        cursor.execute(
            """
            UPDATE books
            SET title              = %s,
                author             = %s,
                publisher          = %s,
                first_publish_year = %s,
                image_url          = %s
            WHERE id = %s RETURNING id, title, author, publisher, first_publish_year, image_url
            """,
            (new_title, new_author, new_publisher, new_year, new_image_name, id),
        )
        updated_book = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Partial update failed")

    cache_invalidate_prefix("books_search")
    cache_invalidate_prefix("authors_search")

    return {
        "message": "Book partially updated",
        "book": {
            "id": updated_book[0],
            "title": updated_book[1],
            "image_url": (
                f"http://127.0.0.1:8000/images/{updated_book[5]}"
                if updated_book[5]
                else None
            ),
        },
    }
