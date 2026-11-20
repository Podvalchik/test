# Парсер e-mail из Telegram-чата

На каждую строку столбца A: ищет сообщения чата с этой строкой, берёт самое длинное, пишет все e-mail из него в столбец B через запятую. Нет совпадения или почты: `—`.

## Экспорт чата
Telegram Desktop → чат → ⋮ → «Экспорт истории чата» → снять все галочки медиа → формат **JSON** → получить `result.json`.

## Запуск
```
pip install openpyxl
python3 parse_emails.py result.json table.xlsx --header
```
Результат: `table_result.xlsx`. Принимает .xlsx и .csv. `--header` если первая строка заголовок.
