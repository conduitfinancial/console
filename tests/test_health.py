from __future__ import annotations

import httpx

from app.main import create_app


async def client():
    app = create_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_live_is_always_ok():
    async with await client() as c:
        response = await c.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_live_body_names_no_environment():
    # Pin: the body is exactly {"status": "ok"} — no environment leak to an
    # unauthenticated caller.
    async with await client() as c:
        response = await c.get("/health/live")
    assert response.json() == {"status": "ok"}


async def test_ready_checks_the_database():
    async with await client() as c:
        response = await c.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
