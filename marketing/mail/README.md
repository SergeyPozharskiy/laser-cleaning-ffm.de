# Mail renderer

Рендерит `.eml`-письма из Jinja2-шаблонов, подставляя данные отправителя из `config.yaml` и получателя из `recipients.yaml`.

## Структура

```
marketing/mail/
├── render_mail.py          # CLI-рендерер (+ опционально SMTP-отправка)
├── requirements.txt        # PyYAML, Jinja2, ruamel.yaml
├── config.yaml             # отправитель + шаблон + SMTP + лимиты
├── .env.example            # шаблон файла с паролем SMTP (скопируй в .env)
├── .send_state.json        # автогенерится: счётчик отправок за день
├── recipients.yaml         # список получателей
├── intro/
│   └── templ/
│       └── tmpl_hausverwaltung_vorstellung.eml   # шаблон
└── sales/
```

## Первый запуск

### 1. Установить Python (если ещё нет)

Проверь:

```powershell
python --version
```

Нужен Python 3.9+. Если нет — поставь с [python.org](https://www.python.org/downloads/) (при установке отметь **Add Python to PATH**).

### 2. Установить зависимости

```powershell
cd "C:\Users\serge\My Drive\!Important Documents\Job\LaserCLeaning\Webpage\marketing\mail"
python -m pip install -r requirements.txt
```

## Подготовка данных

### `config.yaml` — отправитель и шаблон

Заполняется один раз. Текущее содержимое:

```yaml
sender:
  full_name: "Sebastian Morgen"
  email: "info@laser-cleaning-ffm.de"
  phone: "+49 69 943 22 072"

company:
  name: "Laser Cleaning Frankfurt"
  website: "laser-cleaning-ffm.de"
  region: "Frankfurt Rhein-Main und Hessen"

template:
  # Путь к Jinja2-шаблону (относительно config.yaml или абсолютный).
  # Перебивается флагом --template из CLI.
  path: "intro/templ/tmpl_hausverwaltung_vorstellung.eml"
  # Папка, куда сохраняются готовые .eml. Относительный --out
  # склеивается с этой папкой; абсолютный --out — перебивает.
  output_dir: "intro"

recipients:
  # Путь к файлу получателей (относительно config.yaml или абсолютный).
  # Перебивается флагом --recipients из CLI.
  # Меняй на "recipients_hv.yaml" и т.п. для другой кампании.
  path: "recipients.yaml"

# Используется только в режиме --send. Пароль читается из переменной
# окружения SMTP_PASSWORD (или из .env рядом с config.yaml). В YAML
# его класть НЕЛЬЗЯ — файл в git.
smtp:
  host: "smtp.ionos.de"
  port: 587
  security: "starttls"           # starttls | ssl | none
  username: "info@laser-cleaning-ffm.de"
  from_address: "info@laser-cleaning-ffm.de"

# Анти-спам параметры для --send.
send:
  interval_seconds: 60           # базовая пауза между письмами
  jitter_pct: 20                 # ± случайный разброс ±20%
  daily_limit: 30                # стоп после 30 писем в сутки (UTC)
  state_file: ".send_state.json" # счётчик дня, сбрасывается на новой дате
```

### `recipients.yaml` — список получателей

Добавь нового получателя как новый элемент списка:

```yaml
recipients:
  - email: info@aventus-immo.de
    first_name: ""
    last_name: ""
    salutation: ""           # "Herr" / "Frau" / "" — для персонализированного приветствия
    company_name: Aventus Immobilien
    notes: ""                # любые свободные заметки
    status: pending          # pending | sent | replied | bounced
    sent_at: null

  - email: kontakt@example-hv.de
    first_name: Anna
    last_name: Müller
    salutation: Frau
    company_name: Beispiel Hausverwaltung GmbH
    notes: "Empfehlung von Herrn X, gesehen auf Messe 2026"
    status: pending
    sent_at: null
```

**Логика приветствия:**
- Заполнены `salutation` + `last_name` → `Sehr geehrte Frau Müller,`
- Иначе → `Sehr geehrte Damen und Herren,`

## Запуск

```powershell
python render_mail.py --recipient info@aventus-immo.de
```

После запуска:

```
Wrote ...\marketing\mail\intro\hausverwaltung_vorstellung-Aventus-Immobilien.eml
```

**Имя файла генерируется автоматически** по схеме `<template-stem-без-tmpl_>-<company-name-с-дефисами>.eml`:

- `tmpl_hausverwaltung_vorstellung.eml` + `Aventus Immobilien` → `hausverwaltung_vorstellung-Aventus-Immobilien.eml`
- если `company_name` пустой — fallback на `email`-получателя
- папка берётся из `template.output_dir` в `config.yaml`

Хочешь своё имя — передай `--out my_name.eml` (относительный путь склеится с `output_dir`, абсолютный — перебьёт всё).

Открой `.eml` двойным кликом — он импортируется в дефолтный почтовый клиент (Outlook / Thunderbird / Apple Mail) как готовое письмо. Проверь содержимое и нажми «Отправить».

### Пакетный прогон по всем `pending`

```powershell
python render_mail.py --all-pending
```

Сгенерирует по одному `.eml` для каждого получателя со `status: pending` в активном файле получателей (берётся из `config.recipients.path`). Имена файлов автогенерируются (`--out` в этом режиме недопустим).

### Автоматическая отправка через SMTP (`--send`)

Без `--send` скрипт только рендерит `.eml` — отправлять надо руками из почтового клиента. С `--send` он отрендерит файл (как раньше — для аудита) **и** отправит письмо через SMTP, обновит `status: sent` и `sent_at: <ISO timestamp>` в YAML получателей.

**Первая настройка (один раз):**

1. Скопируй `.env.example` в `.env` (рядом с `config.yaml`):
   ```powershell
   cp .env.example .env
   ```
2. Вставь в `.env` пароль от ящика `info@laser-cleaning-ffm.de`:
   ```
   SMTP_PASSWORD=твой_пароль
   ```
   `.env` уже в `.gitignore` — в коммит не уйдёт.
3. Проверь SMTP-настройки в `config.yaml` (`smtp:` секция). По умолчанию — IONOS (`smtp.ionos.de:587`, STARTTLS).

**Запуск:**

```powershell
# Сухой прогон — рендерит .eml, печатает "would send", но НЕ коннектится
# к SMTP и НЕ трогает recipients.yaml. Хорошо для предпросмотра.
python render_mail.py --all-pending --send --dry-run

# Боевой режим
python render_mail.py --all-pending --send
```

Что произойдёт в боевом режиме:

1. Скрипт коннектится к SMTP-серверу.
2. Для каждого `pending`-получателя:
   - рендерит `.eml` в `output_dir`;
   - отправляет через SMTP;
   - **сразу** записывает `status: sent` + `sent_at` в `recipients.yaml` (чтобы при Ctrl+C/падении не было путаницы);
   - инкрементит дневной счётчик в `.send_state.json`;
   - спит `interval_seconds ± jitter_pct%` перед следующим письмом (по умолчанию 60 сек ±20%).
3. По достижении `daily_limit` (по умолчанию 30) — стопается. Запусти на следующий день, продолжит с того места.

**Сбросить дневной счётчик** (например, если ты случайно превысил, а тебе надо ещё): удали `marketing/mail/.send_state.json`.

**Переотправить уже отправленное письмо:** поменяй в `recipients.yaml` `status: sent` → `status: pending` и запусти `--all-pending --send`. Либо отправь конкретному `--recipient ...@... --send` — статус игнорируется в этом режиме (но всё равно обновится).

**SMTP не подключился / 535 auth failed:** проверь `SMTP_PASSWORD`, имя ящика в `smtp.username`, и что у IONOS включена SMTP-отправка для этого ящика.

### Аргументы

| Аргумент | Обязательный | Описание |
|---|---|---|
| `--recipient` | один из | email получателя (ключ поиска в файле получателей). Взаимоисключим с `--all-pending` |
| `--all-pending` | один из | пакетный режим: рендерит письмо для каждого получателя со `status: pending` |
| `--out` | нет | имя/путь файла. Только для одиночного `--recipient`. По умолчанию — `<template-без-tmpl_>-<company-с-дефисами>.eml`. Относительный путь склеивается с `template.output_dir` из `config.yaml`; абсолютный — перебивает |
| `--template` | нет | путь к `.eml`-шаблону. По умолчанию — `template.path` из `config.yaml` |
| `--config` | нет | путь к `config.yaml` (по умолчанию рядом со скриптом) |
| `--recipients` | нет | путь к файлу получателей. По умолчанию — `recipients.path` из `config.yaml`, иначе `recipients.yaml` рядом с конфигом |
| `--send` | нет | после рендера ещё и отправить через SMTP, обновить `status`/`sent_at` в YAML, учитывать `daily_limit` и интервал. Требует `SMTP_PASSWORD` в env/.env |
| `--dry-run` | нет | только с `--send`: рендерит и печатает «would send to …», но не коннектится к SMTP и не трогает YAML |

## Типичные ошибки

| Ошибка | Причина | Решение |
|---|---|---|
| `Recipient '...' not found in recipients file` | email не найден в `recipients.yaml` | проверь написание, добавь получателя |
| `'recipient' is undefined` (или похожее) | в шаблоне используется поле, которого нет в YAML | добавь поле в `recipients.yaml` или `config.yaml` |
| `ModuleNotFoundError: No module named 'yaml'` / `'ruamel'` | не установлены зависимости | `python -m pip install -r requirements.txt` |
| `TemplateSyntaxError` | сломан Jinja2-синтаксис в шаблоне | проверь парность `{% ... %}` и `{{ ... }}` |
| `--send requires SMTP_PASSWORD in env` | пароль не выставлен | скопируй `.env.example` в `.env` и впиши пароль |
| `Daily limit reached (30/30)` | за сутки уже отправлено 30 писем | подожди следующих суток, или подними `send.daily_limit`, или удали `.send_state.json` (на свой страх и риск) |
| `smtplib.SMTPAuthenticationError: 535` | неверный логин/пароль или SMTP для ящика выключен у провайдера | проверь `smtp.username` и `SMTP_PASSWORD`, в админке IONOS включи SMTP |

## Создание нового шаблона

1. Скопируй существующий `.eml` в `intro/templ/` или `sales/templ/` с префиксом `tmpl_`.
2. Пропиши его путь в `config.yaml` → `template.path` (или передавай через `--template`).
3. Используй переменные: `{{ sender.full_name }}`, `{{ sender.email }}`, `{{ sender.phone }}`, `{{ company.name }}`, `{{ company.website }}`, `{{ company.region }}`, `{{ recipient.first_name }}`, `{{ recipient.last_name }}`, `{{ recipient.company_name }}`, `{{ recipient.salutation }}`, `{{ recipient.notes }}`.
3. Для заголовков письма: `{{ headers.from }}`, `{{ headers.to }}`, `{{ headers.date }}` (собираются скриптом, RFC-корректно).
