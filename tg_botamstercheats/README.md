# BCWALLFILES — гайд по запуску

Бот выдаёт файл по ссылке только после подписки на каналы.  
Стек: **aiogram 3.x** + **aiosqlite**. Хостинг: **Render**.

## Что уже настроено в коде

| Параметр | Значение |
| --- | --- |
| Админы | `7479229021`, `823427049` |
| Обязательные каналы | `@amstercheats`, `@amstercloud` |
| Старый канал | `@dfgames2` удалён |
| Старт | `python -u main.py` |
| Health-check | `/` (нужен Render) |

Перед деплоем добавьте бота **администратором** в `@amstercheats` и `@amstercloud`.

---

## 1. Залить код на GitHub

Если репозиторий уже есть — загрузите **новые** изменения (не весь проект с нуля):

```powershell
cd C:\Users\bober\Downloads\tg_botamstercheats
git add .
git commit -m "Update channels, admins, Render config"
git push
```

Если репозитория ещё нет:
1. Создайте репозиторий на GitHub.
2. Залейте туда все файлы проекта (`main.py`, `requirements.txt`, `render.yaml`, `keepalive.py`, `.github/...` и т.д.).

---

## 2. Создать сервис на Render

1. Откройте [https://dashboard.render.com](https://dashboard.render.com) и войдите (удобно через GitHub).
2. **New → Web Service**.
3. Подключите ваш GitHub-репозиторий с ботом.
4. Заполните:

| Поле | Значение |
| --- | --- |
| Name | `bcwallfiles-bot` (любое) |
| Language / Runtime | Python |
| Branch | `main` (или ваша ветка) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python -u main.py` |
| Instance Type | **Free** |
| Health Check Path | `/` |

Можно также: **New → Blueprint** и выбрать репозиторий — подхватится `render.yaml`.

---

## 3. Переменные окружения (Environment)

В настройках сервиса → **Environment** добавьте:

| Key | Value |
| --- | --- |
| `BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | `7479229021,823427049` |
| `MANDATORY_CHANNELS` | `@amstercheats,@amstercloud` |
| `PYTHON_VERSION` | `3.12.8` |

Нажмите **Deploy** / **Save**. Дождитесь статуса **Live**.

---

## 4. После деплоя

1. **Остановите бота на своём ПК** (`python main.py`), иначе будет ошибка `409 Conflict`.
2. Скопируйте URL сервиса, например: `https://bcwallfiles-bot.onrender.com`.
3. Проверьте в браузере: должна открыться страница с текстом `ok`.
4. В Telegram напишите боту `/start` с аккаунта админа — откроется админ-панель.

### Админ-панель
- Файл без подписи → ссылка только на файл.
- Файл с подписью → пользователю уйдут файл + текст.
- Обычный текст → ссылка только на текст.
- Кнопки: **Добавить канал**, **Удалить канал**, **Список каналов**.
- Обязательные `@amstercheats` и `@amstercloud` удалить нельзя.
- Подробно: `ADMIN_GUIDE.md`.

### Пользователь
- Переходит по ссылке → проверка подписки на оба канала → если ок, получает файл и/или текст.
- Если нет — кнопки на каналы и **Проверить подписку**.

---

## 5. Чтобы бот не засыпал (важно для Free)

На бесплатном Render сервис засыпает примерно через **15 минут** без запросов.

### Вариант A — GitHub Actions (ПК можно выключить)

1. Репозиторий уже на GitHub.
2. GitHub → **Settings → Secrets and variables → Actions → New repository secret**.
3. Имя: `KEEP_ALIVE_URL`  
   Значение: `https://ВАШ-СЕРВИС.onrender.com` (ваш реальный URL).
4. Workflow `.github/workflows/keepalive.yml` будет пинговать сервис каждые 5 минут.

### Вариант B — локально (ПК должен быть включён)

```powershell
python keepalive.py https://ВАШ-СЕРВИС.onrender.com
```

---

## 6. Если что-то не работает

| Симптом | Что сделать |
| --- | --- |
| Бот молчит | Смотрите **Logs** на Render; убедитесь, что деплой Live |
| `409 Conflict` | Остановите все локальные копии бота |
| «Подпишитесь…» даже после подписки | Бот не админ в канале — добавьте его админом в оба канала |
| Бот «оживает» только после открытия сайта | Не настроен keepalive — шаг 5 |
| Старый `@dfgames2` всё ещё в проверке | На Render в env должно быть `MANDATORY_CHANNELS=@amstercheats,@amstercloud`, затем Redeploy |

---

## Локальный запуск (для теста)

```powershell
cd C:\Users\bober\Downloads\tg_botamstercheats
pip install -r requirements.txt
python main.py
```

Перед тестом на Render локальный процесс обязательно остановите.
