# Интеграция фронтенда с `feature/svyat`

Актуально для маршрутов и схем backend, слитого в `main` из `feature/svyat` (merge `2c66e64`). Все пути ниже имеют префикс `/api`; базовый URL задаётся `config.apiBaseUrl` либо переменной контейнера. Фронтенд хранит HTTP-адаптер в `src/api.js` и не принимает решение о проходе.

## Роли и вход

- `admin`: сотрудники, группы, журнал, присутствующие.
- `controller`: создание прохода, опрос сессии, отправка кадров.
- `manager`: присутствующие, журнал, план, зоны, аналитика, отчёт, помощник.
- `worker`: привязка устройства, подпись прохода, отправка координат без операторского токена.

Backend защищает операторские маршруты Bearer JWT Keycloak. В этой ветке нет `/auth/config`, `/auth/exchange`, `/auth/refresh`, `/auth/logout`, которые использует браузерный PKCE-клиент. Поэтому обычный вход оператора до их реализации не работает. Для локальной проверки API можно открыть `http://localhost:8080/?devRole=admin`, `manager` или `controller` при `AUTH_DEV_BYPASS=true` на backend. Этот режим ограничен браузерным localhost; сервер должен отдельно включить bypass. Рабочий открывает `?worker=1`.

## Администратор

- `GET /groups`, `POST /groups` (`name`, `parent_id`), `GET /persons`: группы и таблица; `PersonOut` содержит `fio`, `position`, `group`, `status`, `on_site`, `has_face`, `has_active_device`.
- `POST /persons`, `PATCH /persons/{id}`: multipart с `fio`, `position`, `group_id`, `photo`; для создания фото обязательно.
- `POST /persons/{id}/enroll-code`: выдаёт `code`, `enroll_url`, `expires_at`.
- `PATCH /persons/{id}` со `status=blocked`: блокировка. Маршрута отзыва конкретного устройства пока нет.
- `GET /events`: журнал попыток. `GET /site/now`: присутствующие; сервер пока не даёт общего количества людей и числа отказов за сегодня.

## Проходная и телефон

1. `POST /passages` с `{direction:"in"|"out"}` создаёт `session_id`, `nonce`, `qr_payload`, `expires_at`.
2. Рабочий передаёт открытый JWK через `POST /enroll` с кодом. Закрытый ECDSA P-256 ключ остаётся в IndexedDB как неизвлекаемый `CryptoKey`.
3. Рабочий вставляет JSON из `qr_payload` (`sid`, `n`, `d`). Подпись ECDSA SHA-256 ставится на точные UTF-8 байты `session_id|nonce|direction`; raw `r||s` кодируется base64url без padding. `POST /passages/{id}/sign` принимает `device_id`, `signature`.
4. Контролёр опрашивает `GET /passages/{id}` до `signed`, затем снимает три JPEG-кадра и отправляет их multipart под повторяющимся полем `frames` в `POST /passages/{id}/face`. Решение `allow`/`deny` приходит только от сервера.
5. Рабочий отправляет `POST /locations`: `device_id`, точная JSON-строка `payload` с `ts` и `points[{ts,lat,lon,accuracy}]`, а также ECDSA-подпись точных байтов этой строки. Сервер отвечает `accepted`/`rejected`.

В backend пока нет `/worker/me`, поэтому статус своей смены на телефоне неизвестен. Сервер обязан ограничивать приём геолокации открытой сменой. Не реализованы идемпотентность регистрации/прохода и восстановление ключа после сбоя ответа.

## Руководитель

- `GET /site/plan`: размеры изображения, калибровка и многоугольники зон в пикселях. `PUT /site/plan` принимает `image`; `PUT /site/calibration` принимает ровно три точки `{px,py,lat,lon}`; `PUT /zones` заменяет список зон `{name,polygon}`.
- `GET /analytics/heatmap` и `/analytics/routes`: фильтры `from`, `to`, `hour_from`, `hour_to`, `person_ids`. Тепловая карта считает минуты по зонам, маршруты используют пиксельные `x,y`.
- `GET /reports/person/{id}?from&to`: табель конкретного сотрудника. Общего `/reports/shifts` нет. Список сотрудников для выбора из `/site/now` содержит только тех, кто сейчас внутри.
- `POST /assistant/ask` с `question`; `POST /assistant/summary` с `date_from`, `date_to`. Ответ `{status,text,facts_used}`.

`image_url` текущего stub-плана равен `/files/plan/plan.png`, но маршрута файла на ветке нет. Интерфейс показывает зоны без фонового изображения, пока backend не начнёт отдавать доступный URL.

## Статус backend

Маршруты и Pydantic-схемы есть в `feature/svyat`, но большинство обработчиков помечены `STUB`: данные и разрешение прохода возвращаются фиктивно, подписи и лица ещё не проверяются, хранилища нет. Backend уже слит в `main` (merge `2c66e64`). Совпадение HTTP-контракта не означает готовность безопасного допуска или сквозной работы на стенде.
