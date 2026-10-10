# Публикация фронтенда

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
