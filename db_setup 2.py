# -*- coding: utf-8 -*-
"""
Ручной пересев базы. Приложение (api/index.py) само создаёт таблицы и досеивает
данные при первом запросе после деплоя, так что запускать этот файл для обычного
деплоя НЕ обязательно. Он нужен, когда вы:
  - тестируете локально и хотите пересоздать/обновить данные без поднятия сервера;
  - обновили тексты нарушений в shared/violations_data.py и хотите сразу применить
    изменения к уже развёрнутой базе, не дожидаясь следующего холодного старта;
  - хотите обнулить счётчики (см. закомментированный блок в конце main()).

Использование:
    export DATABASE_URL="postgres://...."   # тот же URL, что и на Vercel
    python db_setup.py
"""
import os
import sys

import psycopg2

from shared.violations_data import VIOLATIONS

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


def main():
    if not DATABASE_URL:
        print("Ошибка: переменная окружения DATABASE_URL не задана.\n"
              "Скопируйте строку подключения из Vercel (Storage → ваша база → .env.local) "
              "или выполните: vercel env pull .env.local && export $(cat .env.local | xargs)",
              file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
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

    added = 0
    for v in VIOLATIONS:
        cur.execute("SELECT id FROM violations WHERE title = %s", (v["title"],))
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE violations SET description=%s, keywords=%s, law_article=%s,
                           law_text=%s, refusal_template=%s WHERE id=%s""",
                        (v["description"], v["keywords"], v["law_article"],
                         v["law_text"], v["refusal_template"], row[0]))
        else:
            cur.execute("""INSERT INTO violations (title, description, keywords, law_article,
                           law_text, refusal_template) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (v["title"], v["description"], v["keywords"], v["law_article"],
                         v["law_text"], v["refusal_template"]))
            added += 1

    # Раскомментируй, если нужно обнулить счётчики (например, после тестового прогона):
    # cur.execute("UPDATE violations SET report_count = 0")
    # cur.execute("DELETE FROM reports")

    conn.commit()
    conn.close()
    print(f"Готово. Добавлено новых записей: {added}, всего в списке: {len(VIOLATIONS)}")


if __name__ == "__main__":
    main()
