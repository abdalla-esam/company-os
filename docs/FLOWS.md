# FLOWS — المسارات الحرجة

## 1. دورة كبسولة كاملة (offline)

```bash
AGENCY_OFFLINE_MODE=1 python3 ops/tools/post_cycle_trial.py
```

```
ticket → gate → موافقة بشرية → settle → dispatcher → capsule
  → marketing (copy.md)
  → qa (مراجعة)
  → copy sign-off (بشري)
  → design (image.jpg)
  → pending_approval (SHA-256)
  → موافقة بشرية (re-hash)
  → publishing → published
```

**كل خطوة تطبع سطر JSON. الخروج 0 فقط عند `published`.**

## 2. نبضة المصنع (pulse)

```bash
PYTHONPATH=engine:. python3 engine/orchestration/pulse.py
```

| الخطوة | التكلفة |
|---|---|
| 1. settle | مجاني |
| 2. auto-ideator | **مدفوع** |
| 3. dispatcher | **مدفوع** |
| 4. factory pulse | **مدفوع** |
| 5. directus | **مدفوع** |

⚠️ **ممنوع التشغيل الآلي قبل تفعيل spend cap + kill switch** (موجودان في `config/engine.json → spend`).

## 3. الموافقة البشرية

```
pending_approval
   ↓ hash العناصر
[إنسان يقرأ]
   ↓ approve
re-hash → مطابقة؟ → approved
                ↓ لا
              SecurityError (fail closed)
```

**أي تعديل بين القراءة والموافقة = رفض فوري.**

## 4. الرفض والتعلّم

```
reject بسبب
   ↓
memory/feedback_history.jsonl  (hash-chained, fsync, flock)
   ↓
negative_constraints(domain)
   ↓
تُحقن في prompt الكبسولة التالية
```

## 5. التشغيل والفحص

```bash
bash server_boot.sh            # خلفية + healthcheck
bash server_boot.sh --status
bash server_boot.sh --stop
```

**الصحة:** `GET /api/health` · **الوثائق:** `GET /docs`
