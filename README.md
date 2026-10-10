# Проходная

> **Посмотреть:** https://hack-detect.duckdns.org — выберите роль, пароль не нужен.
> **Как проверить кейс:** [docs/demo-scenario.md](docs/demo-scenario.md), то же в меню стенда («Как проверить кейс»).
> **Что проверено и что не успели:** [docs/limitations.md](docs/limitations.md).

Командный репозиторий кейса «Проходная». Исходные условия и проверочные материалы находятся в `case_prerekvizity/`. Инструкции проекта для агентов — в `AGENTS.md` и `skills/`.

## Структура

| Путь | Содержание |
| --- | --- |
| [`frontend/`](frontend/) | Ролевые веб-интерфейсы, API-адаптер, деморежим, тесты и документация фронта |
| [`.github/workflows/pages.yml`](.github/workflows/pages.yml) | Публикация **только** статического демофронта на GitHub Pages |
| `case_prerekvizity/` | Условия и материалы кейса, включая проверочные изображения |

Бэкенд, Keycloak, модуль лиц и помощник работают на стенде; публичный демофронт без сервера (режим demo) серверную проверку личности не выполняет и для приёмки не используется.

Демофронт из этого репозитория: https://bombomteam.github.io/prohodnaya/.

## Запуск фронта

```powershell
cd frontend
python -m http.server 8080
```

Откройте `http://localhost:8080`. Полные инструкции — в [`frontend/docs/launch.md`](frontend/docs/launch.md).

Для контейнера: `docker build -t prohodnaya-frontend:local ./frontend`. Запуск и подключение API описаны в [`frontend/docs/deployment.md`](frontend/docs/deployment.md).
