#!/usr/bin/env python3
"""
Ищет e-mail'ы в выгрузке Telegram-чата по строкам из таблицы.

Логика на каждую строку столбца A:
  1. Находит все сообщения чата, где встречается эта строка (без учёта регистра).
  2. Берёт самое длинное из них.
  3. Достаёт из него все e-mail'ы (без дублей, в порядке появления).
  4. Пишет их через запятую в столбец B. Нет совпадения или нет e-mail'ов: «—».

Запуск:
  python3 parse_emails.py result.json input.xlsx            -> input_result.xlsx
  python3 parse_emails.py result.json input.csv -o out.xlsx
  Флаг --header: первая строка таблицы это заголовок, её не трогаем.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
DASH = "—"


def flatten_text(text):
    """В JSON-выгрузке Telegram поле text бывает строкой или списком кусков."""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text", ""))
        return "".join(parts)
    return ""


def load_messages(json_path):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    # Выгрузка одного чата: {"messages": [...]}
    # Выгрузка всего аккаунта: {"chats": {"list": [{"messages": [...]}, ...]}}
    chats = []
    if "messages" in data:
        chats.append(data)
    if "chats" in data:
        chats.extend(data["chats"].get("list", []))
    texts = []
    for chat in chats:
        for m in chat.get("messages", []):
            t = flatten_text(m.get("text", ""))
            if t.strip():
                texts.append(t)
    return texts


def normalize_key(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # 79991234567.0 -> 79991234567
    return " ".join(str(value).split())


def find_emails(key, texts_lower, texts):
    if not key:
        return DASH
    k = key.lower()
    best = None
    for low, orig in zip(texts_lower, texts):
        if k in low and (best is None or len(orig) > len(best)):
            best = orig
    if best is None:
        return DASH
    seen, emails = set(), []
    for e in EMAIL_RE.findall(best):
        e = e.rstrip(".")
        if e.lower() not in seen:
            seen.add(e.lower())
            emails.append(e)
    return ", ".join(emails) if emails else DASH


def read_table(path):
    """Возвращает (workbook, worksheet). CSV конвертируется в новую книгу."""
    if path.suffix.lower() == ".csv":
        wb = Workbook()
        ws = wb.active
        with open(path, encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            for row in csv.reader(f, dialect):
                ws.append(row)
        return wb, ws
    wb = load_workbook(path)
    return wb, wb.active


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chat_json", help="result.json из экспорта Telegram Desktop")
    ap.add_argument("table", help="таблица .xlsx или .csv, ключи в столбце A")
    ap.add_argument("-o", "--output", help="куда сохранить (.xlsx)")
    ap.add_argument("--header", action="store_true", help="первая строка это заголовок")
    args = ap.parse_args()

    texts = load_messages(args.chat_json)
    texts_lower = [t.lower() for t in texts]
    print(f"Сообщений в чате: {len(texts)}", file=sys.stderr)

    table = Path(args.table)
    wb, ws = read_table(table)
    start = 2 if args.header else 1
    if args.header and not ws.cell(row=1, column=2).value:
        ws.cell(row=1, column=2).value = "E-mail"

    found = total = 0
    for r in range(start, ws.max_row + 1):
        key = normalize_key(ws.cell(row=r, column=1).value)
        if not key:
            continue
        total += 1
        res = find_emails(key, texts_lower, texts)
        ws.cell(row=r, column=2).value = res
        if res != DASH:
            found += 1

    out = Path(args.output) if args.output else table.with_name(table.stem + "_result.xlsx")
    wb.save(out)
    print(f"Строк: {total}, с e-mail: {found}, прочерков: {total - found}", file=sys.stderr)
    print(f"Сохранено: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
