#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leads — пять команд вместо десятка.

  Прогон (поиск новых компаний → Google-таблица):
    leads start       начать прогон сейчас
    leads schedule    прогон каждый день в 10:00 и при каждом включении Мака
    leads stop        отменить расписание и остановить идущий прогон

  Рассылка:
    leads test        проверка: одно тестовое письмо себе
    leads send        рассылка: повторы тем, кто молчит 5+ дней, и новые письма — до 10 за запуск

Настройки — config.json рядом со скриптом (образец: config.example.json).
Пароль приложения Gmail хранится в Связке ключей macOS: спрашивается один раз.
"""

import getpass, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
LABEL = "studio.bornweb.new-leads"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
KEYCHAIN = "bornweb-leads-smtp"
UID = os.getuid()


def load_config():
    if not CONFIG.exists():
        sys.exit(f"Нет {CONFIG.name}. Скопируйте config.example.json в config.json и впишите свои значения.")
    return json.loads(CONFIG.read_text())


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def pipeline_args(cfg):
    args = [sys.executable, str(HERE / "new_leads.py"), "--region", str(cfg["region"]), "--daily"]
    if cfg.get("sheet_id"):
        args += ["--sheet", cfg["sheet_id"]]
    if cfg.get("gis_city"):
        args += ["--gis", cfg["gis_city"]]
    return args


def launchctl(*a):
    return subprocess.run(["launchctl", *a], capture_output=True, text=True)


# ── прогон ───────────────────────────────────────────────────────────────────

def start(cfg):
    print("Прогон запущен. Займёт от пары минут до получаса; Ctrl+C — прервать (собранное сохранится).\n")
    sys.exit(subprocess.call(pipeline_args(cfg), cwd=HERE))


def schedule(cfg):
    items = "".join(f"<string>{a}</string>" for a in pipeline_args(cfg))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{items}</array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{HERE}/daily.log</string>
  <key>StandardErrorPath</key><string>{HERE}/daily.log</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict></plist>
""")
    launchctl("bootout", f"gui/{UID}/{LABEL}")          # если уже было — перезагружаем с новыми настройками
    r = launchctl("bootstrap", f"gui/{UID}", str(PLIST))
    if r.returncode:
        sys.exit(f"Не получилось включить расписание: {r.stderr.strip()}")
    print("Готово: прогон каждый день в 10:00 и при каждом включении Мака.\n"
          "Если в 10:00 Мак спал — прогон начнётся, как только он проснётся.\n"
          f"Журнал: {HERE}/daily.log · отменить: leads stop")


def stop(cfg):
    was = launchctl("print", f"gui/{UID}/{LABEL}").returncode == 0
    launchctl("bootout", f"gui/{UID}/{LABEL}")
    if PLIST.exists():
        PLIST.unlink()
    # прогон, запущенный руками через leads start, тоже останавливаем
    killed = subprocess.run(["pkill", "-f", str(HERE / "new_leads.py")]).returncode == 0
    if was or killed:
        print("Расписание отменено" + (", идущий прогон остановлен." if killed else ".") +
              "\nСобранное сохранено в кеше. Включить снова: leads schedule")
    else:
        print("Расписания не было, прогон не идёт — отменять нечего.")


# ── рассылка ─────────────────────────────────────────────────────────────────

def smtp_setup(cfg, force=False):
    """Почта и адрес для теста — в config.json, пароль — в Связке ключей."""
    if force or not cfg.get("smtp_user"):
        cfg["smtp_user"] = input("Gmail, с которого идёт рассылка: ").strip()
        save_config(cfg)
    if force or not cfg.get("test_to"):
        cfg["test_to"] = input("Куда прислать тестовое письмо (лучше свой ящик на mail.ru): ").strip()
        save_config(cfg)
    found = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", cfg["smtp_user"], "-w"],
                           capture_output=True, text=True)
    password = found.stdout.strip()
    if force or found.returncode or not password:
        print("Пароль приложения Gmail (16 букв, myaccount.google.com/apppasswords). Ввод не отображается.")
        password = getpass.getpass("Пароль: ").replace(" ", "")
        subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", cfg["smtp_user"],
                        "-w", password], check=True)
        print("Сохранено в Связке ключей — больше спрашивать не буду.\n")
    os.environ.update({"SMTP_USER": cfg["smtp_user"], "SMTP_PASSWORD": password,
                       "SMTP_HOST": cfg.get("smtp_host", "smtp.gmail.com"),
                       "SENDER_NAME": cfg.get("sender", "Артём"),
                       "SHEET_ID": cfg.get("sheet_id", ""), "GOOGLE_KEY": str(HERE / "google_key.json")})


def mail_lists(cfg):
    folder = Path(os.path.expanduser(cfg["mail_dir"]))
    paths = [folder / f for f in cfg["mail_lists"]]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        sys.exit(f"В {folder} нет файлов: {', '.join(missing)} — поправьте mail_lists в config.json")
    return paths


def test(cfg):
    smtp_setup(cfg, force="--reset" in sys.argv)
    import outreach
    for path in mail_lists(cfg):
        rows, _ = outreach.read_rows(str(path))
        row = next((r for r in rows if r["status"] not in outreach.STOP_STATUSES and r["email"].strip()), None)
        if row:
            break
    else:
        sys.exit("В списках нет ни одной строки для примера.")
    try:
        outreach.send_one(cfg["test_to"], "[ТЕСТ] " + outreach.subject(row), outreach.letter(row, outreach.sender_name()))
    except Exception as e:
        sys.exit(f"Письмо не ушло: {e}\nЕсли дело в пароле — leads test --reset")
    print(f"Тестовое письмо (на примере {outreach.company_name(row['name'])}) ушло на {cfg['test_to']}.\n"
          "Проверьте, что оно во «Входящих», а не в «Спаме». Если всё хорошо — leads send")


def send(cfg):
    smtp_setup(cfg)
    import outreach
    from datetime import date, timedelta
    # сначала общий план — чтобы «к отправке 2» по одному файлу не путало
    cutoff = (date.today() - timedelta(days=outreach.FOLLOWUP_AFTER_DAYS)).isoformat()
    plan, room = [], outreach.LIMIT_PER_RUN
    for followup in (True, False):
        for path in mail_lists(cfg):
            rows, _ = outreach.read_rows(str(path))
            n = sum(1 for r in rows if r["email"].strip() and (
                r["status"] == "отправлено" and r["sent_at"] <= cutoff if followup else not r["status"].strip()))
            n = min(n, room)
            if n:
                plan.append(f"{path.stem} — {n}" + (" (повтор)" if followup else ""))
                room -= n
    total = outreach.LIMIT_PER_RUN - room
    if total:
        print(f"Уйдёт писем: {total} ({', '.join(plan)}). Пауза между письмами ~4 мин, всего ~{total * 4} мин.\n")
    left, sheet, first = outreach.LIMIT_PER_RUN, None, True
    for followup in (True, False):            # сначала повторы — им уже пора, потом новые
        for path in mail_lists(cfg):
            if left <= 0:
                break
            rows, delim = outreach.read_rows(str(path))
            sheet = sheet or outreach.Sheet()
            n = outreach.run_batch(str(path), rows, delim, followup, limit=left, sheet=sheet, first=first)
            left -= n
            first = first and n == 0
    sent = outreach.LIMIT_PER_RUN - left
    if not sent:
        print("Отправлять некому: новых адресов нет, повторы ещё рано (через 5 дней после письма).")
    else:
        print(f"\nГотово: отправлено {sent}. Следующие 10 — снова leads send (лучше не чаще пары раз в день).")


COMMANDS = {"start": start, "schedule": schedule, "stop": stop, "test": test, "send": send}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    sys.path.insert(0, str(HERE))
    COMMANDS[sys.argv[1]](load_config())
