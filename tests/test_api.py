"""Tests for the HEM API client.

The client's whole job is to turn HTTP into a typed exception and to attach
the right headers, so that is what these cover.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.home_energy_manager.api import (
    HemAuthError,
    HemConflictError,
    HemConnectionError,
    HemNotFoundError,
    HemPermissionError,
    HemRateLimitError,
    HemResponseError,
    HomeEnergyManagerApi,
)

from .const import API_KEY, BASE_URL, HOST, PORT, SNAPSHOT_URL, STATUS, STATUS_URL


@pytest.fixture
async def api(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> AsyncGenerator[HomeEnergyManagerApi]:
    """Return a client wired to the mocked transport."""
    session = aioclient_mock.create_session(hass.loop)
    yield HomeEnergyManagerApi(session, host=HOST, port=PORT, api_key=API_KEY)
    await session.close()


async def test_base_url_follows_ssl_flag(hass: HomeAssistant) -> None:
    """The scheme comes from use_ssl, not from the port."""
    assert (
        HomeEnergyManagerApi(None, host=HOST, port=PORT, api_key=API_KEY).base_url
        == f"http://{HOST}:{PORT}"
    )
    assert (
        HomeEnergyManagerApi(
            None, host=HOST, port=PORT, api_key=API_KEY, use_ssl=True
        ).base_url
        == f"https://{HOST}:{PORT}"
    )


async def test_reads_send_the_bearer_key_and_no_idempotency_key(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """GETs are authenticated but must not claim to be idempotent mutations."""
    aioclient_mock.get(STATUS_URL, json=STATUS)

    assert await api.async_get_status() == STATUS

    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert "Idempotency-Key" not in headers


async def test_snapshot_and_command_use_their_documented_paths(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """Each read hits the endpoint named in the API docs."""
    aioclient_mock.get(SNAPSHOT_URL, json={"battery_soc": 42})
    aioclient_mock.get(f"{BASE_URL}/api/commands/abc-123", json={"state": "queued"})

    assert await api.async_get_snapshot() == {"battery_soc": 42}
    assert await api.async_get_command("abc-123") == {"state": "queued"}


@pytest.mark.parametrize(
    ("call", "path", "body"),
    [
        ("async_force_charge", "/api/control/force-charge", {"minutes": 30}),
        ("async_force_discharge", "/api/control/force-discharge", {"minutes": 30}),
    ],
)
async def test_force_posts_minutes_with_an_idempotency_key(
    api: HomeEnergyManagerApi,
    aioclient_mock: AiohttpClientMocker,
    call: str,
    path: str,
    body: dict[str, int],
) -> None:
    """A start carries whole minutes and the caller's key."""
    aioclient_mock.post(f"{BASE_URL}{path}", json={"command_id": "abc-123"})

    result = await getattr(api, call)(30, "key-1")

    assert result == {"command_id": "abc-123"}
    assert aioclient_mock.mock_calls[0][2] == body
    assert aioclient_mock.mock_calls[0][3]["Idempotency-Key"] == "key-1"


@pytest.mark.parametrize(
    ("call", "path"),
    [
        ("async_stop_force_charge", "/api/control/force-charge/stop"),
        ("async_stop_force_discharge", "/api/control/force-discharge/stop"),
    ],
)
async def test_stop_posts_no_body(
    api: HomeEnergyManagerApi,
    aioclient_mock: AiohttpClientMocker,
    call: str,
    path: str,
) -> None:
    """A stop takes no payload but is still keyed."""
    aioclient_mock.post(f"{BASE_URL}{path}", json={"command_id": "abc-123"})

    await getattr(api, call)("key-2")

    assert aioclient_mock.mock_calls[0][2] is None
    assert aioclient_mock.mock_calls[0][3]["Idempotency-Key"] == "key-2"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, HemAuthError),
        (403, HemPermissionError),
        (404, HemNotFoundError),
        (409, HemConflictError),
        (429, HemRateLimitError),
        (500, HemResponseError),
        (418, HemResponseError),
    ],
)
async def test_status_codes_map_to_typed_errors(
    api: HomeEnergyManagerApi,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[Exception],
) -> None:
    """Every documented failure gets its own exception type."""
    aioclient_mock.get(STATUS_URL, status=status, json={"error": "nope"})

    with pytest.raises(error):
        await api.async_get_status()


async def test_conflict_carries_the_command_id(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 409 names the command already running, in the message and the attribute."""
    aioclient_mock.get(
        STATUS_URL,
        status=409,
        json={"error": "Action already running", "command_id": "abc-123"},
    )

    with pytest.raises(HemConflictError) as caught:
        await api.async_get_status()

    assert caught.value.command_id == "abc-123"
    assert "abc-123" in str(caught.value)


async def test_conflict_without_a_command_id_is_left_alone(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """No id means no parenthetical clutter on the message."""
    aioclient_mock.get(STATUS_URL, status=409, json={"error": "No snapshot yet"})

    with pytest.raises(HemConflictError) as caught:
        await api.async_get_status()

    assert caught.value.command_id is None
    assert str(caught.value) == "No snapshot yet"


@pytest.mark.parametrize(
    ("header", "expected"),
    [({"Retry-After": "30"}, 30), ({"Retry-After": "soon"}, None), ({}, None)],
)
async def test_rate_limit_parses_retry_after(
    api: HomeEnergyManagerApi,
    aioclient_mock: AiohttpClientMocker,
    header: dict[str, str],
    expected: int | None,
) -> None:
    """Only a whole number of seconds is a usable hint."""
    aioclient_mock.get(
        STATUS_URL, status=429, json={"error": "Too many requests"}, headers=header
    )

    with pytest.raises(HemRateLimitError) as caught:
        await api.async_get_status()

    assert caught.value.retry_after == expected
    assert ("retry after" in str(caught.value)) is (expected is not None)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "from error"}, "from error"),
        ({"message": "from message"}, "from message"),
        ({"error": "wins", "message": "loses"}, "wins"),
        ({}, "HTTP 400"),
    ],
)
async def test_error_message_prefers_error_then_message(
    api: HomeEnergyManagerApi,
    aioclient_mock: AiohttpClientMocker,
    payload: dict[str, str],
    expected: str,
) -> None:
    """HEM uses both keys; fall back to the bare status code."""
    aioclient_mock.get(STATUS_URL, status=400, json=payload)

    with pytest.raises(HemResponseError) as caught:
        await api.async_get_status()

    assert expected in str(caught.value)


async def test_non_dict_payload_is_wrapped(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """A bare list still has to come back as a mapping."""
    aioclient_mock.get(STATUS_URL, json=[1, 2, 3])

    assert await api.async_get_status() == {"data": [1, 2, 3]}


async def test_non_json_error_body_is_truncated(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTML error page must not be pasted whole into the message."""
    aioclient_mock.get(STATUS_URL, status=502, text="x" * 500)

    with pytest.raises(HemResponseError) as caught:
        await api.async_get_status()

    assert len(str(caught.value)) < 250


@pytest.mark.parametrize("exc", [TimeoutError(), ClientError("boom")])
async def test_transport_failures_become_connection_errors(
    api: HomeEnergyManagerApi, aioclient_mock: AiohttpClientMocker, exc: Exception
) -> None:
    """A timeout and a refused connection are the same problem to the caller."""
    aioclient_mock.get(STATUS_URL, exc=exc)

    with pytest.raises(HemConnectionError) as caught:
        await api.async_get_status()

    assert BASE_URL in str(caught.value)


async def test_idempotency_keys_are_unique_and_the_right_length() -> None:
    """HEM requires 16-128 characters and a fresh key per new command."""
    keys = {HomeEnergyManagerApi.new_idempotency_key() for _ in range(100)}

    assert len(keys) == 100
    assert all(16 <= len(key) <= 128 for key in keys)
