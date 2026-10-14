from datetime import timedelta
from pathlib import Path

import pytest

from market_monitor.config import Config, SentimentConfig, load_config
from market_monitor.models import Item
from market_monitor.scoring import Scorer
from market_monitor.scoring.keywords import PhraseMatcher
from market_monitor.scoring.sentiment import LexiconSentiment, OnnxFinbert, Sentiment, load_sentiment

REAL = load_config(Path(__file__).parents[1] / "config.yaml")


def make(title, text="", source="rss:test", tags=None, entities=None, meta=None, now=None):
    from conftest import NOW
    return Item(source=source, published_at=now or NOW, title=title, text=text,
                url=f"https://x.test/{abs(hash(title))}", tags=tags or [],
                entities=entities or [], meta=meta or {})


class FakeModel:
    name = "fake"

    def __init__(self, label="negative", value=-0.9):
        self.calls: list[list[str]] = []
        self.result = Sentiment(label, value, 0.95)

    def predict(self, texts):
        self.calls.append(texts)
        return [self.result for _ in texts]


def scorer(config=REAL, model=None):
    return Scorer(config, model=model, use_default_model=False)


# ---- keyword matcher ----------------------------------------------------------

def test_matcher_case_rules_and_wildcard():
    m = PhraseMatcher(["Fed", "holds rates", "sanction*", "US"])
    assert m.search("Fed Holds Rates Steady") == "Fed"
    assert m.search("investors fed up with tariffs") is None   # "Fed" is case-sensitive
    assert m.search("ECB HOLDS RATES") == "holds rates"         # lowercase -> any case
    assert m.search("New sanctions on Iran") == "sanction*"
    assert m.search("tell us more") is None
    assert m.search("US stocks fall") == "US"
    assert m.search("Fedex shares") is None                     # whole words only


# ---- headline table against the real config.yaml ----------------------------

@pytest.mark.parametrize("title,source,low,high,flag,topic", [
    # The Fed's own title for its rate-decision release.
    ("Federal Reserve issues FOMC statement", "rss:fed", 70, 100, "central_bank_decision", "macro"),
    ("Federal Reserve Board announces enforcement action with small bank", "rss:fed", 20, 30,
     None, None),
    ("Fed cuts rates by 25 basis points, signals more easing", "rss:cnbc_markets", 55, 80,
     "central_bank_decision", "macro"),
    ("ECB holds interest rates steady as inflation cools", "rss:ecb", 70, 100,
     "central_bank_decision", "macro"),
    ("Hackers drain $200M from crypto exchange, withdrawals halted", "rss:coindesk", 60, 100,
     "exchange_hack", "crypto"),
    ("SEC approves spot ether ETF options", "rss:cointelegraph", 45, 100,
     "crypto_etf_decision", "crypto"),
    ("Drone attack shuts Saudi oil terminal; Brent jumps 6%", "gdelt:energy", 45, 100,
     "energy_supply_shock", "geopolitics"),
    ("EU agrees new sanctions on Russian LNG and banks", "rss:bbc_business", 40, 100,
     "sanctions_major", "geopolitics"),
    ("OPEC+ agrees surprise output cut", "rss:oilprice", 40, 100, "opec_decision", "geopolitics"),
    ("Opinion: 5 best stocks to buy now", "rss:marketwatch", 0, 5, None, None),
    ("Celebrity chef opens new restaurant", "rss:bbc_business", 0, 10, None, None),
])
def test_headline_scores(title, source, low, high, flag, topic):
    (s,) = scorer().score([make(title, source=source)])
    assert low <= s.score <= high, (s.score, s.reasons)
    if flag:
        assert flag in s.flags
    if topic:
        assert s.topic == topic


def test_rule_decay_and_noise_applies_in_full():
    cfg = Config.model_validate({"scoring": {"rules": [
        {"name": "a", "weight": 40, "any": ["alpha"]},
        {"name": "b", "weight": 30, "any": ["beta"]},
        {"name": "n", "weight": -25, "any": ["podcast"]},
    ]}})
    (s,) = scorer(cfg).score([make("alpha beta podcast")])
    assert s.score == 40 + 15 - 25


def test_also_any_and_tags_any():
    cfg = Config.model_validate({"scoring": {"rules": [
        {"name": "hack", "weight": 50, "any": ["hack*"], "also_any": ["exchange"]},
        {"name": "c", "weight": 20, "any": ["token"], "tags_any": ["crypto"]},
    ]}})
    s1, s2, s3, s4 = scorer(cfg).score([
        make("Exchange hacked"), make("Hackers target hospital"),
        make("New token launch", tags=["crypto"]), make("New token launch"),
    ])
    assert (s1.score, s2.score, s3.score, s4.score) == (50, 0, 20, 0)


def test_bonuses_source_prefix_mentions_assets_regions_watchlist():
    cfg = Config.model_validate({
        "regions": ["CN"],
        "watchlist": [{"ticker": "AAPL"}],
        "scoring": {
            "source_weights": {"gdelt": -5, "gdelt:special": 7},
            "asset_keywords": {"BTC": ["bitcoin"]},
            "region_keywords": {"CN": ["China"], "US": ["Wall Street"]},
        },
    })
    sc = scorer(cfg)
    a, b, c = sc.score([
        make("bitcoin and China", source="gdelt:x"),
        make("Wall Street", source="gdelt:special", entities=["AAPL"]),
        make("plain", source="rss:x"),
    ], mentions={})
    assert a.score == -5 + 10 + 5
    assert sorted(r.split()[0] for r in a.reasons) == ["assets:BTC", "regions:CN", "source:gdelt"]
    assert "asset:BTC" in a.flags
    assert b.score == 7 + 25 and "watchlist" in b.flags  # US region not active
    item = make("plain")
    (m,) = sc.score([item], mentions={item.id: 4})
    assert m.score == 18  # capped at mentions_bonus_max


def test_data_items_scored_by_magnitude():
    items = [
        make("BTC down", source="coingecko", tags=["crypto", "price_move", "data"],
             meta={"change_24h_pct": -12.0}),
        make("US CPI", source="fred", tags=["macro", "data"],
             meta={"series": "CPIAUCSL", "mode": "release"}),
        make("Quake", source="usgs", tags=["disaster", "earthquake", "data"],
             meta={"magnitude": 7.5, "alert": "red", "tsunami": True}),
        make("AAPL 8-K", source="sec_edgar", tags=["sec", "filing"], entities=["AAPL"],
             meta={"form": "8-K", "items": "2.02,9.01"}),
        make("Upcoming CPI", source="fred:calendar", tags=["calendar", "data"]),
    ]
    s = scorer(Config()).score(items)
    assert s[0].score == 55 + 5 * 4 and "crypto_price_move" in s[0].flags and s[0].topic == "crypto"
    assert s[1].score == 35 + 25 and "macro_release" in s[1].flags
    assert s[2].score == 35 + 20 + 30 + 10 and "major_quake" in s[2].flags
    assert s[3].score == 50 + 25 and "sec_8k" in s[3].flags and s[3].topic == "equities"
    assert s[4].score == 10


# ---- sentiment integration ----------------------------------------------------

def test_model_runs_only_for_prefiltered_non_data_items():
    model = FakeModel()
    s = scorer(model=model).score([
        make("Hackers drain crypto exchange", source="rss:coindesk"),   # passes prefilter
        make("Celebrity chef opens restaurant"),                         # below prefilter
        make("BTC down", source="coingecko", tags=["price_move", "data"],
             meta={"change_24h_pct": -9}),                               # data: no tone
    ])
    assert len(model.calls) == 1 and len(model.calls[0]) == 1
    assert s[0].sentiment_label == "negative" and s[0].sentiment_model == "fake"
    assert any(r.startswith("tone:negative") for r in s[0].reasons)
    assert s[1].sentiment is None and s[2].sentiment is None


def test_no_model_call_when_nothing_passes_prefilter():
    model = FakeModel()
    scorer(model=model).score([make("Celebrity chef opens restaurant")])
    assert model.calls == []


def test_tone_points():
    base = scorer(model=FakeModel("neutral", 0.0)).score([make("Hackers drain crypto exchange")])[0]
    neg = scorer(model=FakeModel("negative", -0.9)).score([make("Hackers drain crypto exchange")])[0]
    pos = scorer(model=FakeModel("positive", 0.6)).score([make("Hackers drain crypto exchange")])[0]
    assert neg.score == pytest.approx(min(100, base.score + 9 + 5))
    assert pos.score == pytest.approx(min(100, base.score + 6))


def test_broken_model_falls_back_to_lexicon():
    class Broken:
        name = "broken"

        def predict(self, texts):
            raise RuntimeError("corrupt model")

    sc = scorer(model=Broken())
    (s,) = sc.score([make("Hackers drain crypto exchange, withdrawals halted")])
    assert s.sentiment_label == "negative" and sc.model.name == "lexicon"


def test_lexicon_sentiment():
    pos, neg, neutral = LexiconSentiment().predict([
        "Stocks rally to record high as profits surge",
        "Shares plunge after fraud probe and bankruptcy warning",
        "Company holds annual meeting",
    ])
    assert pos.label == "positive" and pos.value > 0.5
    assert neg.label == "negative" and neg.value < -0.5
    assert neutral.label == "neutral" and neutral.value == 0


def test_load_sentiment_backends(tmp_path):
    assert load_sentiment(SentimentConfig(backend="none")) is None
    missing = SentimentConfig(backend="auto", model_dir=str(tmp_path / "nope"))
    assert load_sentiment(missing).name == "lexicon"


# ---- ONNX runtime path with a tiny synthetic "FinBERT" ------------------------

def build_fake_finbert(model_dir: Path) -> None:
    """Graph: logits = [-5, 5, 0] * max(input_ids == id('crash')) + [0, 0, 1]
    -> 'crash' => negative, otherwise neutral. Labels like ProsusAI/finbert."""
    import json

    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

    vocab = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "markets": 4, "crash": 5, "rally": 6}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.normalizer = normalizers.Lowercase()
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)])
    tok.save(str(model_dir / "tokenizer.json"))

    inputs = [helper.make_tensor_value_info(n, TensorProto.INT64, ["batch", "seq"])
              for n in ("input_ids", "attention_mask", "token_type_ids")]
    output = helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["batch", 3])
    inits = [
        numpy_helper.from_array(np.array(5, dtype=np.int64), "crash_id"),
        numpy_helper.from_array(np.array([[-5.0, 5.0, 0.0]], dtype=np.float32), "W"),
        numpy_helper.from_array(np.array([0.0, 0.0, 1.0], dtype=np.float32), "B"),
    ]
    nodes = [
        helper.make_node("Equal", ["input_ids", "crash_id"], ["eq"]),
        helper.make_node("Cast", ["eq"], ["eqf"], to=TensorProto.FLOAT),
        helper.make_node("ReduceMax", ["eqf"], ["has"], axes=[1], keepdims=1),
        helper.make_node("MatMul", ["has", "W"], ["mm"]),
        helper.make_node("Add", ["mm", "B"], ["logits"]),
    ]
    graph = helper.make_graph(nodes, "fake_finbert", inputs, [output], inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(model_dir / "model.onnx"))
    (model_dir / "config.json").write_text(json.dumps(
        {"id2label": {"0": "positive", "1": "negative", "2": "neutral"}}))


def test_onnx_finbert_runtime_path(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("onnx")
    build_fake_finbert(tmp_path)
    cfg = SentimentConfig(model_dir=str(tmp_path), batch_size=2)
    assert OnnxFinbert.available(cfg)
    model = load_sentiment(cfg)
    assert model.name == "finbert-onnx"
    results = model.predict(["Markets crash", "markets rally", "unknown words here"])
    assert [r.label for r in results] == ["negative", "neutral", "neutral"]
    assert results[0].value < -0.9


# ---- pipeline: rescoring when a story spreads ---------------------------------

def test_rescore_when_mentions_grow(storage, now):
    from market_monitor import pipeline
    from market_monitor.config import Config as C
    from market_monitor.dedup import Deduplicator

    cfg = C.model_validate({"scoring": {"mentions_bonus_per_source": 6}})
    sc = scorer(cfg)
    first = make("Iran seizes oil tanker in Strait of Hormuz, crude prices climb", source="gdelt:a")
    report = pipeline.RunReport(started_at=now)
    report.new_items = pipeline.store_new([first], storage, Deduplicator(storage, cfg.dedup, now), now, cfg)
    pipeline.score(report, cfg, storage, sc, now)
    assert report.scored[0][1].score == 0

    later = now + timedelta(hours=1)
    dup = Item(source="gdelt:b", published_at=later, url="https://other.test/hormuz",
               title="Iran seizes oil tanker in Strait of Hormuz; crude prices climb")
    report2 = pipeline.RunReport(started_at=later)
    pipeline.store_new([dup], storage, Deduplicator(storage, cfg.dedup, later), later, cfg,
                       report2.bumped_ids)
    pipeline.score(report2, cfg, storage, sc, later)
    (item, new_score, old) = report2.rescored[0]
    assert item.id == first.id and old == 0 and new_score.score == 6

    # The same duplicate URL seen again (still in the feed) must not count twice.
    report3 = pipeline.RunReport(started_at=later)
    pipeline.store_new([dup], storage, Deduplicator(storage, cfg.dedup, later), later, cfg,
                       report3.bumped_ids)
    assert report3.bumped_ids == set()
