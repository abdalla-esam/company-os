# INVENTORY — جرد المستودع

**آخر تحديث:** 2026-10-19 · **الفرع:** main · **آخر commit:** `144bcd2`

## الأرقام

| المقياس | القيمة |
|---|---|
| ملفات Python | 156 |
| أسطر Python | 68,672 |
| اختبارات | 699 (كلها خضراء · 27.9s) |
| ملفات اختبار | 54 |
| دومينات | 9 |
| workers أساسيون | 6 |
| حالات كبسولة | 12 |
| أنواع بوابات | 5 |
| capabilities | 18 |
| أدوات منشورة للموديل | 14 (16 ترسًا − ترسان غير قابلين للتراجع) |

## الطبقات

| المجلد | ملفات | أسطر | المسؤولية |
|---|---|---|---|
| `engine/capsule_engine/` | 24 | 17,628 | النواة: حالة، بوابات، ذاكرة، سبند، صلاحيات، **جسر الأدوات** |
| `engine/orchestration/` | 23 | 17,178 | API، pulse، dispatcher، جسر، observability |
| `engine/workers/` | 7 | 2,907 | عمال التنفيذ |
| `engine/shared_kernel/` | 8 | 2,048 | مسارات، هوية، ذرّية IO |
| `data/domains/` | 29 | 5,131 | منطق الدومينات |
| `ops/` | 10 | 3,925 | تشغيل، نشر، أدوات CLI |
| `tests/` | 54 | 19,835 | المواصفات التنفيذية |
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

| # | الفجوة | الحالة |
|---|---|---|
| 1 | لا `docs/` | ✅ أُغلقت — 7 ملفات |
| 2 | لا CI (`.github/`) | ✅ أُغلق — 3 إصدارات Python |
| 3 | لا بوابة جودة موحّدة | ✅ أُغلقت — `scripts/verify.sh` + `Makefile` |
| 4 | لا لوحة تحكم (`orchestration/cockpit/` مُشار إليها وغير موجودة) | ⬜ الواجهة في `company-mobile` |
| 5 | لا حلقة tool-calling (الموديل يُرجع نصًا فقط) | ⬜ الموجة التالية |
| 6 | لا تقييم جودة لمخرجات الموديل (الـ 630 اختبار كود فقط) | ⬜ الموجة التالية |

## بوابة الجودة

```bash
bash scripts/verify.sh    # أو: make verify
```

| المرحلة | تفحص |
|---|---|
| 1 | كل ملف `.py` يُترجم |
| 2 | 630 اختبارًا · offline · vault معزول |
| 3 | اختبارات الـ invariants لم تُخطَّ |
| 4 | لا مخلفات (`__pycache__`, `.log`, `.sqlite3`, `.DS_Store`) |

**النتيجة الحالية: PASS** (28s)
