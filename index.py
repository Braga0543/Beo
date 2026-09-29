# -*- coding: utf-8 -*-
"""
Бэкенд «Ассистента учителя» для деплоя на Vercel.

Почему не SQLite, как в локальной версии:
Vercel-функции — serverless: файловая система эфемерна, каждый "холодный старт"
может дать новый экземпляр без вчерашних данных, а при нескольких одновременных
запросах разные экземпляры вообще не видят один и тот же файл. Счётчик
«Со мной это было» на SQLite в таких условиях будет расти ненадёжно: обнуляться
после деплоя или на части запросов не сохраняться. Поэтому здесь Postgres
(подключается как "Vercel Postgres" через маркетплейс, на деле это Neon) —
он один общий для всех экземпляров функции, и регистрация случаев корректно
сохраняется между запросами и деплоями.

Vercel сам находит переменную `app` в этом файле по соглашению о расположении
(api/index.py) и разворачивает FastAPI-приложение целиком как одну функцию —
отдельный vercel.json не нужен.
"""
import os
from contextlib import asynccontextmanager, contextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from shared.violations_data import VIOLATIONS

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


@contextmanager
def get_conn():
    """Одно соединение на запрос. Явная ошибка, если DATABASE_URL не настроен —
    это самая частая причина падения при первом деплое (забыли подключить Postgres)."""
    if not DATABASE_URL:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL не задан. Подключите Postgres (Vercel → Storage → Marketplace → Neon) "
                   "и убедитесь, что переменная окружения DATABASE_URL добавлена в проект.",
        )
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn):
    """Идемпотентно создаёт таблицы и досеивает данные. Вызывается один раз на
    холодный старт функции; безопасна при повторных вызовах и при гонках."""
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS violations (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL,
        keywords TEXT NOT NULL,
        law_article TEXT NOT NULL,
        law_text TEXT NOT NULL,
        refusal_template TEXT NOT NULL,
        report_count INTEGER NOT NULL DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS reports (
        id SERIAL PRIMARY KEY,
        violation_id INTEGER NOT NULL REFERENCES violations(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    for v in VIOLATIONS:
        # ON CONFLICT: текст нарушения можно обновлять переразвёртыванием,
        # а report_count (реальные случаи от учителей) при этом не трогаем.
        cur.execute("""
            INSERT INTO violations (title, description, keywords, law_article, law_text, refusal_template)
            VALUES (%(title)s, %(description)s, %(keywords)s, %(law_article)s, %(law_text)s, %(refusal_template)s)
            ON CONFLICT (title) DO UPDATE SET
                description = EXCLUDED.description,
                keywords = EXCLUDED.keywords,
                law_article = EXCLUDED.law_article,
                law_text = EXCLUDED.law_text,
                refusal_template = EXCLUDED.refusal_template
        """, v)
    conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL:
        with get_conn() as conn:
            init_db(conn)
    yield


app = FastAPI(title="Ассистент учителя", lifespan=lifespan)

# Фронтенд отдаётся тем же доменом (public/), так что CORS не обязателен,
# но оставляем разрешение всех источников на случай локальных тестов файла.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ReportIn(BaseModel):
    violation_id: int


def normalize(s: str) -> str:
    return (s or "").strip().lower().replace("ё", "е")


@app.get("/api/search")
def search(q: str = ""):
    """Поиск по title и keywords. Нормализация ё→е выполняется по обе стороны
    прямо в SQL, чтобы не трогать хранимые данные (в них ё должна остаться для показа)."""
    q = normalize(q)
    with get_conn() as conn:
        cur = conn.cursor()
        if not q:
            cur.execute("SELECT id, title, description, report_count FROM violations "
                        "ORDER BY report_count DESC, id")
        else:
            words = [w for w in q.split() if len(w) >= 3] or [q]
            cond = " OR ".join([
                "REPLACE(LOWER(title), 'ё', 'е') LIKE %s OR REPLACE(LOWER(keywords), 'ё', 'е') LIKE %s"
            ] * len(words))
            params = []
            for w in words:
                params += [f"%{w}%", f"%{w}%"]
            cur.execute(
                f"SELECT id, title, description, report_count FROM violations WHERE {cond} "
                "ORDER BY report_count DESC, id", params)
        return {"results": [dict(r) for r in cur.fetchall()]}


@app.get("/api/violation/{violation_id}")
def violation(violation_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, title, description, law_article, law_text, "
                    "refusal_template, report_count FROM violations WHERE id = %s", (violation_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Нарушение не найдено")
        return dict(row)


@app.post("/api/report")
def report(body: ReportIn):
    """Анонимный счётчик: никаких данных о пользователе не сохраняется, только id нарушения."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM violations WHERE id = %s", (body.violation_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Нарушение не найдено")
        cur.execute("UPDATE violations SET report_count = report_count + 1 WHERE id = %s "
                    "RETURNING report_count", (body.violation_id,))
        new_count = cur.fetchone()["report_count"]
        cur.execute("INSERT INTO reports (violation_id) VALUES (%s)", (body.violation_id,))
        conn.commit()
        return {"ok": True, "report_count": new_count}


@app.get("/api/stats")
def stats():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(report_count), 0) AS total FROM violations")
        total = cur.fetchone()["total"]
        cur.execute("SELECT id, title, report_count FROM violations "
                    "ORDER BY report_count DESC, id LIMIT 3")
        top = [dict(r) for r in cur.fetchall()]
        return {"total_reports": total, "top_violations": top}
