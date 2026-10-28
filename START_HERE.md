# Первый запуск Zoom-ассистента

Если у вас уже есть Claude Code, Codex или Cursor с доступом к компьютеру,
попросите его:

> Скачай https://github.com/jabuka777/jabuka-tools. Прочитай
> skills/zoom-meeting-agent/SKILL.md и настрой Zoom-ассистента на моём компьютере.
> Начни с проверки окружения. Я хочу расшифровку и протокол; объясняй только
> действия, которые требуют моего участия. Пароли и коды я введу локально.

Ассистент проверит Python, ffmpeg, браузер, свободное место и модель распознавания,
подготовит отдельное окружение и проведёт короткую тестовую запись. Платный
сервер для этого не нужен. Если AI-клиент работает на удалённом сервере, у него
нет автоматического доступа к вашему компьютеру: начните в локальном клиенте.

## Если устанавливаете сами

Скачайте репозиторий целиком через GitHub → Code → Download ZIP и распакуйте.
Откройте терминал в распакованной папке. Нужен Python 3.10+, предпочтительно 3.12.
На Windows вместо `python` может потребоваться `py`, на macOS/Linux `python3`.

Установите скилл для своего приложения, выполнив одну из команд:

```bash
python tools/install_zoom_skill.py --client claude
python tools/install_zoom_skill.py --client codex
python tools/install_zoom_skill.py --client cursor
```

В Claude Code вызовите `/zoom-meeting-agent`, в Codex `$zoom-meeting-agent`.
Если скилл не появился, перезапустите клиент. Копия скилла ссылается на эту
папку продукта, поэтому не удаляйте и не перемещайте её после установки.

Можно продолжить без скилла, через универсальную обёртку:

```bash
python skills/zoom-meeting-agent/scripts/zoom.py setup
python skills/zoom-meeting-agent/scripts/zoom.py setup --install --download-model
python skills/zoom-meeting-agent/scripts/zoom.py doctor --check-browser --check-model --text
python skills/zoom-meeting-agent/scripts/zoom.py configure meeting demo --minutes 60
```

Последняя команда попросит ссылку и пароль скрыто в терминале. Они сохраняются
локально и не нужны в переписке с ассистентом. Если Zoom требует вход в аккаунт:

```bash
python skills/zoom-meeting-agent/scripts/zoom.py login
```

Войдите в открывшемся браузере и нажмите Enter в терминале. Затем предупредите
участников о записи и запустите:

```bash
python skills/zoom-meeting-agent/scripts/zoom.py configure run demo
```

Это запись без облачной языковой модели. Ctrl+C запрашивает остановку; дождитесь
сохранения текста. Путь результата появится в терминале. Для протокола можно
явно выбрать `--llm claude`, `--llm codex` или `--llm deepseek`: текст получит
соответствующий провайдер. Подписка/API оплачиваются отдельно.

## Управление инструментами AI-клиента

Дополнительно доступен MCP-сервер. Он запускает и останавливает запись, показывает
состояние и читает результат. [Инструкция подключения](skills/zoom-meeting-agent/references/clients.md).
MCP проверен в CI на Linux/macOS и требует POSIX-файловую границу; для Windows используйте
скилл с CLI. Сам запуск MCP к Zoom не подключается. Чтение расшифровки через MCP
передаёт её вашему AI-клиенту, даже при выключенной встроенной LLM записи.

## Что означает «готово»

Установка завершена только после doctor с проверкой браузера и модели.
Запись завершена только при `recording_complete: true` в `status.json`.
Протокол готов как черновик при `status: complete` в `minutes-status.json`.
Проверьте имена, задачи, сроки и отмены по цитатам и исходной записи.

Установка из ZIP проверена в CI на Ubuntu, macOS ARM64 и Windows Server 2025.
На историческом снимке завершена четырёхчасовая локальная запись; в ней
сохранены ошибки распознавания. Настоящая Zoom-встреча, Windows 11 и независимый
первый запуск новичком ещё требуют проверки.
[Доказательства и ограничения](zoom-meeting-agent/VERIFICATION.md),
[помощь при ошибках](skills/zoom-meeting-agent/references/troubleshooting.md).
