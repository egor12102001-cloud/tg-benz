"""
Диагностика gdebenz.ru — запускать на сервере где сайт доступен.
Перехватывает все сетевые запросы и сохраняет в debug_output.json

Запуск:
    python3 debug_site.py
"""
import asyncio
import json
from playwright.async_api import async_playwright

TARGET_URL = "https://gdebenz.ru/aleksandrov"

async def main():
    print(f"Открываю {TARGET_URL} ...")

    requests_log = []
    responses_log = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = await ctx.new_page()

        # Логируем все запросы
        def on_request(req):
            requests_log.append({
                "method": req.method,
                "url": req.url,
                "headers": dict(req.headers),
            })
            print(f"  → {req.method} {req.url}")

        # Логируем все ответы
        async def on_response(resp):
            ct = resp.headers.get("content-type", "")
            entry = {
                "status": resp.status,
                "url": resp.url,
                "content_type": ct,
                "body": None,
            }
            if resp.status == 200:
                try:
                    if "json" in ct:
                        entry["body"] = await resp.json()
                        print(f"  ← {resp.status} JSON  {resp.url}")
                    elif "javascript" in ct or "text" in ct:
                        text = await resp.text()
                        entry["body"] = text[:5000]  # первые 5000 символов
                        print(f"  ← {resp.status} TEXT  {resp.url[:80]}")
                except Exception as e:
                    entry["body"] = f"ERROR reading body: {e}"
            else:
                print(f"  ← {resp.status}      {resp.url[:80]}")
            responses_log.append(entry)

        page.on("request", on_request)
        page.on("response", on_response)

        print("Ждём загрузки страницы (networkidle)...")
        await page.goto(TARGET_URL, wait_until="networkidle", timeout=40000)
        print("Ждём ещё 5 секунд для загрузки карты...")
        await asyncio.sleep(5)

        html = await page.content()
        await browser.close()

    # Сохраняем результаты
    output = {
        "url": TARGET_URL,
        "html_preview": html[:3000],
        "requests": requests_log,
        "responses": [r for r in responses_log if r["body"] is not None],
    }

    with open("debug_output.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print(f"Всего запросов: {len(requests_log)}")
    print(f"Ответов с телом: {len(output['responses'])}")
    print("=" * 60)
    print("\nJSON-ответы от API:")
    for r in output["responses"]:
        if "json" in r.get("content_type", "") and r["body"]:
            print(f"\n  URL: {r['url']}")
            body_str = json.dumps(r["body"], ensure_ascii=False)
            print(f"  Данные: {body_str[:500]}")

    print("\nРезультат сохранён в debug_output.json")
    print("Отправьте этот файл разработчику для анализа.")

if __name__ == "__main__":
    asyncio.run(main())
