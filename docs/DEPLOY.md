# Развёртывание (Linux + systemd)

Пример для установки в `/opt/family-bot` под отдельным пользователем `content`.

> Claude CLI работает в режиме `bypassPermissions` только не под root — поэтому бот
> запускается под непривилегированным пользователем.

## 1. Код и виртуальное окружение

```bash
sudo mkdir -p /opt/family-bot
sudo chown content:content /opt/family-bot
sudo -u content bash -lc '
  cd /opt/family-bot
  git clone https://github.com/<you>/family-bot.git .
  python3 -m venv .venv --system-site-packages
  ./.venv/bin/pip install -r requirements.txt
'
```

`faster-whisper` при первом голосовом скачает модель в `models/` (переопределяется `WHISPER_DIR`).

## 2. Окружение

Создайте `/opt/family-bot/env` по образцу [`.env.example`](../.env.example):

```
TG_TOKEN=...
CLAUDE_MODEL=haiku
TICKET_MODEL=sonnet
# iCloud (необязательно)
ICLOUD_EMAIL=...
ICLOUD_APP_PASSWORD=...
ICLOUD_CALENDAR=Семья
```

Файл должен быть доступен только пользователю бота:

```bash
sudo chown content:content /opt/family-bot/env
sudo chmod 600 /opt/family-bot/env
```

Claude CLI должен быть авторизован от имени пользователя `content` (единожды: `claude` и вход,
либо переменная `CLAUDE_CODE_OAUTH_TOKEN` в `env`).

## 3. systemd-сервис

```bash
sudo cp deploy/family-bot.service /etc/systemd/system/family-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now family-bot
```

Юнит запускает `/opt/family-bot/.venv/bin/python -m familybot`.

## 4. Обслуживание

```bash
# логи (маркеры [sync], [msg], [intent], [batch])
journalctl -u family-bot -f

# перезапуск после обновления кода
git -C /opt/family-bot pull
sudo systemctl restart family-bot

# бэкап базы
cp /opt/family-bot/family.db ./family-$(date +%F).db
```

## Память

На маленьких VPS (≈2 ГБ) whisper и Claude в пике могут упереться в RAM — добавьте своп:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Обновление токена getUpdates

Один токен = один поллер `getUpdates`. Не запускайте вторую копию бота с тем же `TG_TOKEN` —
Telegram будет отдавать апдейты по очереди и обработка станет непредсказуемой.
