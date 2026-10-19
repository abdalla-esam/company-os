# RUN — التشغيل

## المتطلبات

- Python ≥ 3.9 (مُختبر على 3.9.6)
- `pip install -r requirements.txt`
- لا مفاتيح مطلوبة في الوضع offline

## الاختبارات (البوابة الأساسية)

```bash
bash scripts/verify.sh
```

أو يدويًا:

```bash
PYTHONPATH=engine:. AGENCY_OFFLINE_MODE=1 \
  python3 -m unittest discover -s tests -p "test_*.py"
```

**المتوقع:** `Ran 630 tests ... OK`

## الخادم

```bash
bash server_boot.sh            # خلفية + healthcheck
bash server_boot.sh --status
bash server_boot.sh --stop
```

| العنوان | الغرض |
|---|---|
| `http://localhost:8000/` | الجذر |
| `http://localhost:8000/docs` | OpenAPI |
| `http://localhost:8000/api/health` | الصحة |

## دورة كبسولة offline

```bash
AGENCY_OFFLINE_MODE=1 python3 ops/tools/post_cycle_trial.py
```

ينتهي بـ `published` ويعيد 0. **صفر استدعاءات خارجية.**

## نبضة واحدة

```bash
PYTHONPATH=engine:. python3 engine/orchestration/pulse.py
```

⚠️ تتطلب spend cap مفعّل قبل أي تشغيل آلي.

## الوضع الحقيقي (بمفتاح)

في `.env`:

```
AGENCY_OFFLINE_MODE=0
AGENCY_LLM_API_KEY=...
AGENCY_LLM_BASE_URL=https://api.openai.com/v1
```

**قبل ذلك:** راجع `config/engine.json → spend` واضبط `daily_usd`.

## الأوامر الشائعة

| الأمر | الغرض |
|---|---|
| `make test` | الاختبارات |
| `make verify` | الاختبارات + فحص البنية |
| `make run` | الخادم |
| `make cycle` | دورة كبسولة offline |
| `make clean` | إزالة المخلفات |
