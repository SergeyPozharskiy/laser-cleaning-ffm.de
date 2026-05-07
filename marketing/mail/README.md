# Mail renderer

Рендерит `.eml`-письма из Jinja2-шаблонов, подставляя данные отправителя из `config.yaml` и получателя из `recipients.yaml`.

## Структура

```
marketing/mail/
├── render_mail.py          # CLI-рендерер
├── requirements.txt        # PyYAML, Jinja2
├── config.yaml             # отправитель + путь к шаблону
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

### Аргументы

| Аргумент | Обязательный | Описание |
|---|---|---|
| `--recipient` | один из | email получателя (ключ поиска в файле получателей). Взаимоисключим с `--all-pending` |
| `--all-pending` | один из | пакетный режим: рендерит письмо для каждого получателя со `status: pending` |
| `--out` | нет | имя/путь файла. Только для одиночного `--recipient`. По умолчанию — `<template-без-tmpl_>-<company-с-дефисами>.eml`. Относительный путь склеивается с `template.output_dir` из `config.yaml`; абсолютный — перебивает |
| `--template` | нет | путь к `.eml`-шаблону. По умолчанию — `template.path` из `config.yaml` |
| `--config` | нет | путь к `config.yaml` (по умолчанию рядом со скриптом) |
| `--recipients` | нет | путь к файлу получателей. По умолчанию — `recipients.path` из `config.yaml`, иначе `recipients.yaml` рядом с конфигом |

## Типичные ошибки

| Ошибка | Причина | Решение |
|---|---|---|
| `Recipient '...' not found in recipients file` | email не найден в `recipients.yaml` | проверь написание, добавь получателя |
| `'recipient' is undefined` (или похожее) | в шаблоне используется поле, которого нет в YAML | добавь поле в `recipients.yaml` или `config.yaml` |
| `ModuleNotFoundError: No module named 'yaml'` | не установлены зависимости | `python -m pip install -r requirements.txt` |
| `TemplateSyntaxError` | сломан Jinja2-синтаксис в шаблоне | проверь парность `{% ... %}` и `{{ ... }}` |

## Создание нового шаблона

1. Скопируй существующий `.eml` в `intro/templ/` или `sales/templ/` с префиксом `tmpl_`.
2. Пропиши его путь в `config.yaml` → `template.path` (или передавай через `--template`).
3. Используй переменные: `{{ sender.full_name }}`, `{{ sender.email }}`, `{{ sender.phone }}`, `{{ company.name }}`, `{{ company.website }}`, `{{ company.region }}`, `{{ recipient.first_name }}`, `{{ recipient.last_name }}`, `{{ recipient.company_name }}`, `{{ recipient.salutation }}`, `{{ recipient.notes }}`.
3. Для заголовков письма: `{{ headers.from }}`, `{{ headers.to }}`, `{{ headers.date }}` (собираются скриптом, RFC-корректно).
