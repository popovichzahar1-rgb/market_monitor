# Market Monitor

Особистий конвеєр моніторингу подій, що впливають на ринки акцій і крипти,
зі сповіщеннями в Telegram. **Лише збір і фільтрація інформації, жодної
автоматичної торгівлі. Не є фінансовою порадою.**

> Статус: етап 1 з 7 (каркас, RSS-колектор, SQLite, dry-run).
> Повна інструкція (BotFather, GitHub Secrets, Actions) з'явиться на етапі 7.

## Швидкий старт локально

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # секрети поки не потрібні
python -m pytest                # тести на фікстурах, без мережі
python -m market_monitor run --dry-run   # реальні RSS, друк замість відправки
python -m market_monitor status          # стан джерел
```

Повторний `run --dry-run` покаже «Нових записів немає»: дедуплікація за URL
працює, а незмінені фіди віддають `304 Not Modified`.

## Конфігурація

- `config.yaml` — робочий конфіг (без секретів), список відстеження порожній.
- `config.example.yaml` — те саме з **прикладами** компаній (не рекомендації).
- `.env` / GitHub Secrets — токени й ключі (див. `.env.example`).
