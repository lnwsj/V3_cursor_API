# V3 Cursor API: คู่มือผู้เรียก API

เอกสารนี้อธิบาย flow ที่แนะนำสำหรับ client ภายนอก โดยใช้ Gateway เป็นจุดเชื่อมต่อเดียว

> **สถานะสำคัญ ณ 2026-08-20:** ตัวอย่างในเอกสารนี้เป็น target/accepted contract จาก release ก่อนหน้า ไม่ใช่หลักฐานว่า Gateway `refactor-base` ใช้งานได้แล้ว. Current refactor ยังมี release blockers ที่ upload, auth, dynamic render, dispatch และ download. อ่าน [`V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md`](V3_CURSOR_API_CURRENT_STATE_AUDIT_TH.md) ก่อนนำตัวอย่างไปใช้กับ environment ใด

## 1. ตั้งค่าพื้นฐาน

```bash
BASE="https://example.invalid/v3api"
API_KEY="<API_KEY>"
AUTH=(-H "Authorization: Bearer $API_KEY")
```

ถ้าเรียก local development ให้ใช้ `BASE=http://127.0.0.1:8788` และถ้าเรียก Worker โดยตรงต้องใช้ internal token แทน Bearer token

สำหรับ public Green Cutdee ที่ตรวจเมื่อ 2026-08-20 ให้ใช้ `BASE=https://green.cutdee.com/v3api`
เฉพาะ endpoint ใน API proxy และจำไว้ว่า frontend ที่ root เรียก `/api/...` โดยตรง
เอกสารนี้ยังไม่เปลี่ยน target dynamic contract เป็น production PASS เพราะ source refactor
ยัง release-blocked

## 2. Authentication

มีสองรูปแบบหลัก:

- API key: ส่ง `Authorization: Bearer <API_KEY>` ทุก user route
- Session cookie: แลก Bearer token ที่ `POST /api/auth/session` แล้วใช้ cookie ที่ server คืนให้

ตัวอย่าง session exchange:

```bash
curl -fsS -X POST "$BASE/api/auth/session" \
  -H "Authorization: Bearer $API_KEY" \
  -c session.cookies
```

การสมัครและ login ผ่าน portal API:

```bash
curl -fsS -X POST "$BASE/api/v1/auth/signup" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"<PASSWORD>"}'

curl -fsS -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"<PASSWORD>"}'
```

ห้ามนำ API key หรือ session cookie ไปใส่ใน client-side log หรือ URL query string

## 3. Upload ไฟล์

บทบาทที่รองรับคือ `product`, `background`, `cover`, `audio`, `source` และ `product_root`

### Current extracted Gateway contract: multipart upload

```bash
PRODUCT_JSON=$(curl -fsS -X POST "$BASE/api/v1/uploads/product" \
  "${AUTH[@]}" \
  -F "file=@product.mp4;filename=product.mp4")

PRODUCT_ID=$(printf '%s' "$PRODUCT_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["file_id"])')
```

Response สำคัญ:

```json
{
  "file_id": "product_<timestamp>_<random>",
  "role": "product",
  "size": 123456,
  "filename": "product_<timestamp>_<random>.mp4"
}
```

Current extracted code รับ multipart field ชื่อ `file` และคืน `file_id`; อย่างไรก็ตาม audit ล่าสุดพบ `NameError` ใน upload handler ก่อนเขียนไฟล์ จึงต้องแก้และรัน upload smoke ก่อนถือว่า route นี้ใช้งานได้

Production/legacy snapshot บางรุ่นเคยรับ raw request body และใช้ `X-Filename`; อย่านำสอง contract มาปะปนกัน

อย่าสับสนกับ compatibility route `POST /api/jobs/upload` ซึ่งปัจจุบันคืน `files: []` เป็น placeholder

## 4. สร้างงานตาม pipeline

สำหรับ TC01-TC06 ให้ใช้ route แบบ dynamic JSON เป็นหลัก:

```text
POST /api/{tc}/render
```

ตัวอย่าง TC01:

```bash
BG_JSON=$(curl -fsS -X POST "$BASE/api/v1/uploads/background" \
  "${AUTH[@]}" \
  -F "file=@background.mp4;filename=background.mp4")
BG_ID=$(printf '%s' "$BG_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["file_id"])')

JOB_JSON=$(curl -fsS -X POST "$BASE/api/tc01/render" \
  "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d "{
    \"files\": {
      \"product\": [\"$PRODUCT_ID\"],
      \"background\": [\"$BG_ID\"]
    },
    \"settings\": {
      \"width\": 1080,
      \"height\": 1920,
      \"fps\": 30,
      \"encoder\": \"auto\",
      \"key_color\": \"#00FF00\",
      \"similarity\": 0.29,
      \"blend\": 0.04
    }
  }")

JOB_ID=$(printf '%s' "$JOB_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["job_id"])')
```

`files` ใช้ upload IDs จาก Gateway ไม่ใช่ชื่อไฟล์บนเครื่อง client

หมายเหตุ: `/api/{tc}/render` เป็น target contract ของ Gateway รุ่นที่ทำ E2E แล้ว; source `refactor-base` ที่ audit ล่าสุดยังมีเพียง compatibility route `/api/render/{tc}` ซึ่งคืน echo payload และยังไม่ enqueue render จริง

### FormData compatibility route

UI-compatible route คือ:

```text
POST /api/render/{tc}
```

ใช้ multipart fields ชื่อ `product`, `background`, `cover`, `audio`, `source`, `product_root` หรือ plural fields เช่น `products`, `backgrounds`, `sources`

```bash
curl -fsS -X POST "$BASE/api/render/tc01" \
  "${AUTH[@]}" \
  -F product=@product.mp4 \
  -F background=@background.mp4 \
  -F width=1080 \
  -F height=1920 \
  -F fps=30
```

## 5. Poll สถานะ

ใช้ route ใด route หนึ่งให้สม่ำเสมอ:

```bash
while true; do
  JOB_JSON=$(curl -fsS "$BASE/api/v1/jobs/$JOB_ID" "${AUTH[@]}")
  STATUS=$(printf '%s' "$JOB_JSON" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("status"))')
  printf 'status=%s\n' "$STATUS"
  case "$STATUS" in
    succeeded|partial|failed|cancelled|invalid_input) break ;;
  esac
  sleep 2
done
```

`GET /api/v1/jobs/{id}/live` เหมาะกับหน้า UI เพราะคืน worker node, load และ ETA เพิ่มเติม

## 6. Download output

ต้องใช้ชื่อไฟล์จริงจาก field `output_files` หรือ `output_file`:

```bash
OUTPUT_FILE=$(printf '%s' "$JOB_JSON" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print((d.get("output_files") or [d.get("output_file")])[0])')

curl -fsS -o output.mp4 \
  "$BASE/api/v1/jobs/$JOB_ID/download/$OUTPUT_FILE" \
  "${AUTH[@]}"
```

ห้ามใช้ wildcard เช่น `output_*.mp4` ใน URL เพราะ Gateway ตรวจชื่อไฟล์แบบ safe filename และต้องตรงกับ output manifest

ทางเลือกสำหรับหลาย output:

```text
GET /api/job/{job_id}/download-all
```

## 7. ควบคุมงาน

```text
POST /api/jobs/{job_id}/cancel
POST /api/jobs/{job_id}/pause
POST /api/jobs/{job_id}/resume
```

คำสั่งเป็น cooperative signal ไป Worker จึงไม่รับประกันว่าจะหยุดในเฟรมเดียวทันที งานที่กำลังอยู่ใน FFmpeg อาจใช้เวลาสั้น ๆ ก่อนเปลี่ยนเป็น terminal state

## 8. Dry-run และข้อมูลระบบ

ก่อนเริ่ม render ใช้:

```text
POST /api/{tc}/dry-run
```

Dry-run คำนวณจำนวน stage/output จาก settings แต่ไม่ probe media และไม่เรียก FFmpeg จึงใช้ตรวจ input mapping เบื้องต้นเท่านั้น

ใน source `refactor-base` มี planner module แต่ยังไม่พบ HTTP `/api/{tc}/dry-run` route ที่ใช้งานได้ จึงอย่าใช้ตัวอย่าง dry-run เป็น health check ของ current Gateway

Public health:

```text
GET /healthz                 # direct gateway only
GET /api/health
GET /api/version
GET /api/cluster/health
GET /api/cluster/public
```

บน public host ให้ใช้ `GET /v3api/healthz` สำหรับ JSON health; `GET /healthz`
ที่ root public เป็น HTML ของ frontend และ `GET /openapi.json` ก็เป็น HTML เช่นกัน

## 9. สถานะและ error ที่พบบ่อย

| HTTP | ความหมาย | การแก้เบื้องต้น |
|---:|---|---|
| 400 | field, role, settings หรือ filename ไม่ถูกต้อง | ตรวจ schema และค่าที่รองรับ |
| 401 | Bearer/session ไม่ถูกต้อง | ขอ token ใหม่และอย่าใช้ internal token เป็น user token |
| 404 | job/file/output ไม่พบ หรือไม่ใช่ resource ของ user | ตรวจ ID และ ownership |
| 409 | job อยู่ใน state ที่สั่ง action ไม่ได้ | poll state ก่อนสั่งซ้ำ |
| 429 | worker queue เต็ม | ลด concurrency หรือรอคิว |
| 502 | Gateway ส่งงานไป Worker ไม่สำเร็จ | ตรวจ cluster health และ worker health |
| 503 | ไม่มี worker ที่พร้อมรับงาน | ตรวจ registry และ enabled/healthy state |

## 10. Contract ที่ต้องระวัง

- `/api/v1/jobs` เป็น legacy TC01-style route; ถ้าต้องการ TC02-TC06 ให้ใช้ `/api/{tc}/render`
- `settings` และ `values` ใน dynamic route ถูก merge โดย `values` มี precedence สูงกว่า
- `product_root` ต้องเป็น ZIP หรือ path ที่ Worker resolve ได้ภายใน job directory
- output อยู่ local Worker; job database ไม่ได้ทำให้ไฟล์ output มี redundancy อัตโนมัติ
