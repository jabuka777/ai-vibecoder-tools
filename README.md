# AI Vibecoder Tools — бесплатные инструменты с ИИ-агентами, которые работают у вас на компьютере

[![Stars](https://img.shields.io/github/stars/jabuka777/ai-vibecoder-tools?style=flat&color=fc6206)](https://github.com/jabuka777/ai-vibecoder-tools/stargazers)
[![License: MIT](https://img.shields.io/badge/license-MIT-black)](LICENSE)
[![Visitors](https://visitor-badge.laobi.icu/badge?page_id=jabuka777.ai-vibecoder-tools&left_text=visitors)](https://github.com/jabuka777/ai-vibecoder-tools)
[![Telegram](https://img.shields.io/badge/Telegram-%40Eugene__Amb-26A5E4?logo=telegram&logoColor=white)](https://t.me/Eugene_Amb)

Открытая коллекция небольших рабочих инструментов, которые мы собираем вместе с
ИИ-агентами (Claude Code, Codex) и выкладываем бесплатно. Каждый инструмент решает
одну конкретную боль, работает локально на вашем компьютере, проверен в бою и описан
честно: что делает, сколько занял, где грабли.

Делает [Ябука](https://jabuka.ru) — сервис Apple в Москве. Мы чиним iPhone и MacBook,
а внутри давно работаем с ИИ-агентами. Инструменты, которые делаем для себя,
отдаём всем: так и клиентам полезно, и нам понятнее, чего людям не хватает.

## Инструменты

| Инструмент | Какую боль закрывает | Сколько заняло | Стек |
|---|---|---|---|
| [**Zoom Meeting Agent**](zoom-meeting-agent/) | Не успеваете на созвон или хотите протокол без ручной записи: ИИ-агент заходит в Zoom, расшифровывает речь локально, отвечает в чате и присылает выжимку | ~20 минут до первого входа | Python, Playwright, Zoom Meeting SDK, faster-whisper, Claude Code |

Следующий на очереди: **бэкап iPhone и iCloud в облака одной фразой ИИ** (MCP-сервер).
Боль: с апреля 2026 iCloud+ в России не оплатить, а фото продолжают копиться.

## Частые вопросы

**Как записать и расшифровать Zoom-встречу бесплатно, без облачных сервисов?**
Zoom Meeting Agent пишет звук встречи в браузере на вашем компьютере и расшифровывает
его faster-whisper локально. Облачные сервисы расшифровки не нужны, звук никуда не
уходит. Подходит для Windows, macOS и Linux.

**Можно ли отправить ИИ-ассистента на созвон вместо себя?**
Да. Во встречи своего аккаунта агент заходит через Meeting SDK (`bot.py`), в любую
встречу по ссылке — через обычный веб-клиент Zoom (`webjoin.py`) как участник без
микрофона и камеры. Он слушает, отвечает в чате, когда к нему обращаются, и после
встречи присылает выжимку: о чём говорили, договорённости, открытые вопросы.

**Почему Zoom-бот не заходит в чужую встречу и показывает «Sign in»?**
С 2 марта 2026 Zoom требует для ботов на Meeting SDK OBF-токен: в чужие встречи бот
попадает только вместе с человеком, который авторизовал приложение и уже сидит во
встрече. Веб-клиент Zoom под это ограничение не попадает, поэтому в инструменте два режима.

**Работает ли это локально и что нужно от компьютера?**
Браузер, запись звука и расшифровка идут на вашей машине. Нужен процессор с AVX2
(Intel с 2013 года, AMD с 2015) или Apple Silicon, 4–8 ГБ памяти, 2 ГБ на диске.
Наружу уходит только текст для ответа в чате и выжимки, и только если включён агент;
с `--no-agent` всё полностью офлайн.

**Это безопасно?**
Ключи только в `.env`, который не попадает в git. В репозитории нет ни токенов, ни
учётных данных: история коммитов проверена. Код открыт под MIT, читайте и меняйте.

**Можно ли повторить без навыков программирования?**
Да: установка в несколько команд под каждую ОС и пошаговая регистрация
(`REGISTRATION.md`). Если настраивает ваш ИИ-агент, он прочитает `AGENTS.md` и
попросит у вас только код из письма Zoom.

## Принципы

- **Локально по умолчанию.** Данные обрабатываются на вашем компьютере, в облако
  уходит минимум и только с вашего ведома.
- **Честно про ограничения.** Если платформа что-то запрещает, это написано в README
  вместе с обходом, если он есть.
- **Повторяемо.** Установка в несколько команд, проверка без людей, где возможно.
- **Без ключей в коде.** Всё через `.env`, есть `.env.example`.

## Автор и обратная связь

**[Евгений Амбросюк](https://t.me/Eugene_Amb)** — основатель [Ябуки](https://jabuka.ru),
собирает рабочие инструменты с ИИ-агентами и рассказывает, как: канал
[ИИбанутИИ](https://t.me/iibanutii).

- Что-то не работает на вашей системе — [Issues](https://github.com/jabuka777/ai-vibecoder-tools/issues)
  или Telegram [@Eugene_Amb](https://t.me/Eugene_Amb). Windows и macOS особенно ждём.
- Применили инструмент — напишите, что получилось. Отзывы решают, что собирать дальше.
- ⭐ Звезда репозиторию — лучший способ сказать «полезно».

Лицензия — [MIT](LICENSE). Ссылка на источник приветствуется.

---

*English:* free, open-source tools built by vibecoding with AI agents (Claude Code),
running locally on your machine. **Zoom Meeting Agent**: an AI assistant that joins
Zoom meetings, transcribes speech locally with faster-whisper (no cloud transcription),
answers in meeting chat via Claude and posts a meeting summary. Works for your own
meetings via Meeting SDK and for any meeting link via the Zoom web client, which
sidesteps the March 2026 OBF restriction for SDK bots. Windows, macOS, Linux.
Built by [Jabuka](https://jabuka.ru), an Apple repair service in Moscow, and
[Evgeny Ambrosyuk](https://t.me/Eugene_Amb). Next up: an MCP server for backing up
iPhone and iCloud photos to any cloud with one sentence to your AI.
