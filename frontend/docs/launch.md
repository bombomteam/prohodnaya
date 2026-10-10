# Запуск и интеграция

Локально из `frontend`: `python -m http.server 8080`, затем открыть http://localhost:8080. Проверка: `npm test` и `npm run build:pages`.

Для API-режима измените только `mode: "api"` и `apiBaseUrl` в `config.js`; например `http://localhost:8000/api`. Контейнер генерирует `config.js` при старте из переменных окружения. Backend должен разрешить origin фронта через `CORS_ORIGINS=http://localhost:8080` при раздельных портах.

Операторские запросы требуют Bearer JWT. В `feature/svyat` нет маршрутов `/auth/*` для браузерного PKCE-входа. Для локального тестирования stub API запустите backend с `AUTH_DEV_BYPASS=true` и откройте `?devRole=admin`, `?devRole=manager` либо `?devRole=controller` на localhost. Не включайте bypass на публичном стенде. Для рабочего используйте `?worker=1`.

Стенд приложения: https://hack-detect.duckdns.org. Keycloak: https://auth.hack-detect.duckdns.org (внешняя админка закрыта). Камера, геолокация, WebCrypto и PKCE требуют HTTPS или localhost; фронт показывает понятную ошибку в небезопасном контексте. При смене origin ключ устройства из IndexedDB недоступен, нужна новая привязка.

Подробнее: [контракт](frontend-api.md), [задание бэкендеру по входу](backend-auth-handoff.md), [ограничения](limitations.md). Push в `main` выкатывается на стенд. Работать в отдельной ветке, сливать через PR после зелёной `ci-ok`. Уже импортированный realm после изменения scopes должен обновить владелец стенда.
