# Подключение клиентов

Скилл содержит порядок работы; MCP даёт клиенту инструменты записи. Один и тот
же код MCP подходит разным клиентам. DeepSeek API является backend для текста,
а не самостоятельным MCP-клиентом. Для работы через DeepSeek-модель внутри
другого клиента MCP подключается к этому клиенту.

## Установить скилл

Из корня полного скачанного репозитория:

```text
python tools/install_zoom_skill.py --client claude
python tools/install_zoom_skill.py --client codex
python tools/install_zoom_skill.py --client cursor
```

Выполняй только команду для выбранного приложения. Установщик копирует скилл
в пользовательскую папку клиента и записывает путь к этому репозиторию.
Другие скиллы и конфигурация клиента сохраняются. Повтор той же версии ничего
не меняет; `--replace` сохраняет прежний скилл вне папки обнаружения навыков.
Не перемещай репозиторий после установки без обновления скилла.

Claude Code: `/zoom-meeting-agent`. Codex: `$zoom-meeting-agent`. Можно также
попросить обычными словами настроить Zoom-ассистента. Если скилл не появился,
перезапусти клиент. Это локальные клиенты с доступом к компьютеру; обычная
веб-переписка не получает автоматически доступ к локальным файлам и браузеру.

## MCP

Сначала установи дополнительные зависимости: `zoom.py setup --install-mcp`.
Сервер не требует открывать порт в интернет. Транспорт stdio, один приватный
home на пользователя. Не подключай папку с чужими записями.

Установщик умеет вывести точную конфигурацию с путями текущего компьютера:

```text
python tools/install_zoom_skill.py --client codex --print-mcp
python tools/install_zoom_skill.py --client claude --print-mcp
python tools/install_zoom_skill.py --client cursor --print-mcp
```

Эти команды только печатают фрагмент конфигурации. Сохрани существующие
настройки клиента и добавь одну запись zoom-meeting-agent. Не заменяй весь
config.toml или mcp.json. У Codex также есть `codex mcp add`, у Claude Code
`claude mcp add --transport stdio`: передавай абсолютный Python из home/.venv,
абсолютный mcp_server.py и `--home` из ответа `paths`. Проверяй команду в
текущем клиенте, не вставляй в неё пароль или токен.

После подключения выполни doctor и list_meeting_profiles. Они не должны
входить в Zoom. Запись начинается только с отдельного start_web. Доступны
семь инструментов: doctor, list_meeting_profiles, list_sessions, session_status,
read_result, start_web, stop_session. read_result отправляет прочитанный текст
в контекст AI-клиента: объясни это до чтения.

На Linux проверены настоящий MCP handshake и вызовы через SDK 2.0.0.
Файловые операции требуют POSIX; на Windows они отклоняются. macOS отдельно
не прогонялся. Установка MCP не подменяет проверку реальной Zoom-встречи.

Источники форматов: [Codex skills](https://learn.chatgpt.com/docs/build-skills),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[Claude Code skills](https://code.claude.com/docs/en/skills),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Cursor skills](https://cursor.com/docs/skills),
[Cursor MCP](https://cursor.com/docs/mcp),
[DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/).
