# Keycloak для «Проходной»

Подготовлен стенд Keycloak 26.5.7: realm construction, роли admin/manager/controller, public client panel (Code Flow + PKCE S256), audience access-api, access token 300 секунд. Рабочих и роли worker здесь нет. Это IAM-конфигурация, не серверная проверка физического прохода.

## Запуск

Нужен Docker Desktop с Linux containers. Из корня проекта скопировать `.env.example` в `.env`, только если `.env` ещё нет. В существующий `.env` добавить настройки Keycloak из примера, сохранив остальные значения. Указать четыре разных случайных пароля в KC_BOOTSTRAP_ADMIN_PASSWORD, KC_ADMIN1_PASSWORD, KC_MANAGER1_PASSWORD, KC_CONTROLLER1_PASSWORD. Не коммитить `.env`. Для локального фронта удобно FRONTEND_PORT=8080.

```powershell
docker compose up -d --build keycloak
docker compose logs --tail=100 keycloak
```

Дождаться сообщения о запуске. Затем один раз установить пароли импортированных пользователей:

```powershell
docker compose exec -T keycloak bash /opt/keycloak/project-scripts/bootstrap-users.sh
```

Команда использует временную конфигурацию kcadm, удаляет её при завершении и не выводит пароли. Повторный вызов явно сбрасывает пароли трёх тестовых операторов из текущего окружения; после изменения `.env` сначала пересоздать контейнер через `docker compose up -d --force-recreate keycloak`. Это команда настройки стенда, не provisioning рабочего.

Админ-консоль: http://localhost:8081/admin — kcadmin и KC_BOOTSTRAP_ADMIN_PASSWORD. Операторы: admin1, manager1, controller1 с соответствующими паролями из `.env`. Пока команда установки паролей не выполнена, эти аккаунты не готовы к входу.

## Проверка на машине с Docker

```powershell
Invoke-RestMethod http://localhost:8081/realms/construction/.well-known/openid-configuration
docker compose restart keycloak
```

Проверить issuer `http://localhost:8081/realms/construction`, доступность jwks_uri. Через браузерный Code + PKCE flow клиента panel проверить вход каждого оператора, realm_access.roles и aud=access-api в access token. Запрос без PKCE должен отклоняться; password grant выключен. Нельзя проверять вход через grant_type=password.

Для проверки сохранности: удалить только контейнер через `docker compose rm -sf keycloak`, затем `docker compose up -d --build keycloak`; роли и пользователи должны сохраниться. Не использовать `down -v`: он удаляет постоянные данные. Проверка первого импорта на пустом хранилище выполняется в отдельном Compose project с отдельным volume; существующий стенд не стирать.

## Данные и импорт

Встроенная H2 dev-file хранится в volume keycloak_data. Это согласованный вариант демонстрационного стенда, не промышленная БД. Для эксплуатации перенести Keycloak в отдельную PostgreSQL-базу с собственной ролью; миграции приложения её не трогают. start-dev требует записи при Quarkus augmentation, поэтому для этого сервиса read_only=false; остальные ограничения Compose сохранены, frontend не изменён.

Файл import/construction-realm.json не содержит credentials, парольных хешей и секретов. Пароли задаются отдельной явной командой из окружения. При старте существующий realm пропускается: изменение JSON или PANEL_ORIGIN не обновляет живой realm автоматически. Для существующего realm изменить panel → Valid Redirect URIs и Web Origins в админ-консоли; импортный JSON нужен для нового пустого окружения.

## HTTPS и конфиг команды

Для телефона/планшета нужен HTTPS и у фронта, и у Keycloak. Перед первым импортом задать KC_PUBLIC_URL=https://адрес-keycloak, PANEL_REDIRECT_URI=https://адрес-фронта/ и PANEL_ORIGIN=https://адрес-фронта. За доверенным reverse proxy использовать KC_PROXY_HEADERS=xforwarded; он обязан перезаписывать forwarded-заголовки. KC_BIND по умолчанию loopback, удалённый доступ настраивается владельцем инфраструктуры. Туннель может проксировать localhost:8081. После смены quick tunnel обновить `.env`, пересоздать контейнер и вручную обновить URLs panel в существующем realm.

Backend получает JWKS через KC_INTERNAL_URL=http://keycloak:8080, но проверяет issuer по KC_PUBLIC_URL. Для фронта: realm construction, client_id panel. Backend обязан проверять подпись, exp, iss, aud и права; ID token в API не передаётся.

Предложенные фронтом /auth/config, /auth/exchange, /auth/refresh, /auth/logout пока не реализованы. Сам по себе запуск Keycloak не включает вход в приложении. Frontend и backend этой задачей не меняются.

## Проверено и не проверено

Локально проверяются JSON, роли, параметры клиентов и отсутствие секретов. Docker отсутствует в среде автора изменений, поэтому compose config, старт, импорт, браузерный вход, подписи JWT, смена hostname и сохранность после пересоздания ещё требуют живого прогона. Не считать этот файл отчётом об успешном запуске.

Официальные инструкции: [контейнеры](https://www.keycloak.org/server/containers), [импорт](https://www.keycloak.org/server/importExport), [hostname](https://www.keycloak.org/server/hostname).

## Переносимый Docker-образ

Realm и скрипты теперь включены в собственный образ; bind mounts из репозитория не нужны. При старте entrypoint копирует шаблон realm в volume и передаёт управление Keycloak через exec. Существующий realm по-прежнему не перезаписывается. Healthcheck проверяет /health/ready на внутреннем management-порту 9000 без curl/wget; порт 9000 наружу не опубликован.

Собрать отдельно из корня репозитория:

```powershell
docker build -t prohodnaya-keycloak:local ./keycloak
```

Для выкладки через registry заменить имя на своё, затем выполнить:

```powershell
docker tag prohodnaya-keycloak:local registry.example.org/team/prohodnaya-keycloak:v1
docker push registry.example.org/team/prohodnaya-keycloak:v1
```

На сервере задать KEYCLOAK_IMAGE и KEYCLOAK_TAG в `.env` на опубликованные имя и тег. Скопировать compose.yaml и заполненный `.env` по защищённому каналу (секреты не в registry), установить Docker Engine и Compose plugin. Исходники keycloak/ при запуске опубликованного образа не нужны:

```sh
docker compose pull keycloak
docker compose up -d --no-build keycloak
docker compose ps keycloak
docker compose exec -T keycloak bash /opt/keycloak/project-scripts/bootstrap-users.sh
```

Дождаться healthy перед установкой паролей. Bootstrap выполнять только при первом развёртывании либо намеренном сбросе паролей. Для обновления образа повторить pull/up без bootstrap, сохраняя volume. Перед обновлением версии Keycloak сделать согласованную резервную копию хранилища.

Если запускаете образ без Compose, задайте runtime-переменные KC_HOSTNAME (в Compose он берётся из KC_PUBLIC_URL), PANEL_REDIRECT_URI, PANEL_ORIGIN и KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD; подключите постоянный volume к /opt/keycloak/data. Для bootstrap понадобятся ещё три KC_*1_PASSWORD. Переменные приложения KC_PUBLIC_URL/KC_INTERNAL_URL сами по себе не настраивают hostname образа без Compose.

Успешный healthcheck подтверждает готовность процесса, но не проверяет realm, пароли или PKCE. Сборка и запуск образа в этой среде не проверены: Docker отсутствует. Публикация в registry и деплой на сервер этим изменением не выполнялись.
