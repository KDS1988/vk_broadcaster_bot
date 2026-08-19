"""
vk_broadcast_bot.py
--------------------
Источник правды — лист "Calendar" гугл-таблицы (тот же, что уже питает
fhm-vmix пайплайн). Колонки: День|Дата|Тур|Время|Год|Пара|Наименование|Адрес.
Бот берёт строки за дату TARGET_DATE (формат ДД.ММ.ГГГГ, как в колонке
"Дата") без заполненного "Ключа", создаёт трансляцию в VK-сообществе через
обычный веб-интерфейс (закрытый Live API не используется), и пишет ключ +
ссылку обратно в колонки I/J той же строки.

Название трансляции:  "Время | Пара | Дата"   (например "10:00 | ЦСКА - Динамо | 06.09.2025")
Обложка: файл bg.png рядом со скриптом — если файла нет, трансляция создаётся без обложки.
Плейлист по году — ПОКА НЕ РЕАЛИЗОВАНО, см. TODO внизу файла.

--------------------------------------------------------------------
ЧТО ЖИВЬЁМ ПРОВЕРЕНО (создано 5 реальных трансляций в процессе разработки):
  - клик "Добавить" -> "Начать трансляцию" -> "Приложение" -> "Продолжить"
  - вкладка "Информация": поле названия, категория "Спорт", кнопка "Далее"
  - вкладка "Настройки": кнопка "Создать трансляцию"
  - после создания VK редиректит на vkvideo.ru/live-<id>_<video_id> —
    это и есть ссылка на трансляцию
  - на этой странице два инпута: URL сервера (всегда один и тот же,
    rtmp://vsu.mycdn.me/input/) и Ключ трансляции (уникальный)

ЧТО НЕ ПРОВЕРЕНО ЖИВЬЁМ (реализовано по интерфейсу, но не протестировано):
  - загрузка обложки (set_input_files) — при первом боевом запуске
    проверь на 1 матче с реальным bg.png, прежде чем доверять пачке
  - установка времени начала трансляции (поле "Начало трансляции" на
    вкладке "Настройки") — сейчас закомментировано, трансляция стартует
    сразу по факту создания ("Сейчас"); включай и проверяй сам
  - плейлист по году — интерфейс создания трансляции не содержит поля
    "Плейлист", им придётся заниматься отдельным шагом после создания
    (открыть плейлист / видео и привязать вручную или через ещё не
    найденный элемент интерфейса) — см. TODO в конце файла
--------------------------------------------------------------------

Установка:
    pip install -r requirements.txt
    playwright install chromium

Запуск (локально, для теста):
    TARGET_DATE=06.09.2025 SPREADSHEET_ID=... python vk_broadcast_bot.py
"""
import asyncio
import logging
import os
import random
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

# ---------------------------------------------------------------- НАСТРОЙКИ

STATE_FILE = os.environ.get("VK_STATE_PATH", "vk_storage_state.json")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Calendar")
TARGET_DATE = os.environ["TARGET_DATE"]  # формат ДД.ММ.ГГГГ, как в колонке "Дата" — это и есть "кнопка": какую дату передашь, те матчи и создаст

COMMUNITY_URL = "https://vkvideo.ru/@club226850050/lives"
COMMUNITY_OWNER_ID = "-226850050"  # ID сообщества (с минусом) — используется в deep-link ниже
COVER_FILE = Path(__file__).parent / "bg.png"

# Колонки листа Calendar (A=1)
COL_DAY = 1        # День
COL_DATE = 2       # Дата
COL_ROUND = 3      # Тур
COL_TIME = 4       # Время
COL_YEAR = 5       # Год
COL_MATCH = 6      # Пара
COL_VENUE = 7      # Наименование
COL_ADDRESS = 8    # Адрес
COL_KEY_OUT = 9    # I — сюда пишем ключ
COL_LINK_OUT = 10  # J — сюда пишем ссылку

MIN_DELAY_SEC = 4
MAX_DELAY_SEC = 10
MAX_RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vk_bot")


# ---------------------------------------------------------------- GOOGLE SHEETS

def open_sheet():
    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)


def load_pending_rows(ws):
    all_values = ws.get_all_values()
    pending = []
    for idx, row in enumerate(all_values[1:], start=2):
        row = row + [""] * max(0, COL_LINK_OUT - len(row))
        if not row[COL_MATCH - 1].strip():
            continue
        if row[COL_DATE - 1].strip() != TARGET_DATE:
            continue
        if row[COL_KEY_OUT - 1].strip():
            continue  # уже обработана
        pending.append({
            "row_index": idx,
            "date": row[COL_DATE - 1].strip(),
            "time": row[COL_TIME - 1].strip(),
            "year": row[COL_YEAR - 1].strip(),
            "match": row[COL_MATCH - 1].strip(),
        })
    return pending


def build_title(m: dict) -> str:
    return f"{m['time']} | {m['match']} | {m['date']}"


def write_result(ws, row_index: int, key: str, link: str):
    ws.update_cell(row_index, COL_KEY_OUT, key)
    ws.update_cell(row_index, COL_LINK_OUT, link)


# ---------------------------------------------------------------- VK FLOW

async def create_broadcast(page, title: str) -> dict:
    # Идём сразу по прямой ссылке с явным ID сообщества, а не через клик
    # "Добавить" -> "Начать трансляцию" — на странице минимум две кнопки
    # с текстом "Добавить" (своя у сообщества и общая в шапке сайта,
    # которая ведёт на личный профиль), и в автоматическом запуске
    # клик иногда попадал не туда. Этот URL — то, куда сам VK
    # переходит после клика "Начать трансляцию" (подсмотрено в адресной
    # строке во время ручного теста), так что chosenOwnerId однозначно
    # фиксирует нужное сообщество.
    live_flow_url = f"{COMMUNITY_URL}?chosenOwnerId={COMMUNITY_OWNER_ID}&z=onboarding_live_flow"
    await page.goto(live_flow_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1500)

    try:
        await page.get_by_text("Приложение", exact=True).click()
        await page.get_by_role("button", name="Продолжить").click()

        await page.get_by_placeholder("Например, «Смотрю фильмы всю ночь»").fill(title)
        await page.get_by_role("tab", name="Спорт").click()
    except Exception:
        debug_path = f"debug_error_{int(asyncio.get_event_loop().time())}.png"
        try:
            await page.screenshot(path=debug_path)
            log.error("Шаг создания не прошёл — скриншот сохранён в %s", debug_path)
        except Exception:
            pass
        raise

    # Обложка — best-effort, не проверено живьём (см. шапку файла)
    if COVER_FILE.exists():
        try:
            file_input = page.locator('input[type="file"]')
            await file_input.set_input_files(str(COVER_FILE))
            await page.wait_for_timeout(1000)
        except Exception as e:
            log.warning("Не удалось загрузить обложку (%s), продолжаю без неё", e)

    # TODO (не проверено живьём): установка времени начала трансляции.
    # Поле "Начало трансляции" на вкладке "Настройки" — сейчас не трогаем,
    # трансляция стартует по факту создания ("Сейчас"). Раскомментируй и
    # проверь на 1 матче, прежде чем полагаться на это в бою:
    #
    # await page.get_by_text("Настройки", exact=True).click()
    # start_field = page.locator('text=Начало трансляции').locator('xpath=following::input[1]')
    # await start_field.fill(f"{scheduled_dt:%d.%m.%Y %H:%M}")

    await page.get_by_role("button", name="Далее").click()
    await page.get_by_role("button", name="Создать трансляцию").click()

    await page.wait_for_url("**/live-*", timeout=15000)
    await page.wait_for_timeout(1500)

    rtmp_url = await page.locator('text=URL сервера').locator('xpath=following::input[1]').input_value()
    stream_key = await page.locator('text=Ключ трансляции').locator('xpath=following::input[1]').input_value()
    link = page.url.split("?")[0]

    return {"rtmp_url": rtmp_url, "stream_key": stream_key, "link": link}


# ---------------------------------------------------------------- ОСНОВНОЙ ЦИКЛ

async def run():
    ws = open_sheet()
    pending = load_pending_rows(ws)
    log.info("Дата %s: к обработке %d строк", TARGET_DATE, len(pending))

    if not pending:
        log.info("Нет строк для обработки — либо всё уже создано, либо на эту дату нет матчей.")
        return

    async with async_playwright() as p:
        # headless=False — все наши успешные тесты создания трансляций были
        # в видимом окне; headless-Chromium VK может показывать иначе
        # (антибот-проверку вместо обычной формы). Раннер — эта же машина
        # с активной GUI-сессией, так что видимое окно во время
        # автозапуска — нормально.
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=STATE_FILE)

        for m in pending:
            title = build_title(m)
            result = None
            for attempt in range(1, MAX_RETRIES + 1):
                page = await context.new_page()
                try:
                    result = await create_broadcast(page, title)
                    break
                except Exception as e:
                    log.warning("Строка %s, попытка %d: %s", m["row_index"], attempt, e)
                    result = None
                finally:
                    await page.close()
                await asyncio.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))

            if result:
                write_result(ws, m["row_index"], result["stream_key"], result["link"])
                log.info("Строка %s: ок — %s", m["row_index"], result["link"])
            else:
                log.error("Строка %s: не удалось создать трансляцию после %d попыток", m["row_index"], MAX_RETRIES)

            await asyncio.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(run())

# ---------------------------------------------------------------- TODO: ПЛЕЙЛИСТЫ
# Форма создания трансляции не содержит поля "Плейлист" (проверено живьём).
# Обнаружено: "Добавить" -> "Создать плейлист" открывает отдельный диалог
# для создания ПУСТОГО плейлиста (название + обложка) — это не то же самое,
# что привязка уже созданной трансляции к плейлисту. Похоже, привязка
# происходит отдельным действием на странице самого видео/трансляции
# (или в общем списке "Видео" сообщества) — этот флоу ещё не найден и не
# реализован. Следующий шаг: вручную пройти этот флоу в браузере, найти
# соответствующий запрос/кнопку, и добавить как отдельный шаг после
# create_broadcast() — например assign_to_playlist(page, year).
