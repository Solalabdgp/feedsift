"""CRM-эндпоинты: воронка, переходы состояний, статистика.

Требуют настоящий Postgres (TEST_DATABASE_URL) — схема опирается на JSONB,
ARRAY и ON CONFLICT, и проверять её на чём-то другом означало бы проверять
не тот SQL, который поедет в прод. Без переменной модуль пропускается целиком.
"""
import pytest

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


# ------------------------------------------------------------------- список


async def test_list_returns_not_contacted_by_default(seeded) -> None:
    client, ids = seeded
    body = (await client.get("/api/leads")).json()

    assert body["total"] == 3
    assert len(body["items"]) == 3
    # Нет строки в lead_crm = «не написал». Заводить её заранее не нужно.
    assert {item["contact_state"] for item in body["items"]} == {"not_contacted"}
    assert body["items"][0]["source"] == "alpha"
    # Новые сверху.
    assert [item["id"] for item in body["items"]] == sorted(ids, reverse=True)


async def test_pagination(seeded) -> None:
    client, _ = seeded
    page = (await client.get("/api/leads", params={"limit": 2, "offset": 0})).json()
    assert len(page["items"]) == 2
    assert page["total"] == 3  # total — про весь срез, не про страницу

    tail = (await client.get("/api/leads", params={"limit": 2, "offset": 2})).json()
    assert len(tail["items"]) == 1
    assert {i["id"] for i in page["items"]}.isdisjoint({i["id"] for i in tail["items"]})


async def test_unknown_filter_is_rejected(seeded) -> None:
    client, _ = seeded
    assert (await client.get("/api/leads", params={"filter": "выдумка"})).status_code == 422


async def test_detail_includes_raw_item(seeded) -> None:
    client, ids = seeded
    body = (await client.get(f"/api/leads/{ids[0]}")).json()

    assert body["id"] == ids[0]
    assert body["raw_item"]["text"].startswith("Текст записи")
    assert body["raw_item"]["url"].startswith("https://example.test/")
    assert body["llm_summary_ru"].startswith("Пересказ")
    assert body["crm"] is None


async def test_detail_404(seeded) -> None:
    client, _ = seeded
    assert (await client.get("/api/leads/999999")).status_code == 404


# -------------------------------------------------------------- переходы


async def test_contact_then_response_then_earnings(seeded) -> None:
    client, ids = seeded
    lead = ids[0]

    contact = await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
    assert contact.status_code == 200
    assert contact.json()["contact_state"] == "contacted"
    assert contact.json()["contacted_at"] is not None

    reply = await client.patch(
        f"/api/leads/{lead}/response",
        json={"response_text": "Ответил, обсуждаем объём", "taken_in_work": True},
    )
    assert reply.status_code == 200
    assert reply.json()["taken_in_work"] is True
    assert reply.json()["in_work_at"] is not None

    money = await client.patch(
        f"/api/leads/{lead}/earnings", json={"earnings": 450.5, "currency": "EUR"}
    )
    assert money.status_code == 200
    assert money.json()["earnings"] == "450.50"
    assert money.json()["currency"] == "EUR"


async def test_response_requires_contacted(seeded) -> None:
    client, ids = seeded
    response = await client.patch(
        f"/api/leads/{ids[0]}/response",
        json={"response_text": "ответ", "taken_in_work": True},
    )
    assert response.status_code == 400


async def test_response_after_postponed_is_rejected(seeded) -> None:
    client, ids = seeded
    await client.patch(f"/api/leads/{ids[0]}/contact", json={"contact_state": "postponed"})
    response = await client.patch(
        f"/api/leads/{ids[0]}/response",
        json={"response_text": "ответ", "taken_in_work": True},
    )
    assert response.status_code == 400


async def test_earnings_require_taken_in_work(seeded) -> None:
    client, ids = seeded
    lead = ids[0]
    await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
    await client.patch(
        f"/api/leads/{lead}/response", json={"response_text": "отказ", "taken_in_work": False}
    )

    response = await client.patch(f"/api/leads/{lead}/earnings", json={"earnings": 100})
    assert response.status_code == 400


async def test_negative_earnings_rejected(seeded) -> None:
    client, ids = seeded
    response = await client.patch(f"/api/leads/{ids[0]}/earnings", json={"earnings": -1})
    assert response.status_code == 422


async def test_bad_currency_rejected(seeded) -> None:
    client, ids = seeded
    response = await client.patch(
        f"/api/leads/{ids[0]}/earnings", json={"earnings": 10, "currency": "рубли"}
    )
    assert response.status_code == 422


async def test_unwork_with_earnings_is_refused(seeded) -> None:
    """Снять «взят в работу» с проставленным заработком нельзя: это стёрло бы
    единственное место, где сумма хранится."""
    client, ids = seeded
    lead = ids[0]
    await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
    await client.patch(
        f"/api/leads/{lead}/response", json={"response_text": "да", "taken_in_work": True}
    )
    await client.patch(f"/api/leads/{lead}/earnings", json={"earnings": 100})

    response = await client.patch(
        f"/api/leads/{lead}/response", json={"response_text": "передумал", "taken_in_work": False}
    )
    assert response.status_code == 400


async def test_contacted_at_is_not_moved_by_repeated_contact(seeded) -> None:
    client, ids = seeded
    lead = ids[0]
    first = (
        await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
    ).json()["contacted_at"]

    await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "postponed"})
    again = (
        await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
    ).json()["contacted_at"]

    assert again == first


async def test_contact_state_not_contacted_is_rejected(seeded) -> None:
    client, ids = seeded
    response = await client.patch(
        f"/api/leads/{ids[0]}/contact", json={"contact_state": "not_contacted"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- фильтры


async def test_filters(seeded) -> None:
    client, ids = seeded
    await client.patch(f"/api/leads/{ids[0]}/contact", json={"contact_state": "contacted"})
    await client.patch(
        f"/api/leads/{ids[0]}/response", json={"response_text": "да", "taken_in_work": True}
    )
    await client.patch(f"/api/leads/{ids[1]}/contact", json={"contact_state": "postponed"})

    async def ids_for(name: str) -> set[int]:
        body = (await client.get("/api/leads", params={"filter": name})).json()
        return {item["id"] for item in body["items"]}

    assert await ids_for("contacted") == {ids[0]}
    assert await ids_for("postponed") == {ids[1]}
    assert await ids_for("not_contacted") == {ids[2]}
    assert await ids_for("in_work") == {ids[0]}
    assert await ids_for("no_deal") == set()
    assert await ids_for("all") == set(ids)


# ----------------------------------------------------------- мягкое удаление


async def test_soft_delete_hides_lead_everywhere(seeded) -> None:
    client, ids = seeded
    lead = ids[0]
    assert (await client.delete(f"/api/leads/{lead}")).status_code == 204

    body = (await client.get("/api/leads")).json()
    assert body["total"] == 2
    assert lead not in {item["id"] for item in body["items"]}
    # И не возвращается как «не написал» ни в одном срезе.
    not_contacted = (await client.get("/api/leads", params={"filter": "not_contacted"})).json()
    assert lead not in {item["id"] for item in not_contacted["items"]}
    assert (await client.get(f"/api/leads/{lead}")).status_code == 404


async def test_delete_is_idempotent(seeded) -> None:
    client, ids = seeded
    assert (await client.delete(f"/api/leads/{ids[0]}")).status_code == 204
    assert (await client.delete(f"/api/leads/{ids[0]}")).status_code == 204


async def test_delete_unknown_lead_404(seeded) -> None:
    client, _ = seeded
    assert (await client.delete("/api/leads/999999")).status_code == 404


async def test_patch_on_deleted_lead_404(seeded) -> None:
    client, ids = seeded
    await client.delete(f"/api/leads/{ids[0]}")
    response = await client.patch(
        f"/api/leads/{ids[0]}/contact", json={"contact_state": "contacted"}
    )
    assert response.status_code == 404


# ------------------------------------------------------------------ статистика


async def test_stats(seeded) -> None:
    client, ids = seeded
    await client.patch(f"/api/leads/{ids[0]}/contact", json={"contact_state": "contacted"})
    await client.patch(
        f"/api/leads/{ids[0]}/response", json={"response_text": "да", "taken_in_work": True}
    )
    await client.patch(f"/api/leads/{ids[0]}/earnings", json={"earnings": 300})
    await client.patch(f"/api/leads/{ids[1]}/contact", json={"contact_state": "postponed"})

    stats = (await client.get("/api/stats")).json()
    assert stats["total_leads"] == 3
    assert stats["contacted"] == 1
    assert stats["postponed"] == 1
    assert stats["not_contacted"] == 1
    assert stats["in_work"] == 1
    assert stats["no_deal"] == 0
    assert stats["removed"] == 0
    assert stats["earnings_total"] == [{"currency": "USD", "amount": "300.00"}]


async def test_stats_keep_earnings_of_deleted_leads(seeded) -> None:
    """Убрать лид из рабочего списка — не то же самое, что отменить деньги."""
    client, ids = seeded
    lead = ids[0]
    await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
    await client.patch(
        f"/api/leads/{lead}/response", json={"response_text": "да", "taken_in_work": True}
    )
    await client.patch(f"/api/leads/{lead}/earnings", json={"earnings": 300})
    await client.delete(f"/api/leads/{lead}")

    stats = (await client.get("/api/stats")).json()
    assert stats["total_leads"] == 2
    assert stats["removed"] == 1
    assert stats["in_work"] == 0  # удалённый выпал из воронки
    assert stats["earnings_total"] == [{"currency": "USD", "amount": "300.00"}]


async def test_stats_group_earnings_by_currency(seeded) -> None:
    client, ids = seeded
    for lead, amount, currency in ((ids[0], 100, "USD"), (ids[1], 200, "EUR")):
        await client.patch(f"/api/leads/{lead}/contact", json={"contact_state": "contacted"})
        await client.patch(
            f"/api/leads/{lead}/response", json={"response_text": "да", "taken_in_work": True}
        )
        await client.patch(
            f"/api/leads/{lead}/earnings", json={"earnings": amount, "currency": currency}
        )

    stats = (await client.get("/api/stats")).json()
    assert stats["earnings_total"] == [
        {"currency": "EUR", "amount": "200.00"},
        {"currency": "USD", "amount": "100.00"},
    ]


# ------------------------------------------- фильтр по доставке в Telegram


async def test_only_delivered_leads_are_listed(seeded, hidden_ids) -> None:
    """В matches лежит весь поток пайплайна; дашборд — только то, что дошло.

    На проде это 531 notified против 7244 suppressed: без фильтра в список
    попадал весь поток.
    """
    client, ids = seeded
    assert hidden_ids, "фикстура обязана завести не-notified матчи"

    body = (await client.get("/api/leads", params={"limit": 200})).json()
    listed = {item["id"] for item in body["items"]}

    assert listed == set(ids)
    assert body["total"] == len(ids)
    assert listed.isdisjoint(hidden_ids)


async def test_suppressed_lead_is_not_reachable_by_id(seeded, hidden_ids) -> None:
    """Прямая ссылка на отсеянный лид — 404, а не показ его карточки."""
    client, _ = seeded
    for hidden in hidden_ids:
        assert (await client.get(f"/api/leads/{hidden}")).status_code == 404


async def test_suppressed_lead_cannot_be_mutated(seeded, hidden_ids) -> None:
    """Иначе отсеянный лид правился бы через PATCH по угаданному id."""
    client, _ = seeded
    hidden = hidden_ids[0]

    assert (
        await client.patch(f"/api/leads/{hidden}/contact", json={"contact_state": "contacted"})
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/leads/{hidden}/response",
            json={"response_text": "x", "taken_in_work": True},
        )
    ).status_code == 404
    assert (
        await client.patch(f"/api/leads/{hidden}/earnings", json={"earnings": 1})
    ).status_code == 404
    assert (await client.delete(f"/api/leads/{hidden}")).status_code == 404


async def test_stats_count_only_delivered(seeded, hidden_ids) -> None:
    client, ids = seeded
    stats = (await client.get("/api/stats")).json()

    assert stats["total_leads"] == len(ids)
    assert stats["not_contacted"] == len(ids)
