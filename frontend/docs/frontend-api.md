# Контракт фронтенда и backend

Статус: реализованный в адаптере проект контракта, требующий согласования с backend и отсутствующими в checkout файлами 01–08. Не заменяет их и не доказывает работу серверных проверок.

## Операторы

Все операторские запросы: Authorization: Bearer access_token Keycloak, без cookie. Realm construction, public client panel, audience access-api; backend проверяет подпись/exp/iss/aud и realm-роли. 401 предлагает повторный вход, 403 сообщает недостаток прав. Токены только в памяти. Timeout 15 секунд; автоматического повтора мутирующих запросов нет.

| Маршрут | Права и данные |
| --- | --- |
| GET /employees; POST /employees; PATCH /employees/:id | admin; FormData name/company/role (специальность, не IAM-роль)/referencePhoto; сервер проверяет эталон |
| POST /employees/:id/enroll-code | admin; ответ code/expiresAt; выпуск нового одноразового кода |
| DELETE /employees/:id/device | admin; отзыв ключа |
| POST /employees/:id/deactivate | admin; сервер запрещает при открытой смене |
| GET /reports/people | admin/manager; минимальный список id/name для фильтров, не административный реестр |
| GET /dashboard; GET /passes/attempts | admin/manager; присутствующие и журнал |
| POST /passes/challenges; GET /passes/challenges/:id | controller; direction entry/exit, ответ id/code/direction/expiresAt/status |
| POST /passes/attempts | controller; FormData challengeId/direction/faceImage, Idempotency-Key; атомарное серверное решение |
| GET /site; GET /analytics; GET /reports/shifts | admin/manager; общие фильтры from/to/hourFrom/hourTo/employeeIds |
| PUT /site | admin; JSON name/latMin/latMax/lonMin/lonMax/zones; при загрузке плана multipart metadata(JSON)/planImage |
| POST /assistant/questions; POST /assistant/summaries | admin/manager; question или from/to, ответ status/answer |

Форматы аналитики и отчётов остаются как в существующем UI: zones(id/name/count), routes(employeeId/employeeName/points(x/y/at)), metric, totalPoints; отчёт shifts(id/employeeName/startedAt/endedAt/workedMinutes), people(employeeName/shiftCount/workedMinutes). Сайт может вернуть planImageUrl; фронт рисует план под сеткой зон. Расположение зон и калибровку backend обязан согласовать с этой сеткой или заменить представление вместе с адаптером. Сетка больше не ограничена девятью зонами.

Журнал/результат: outcome allowed/denied/pending/error, reason, employeeName, at, direction. Сервер принимает решение, UI его отображает; один Idempotency-Key сохраняется для повторной отправки текущего действия после сетевой ошибки.

## Предложение ECDSA device-v1 — требует явного включения

GET /worker/config с ответом {"deviceProtocol":"device-v1"} включает следующий адаптер; до подтверждения сервером значение пустое и не позволяет подписывать/отправлять неподтверждённый протокол. Рабочий не логинится в Keycloak, WebAuthn не используется.

- ECDSA P-256, SHA-256; закрытый CryptoKey extractable=false сохраняется в IndexedDB, открытый ключ — JWK.
- Подписываются UTF-8 байты JSON.stringify объекта строго в порядке: version, method, path, body, nonce, timestamp. version=1, method uppercase, path относительно API без префикса, отсутствующее body=null. Вложенное body сериализуется с порядком полей отправленного запроса; сервер должен воспроизвести байты без сортировки/округления/переименования. Подпись WebCrypto — raw r||s, base64url без padding, не DER.
- Proof: keyId, nonce, timestamp, signature. Для обычных запросов JSON proof передаётся в X-Device-Proof; при регистрации находится в body.proof.
- POST /enroll/options (без IAM): code/publicKey → nonce. POST /enroll: code/publicKey/proof → keyId. Подписываемое body регистрации — только code/publicKey, без самого proof. Код и challenge сервер связывает с ключом и человеком; private key не передаётся.
- POST /worker/nonce (без IAM): keyId/method/path → nonce; сервер выдаёт ограниченный по сроку challenge и связывает с запросом/ключом. Подпись и свежесть проверяются до доступа к данным. Rate limit, TTL и защита от повтора — обязательные серверные обязанности.
- GET /worker/me — подписан, ответ employee или отсутствие привязки. До первой регистрации UI не вызывает его.
- POST /worker/pass/options — подписанное body code → challengeId/direction/nonce. Подтверждение nonce относится к этой попытке и её направлению.
- POST /worker/pass/confirm — подписанное body challengeId/direction, используется nonce из options. Сервер проверяет ключ, принадлежность и срок, затем отдельно требуется лицо на проходной.
- POST /locations — подписанное body shiftId/batchId/source=phone/points[{latitude,longitude,accuracy,capturedAt}]. Одна измеренная точка может составлять пачку. Сервер не доверяет source и владельцу из тела, устанавливает их по зарегистрированному ключу. Повторы batchId не дублируют события.

Если финальные 01–08 используют другой формат, изменить device.js и api.js до включения API, а не подгонять backend к этому предложению. После потери ответа /enroll серверу нужен безопасный повтор/восстановление для того же ключа; текущий клиент сохраняет ключ до отправки, но серверная идемпотентность ещё должна быть согласована.

## Ограничения

Формат калибровки и защищённый протокол должны быть подтверждены реальным backend. Фронт не выполняет распознавание и не реализует nonce-хранилище. Обновление challenge позволяет увидеть статус после ошибки, но отдельный endpoint восстановления окончательного решения должен быть согласован; повтор не считается безопасным без серверной идемпотентности.

## Предложение интеграции PKCE без браузерных зависимостей

Следующие endpoints пока требуют реализации на backend. Все HTTP-запросы идут через api.js. Пароль оператор вводит только в Keycloak.

- GET /auth/config → authorizationEndpoint (HTTPS, кроме localhost). Realm и клиент фиксированы командой; endpoint авторизации Keycloak.
- POST /auth/exchange: code, codeVerifier, redirectUri → accessToken, refreshToken, expiresIn (секунды), user {name, preferred_username, roles}. Backend обменивает code у Keycloak, проверяет подпись/exp/iss/aud и возвращает проверенный профиль. Разрешённые redirectUri задаются allowlist. Public client panel не требует браузерного секрета.
- POST /auth/refresh: refreshToken → тот же ответ; сервер проверяет новый access token.
- POST /auth/logout: refreshToken → завершение сессии/отзыв refresh token. Самостоятельно живущий access token имеет короткий срок действия.

Браузер генерирует PKCE S256 и state. Только временные verifier/state/redirectUri сохраняются в sessionStorage для перехода в Keycloak; callback проверяет state и срок десять минут. Токены хранятся только в памяти. Backend не логирует code, verifier и токены, ограничивает CORS и частоту запросов. Это отдельное решение, требующее согласования команды; живой Keycloak этим локальным прогоном не проверялся.
