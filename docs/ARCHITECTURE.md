# ARCHITECTURE — المعمارية

## المبدأ

**النواة لا تعرف الدومينات. الدومينات لا تعرف بعضها. الحالة على القرص.**

```
┌──────────────────────────────────────────────┐
│  ops/ + engine/orchestration/api.py          │  واجهة (CLI · REST)
├──────────────────────────────────────────────┤
│  engine/orchestration/                       │  تنسيق: pulse · dispatcher · bridge
├──────────────────────────────────────────────┤
│  engine/workers/                             │  تنفيذ: 6 عمال
├──────────────────────────────────────────────┤
│  engine/capsule_engine/  ★ النواة            │  حالة · بوابات · ذاكرة · سبند
│  engine/shared_kernel/                       │  مسارات · هوية · IO ذرّي
├──────────────────────────────────────────────┤
│  data/domains/<name>/                        │  منطق دومين (معزول)
└──────────────────────────────────────────────┘
         ↕ كل الحالة: ملفات JSON/JSONL
```

## اتجاه الاعتماد (مُختبَر آليًا)

```
shared_kernel  ←  capsule_engine  ←  workers  ←  orchestration
                                        ↑
                                  data/domains (مسموح للأعلى فقط)
```

**مُفروض بواسطة:** `tests/test_dependency_direction.py`
- النواة **ممنوعة** من استيراد `data/domains` (حتى lazy imports).
- الانعكاسات الموجودة في `workers/` و `orchestration/` مثبّتة كمجموعة محددة — أي إضافة تكسر الاختبار.

## المكوّنات الأساسية

| المكوّن | الملف | الدور |
|---|---|---|
| **State machine** | `manifest.py` | 12 حالة · `TRANSITIONS` صريحة · TOCTOU hash |
| **Router** | `router.py` | يقرأ manifest غير مقفول ويقرر — لا ينفّذ |
| **Worker base** | `worker.py` | `run()` يقفل → ينفّذ → يسجّل → يحرّر |
| **Gates** | `gates.py` | 1..N بوابة · hash لكل عنصر |
| **Memory** | `memory.py` | ledger hash-chained · يغذّي قيود سلبية |
| **Spend** | `spend_control.py` | `authorize` قبل · `record` بعد |
| **Capabilities** | `capabilities.py` | HMAC grants بدل توكن شامل |
| **Model seam** | `gateway.py` | OpenAI-compatible + offline |
| **Task types** | `task_type.py` | pipeline كـ JSON |
| **Gears** | `gear_registry.py` | 16 فعل قانوني |
| **Trace** | `trace.py` | `execution_trace.jsonl` |

## مسار الكبسولة

```
draft → in_progress → pending_qa → pending_design → pending_approval
                            ↓            ↓                ↓
                     needs_revision ←────┘          approved → published → archived
                                                          ↓
                                                      stalled / failed
```

## ما هو **بيانات** وما هو **كود**

| بيانات (تُعدّل بحرية) | كود (دستور) |
|---|---|
| `config/task_types/*.json` | `TRANSITIONS` |
| `config/engine.json` (بوابات، سبند، ذاكرة) | ترتيب البوابات |
| `config/company_profile.json` | حدود المحاولات |
| `data/domains/<name>/` | الحالات النهائية |
