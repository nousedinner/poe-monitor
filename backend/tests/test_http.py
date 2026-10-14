import time
from unittest.mock import Mock
import pytest
from app.poe_api import PoeAPI


class Response:
    def __init__(self, status=200, result=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.data = {"result": result or [], "id": "q", "total": len(result or [])}

    def json(self):
        return self.data


def test_upstream_401_is_not_retried_or_refreshed(context):
    api = PoeAPI("synthetic-cookie")
    api.session.request = Mock(return_value=Response(401))
    with pytest.raises(RuntimeError, match="401"):
        api.search_account_items("S30赛季", "account")
    assert api.session.request.call_count == 1
    api.close()


def test_transient_status_and_429_retry_after_shared_delay(context, monkeypatch):
    api = PoeAPI("synthetic-cookie")
    api.session.request = Mock(
        side_effect=[Response(503), Response(429, headers={"Retry-After": "2"}), Response()]
    )
    waits = []
    postpones = []
    monkeypatch.setattr(api, "wait", waits.append)
    monkeypatch.setattr(api.pacer, "postpone", postpones.append)
    api.search_account_items("S30赛季", "account")
    assert api.session.request.call_count == 3 and waits == [1, 2] and postpones == [2]
    api.close()


def test_global_100_cap_is_not_extended(context):
    api = PoeAPI("synthetic-cookie")
    api.session.request = Mock(return_value=Response(result=[str(i) for i in range(200)]))
    response = api.search_global_items("S30赛季", {"type": "Sword"})
    assert len(response["item_ids"]) == 100 and response["total"] == 200
    api.close()


def test_deadline_prevents_network_request(context):
    api = PoeAPI("synthetic-cookie")
    api.deadline = time.monotonic() - 1
    api.session.request = Mock()
    with pytest.raises(TimeoutError):
        api.search_account_items("S30赛季", "account")
    api.session.request.assert_not_called()
    api.close()


def test_fetch_ids_and_query_are_encoded(context):
    api = PoeAPI("synthetic-cookie")
    api.session.request = Mock(return_value=Response())
    api.fetch_items_batch(["id?x=1"], "q&extra=1")
    assert "id%3Fx%3D1?query=q%26extra%3D1" in api.session.request.call_args.args[1]
    api.close()
