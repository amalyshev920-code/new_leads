"""python test_outreach.py — проверка сборки писем без отправки и без настоящих адресов."""

from outreach import CONTACTS, NICHES, company_name, followup_letter, letter

assert company_name('ООО "НРСУ"') == "ООО «НРСУ»"
assert company_name("ООО «Промресурс»") == "ООО «Промресурс»"

row = {"name": "ООО «Пример»", "reg_date": "2026-09-01", "niche": "стройка", "email": "test@example.com"}
body = letter(row, "Артём")
assert body.startswith("Здравствуйте!\n\nУ ООО «Пример» пока нет сайта.")
assert "зарегистрирован" not in body  # про регистрацию в письме не пишем
assert "Заказчик, прежде чем доверить объект" in body and "/cases/grad-m" in body
assert "бесплатн" not in body.lower()   # бесплатного демо больше нет
for line in ("Сайт: https://bornweb.studio", "Telegram: https://t.me/BornWebStudio", "WhatsApp: https://wa.me/"):
    assert line in body and line in followup_letter(row, "Артём")

assert "ТФМ" in letter({**row, "niche": "логистика"}, "Артём")
assert "Сайта клиники у нас пока нет" in letter({**row, "niche": "медицина"}, "Артём")
assert "Сайта учебного центра у нас пока нет" in letter({**row, "niche": "образование"}, "Артём")

for n in NICHES:
    letter({**row, "niche": n}, "Артём")
try:
    letter({**row, "niche": "салоны"}, "Артём")
    raise AssertionError("неизвестная ниша должна падать")
except ValueError:
    pass

print("ok")
