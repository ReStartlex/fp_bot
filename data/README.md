# `data/` — постоянные данные

Эта папка хранит локальную БД бота и пользовательские маппинги.
**В Git не попадает** (см. `.gitignore`).

## `mappings.csv` — маппинги NS<->FunPay

Главный конфиг бота. Каждая строка = один лот на FunPay привязан к одному
service_id из `ns.gifts`.

### Колонки

| Колонка               | Обяз. | Описание                                                                        |
|-----------------------|-------|---------------------------------------------------------------------------------|
| `funpay_lot_id`       | да    | ID лота на FunPay (видно в URL: `/lots/offer?id=12345678`)                      |
| `ns_service_id`       | да    | ID товара в `ns.gifts` (см. вывод `/api/v2/stock` или `check_ns`)               |
| `markup_percent`      | нет   | Наценка %, override глобальной из `.env`                                        |
| `stock_cap`           | нет   | Потолок стока на FunPay, override глобального                                   |
| `ns_fields_template`  | нет   | JSON-шаблон для NS `create_order`. `@QUANTITY` подставляется из FunPay-заказа.  |
| `enabled`             | нет   | `true`/`false`. По умолчанию `true`                                             |
| `label`               | нет   | Подпись для логов (для тебя)                                                    |

### Пример

См. `mappings.example.csv`.

### Импорт

После правки CSV — на сервере:

```bash
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python \
    -m src.tools.import_mappings /opt/funpay-ns-bot/data/mappings.csv
```

После импорта можно делать dry-run прогон, чтобы посмотреть какие
изменения бот собирался бы внести (НИЧЕГО не пишет на FunPay):

```bash
sudo -u bot /opt/funpay-ns-bot/.venv/bin/python -m src.tools.dry_run_sync
```
