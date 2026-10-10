# Проходная

Командный репозиторий кейса «Проходная». Исходные условия и проверочные материалы находятся в `case_prerekvizity/`. Инструкции проекта для агентов — в `AGENTS.md` и `skills/`.

## Структура

| Путь | Содержание |
| --- | --- |
| [`frontend/`](frontend/) | Ролевые веб-интерфейсы, API-адаптер, деморежим, тесты и документация фронта |
| [`.github/workflows/pages.yml`](.github/workflows/pages.yml) | Публикация **только** статического демофронта на GitHub Pages |
| `case_prerekvizity/` | Условия и материалы кейса, включая проверочные изображения |

Бэкенд пока не добавлен. Публичный демофронт не выполняет серверную проверку личности и не соответствует допуску кейса как готовая система. Текущие ограничения описаны в [`frontend/docs/limitations.md`](frontend/docs/limitations.md).

Демофронт из этого репозитория: https://bombomteam.github.io/prohodnaya/.

## Запуск фронта

```powershell
cd frontend
python -m http.server 8080
```

Откройте `http://localhost:8080`. Полные инструкции — в [`frontend/docs/launch.md`](frontend/docs/launch.md).
