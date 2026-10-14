import logging

from market_monitor.logging_setup import RedactingFilter, setup_logging


def test_redacts_bot_token_api_keys_and_known_secrets(capsys):
    setup_logging("INFO", secrets=["supersecretvalue"])
    log = logging.getLogger("t")
    log.info("GET https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ/getUpdates")
    log.info("fred https://api.stlouisfed.org/x?series_id=CPI&api_key=abcdef123456")
    log.info("value=%s", "supersecretvalue")
    err = capsys.readouterr().err
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ" not in err
    assert "abcdef123456" not in err and "api_key=***" in err
    assert "supersecretvalue" not in err


def test_redacts_exception_text(capsys):
    setup_logging("INFO", secrets=["topsecret99"])
    try:
        raise ValueError("bad topsecret99")
    except ValueError:
        logging.getLogger("t").exception("failed")
    assert "topsecret99" not in capsys.readouterr().err


def test_filter_keeps_plain_messages():
    f = RedactingFilter()
    assert f.redact("nothing here") == "nothing here"


def test_exception_log_keeps_formatted_message(capsys):
    setup_logging("INFO")
    try:
        raise ValueError("x")
    except ValueError:
        logging.getLogger("t").exception("collector %s failed", "gdelt")
    err = capsys.readouterr().err
    assert "collector gdelt failed" in err and "%s" not in err
