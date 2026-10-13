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
