"""
capture_session.py
-------------------
Разовый (и по мере протухания сессии — повторяемый) шаг: логинишься в VK
руками в открывшемся окне браузера — включая 2FA/капчу/подтверждение
устройства, если VK попросит — и скрипт сохраняет cookies + localStorage
в vk_storage_state.json.

Дальше vk_broadcast_bot.py просто подгружает этот файл и работает уже
залогиненным, без единого запроса пароля.

Запускать с обычного ПК/сервера, с которого вы планируете гонять бота
дальше — и желательно с тем же стабильным IP, чтобы VK не считал сессию
подозрительной и не запрашивал повторное подтверждение при каждом запуске.

Установка (один раз):
    pip install playwright
    playwright install chromium
"""
import asyncio
from playwright.async_api import async_playwright

STATE_FILE = "vk_storage_state.json"

# TODO: подставь реальный адрес раздела трансляций своего сообщества.
# Обычно это что-то вроде https://vk.com/video?act=live_settings&gid=<ID сообщества без минуса>
COMMUNITY_LIVE_URL = "https://vk.com/video?act=live_settings&gid=YOUR_GROUP_ID"


async def main():
    async with async_playwright() as p:
        # headless=False — специально, чтобы можно было руками пройти логин/2FA/капчу
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://vk.com")
        print("1. Залогинься в VK в открывшемся окне (включая любые проверки, если попросит).")
        input("   Когда увидишь свою ленту/профиль — вернись сюда и нажми Enter...")

        await page.goto(COMMUNITY_LIVE_URL)
        print("2. Проверь, что открылась именно страница управления трансляциями сообщества.")
        input("   Если всё ок — нажми Enter, чтобы сохранить сессию...")

        await context.storage_state(path=STATE_FILE)
        print(f"Готово: сессия сохранена в {STATE_FILE}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
