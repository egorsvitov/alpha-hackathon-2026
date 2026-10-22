import asyncio
import sys
from pathlib import Path

# Добавляем путь к backend и корню репозитория в sys.path
backend_dir = Path(__file__).resolve().parent
repo_root = backend_dir.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(backend_dir))

import httpx
from main import app


async def run_e2e_tests() -> None:
    print("=== Начало E2E проверки Backend сервиса ===")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Проверка корневого эндпоинта
        r = await client.get("/")
        assert r.status_code == 200, f"Ошибка healthcheck: {r.status_code}"
        print("[OK] Healthcheck root эндпоинт доступен")

        # 2. Выбор компании
        r = await client.get("/api/v1/companies")
        assert r.status_code == 200, f"Ошибка /companies: {r.status_code}"
        companies = r.json()
        assert len(companies) >= 3, "Ожидалось не менее 3 профилей компаний"
        print(f"[OK] Профили компаний получены ({len(companies)} шт.): {[c['company_id'] for c in companies]}")

        target_company = companies[0]["company_id"]

        # 3. Получение персонализированной ленты под разные роли
        for role in ["cfo", "ceo", "treasurer"]:
            r = await client.get(
                f"/api/v1/companies/{target_company}/news-feed?role={role}"
            )
            assert r.status_code == 200, f"Ошибка /news-feed для {role}: {r.status_code}"
            feed = r.json()
            assert len(feed) >= 6, f"Ожидалось не менее 6 событий в ленте, получено {len(feed)}"
            top_event = feed[0]
            print(
                f"[OK] Лента для роли {role}: {len(feed)} событий. Топ: «{top_event['event']['title']}» (score: {top_event['relevance']['score']})"
            )

        # 4. Открытие детальной карточки инсайта
        target_event = feed[0]["event"]["event_id"]
        r = await client.get(
            f"/api/v1/news/{target_event}/analysis?company_id={target_company}&role=cfo"
        )
        assert r.status_code == 200, f"Ошибка /analysis: {r.status_code}"
        insight = r.json()
        assert insight["status"] == "ok"
        assert len(insight["what_happened"]["facts"]) > 0
        assert len(insight["why_it_matters"]) > 0
        assert len(insight["risks"]) > 0
        assert len(insight["next_actions"]) > 0
        assert len(insight["evidence"]) > 0
        print(f"[OK] Карточка инсайта {insight['run_id']} сформирована корректно:")
        print(f"   - Доказательств/источников: {len(insight['evidence'])}")
        print(f"   - Действий ЛПР: {len(insight['next_actions'])}")
        if insight.get("bank_position"):
            print("   - Присутствует официальная позиция Альфа-Банка")

        # 5. Уточняющий вопрос ЛПР (Критерий №10)
        r = await client.post(
            "/api/v1/chat/ask",
            json={
                "company_id": target_company,
                "news_id": target_event,
                "role": "cfo",
                "run_id": insight["run_id"],
                "question": "Каковы риски изменения процентной ставки?",
            },
        )
        assert r.status_code == 200, f"Ошибка /chat/ask: {r.status_code}"
        qa_data = r.json()
        assert len(qa_data["text"]) > 0
        print("[OK] Уточняющий диалог ЛПР отработал:")
        print(f"   - Ответ: {qa_data['text'][:80]}...")
        print(f"   - Источники: {qa_data['evidence_ids']}")

        # 6. Добавление пользовательской внешней новости
        r = await client.post(
            "/api/v1/news",
            json={
                "title": "Новое регуляторное требование по раскрытию рисков",
                "text": "ЦБ опубликовал указание о правилах раскрытия чувствительной информации.",
            },
        )
        assert r.status_code == 201, f"Ошибка /news: {r.status_code}"
        uploaded_event = r.json()
        assert uploaded_event["user_uploaded"] is True
        print(f"[OK] Пользовательская новость добавлена с ID: {uploaded_event['event_id']}")

        # Проверка, что новость появилась в ленте
        r = await client.get(
            f"/api/v1/companies/{target_company}/news-feed?role=cfo"
        )
        updated_feed = r.json()
        assert any(
            item["event"]["event_id"] == uploaded_event["event_id"]
            for item in updated_feed
        ), "Загруженная новость не найдена в обновленной ленте"
        print("[OK] Загруженная новость мгновенно проиндексирована в ленте")

        # 7. Фидбек ЛПР
        r = await client.post(
            "/api/v1/feedback",
            json={"run_id": insight["run_id"], "useful": True},
        )
        assert r.status_code == 204, f"Ошибка /feedback: {r.status_code}"
        print("[OK] Фидбек успешно принят (204 No Content)")

        # 8. Метрики сервиса (Критерий №8)
        r = await client.get("/api/v1/metrics")
        assert r.status_code == 200, f"Ошибка /metrics: {r.status_code}"
        metrics = r.json()
        print(f"[OK] Метрики качества сервиса получены: {metrics}")

    print("\n=== Все сквозные E2E сценарии успешно пройдены! ===")


if __name__ == "__main__":
    asyncio.run(run_e2e_tests())
