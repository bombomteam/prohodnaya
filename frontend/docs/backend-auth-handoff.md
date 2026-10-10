# Что реализовать бэкендеру для входа операторов

Фронт уже запускает Authorization Code + PKCE S256, хранит токены в памяти и отправляет access token в `Authorization: Bearer …`. Ему нужны четыре маршрута ниже с префиксом `/api`. Они вызываются без Bearer-токена; рабочий продолжает использовать enroll-код и ECDSA, без Keycloak.

## 1. GET /api/auth/config

Ответ HTTP 200:

```json
{
  "authorizationEndpoint": "https://auth.hack-detect.duckdns.org/realms/construction/protocol/openid-connect/auth"
}
```

Фронт сам добавляет `client_id=panel`, `response_type=code`, `scope=openid`, `state`, `code_challenge`, `code_challenge_method=S256` и `redirect_uri=location.origin + "/"`.

## 2. POST /api/auth/exchange

Тело JSON:

```json
{
  "code": "полученный authorization code",
  "codeVerifier": "сохранённый фронтом PKCE verifier",
  "redirectUri": "https://hack-detect.duckdns.org/"
}
```

Проверить `redirectUri` по списку разрешённых адресов приложения. Обменять код через внутренний URL Keycloak `/realms/construction/protocol/openid-connect/token`: form-urlencoded с `grant_type=authorization_code`, `client_id=panel`, `code`, `code_verifier`, `redirect_uri`. Клиент `panel` публичный, client secret ему не нужен. Внешний `issuer` остаётся `https://auth.hack-detect.duckdns.org/realms/construction`.

Проверить полученный access token через существующий валидатор JWKS: подпись, `exp`, `iss`, `aud=access-api`, `sub`. Профиль и роли брать только из проверенных claims; роли — из `realm_access.roles`, оставлять `admin`, `manager`, `controller`. Не принимать профиль/роли из запроса браузера.

Ответ HTTP 200 (camelCase, как ожидает фронт):

```json
{
  "accessToken": "access_token из Keycloak",
  "refreshToken": "refresh_token из Keycloak",
  "expiresIn": 300,
  "user": {
    "sub": "sub из проверенного токена",
    "preferred_username": "controller1",
    "roles": ["controller"]
  }
}
```

`expiresIn` — фактическое `expires_in` Keycloak в секундах. Не возвращать ID token вместо access token.

## 3. POST /api/auth/refresh

JSON `{ "refreshToken": "…" }`. Вызвать token endpoint с `grant_type=refresh_token`, `client_id=panel`, `refresh_token`. Проверить новый access token так же, как при обмене кода. Вернуть тот же формат, что `/auth/exchange`, включая новый refresh token и актуальные роли.

## 4. POST /api/auth/logout

JSON `{ "refreshToken": "…" }`. Выполнить logout/revocation в Keycloak для клиента `panel` и этого refresh token, чтобы его нельзя было использовать повторно. Ответ HTTP 200 `{ "ok": true }` или 204. Фронт очищает свои токены из памяти независимо от результата запроса; физическую смену logout не закрывает.

## Ошибки и выкладка

- HTTP-ошибки возвращать JSON `{ "code": "…", "message": "Текст для пользователя" }`, без токенов, verifier и внутренних ответов Keycloak. Неверный/истёкший code или refresh token — 401; недоступность Keycloak — 503. Тела этих запросов не писать в логи.
- Ответы с токенами отдавать с `Cache-Control: no-store`. Для приложения на одном origin API идёт через Caddy; cookies фронт не использует (`credentials: omit`).
- В realm у `panel` добавлены default scopes `basic` и `acr`. После слияния PR владелец стенда должен обновить уже импортированный realm, сохранив пользователей и их пароли. Одна замена JSON в Git не изменяет существующий realm. Проверить также audience mapper `access-api` и разрешённый redirect URI `https://hack-detect.duckdns.org/`.
- Включить на стенде `FRONTEND_MODE=api` и адрес API согласно существующему compose/entrypoint. При проверке 10.10.2026 `/config.js` отдавал `mode: "demo"`, а `/api/auth/config` — 404. Это отдельные препятствия входу после исправления scopes.
- Ссылку активации бэкенд оставляет `/enroll?code=…`: фронт поддерживает её и заполняет enroll-код на экране рабочего. Код не отправляется автоматически: рабочий нажимает «Привязать устройство». Это путь браузерного UI; API остаётся `POST /api/enroll`.

## Проверка стыка

Открыть приложение с `/index.html`, выполнить вход: redirect и обмен кода используют корневой `/`; в access token есть `sub`, `aud=access-api` и нужная роль. Проверить разрешённый запрос, обновление токена, выход и невозможность повторного refresh. Для рабочего проверить открытие `/enroll?code=…`, успешную активацию и понятные `message` для технических отказов. HTTP 200 с `decision: "deny"` — обычный результат прохода, а не техническая HTTP-ошибка.
