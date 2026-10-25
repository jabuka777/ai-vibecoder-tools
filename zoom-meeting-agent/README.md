# Zoom Meeting Agent — ИИ-агент, который сидит в Zoom-созвоне

**Что делает:** заходит во встречу Zoom как обычный участник, слушает и
расшифровывает речь **локально** (whisper, звук никуда не уходит), отвечает в чате
встречи через Claude, когда к нему обращаются, и после созвона присылает **выжимку**:
о чём говорили, главное, договорённости, открытые вопросы.

**Сколько заняло:** около 20 минут. Задачу ставили с телефона Claude Code во время
разборов на вайбкодинг-хакатоне: «подключи агента к Zoom». Агент сам завёл
Zoom-аккаунт и приложение в Marketplace, написал бота и проверил его на тестовой
встрече. Через час этот же агент тихо (без микрофона и камеры) сидел на чужом
разборе на 37 человек и расшифровывал его.

## Два режима

| Режим | Скрипт | Куда пускает | Что умеет |
|---|---|---|---|
| **SDK-бот** | `bot.py` | Встречи **вашего** Zoom-аккаунта | Слушает, расшифровывает, отвечает в чате, пишет в чат по команде (`say.py`), выжимка |
| **Слушатель через веб-клиент** | `webjoin.py` | **Любая** встреча по ссылке, в том числе «только для залогиненных» | Заходит без микрофона и камеры, расшифровывает, выжимка. В чат не пишет |

### Почему два режима: ограничение Zoom для ботов

С **2 марта 2026** Zoom пускает ботов на Meeting SDK во встречи **чужих**
аккаунтов только по OBF-токену. Нужен человек, который авторизовал ваше приложение
через OAuth и уже сидит во встрече. Анонимный вход SDK-ботов в чужие встречи не
работает (`AppCanNotAnonymousJoinMeeting`): вместо встречи бот видит экран «Sign in».
Источник — [FAQ Zoom по авторизации Meeting SDK](https://developers.zoom.us/docs/meeting-sdk/obf-faq/).

Обычный **веб-клиент** (`app.zoom.us/wc/join/…`) — это не SDK, и на него
ограничение не распространяется. `webjoin.py` заходит так же, как человек из браузера.

## Как устроено

```
Zoom ──звук──► headless Chromium (Playwright)
                 ├─ static/audio-hook.js: перехват всего, что идёт в динамики
                 │  (AudioContext.destination, WebRTC-треки, <audio>.srcObject)
                 │  → MediaRecorder, самостоятельные webm-куски по 20 с
                 └─ чат и события → events.jsonl
куски ──► stt.py (faster-whisper small, CPU int8, ffmpeg) ──► transcript.md
transcript + чат ──► agent.py (claude -p) ──► ответ в чат / summary.md / Telegram
```

- `bot.py` — локальный сервер: подпись SDK-JWT (HS256), приём звука, очередь чата.
  Страница с Meeting SDK Web (client view) открывается в headless Chromium.
- `webjoin.py` — тот же перехват звука через `add_init_script`. Куски передаются в
  Python через `expose_binding`: со страницы `https://app.zoom.us` нельзя слать
  запросы на `127.0.0.1`.
- `stt.py` — расшифровка. Звук декодируется системным ffmpeg: `av` из колеса
  бывает несовместим (`metadata_errors`).
- `agent.py` — ответы и выжимка через Claude Code CLI (`claude -p`). Можно заменить
  на любой LLM: функция `ask()`.

## Установка

```bash
git clone https://github.com/jabuka777/ai-vibecoder-tools && cd ai-vibecoder-tools/zoom-meeting-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
pip install playwright && playwright install chromium     # системный python для bot.py/webjoin.py
./setup-sdk.sh                                            # Zoom Meeting SDK Web → static/ (нужен npm)
cp .env.example .env                                      # и заполнить
```

Нужны `ffmpeg`, Node.js (только для `setup-sdk.sh`) и
[Claude Code](https://claude.com/claude-code) (`claude` в PATH) для ответов и выжимки.

### Ключи Zoom (для `bot.py`)

1. Завести отдельный Zoom-аккаунт для бота, **с паролем**, а не через Google/SSO:
   автоматике нужен пароль.
2. [marketplace.zoom.us](https://marketplace.zoom.us) → Develop → Build app →
   **General app** → Embed → включить **Meeting SDK**.
3. Client ID и Client Secret — в `.env`. Для встреч своего аккаунта ревью Zoom не нужно.

### Сессия для `webjoin.py` (встречи «только для залогиненных»)

Залогиньтесь аккаунтом бота в Playwright и сохраните `storage_state.json` рядом со
скриптами: `context.storage_state(path="storage_state.json")`. Перед входом во
встречу поменяйте в профиле Zoom имя и фото: участники видят их, а Zoom сам
подтягивает логотип компании по домену почты.

## Запуск

```bash
# встреча своего аккаунта: слушает, отвечает на «бот, …» в чате, выжимка в конце
python3 bot.py --mn 81234567890 --pwd <пароль> --minutes 60
python3 say.py "текст в чат встречи"

# любая встреча по ссылке https://zoom.us/j/<номер>?pwd=<pwd>: тихо слушает и делает выжимку
python3 webjoin.py --mn <номер> --pwd <pwd> --name "AI-агент" --minutes 120
```

Результаты лежат в `sessions/<дата>-<номер>-…/`:
- `transcript.md` — расшифровка;
- `events.jsonl` — чат и участники;
- `summary.md` — выжимка;
- `last.png` — скриншот экрана бота.

### Проверка без людей

Второй экземпляр бота «говорит» wav-файл в микрофон:

```bash
python3 bot.py --mn … --pwd … --no-stt --no-agent --port 8792 --name "Голос" --mic-file speech.wav --minutes 2 &
python3 bot.py --mn … --pwd … --minutes 1.5
```

## Этика и ограничения

- Предупреждайте участников, что встречу расшифровывает агент. В чужие встречи
  заходите с понятным именем и без микрофона.
- В чат чужой встречи пишите по минимуму: одно представление, без спама.
- Бесплатный Zoom режет встречу на 40 минутах. Встроенные субтитры Zoom
  (`onReceiveTranscriptionMsg`) на бесплатном тарифе не приходят, поэтому здесь свой whisper.
- Ловушка: пароль, вставленный через `browser_run_code` в Playwright MCP, попадает
  в лог агента. Подставляйте секреты из файла или с локального сервера.

---

Автор — [Евгений Амбросюк](https://t.me/Eugene_Amb), вайбкодер.
Как собирал и что сломалось по дороге — спрашивайте в Telegram или в
[Issues](https://github.com/jabuka777/ai-vibecoder-tools/issues). Отзывы и истории,
как вы это применили, особенно ценны.
