import httpx
import pytest

from market_monitor.http import HttpError, RobotsDisallowed


def test_retries_on_503_then_succeeds(make_http):
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="ok")

    client = make_http(handler, retries=3)
    assert client.get("https://a.example/x").text == "ok"
    assert len(calls) == 3


def test_gives_up_after_retries(make_http):
    client = make_http(lambda r: httpx.Response(500), retries=2, respect_robots_txt=False)
    with pytest.raises(HttpError, match="after 3 attempts"):
        client.get("https://a.example/x")


def test_does_not_retry_404_and_hides_query_in_error(make_http):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    client = make_http(handler, respect_robots_txt=False)
    with pytest.raises(HttpError) as err:
        client.get("https://a.example/x?api_key=SECRET")
    assert len(calls) == 1
    assert "SECRET" not in str(err.value)


def test_honours_retry_after(make_http):
    sleeps = []
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    client = make_http(lambda r: next(responses), respect_robots_txt=False)
    client._sleep = sleeps.append
    client.get("https://a.example/x")
    assert 7 in sleeps


def test_robots_txt_disallow_is_respected(make_http):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")

    client = make_http(handler)
    assert client.get("https://a.example/public").text == "ok"
    with pytest.raises(RobotsDisallowed):
        client.get("https://a.example/private/feed")


def test_per_host_throttle_sleeps(make_http):
    sleeps = []
    clock = iter([0.0, 0.1, 1.0])
    client = make_http(lambda r: httpx.Response(200), per_host_min_interval_s=1.0,
                       respect_robots_txt=False)
    client._sleep, client._clock = sleeps.append, lambda: next(clock)
    client.get("https://a.example/1")
    client.get("https://a.example/2")
    assert sleeps and sleeps[0] == pytest.approx(0.9)


def test_budget_stops_retries_and_new_requests(make_http):
    import time as _t

    from market_monitor.http import BudgetExceeded

    clock = [0.0]
    client = make_http(lambda r: httpx.Response(503), retries=5, backoff_s=10, respect_robots_txt=False)
    client._clock = lambda: clock[0]
    client._sleep = lambda s: clock.__setitem__(0, clock[0] + s)
    client.set_budget(15)
    with pytest.raises(BudgetExceeded):
        client.get("https://a.example/x")          # 1st retry waits 10 s, the 2nd (20 s) would overrun
    assert clock[0] == 10
    with pytest.raises(BudgetExceeded):
        client.get("https://a.example/y")          # budget left 5 s, the first retry won't fit
    client.set_budget(None)


def test_no_retry_on_429_for_flagged_hosts(make_http):
    calls = []
    client = make_http(lambda r: (calls.append(1), httpx.Response(429, headers={"Retry-After": "60"}))[1],
                       respect_robots_txt=False)
    client.no_retry_on_429("api.gdeltproject.org")
    with pytest.raises(HttpError) as err:
        client.get("https://api.gdeltproject.org/api/v2/doc/doc")
    assert err.value.status == 429 and len(calls) == 1
