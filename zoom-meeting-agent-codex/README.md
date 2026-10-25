# Zoom Meeting Agent · Codex

Отдельный запуск агента Zoom для пользователя Codex. Claude не нужен. Запись и распознавание работают локально; Codex по явному выбору получает текст и готовит черновик протокола с цитатами.

Скачайте весь репозиторий или архив выпуска. Папка `zoom-meeting-agent/` содержит общее ядро, а эта папка содержит отдельный запуск, настройки и записи. Скопировать только `run.py` недостаточно.

## Установка

Нужны Python 3.10+, ffmpeg и Node.js. Windows/macOS вручную не проверены; локальные проверки выполнены на Linux.

```bash
git clone https://github.com/jabuka777/ai-vibecoder-tools
cd ai-vibecoder-tools/zoom-meeting-agent-codex
python -m venv .venv
```

Активировать: `source .venv/bin/activate` на Linux/macOS или `.venv\Scripts\Activate.ps1` в Windows PowerShell.

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
npm install -g @openai/codex@0.164.0
codex login
python run.py doctor --backend codex
```

Используется уже настроенный вход Codex. Копировать токены в проект не нужно. Версия CLI зафиксирована, потому что её набор инструментов проверен отдельным тестом. При другом CLI запуск LLM остановится до передачи речи.

CLI может добавить к тексту встречи глобальные инструкции и каталог навыков вашей установки. Инструменты исполнения отключены, но отсутствие служебного контекста в запросе не гарантируется. Подробности и результаты проверки — в [VERIFICATION.md](../zoom-meeting-agent/VERIFICATION.md).

## Попробовать

Для встречи с обязательным входом аккаунтом: `python run.py login`. После входа закройте настройку нажатием Enter в терминале.

Только локальная расшифровка:

```bash
python run.py web --mn 12345678901 --minutes 60
```

Расшифровка и черновик через Codex:

```bash
python run.py web --mn 12345678901 --minutes 60 --llm codex --model gpt-6-luna
```

По умолчанию используется `gpt-6-luna`, модель для извлечения данных и выжимок из [справочника OpenAI](https://learn.chatgpt.com/docs/models). Модель можно заменить через `--model` на доступную вашему аккаунту. Флаг `--llm codex` разрешает отправку текста в OpenAI. Zoom требует интернет и при выключенной LLM.

Результаты появятся в `sessions/` этой папки. Полный протокол: `summary.md`; факты и цитаты: `summary.json`; состояние: `status.json` и `minutes-status.json`. Ошибка записи не маскируется успешным протоколом.

Повторить протокол завершённой записи:

```bash
python run.py summarize sessions/КАТАЛОГ --model gpt-6-luna
```

В SDK-режиме доступны ответы в чат. Сначала заполните `.env` по примеру, выполните `python run.py setup-sdk` и [настройте Zoom-приложение](../zoom-meeting-agent/REGISTRATION.md). Затем `python run.py sdk --mn 12345678901 --llm codex --model gpt-6-luna`.

## Проверить код и модель

Без облака:

```bash
python -m unittest discover -s ../zoom-meeting-agent/tests -v
python run.py evaluate --offline
```

Живая оценка Codex на 16 вымышленных сценариях, с расходом лимита:

```bash
python run.py evaluate --model gpt-6-luna
```

Команда сохраняет результаты и метрики. Она не присоединяется к Zoom и не отправляет сообщения участникам. Проверяет проект, исполнителя, срок, предложения и отмены; это собственные сценарии, не исходный набор друга из 50 тестов.

Протокол всегда остаётся черновиком. Наличие цитаты не доказывает правильность извлечённого смысла; поздние отмены необходимо сопоставлять с ранними поручениями. [Устройство, защита, данные и ограничения общего ядра](../zoom-meeting-agent/README.md).
