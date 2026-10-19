# W1 · Build — ما بُني فعلًا

**الدور:** Builder

## الملفات

| الملف | الحالة | الحجم | الدور |
|---|---|---|---|
| `engine/capsule_engine/tool_bridge.py` | جديد | ~430 سطر | العقد → أداة، والتنفيذ المحكوم |
| `engine/capsule_engine/tool_loop.py` | جديد | ~250 سطر | الحلقة المحدودة |
| `engine/capsule_engine/gateway.py` | معدّل | +~120 سطر | `complete_turn` + `AssistantTurn` + `tools` |
| `engine/capsule_engine/worker.py` | معدّل | +~75 سطر | `BaseWorker.run_tools()` |
| `engine/capsule_engine/__init__.py` | معدّل | +~20 سطر | التصدير |

## tool_bridge.py

- `tool_name_for("text:draft")` → `gear_text__draft` (آمن لـ OpenAI: `^[a-zA-Z0-9_-]{1,64}$`).
- `gear_for_tool(...)` عكسه، ويرفض أي اسم غير معروف.
- `gear_tools(include_irreversible=False, only=None)`:
  - **14 أداة** افتراضيًا = 16 − `social:publish` − `message:send`.
  - الحقول المطلوبة/المسموحة تُقرأ من `GearContract` — لا نسخ يدوي.
  - `additionalProperties: false` على كل أداة.
- `FIELD_SCHEMAS`: جدول أنواع الحقول (23 حقلًا). **حقل بلا نوع = خطأ عند الاستيراد** (حارس ضد الانحراف).
- `GearToolRunner.call(...)`:
  - يحوّل أي `BridgeError` إلى `ToolCallOutcome` — **لا استثناء**.
  - `SpendCapExceeded` **لا يُلتقط** — يعبر الحلقة.
  - يسجّل `gear_exchanges` ويطوي `usage` (best-effort، لا يُسقط الاستدعاء).

## tool_loop.py

```python
ToolLoop(gateway, runner, max_steps=4, max_tool_calls=6, allowed_gears=None)
    .run(manifest, messages, tools) -> LoopResult
```

- `LoopResult`: `content` · `stopped_reason` · `messages` · `turns` · `tool_calls` · `usage` · `outcomes`.
- `stopped_reason`: `final` · `max_steps` · `max_tool_calls`.
- الأخطاء المُعادة للموديل: `unknown_tool` · `tool_not_allowed` · `invalid_arguments_json` · `invalid_arguments` · خطأ العقد من الجسر.
- `on_event` للرصد — فشل المراقب لا يكسر الحلقة.

## gateway.py

- `_call_llm(..., tools=...)`: يضيف `tools` + `tool_choice="auto"` **فقط** عند وجود أدوات. بلا أدوات الجسم يبقى كما هو حرفيًا.
- `AssistantTurn`: `content` · `tool_calls` · `usage` · `as_message()`.
- `_normalise_tool_calls`: يرفض ما ليس قائمة، أو أكثر من 8 استدعاءات، أو بلا `function.name`، أو وسائط ليست JSON/كائن. يمنح `id` بديلًا ثابتًا.
- `complete_turn(manifest, messages, tools)`: نفس منطق tier + fallback + spend الخاص بـ `complete`، لكن الرد قد يحمل استدعاءات.
- `content: null` + استدعاءات = **نجاح** (كان سابقًا "empty content").
- `OfflineModelGateway.complete_turn`: لا يخترع أدوات — ينهي الحلقة في دورة واحدة بصدق.

## worker.py

`BaseWorker.run_tools(capsule_path, manifest, messages, *, gears, max_steps, max_tool_calls, include_irreversible, offline, temperature, step_name)`:

1. يبني سياق الكبسولة والحوكمة (`domain` + `autonomy_level`).
2. ينشر الأدوات المسموحة.
3. يشغّل الحلقة داخل `trace_step`.
4. يطوي `usage` في `metadata.usage` ويكتب سطر تاريخ.

## قرارات بُنيت أثناء التنفيذ

- `ToolCallOutcome.tool_name` — ضروري لإعادة اسم أداة **مجهولة** إلى الموديل (وإلا انفجر الجسر).
- `usage` يُنقل عبر `ToolCallOutcome` وإلا ضاع عند إعادة بناء `BridgeResult` للتسجيل.
- `run_tools` يمرّر الـ manifest الحقيقي (لا نسخة) حتى تُسجَّل التبادلات على الكبسولة.
