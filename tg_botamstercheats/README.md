# BCWALLFILES Telegram-бот

Выдача файлов после проверки подписки. Стек: aiogram 3.x + aiosqlite.

## Render

1. Залейте репозиторий на GitHub.
2. На [render.com](https://render.com) → **New** → **Blueprint** (или **Web Service**) и укажите этот репозиторий.
3. В Environment задайте:
   - `BOT_TOKEN` — токен от @BotFather
   - `ADMIN_IDS` — `7479229021,823427049`
   - `MANDATORY_CHANNEL` — `@dfgames2`
4. Build: `pip install -r requirements.txt`
5. Start: `python -u main.py`
6. Health Check Path: `/`
7. Остановите бота на своём ПК, иначе будет `409 Conflict`.

На бесплатном Web Service Render засыпает примерно через 15 минут без HTTP-запросов.

Автопинг:
- локально: `python keepalive.py https://ВАШ-СЕРВИС.onrender.com`
- без ПК: залейте репозиторий на GitHub, в Actions Secrets добавьте `KEEP_ALIVE_URL`, workflow `.github/workflows/keepalive.yml` будет пинговать каждые 5 минут

SQLite на free-плане не переживает перезапуск контейнера. На платном тарифе можно смонтировать Disk и задать `DATA_DIR=/var/data`.

## Локально

```bash
pip install -r requirements.txt
python main.py
```
