# Фронтенд «Проходной»

Статическое приложение на ES-модулях без npm-зависимостей и сборщика. Правила работы: [AGENTS.md](AGENTS.md).

Из каталога `frontend`:

```powershell
python -m http.server 8080
npm test
npm run build:pages
```

Открыть http://localhost:8080. Деморежим работает без сервера и не доказывает безопасность прохода. В API-режиме фронтенд соответствует схемам backend, слитого из `feature/svyat`; настройка и ограничения описаны в [контракте](docs/frontend-api.md). Для локальной проверки операторских экранов нужны `?devRole=admin|manager|controller` и `AUTH_DEV_BYPASS=true` на backend.

См. [запуск](docs/launch.md) и [ограничения](docs/limitations.md).
