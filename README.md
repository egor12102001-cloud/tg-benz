# tg-benz

Telegram-бот для получения актуальной информации о наличии топлива и ценах на АЗС с сайта [gdebenz.ru](https://gdebenz.ru).

## Возможности

- Просмотр списка городов с карты gdebenz.ru
- Поиск города по названию
- Информация по каждой АЗС: название, адрес, виды топлива, наличие, цена
- Пагинация по городам (кнопки ◀/▶)
- Ввод города просто текстом, без команд

## Установка

```bash
git clone <repo>
cd tg-benz
pip install -r requirements.txt
playwright install chromium   # или убедитесь что путь к Chromium верный в scraper.py
```

## Запуск

```bash
export BOT_TOKEN=your_token_here
python3 bot.py
```

Или через `.env`:
```bash
cp .env.example .env
# вставьте токен в .env
source .env && python3 bot.py
```

## Конфигурация

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен Telegram-бота (обязательно) |

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветствие и список команд |
| `/city` | Выбрать город из полного списка |
| `/search <город>` | Найти город по названию |
| `/help` | Справка |

Также можно просто написать название города в чат.

## Структура

```
bot.py        — Telegram-бот (aiogram 3.x)
scraper.py    — парсер gdebenz.ru (Playwright + BeautifulSoup + aiohttp)
requirements.txt
.env.example
```

## Как работает парсер

1. Открывает страницу города в headless Chromium (Playwright)
2. Перехватывает все JSON-ответы от API сайта
3. Извлекает данные об АЗС: название, адрес, виды и статус топлива, цены
4. Если API не найден — парсит HTML-структуру страницы
