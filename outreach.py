"""Письма новым компаниям: предпросмотр, тестовое письмо, отправка, повтор.

    python outreach.py preview companies.csv        показать все письма, ничего не отправляя
    python outreach.py test companies.csv           первое письмо — себе на SMTP_USER
    python outreach.py send companies.csv           до 10 новых писем, пауза ~4 мин
    python outreach.py followup companies.csv       до 10 повторных тем, кто молчит 5+ дней
    python outreach.py sync companies.csv           перенести «отправлено» из CSV в Google-таблицу

Колонки CSV: name, reg_date (ГГГГ-ММ-ДД), niche, email, status, sent_at, message_id.
status: пусто → «отправлено» → «повтор». «ответил» / «отказ» ставите руками —
таким больше ничего не уходит. Статус пишется в файл после каждого письма.

Переменные окружения: SMTP_USER, SMTP_PASSWORD (пароль приложения, не основной),
SMTP_HOST (по умолчанию smtp.gmail.com), SENDER_NAME (по умолчанию «Артём»),
TEST_TO (куда слать тестовое письмо; по умолчанию себе же, на SMTP_USER).

После каждого отправленного письма компания помечается в общей Google-таблице
(лист «Без сайта», поиск по почте): «Статус» → «написали», в «Заметки»
дописывается «рассылка ДД.ММ.ГГГГ» или «повтор ДД.ММ.ГГГГ». Таблица и ключ —
SHEET_ID и GOOGLE_KEY (обычно их подставляет leads.py из config.json).
Если таблица недоступна, письмо всё равно считается отправленным — только
предупреждение; догнать потом можно командой sync.
"""

import csv
import os
import random
import re
import smtplib
import sys
import time
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

LIMIT_PER_RUN = 10
PAUSE_SECONDS = 240
PAUSE_JITTER = 60
FOLLOWUP_AFTER_DAYS = 5
STOP_STATUSES = {"ответил", "отказ"}

CONTACTS = (
    "Сайт: https://bornweb.studio\n"
    "Telegram: https://t.me/BornWebStudio\n"
    "WhatsApp: https://wa.me/79685174655\n"
)

# Таблица и ключ приходят из config.json через leads.py (или из переменных окружения).
SHEET_ID = os.environ.get("SHEET_ID", "")
GOOGLE_KEY = os.environ.get("GOOGLE_KEY", str(Path(__file__).with_name("google_key.json")))

CASES = {
    "tfm": (
        "Недавно пересобрали сайт транспортно-экспедиторской группы ТФМ во Владивостоке: "
        "35 страниц под услуги и направления, 148 редиректов со старых адресов, чтобы "
        "позиции в поиске переехали вместе с сайтом."
    ),
    "grad-m": (
        "Например, сайт архитектурной мастерской «Град М» во Владивостоке стоит "
        "на первом месте в Google по запросу «архитектурная компания Владивосток», "
        "и две трети посетителей приходят из поиска, без рекламы."
    ),
    "probiotic": (
        "Например, интернет-магазин для «Пробиотика», производителя пробиотиков "
        "и БАД: каталог по категориям, корзина, оформление заказа и личный кабинет."
    ),
    "bayangol": (
        "Например, сайт доставки ресторана «Баян Гол»: меню на 261 блюдо, заказы "
        "приходят прямо на кухню в Telegram. Раньше ресторан платил студии 25 000 ₽ "
        "в месяц за «аренду» сайта, теперь сайт принадлежит ему — это 300 000 ₽ "
        "экономии в год."
    ),
}

# ниша → (кто ищет компанию в интернете, какой кейс показать)
NICHES = {
    "логистика": ("Грузовладелец перед первой заявкой ищет перевозчика в интернете", "tfm"),
    "стройка": ("Заказчик, прежде чем доверить объект, ищет подрядчика в интернете", "grad-m"),
    "архитектура": ("Заказчик выбирает архитектора по портфолио и ищет его в интернете", "grad-m"),
    "недвижимость": ("Покупатель начинает поиск объекта в интернете", "grad-m"),
    "медицина": ("Пациент, прежде чем записаться, ищет клинику в интернете", "probiotic"),
    "образование": ("Будущий ученик выбирает, где учиться, в интернете", "grad-m"),
    "автосервисы": ("Водитель ищет автосервис рядом в интернете", "bayangol"),
    "производство": ("Оптовый покупатель, прежде чем запросить прайс, ищет производителя в интернете", "probiotic"),
    "мебель": ("Покупатель мебели сначала смотрит работы и цены в интернете", "probiotic"),
    "рестораны": ("Гость выбирает, где поесть и что заказать, в интернете", "bayangol"),
}


# ниши, где своего кейса нет: говорим это прямо, а не делаем вид, что он есть
DISCLAIMERS = {
    "медицина": "Сайта клиники у нас пока нет — говорю как есть.",
    "образование": "Сайта учебного центра у нас пока нет — говорю как есть.",
}


def company_name(raw: str) -> str:
    """ООО "НРСУ" → ООО «НРСУ»"""
    return re.sub(r'"([^"]*)"', r"«\1»", raw.strip())


def subject(row: dict) -> str:
    return f"Сайт для {company_name(row['name'])}"


def letter(row: dict, sender: str) -> str:
    niche = row["niche"].strip().lower()
    if niche not in NICHES:
        raise ValueError(f"{row['name']}: ниша «{row['niche']}» не из списка: {', '.join(NICHES)}")
    searcher, case = NICHES[niche]
    case_text = CASES[case]
    if niche in DISCLAIMERS:
        case_text += " " + DISCLAIMERS[niche]
    return (
        "Здравствуйте!\n\n"
        f"У {company_name(row['name'])} пока нет сайта. "
        f"{searcher} — и сейчас о вас ничего не найдёт.\n\n"
        f"Мы — Born Web, делаем сайты для бизнеса. {case_text}\n"
        f"Подробнее: https://bornweb.studio/cases/{case}\n\n"
        "Если сайт у вас в планах — пришлём, как он мог бы выглядеть для вашей ниши. "
        "Если не актуально — ответьте «нет», больше не напишем.\n\n"
        f"{sender}, Born Web\n"
        f"{CONTACTS}"
    )


def followup_letter(row: dict, sender: str) -> str:
    return (
        "Здравствуйте!\n\n"
        f"Пишем ещё раз про сайт для {company_name(row['name'])} — вдруг письмо затерялось. "
        "Если тема актуальна, пришлём пример под вашу нишу. "
        "Если нет — ответьте «нет», больше не напишем.\n\n"
        f"{sender}, Born Web\n"
        f"{CONTACTS}"
    )


def read_rows(path: str) -> tuple[list[dict], str]:
    # Excel сохраняет CSV то в UTF-8, то в cp1251, с «;» — принимаем оба варианта
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                text = f.read()
            break
        except UnicodeDecodeError:
            continue
    else:
        sys.exit(f"{path}: не удалось прочитать ни в UTF-8, ни в cp1251")
    header = text.splitlines()[0]
    delimiter = ";" if header.count(";") > header.count(",") else ","
    rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    for column in ("status", "sent_at", "message_id"):
        rows = [{**r, column: r.get(column) or ""} for r in rows]
    return rows, delimiter


def write_rows(path: str, rows: list[dict], delimiter: str) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def sender_name() -> str:
    return os.environ.get("SENDER_NAME", "Артём")


def smtp_config() -> tuple[str, str, str]:
    user = os.environ.get("SMTP_USER")
    password = (os.environ.get("SMTP_PASSWORD") or "").replace(" ", "")
    if not user or not password:
        sys.exit("Задайте SMTP_USER и SMTP_PASSWORD (пароль приложения).")
    if not password.isascii():
        sys.exit("SMTP_PASSWORD: похоже, вместо пароля подставлена подсказка. "
                 "Пароль приложения Google — 16 латинских букв.")
    return os.environ.get("SMTP_HOST", "smtp.gmail.com"), user, password


def send_one(to: str, subj: str, body: str, reply_to_id: str = "") -> str:
    host, user, password = smtp_config()
    msg = EmailMessage()
    msg["From"] = formataddr((f"{sender_name()}, Born Web", user))
    msg["To"] = to
    msg["Subject"] = subj
    msg["Message-ID"] = make_msgid(domain=user.split("@")[1])
    if reply_to_id:  # повтор уходит ответом в ту же цепочку
        msg["In-Reply-To"] = msg["References"] = reply_to_id
    msg.set_content(body)
    with smtplib.SMTP_SSL(host, 465) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return msg["Message-ID"]


class Sheet:
    """Лист «Без сайта» общей Google-таблицы. Ошибки не роняют рассылку."""

    def __init__(self) -> None:
        self.ws = None
        if not SHEET_ID:
            return
        try:
            import warnings
            warnings.filterwarnings("ignore")      # google-auth ругается на Python 3.9
            import gspread
            self.ws = gspread.service_account(filename=GOOGLE_KEY).open_by_key(SHEET_ID).worksheet("Без сайта")
            head = self.ws.row_values(1)
            self.col = {name: head.index(name) + 1 for name in ("Почта", "Статус", "Заметки")}
        except Exception as e:
            print(f"  ⚠ Google-таблица недоступна ({type(e).__name__}: {e}) — статусы не пишутся, потом: sync")
            self.ws = None

    def _call(self, fn, *args):
        """Запрос к таблице с повтором: у Google лимит ~60 запросов в минуту."""
        for attempt in range(5):
            try:
                return fn(*args)
            except Exception as e:
                if "429" not in str(e) and "Quota" not in str(e) or attempt == 4:
                    raise
                time.sleep(20 * (attempt + 1))

    def mark(self, email: str, note: str) -> None:
        if not self.ws:
            return
        try:
            if not hasattr(self, "mails"):        # столбцы читаем один раз за запуск
                self.mails = self._call(self.ws.col_values, self.col["Почта"])
                self.notes = self._call(self.ws.col_values, self.col["Заметки"])
            rows = [i for i, m in enumerate(self.mails, 1)
                    if i > 1 and email.strip().lower() in [x.strip().lower() for x in m.split(";")]]
            if not rows:
                print(f"  (в таблице нет {email} — пропускаю)")
                return
            for i in rows:
                notes = self.notes[i - 1] if i <= len(self.notes) else ""
                if note in notes:                     # уже помечено — sync можно гонять сколько угодно
                    print(f"  таблица: строка {i} уже помечена")
                    continue
                new_notes = f"{notes}; {note}" if notes else note
                self._call(self.ws.update_cell, i, self.col["Статус"], "написали")
                self._call(self.ws.update_cell, i, self.col["Заметки"], new_notes)
                while len(self.notes) < i:
                    self.notes.append("")
                self.notes[i - 1] = new_notes
                print(f"  таблица: строка {i} → написали")
        except Exception as e:
            print(f"  ⚠ не удалось обновить таблицу для {email}: {type(e).__name__}")


def ru_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


def pause() -> None:
    seconds = PAUSE_SECONDS + random.randint(-PAUSE_JITTER, PAUSE_JITTER)
    print(f"  пауза {seconds} с")
    time.sleep(seconds)


def run_batch(path: str, rows: list[dict], delimiter: str, followup: bool,
              limit: int = LIMIT_PER_RUN, sheet: "Sheet | None" = None, first: bool = True) -> int:
    """Отправляет до limit писем из одного файла. Возвращает, сколько ушло."""
    cutoff = (date.today() - timedelta(days=FOLLOWUP_AFTER_DAYS)).isoformat()
    if followup:
        todo = [i for i, r in enumerate(rows)
                if r["status"] == "отправлено" and r["sent_at"] <= cutoff and r["email"].strip()]
    else:
        todo = [i for i, r in enumerate(rows) if not r["status"].strip() and r["email"].strip()]
    todo = todo[:limit]
    if not todo:
        return 0
    print(f"{os.path.basename(path)}: {'повторов' if followup else 'новых писем'} к отправке {len(todo)}")
    sheet = sheet or Sheet()
    for n, i in enumerate(todo):
        if not (first and n == 0):
            pause()
        row = rows[i]
        if followup:
            send_one(row["email"], "Re: " + subject(row), followup_letter(row, sender_name()), row["message_id"])
            rows[i] = {**row, "status": "повтор"}
        else:
            msg_id = send_one(row["email"], subject(row), letter(row, sender_name()))
            rows[i] = {**row, "status": "отправлено", "sent_at": date.today().isoformat(), "message_id": msg_id}
        write_rows(path, rows, delimiter)  # статус сохраняется сразу — обрыв не приведёт к дублю
        print(f"✓ {row['email']}  {company_name(row['name'])}")
        today = ru_date(date.today().isoformat())
        sheet.mark(row["email"], f"повтор {today}" if followup else f"рассылка {today}")
    return len(todo)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("preview", "test", "send", "followup", "sync"):
        sys.exit(__doc__)
    command, path = sys.argv[1], sys.argv[2]
    rows, delimiter = read_rows(path)
    if command == "sync":
        sent = [r for r in rows if r["status"] in ("отправлено", "повтор") and r["sent_at"]]
        print(f"Отправленных в {path}: {len(sent)}")
        sheet = Sheet()
        for r in sent:
            sheet.mark(r["email"], f"рассылка {ru_date(r['sent_at'])}")
        return
    active = [r for r in rows if r["status"] not in STOP_STATUSES]
    letters = [(r, letter(r, sender_name())) for r in active]  # ошибки в нишах/датах — до отправки

    if command == "preview":
        for row, body in letters:
            print(f"Кому: {row['email']}\nТема: {subject(row)}\n\n{body}\n{'─' * 60}")
    elif command == "test":
        row, body = letters[0]
        _, user, _ = smtp_config()
        to = os.environ.get("TEST_TO", user)
        send_one(to, "[ТЕСТ] " + subject(row), body)
        print(f"Тестовое письмо отправлено на {to}")
    else:
        run_batch(path, rows, delimiter, followup=command == "followup")


if __name__ == "__main__":
    main()
