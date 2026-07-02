# Установка на чистый сервер Debian

## 1. Обновление системы

```bash
sudo apt update && sudo apt upgrade -y
```

## 2. Установка Python и зависимостей системы

```bash
sudo apt install -y python3 python3-pip python3-venv git \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0
```

> Эти пакеты нужны для работы Chromium внутри Playwright.

## 3. Клонирование репозитория

```bash
git clone https://github.com/ruby-rs/tg-benz.git
cd tg-benz
```

## 4. Создание виртуального окружения

```bash
python3 -m venv venv
source venv/bin/activate
```

## 5. Установка Python-зависимостей

```bash
pip install -r requirements.txt
```

## 6. Установка Chromium для Playwright

```bash
playwright install chromium
playwright install-deps chromium
```

## 7. Настройка токена бота

Создайте токен бота через [@BotFather](https://t.me/BotFather) в Telegram, затем:

```bash
cp .env.example .env
nano .env
```

Вставьте ваш токен:

```
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
```

## 8. Тестовый запуск

```bash
source venv/bin/activate
export $(cat .env | xargs)
python3 bot.py
```

Если в консоли появилось `Bot started` — всё работает. Остановить: `Ctrl+C`.

---

## 9. Автозапуск через systemd (работа в фоне)

Создайте unit-файл (замените `YOUR_USER` и путь на свои):

```bash
sudo nano /etc/systemd/system/tg-benz.service
```

Содержимое файла:

```ini
[Unit]
Description=tg-benz Telegram Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/tg-benz
EnvironmentFile=/home/YOUR_USER/tg-benz/.env
ExecStart=/home/YOUR_USER/tg-benz/venv/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Активируйте и запустите:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-benz
sudo systemctl start tg-benz
```

Проверить статус:

```bash
sudo systemctl status tg-benz
```

Смотреть логи в реальном времени:

```bash
journalctl -u tg-benz -f
```

---

## Обновление бота

```bash
cd tg-benz
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart tg-benz
```
