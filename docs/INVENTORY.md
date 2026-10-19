# INVENTORY — جرد المستودع

**آخر تحديث:** 2026-10-19 · **الفرع:** main · **آخر commit:** `144bcd2`

## الأرقام

| المقياس | القيمة |
|---|---|
| ملفات Python | 150 |
| أسطر Python | 66,576 |
| اختبارات | 630 (كلها خضراء · 27.5s) |
| ملفات اختبار | 50 |
| دومينات | 9 |
| workers أساسيون | 6 |
| حالات كبسولة | 12 |
| أنواع بوابات | 5 |
| capabilities | 18 |

## الطبقات

| المجلد | ملفات | أسطر | المسؤولية |
|---|---|---|---|
| `engine/capsule_engine/` | 22 | 16,547 | النواة: حالة، بوابات، ذاكرة، سبند، صلاحيات |
| `engine/orchestration/` | 23 | 17,178 | API، pulse، dispatcher، جسر، observability |
| `engine/workers/` | 7 | 2,907 | عمال التنفيذ |
| `engine/shared_kernel/` | 8 | 2,048 | مسارات، هوية، ذرّية IO |
| `data/domains/` | 28 | 4,726 | منطق الدومينات |
| `ops/` | 10 | 3,925 | تشغيل، نشر، أدوات CLI |
| `tests/` | 50 | 18,821 | المواصفات التنفيذية |
| `config/` | — | — | إعدادات JSON |

## الدومينات التسعة

`analytics` · `client_management` · `design` · `development` · `finance` · `ideation` · `marketing` · `operations` · `people`

**النشط فعليًا:** `marketing` · `ideation` · `design` (الباقي هياكل).

## العمال الستة

| Worker | الحالة |
|---|---|
| `marketing` | LLM → copy.md |
| `qa` | مراجعة تلقائية |
| `design` | توليد صورة |
| `gate_runner` | تشغيل البوابات |
| `governor` | قرار أتمتة |
| `publishing` | نشر خارجي |

## ملفات لا تُلتزم بـ git (صحيحة)

`.env` · `config/users.json` · `config/.bootstrap_admin_password` · `data/capsules/` · `data/index/` · `data/assets/vault/` · `*.log` · `*.lock`

## نظافة المستودع (تمت)

- حُذف: `.DS_Store` ×5 · `__pycache__` · `engine/data/` (مخلفات) · `pulse.log` · `pulse_stderr.log` · ملفات `.lock` القديمة
- **لم يُحذف أي كود أو اختبار أو إعداد.**

## الفجوات المعروفة

| # | الفجوة |
|---|---|
| 1 | لا `docs/` — تم إنشاؤها الآن |
| 2 | لا CI (`.github/`) |
| 3 | لا `pyproject.toml` / إعداد pytest |
| 4 | لا لوحة تحكم (`orchestration/cockpit/` مُشار إليها وغير موجودة) |
| 5 | لا حلقة tool-calling (الموديل يُرجع نصًا فقط) |
| 6 | لا تقييم جودة لمخرجات الموديل (الـ 630 اختبار كود فقط) |
