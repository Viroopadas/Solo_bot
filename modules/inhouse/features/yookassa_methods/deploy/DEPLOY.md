# Выкладка темы `yookassa_methods` (кнопки «СБП / Банковская карта / SberPay и другие способы»)

Только по отдельному слову владельца. Порядок — раздел 5 документа
`.ai-engine/reports/2026-10-09-payment-buttons-sbp-card-requirements-and-solution.md`, приёмка — раздел 6.

Серверы:
- бот и Caddy бота — `solobot-remnawave` (`/root/Solo_bot`, Caddy в Docker, файл `/opt/remnawave/caddy/Caddyfile`);
- российский сервер — `test-bg-ru1` (`93.189.229.168`, Caddy 2.11 системной службой, `/etc/caddy/Caddyfile`).

## Быстрый путь: скрипт `install.py`

Всё, что описано ниже, делает одна команда с машины оператора (из этой папки, нужен `ssh` к обоим серверам):

```bash
python install.py check     # только смотрит и пишет, что изменит
python install.py install   # ставит: код, страница, оба Caddy (с копиями), перезапуск, проверка
python install.py disable   # откат: тема выключена, бот перезапущен
python install.py enable    # обратно
```

Адреса серверов, доменов и путей — параметры скрипта (`python install.py -h`), по умолчанию — нынешний прод.
Домен оплаты задаётся одним местом — `PUBLIC_BASE_URL` в `../settings.py`. Перед установкой на новом
сервере: запись DNS домена оплаты на российский сервер; если бот там не наш форк — ключ `--copy-module`.
Повторный запуск ничего не задваивает: всё, что уже совпадает, пропускается.

Ниже — те же шаги вручную.

## 0. Перед началом

1. Сверка локальной копии бота с сервером (раздел 3а документа): отпечатки файлов `/root/Solo_bot` против
   `bots/Solo_bot`. Расхождения — сначала разобрать.
2. Код закоммичен в форк (`git -C bots/Solo_bot log --oneline -3`).

## 1. Российский сервер: страница и Caddy

```bash
ssh root@93.189.229.168
STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-inhouse-$STAMP
mkdir -p /var/www/pay-omenso/static
```

С локальной машины (из `bots/Solo_bot/modules/inhouse/features/yookassa_methods/deploy/`):

```bash
scp index.html unavailable.html root@93.189.229.168:/var/www/pay-omenso/
scp static/pay.js static/pay.css root@93.189.229.168:/var/www/pay-omenso/static/
```

Ссылка в бот на странице «Оплата временно недоступна» (адрес бота — `REDIRECT_LINK` из `config.py`, это
публичная ссылка `t.me`):

```bash
# на сервере бота:
cd /root/Solo_bot && python3 -c "import config; print(config.REDIRECT_LINK)"
# на российском сервере, подставив напечатанную ссылку:
sed -i 's#__BOT_LINK__#https://t.me/<бот>#' /var/www/pay-omenso/unavailable.html
```

Дописать в конец `/etc/caddy/Caddyfile` блок из `Caddyfile.pay.omenso.net` (чужие блоки не трогать), затем:

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile     # без перезапуска службы
curl -sI https://pay.omenso.net/p/test | head -5                     # 200, Cache-Control: no-store
curl -s -o /dev/null -w '%{http_code}\n' https://pay.omenso.net/inhouse/pay   # 405 (GET запрещён)
```

Ошибка проверки — вернуть копию: `cp /etc/caddy/Caddyfile.bak-inhouse-$STAMP /etc/caddy/Caddyfile`.

## 2. Caddy бота

```bash
ssh root@<solobot-remnawave>
STAMP=$(date +%Y%m%d-%H%M%S)
cp -a /opt/remnawave/caddy/Caddyfile /opt/remnawave/caddy/Caddyfile.bak-inhouse-$STAMP
```

В блок `store.potok-lubvi.ru`, рядом со строками `/p2pguard2/hook`, вписать строки из `Caddyfile.store-snippet`.
Файл смонтирован в контейнер: править на месте (редактором), не `sed -i` и не заменой файла.

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

До перезапуска бота адрес ответит ошибкой: модуль ещё не загружен, это нормально.

## 3. Модуль на сервер и перезапуск бота

С локальной машины (папку `tests/` на сервер можно не класть, бот её не читает):

```bash
cd bots/Solo_bot/modules
tar --exclude='inhouse/tests' --exclude='__pycache__' -czf /tmp/inhouse.tgz inhouse
scp /tmp/inhouse.tgz root@<solobot-remnawave>:/tmp/
```

На сервере бота:

```bash
cd /root/Solo_bot/modules && tar -xzf /tmp/inhouse.tgz
systemctl restart bot
journalctl -u bot -n 300 --no-pager | grep -E '\[inhouse|Загружен модуль: modules.inhouse|/inhouse/pay'
```

Ожидается:
- `[Modules] Загружен модуль: modules.inhouse.router`;
- `[Modules] Найден вебхук в модуле inhouse: /inhouse/pay`;
- `[inhouse.yk] feature on: 5.1.2, links https://pay.omenso.net/p/…, flow [...'YOOKASSA_SBP', 'YOOKASSA_CARD']`.

Строка `[inhouse.yk] feature stays off: <причина>` — тема не включилась, бот работает по-старому (кнопка
«Оплатить»). Причина в строке.

Секрет меток создаётся сам при первом запуске: `/root/Solo_bot/storage/inhouse/yookassa_methods.secret`
(права 600). Не копировать, не показывать. Если его удалить, уже отправленные кнопки покажут «Ссылка
недействительна», новые экраны работают.

## 4. Значок СБП — сразу после запуска

Открыть экран покупки при нехватке денег. Если экран не появился или сломан — в
`features/yookassa_methods/settings.py` поставить `SBP_ICON_CUSTOM_EMOJI_ID = ""` и `systemctl restart bot`
(кнопка станет «🏦 СБП»).

## 5. Купон

Админка бота → «Настройки → Кнопки» → «Активировать купон» — выключить.

## 6. Приёмка

Раздел 6 документа: экран, три кнопки с телефона (российский мобильный интернет, с VPN и без), повтор и две
вкладки (один счёт в кабинете ЮKassa), старая и испорченная ссылка, затем три малые оплаты. В журнале бота на
каждое открытие ссылки — одна строка:

```bash
journalctl -u bot --since '-30 min' --no-pager | grep '\[inhouse.yk\] open'
# result=created method=sbp user=... checkout=... payment=<ID ЮKassa> ms=...
```

`reason=method not applied (...)` у способа `sbp`/`card` — способ не выбрался, человек увидел общую страницу
ЮKassa (оплата при этом работает). Это повод разбираться, деньги не теряются.

## Откат

Ускорение соединений: каждый вызов библиотеки ЮKassa получает отдельную сессию HTTP;
общие только соединения, без общих cookies и заголовков. Подготовка соединения выполняется
в одном фоновом потоке, не чаще раза в 40 секунд, без авторизации и переходов по другим адресам.
При остановке темы исходный способ работы библиотеки возвращается, общие соединения закрываются.
Уже выполняющийся сетевой запрос этим не отменяется.

Российская проверка `/inhouse/ping` использует те же соединения, что и пересылка оплаты.
Порог `health_fails 3` сохраняет путь после единичного сбоя проверки; после трёх неудач
показывается страница недоступности до успешной проверки. На стороне бота отдельный
отложенный заголовок для `/inhouse/pay` сохраняет HTML-страницы, несмотря на общий
`Content-Type text/plain` существующего сайта.

Основной — строка и перезапуск (секунды):

```bash
# /root/Solo_bot/modules/inhouse/settings.py
FEATURES = {"yookassa_methods": False}
systemctl restart bot
```

Новые экраны снова «Оплатить». Уже отправленные кнопки-ссылки показывают «Оплата временно недоступна. Вернитесь
в бот и откройте покупку заново». Выключатель купона, DNS и блоки Caddy можно оставить — они безвредны.

Быстрый откат без правки файла: выключить модуль `inhouse` в админке («Модули»). Все части темы проверяют
выключатель при каждом вызове. Обратное включение в админке без перезапуска бота тему не поднимает — нужен
`systemctl restart bot`.

Полное удаление: откат строкой → `rm -rf /root/Solo_bot/modules/inhouse` → `systemctl restart bot`; Caddy — из
копий `*.bak-inhouse-*` (российский: `cp`, бота: `cat копия > /opt/remnawave/caddy/Caddyfile`, затем `reload`).
Таблица `inhouse_yk_links` без модуля не мешает; удалить при желании: `DROP TABLE inhouse_yk_links;`.
