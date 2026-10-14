import threading
import time

import pytest

from market_monitor import cli, pipeline
from market_monitor.collectors import CollectResult, Collector
from market_monitor.config import Config
from market_monitor.models import Item
from market_monitor.state import MAGIC, StateError, pack, unpack
from market_monitor.storage import Storage

KEY = "correct horse battery staple 42"


def make_db(path, now, n=3):
    with Storage(path) as s:
        for i in range(n):
            s.insert_item(Item(source="x", published_at=now, title=f"t{i}", url=f"https://x.test/{i}"), now)
        s.kv_set("override:digest_time", '"19:30"')


def test_roundtrip_encrypted(tmp_path, now):
    db, enc, out = tmp_path / "a.db", tmp_path / "s.db.enc", tmp_path / "b.db"
    make_db(db, now)
    pack(db, enc, KEY)
    blob = enc.read_bytes()
    assert blob.startswith(MAGIC) and b"https://x.test" not in blob  # nothing readable
    unpack(enc, out, KEY)
    with Storage(out) as s:
        assert s.count_items() == 3 and s.kv_get("override:digest_time") == '"19:30"'


def test_wrong_key_and_tampering_fail_safely(tmp_path, now):
    db, enc, out = tmp_path / "a.db", tmp_path / "s.db.enc", tmp_path / "b.db"
    make_db(db, now)
    pack(db, enc, KEY)
    with pytest.raises(StateError, match="wrong STATE_ENCRYPTION_KEY"):
        unpack(enc, out, "another key that is long enough")
    blob = bytearray(enc.read_bytes())
    blob[-5] ^= 1
    enc.write_bytes(bytes(blob))
    with pytest.raises(StateError):
        unpack(enc, out, KEY)
    assert not out.exists()  # a failed restore never leaves a database behind
    pack(db, enc, KEY)
    with pytest.raises(StateError, match="not set"):
        unpack(enc, out, None)


def test_short_key_and_corrupt_db_are_rejected(tmp_path, now):
    db = tmp_path / "a.db"
    make_db(db, now)
    with pytest.raises(StateError, match="at least 16"):
        pack(db, tmp_path / "x.enc", "short")
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(StateError):
        pack(bad, tmp_path / "y.enc", KEY)


def test_cli_state_requires_key(tmp_path, monkeypatch, now):
    db = tmp_path / "a.db"
    make_db(db, now)
    monkeypatch.delenv("STATE_ENCRYPTION_KEY", raising=False)
    assert cli.main(["--db", str(db), "state", "pack", "--file", str(tmp_path / "s.enc")]) == 2
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", KEY)
    assert cli.main(["--db", str(db), "state", "pack", "--file", str(tmp_path / "s.enc")]) == 0
    assert cli.main(["--db", str(tmp_path / "r.db"), "state", "unpack", "--file", str(tmp_path / "s.enc")]) == 0


# ---- parallel collection ------------------------------------------------------

class Slow(Collector):
    def __init__(self, name, delay, items=1, fail=False):
        super().__init__(None)
        self.name, self.delay, self.n, self.fail = name, delay, items, fail
        self.thread = None

    def collect(self, ctx):
        self.thread = threading.current_thread().name
        ctx.state.kv_set(f"seen:{self.name}", "1")
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return CollectResult(items=[
            Item(source=self.name, published_at=ctx.now, title=f"Story {self.name.upper()}{i} from source",
                 url=f"https://{self.name}.test/{i}") for i in range(self.n)])


def test_collectors_run_in_parallel_with_deterministic_results(storage, secrets, make_http, now):
    collectors = [Slow("a", 0.3, 2), Slow("b", 0.3, 1, fail=True), Slow("c", 0.3, 3)]
    start = time.monotonic()
    report = pipeline.collect(Config(), secrets, storage, make_http(lambda r: None), now, collectors)
    assert time.monotonic() - start < 0.8                 # ~0.3 s, not 0.9 s
    assert [s.source for s in report.sources] == ["a", "b", "c"]
    assert [s.ok for s in report.sources] == [True, False, True]
    assert [i.source for i in report.new_items] == ["a", "a", "c", "c", "c"]
    assert storage.kv_get("seen:b") == "1"                 # buffered state flushed even on failure
    assert len({c.thread for c in collectors}) == 3


def test_throttle_is_thread_safe(make_http):
    import httpx

    stamps = []
    client = make_http(lambda r: (stamps.append(time.monotonic()), httpx.Response(200))[1],
                       per_host_min_interval_s=0.1, respect_robots_txt=False)
    client._sleep = time.sleep
    threads = [threading.Thread(target=client.get, args=("https://one.test/x",)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    gaps = sorted(b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:]))
    assert len(stamps) == 4 and gaps[0] >= 0.09
