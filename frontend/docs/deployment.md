# Публикация фронтенда

## Docker Compose (основной способ)

Стенд описан в `compose.yaml` в корне репозитория. Настройки — в `.env` (образец с пояснениями: `.env.example`, секретов там нет). Из корня репозитория:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
```

Фронт откроется на порту `FRONTEND_PORT` (по умолчанию 80; на Windows удобнее 8080). Контейнер запускается с ограничениями: файловая система только для чтения (запись только в `/tmp`), без root и Linux-привилегий, лимиты памяти/CPU/процессов, ротация логов. `config.js` генерируется при старте в `/tmp` и отдаётся по адресу `/config.js`. Остановить: `docker compose down`.

## Docker без Compose

Контекст сборки — папка `frontend`. Образ содержит только HTML, CSS, JS и Nginx на порту 8080; Node.js и материалы кейса в контейнер не копируются. Из корня репозитория:

```powershell
docker build -t prohodnaya-frontend:local ./frontend
docker run --rm --name prohodnaya-frontend -p 8080:8080 prohodnaya-frontend:local
```

Откройте `http://localhost:8080`. Проверка состояния: `http://localhost:8080/healthz`. По умолчанию запускается **деморежим** с данными в `localStorage` браузера; образ не содержит бэкенд и не выполняет серверную проверку личности.
Пример переменных с пояснениями лежит в [`docker.env.example`](../docker.env.example); его можно передать через `docker run --env-file frontend/docker.env.example ...` из корня репозитория.

Workflow [`.github/workflows/frontend-image.yml`](../../.github/workflows/frontend-image.yml) собирает и проверяет контейнер при изменении `frontend/` в `main`, затем публикует `ghcr.io/bombomteam/prohodnaya-frontend:latest` и тег полного SHA коммита. Для запуска готового образа используйте `docker pull ghcr.io/bombomteam/prohodnaya-frontend:latest`; если пакет закрыт настройками GitHub Packages, сначала выполните `docker login ghcr.io` с правом чтения пакета.

При наличии бэкенда на одной Docker-сети можно направить `/api/*` через Nginx на его HTTP origin. Например, если сервис доступен контейнеру под именем `backend` на порту 8000 и реализует пути `/api/*`:

```powershell
docker network create prohodnaya
docker run --rm --name prohodnaya-frontend --network prohodnaya -p 8080:8080 -e FRONTEND_MODE=api -e API_UPSTREAM=http://backend:8000 prohodnaya-frontend:local
```

Бэкенд должен быть запущен в той же сети **до** старта фронта. `API_BASE_URL` в этом варианте равен `/api`; браузер обращается к тому же origin, а Nginx передаёт полный путь `/api/...` бэкенду. Если бэкенд опубликован отдельно, вместо `API_UPSTREAM` передайте `-e FRONTEND_MODE=api -e API_BASE_URL=https://api.example.org/api`. Такой адрес должен открываться **из браузера пользователя**; на другом устройстве `localhost` указывает на само это устройство. Для разных origin сервер должен разрешить CORS и передачу cookie. Адрес API и режим записываются в публичный `config.js` при запуске контейнера; секреты через эти переменные передавать нельзя.

Камера, WebAuthn и геолокация на удалённом телефоне требуют HTTPS для **фронта**. TLS можно завершать на внешнем reverse proxy. Для WebAuthn сервер должен использовать RP ID домена фронта. Установка `FRONTEND_MODE=api` сама по себе не добавляет сервер, БД и проверки прохода. Поля и пути API описаны в [контракте](frontend-api.md).

## GitHub Pages

Публичный адрес актуальной версии из `main`: https://bombomteam.github.io/prohodnaya/.

Workflow [`.github/workflows/pages.yml`](../../.github/workflows/pages.yml) запускается при push в `main` и вручную. Он выполняет тесты, собирает статический артефакт и публикует его через GitHub Pages. Сборка находится в `frontend/scripts/build-pages.mjs` и копирует только `index.html`, `styles.css`, `config.js` и четыре JS-модуля из `src/`. Фотографии и документы кейса не входят в Pages-артефакт.

Проверить артефакт локально:

```powershell
cd frontend
npm test
npm run build:pages
```

Затем запустите статический сервер из `frontend/dist/pages/` и откройте его в браузере. Скрипт сборки намеренно допускает только `mode: "demo"`; реальный режим потребует отдельного HTTPS API, настроенного адреса, проверки CORS/cookie и нового решения по развёртыванию.

Для первоначального включения GitHub Pages в настройках репозитория источник публикации должен быть `GitHub Actions`. После запуска workflow проверьте успешный job `deploy` и ссылку `page_url` в Actions. GitHub Pages размещает статические файлы, поэтому запуск сервера, БД и модели остаётся отдельной задачей команды.

## Sites

Отдельный публичный демостенд был опубликован ранее: https://prohodnaya-team-demo.vardgesg286.chatgpt.site. Это снимок фронта на момент предыдущей публикации. GitHub Pages после включения станет способом публикации версии из этого репозитория; обновление GitHub не обновляет автоматически стенд Sites.
