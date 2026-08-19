# V3 Cursor API: คู่มือ Pipeline และ Settings

## 1. ตาราง Pipeline

| Pipeline | Input หลัก | พฤติกรรม | Output โดยทั่วไป |
|---|---|---|---|
| TC01 | product + background | chroma key และ overlay | 1 final ต่อ product |
| TC02 | product + background | reframe matrix แล้ว chroma ทุกผลลัพธ์ | 7 x composition ต่อ product; default 21 |
| TC03 | product + background + optional audio | split segment, ping-pong/batch matching แล้ว chroma | จำนวน segment ตาม duration |
| TC04 | product + background + optional audio | reframe, validate intermediate, batch และ final chroma | segment x lens x composition |
| TC05 | source | reframe-only ไม่ทำ chroma | 7 x composition ต่อ source; default 21 |
| TC06 | product root folders | product folder, background, audio และ audio-master | 1 final ต่อ audio/folder |

จำนวนจริงขึ้นกับจำนวนไฟล์, composition, duration และ settings ที่ส่งเข้า pipeline

## 2. Settings กลาง

| Field | ค่าแนะนำ/ขอบเขต | ความหมาย |
|---|---|---|
| `width` | เลขคู่ 360-3840 | ความกว้าง output |
| `height` | เลขคู่ 360-3840 | ความสูง output |
| `fps` | 15-60 | frame rate |
| `bitrate` | เช่น `6000k` | video bitrate |
| `encoder` | `auto`, `h264_videotoolbox`, `h264_nvenc`, `libx264` ตาม worker | encoder alias |
| `preset` | `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium`, `slow`, `hq` | speed/quality profile |
| `key_color` | `#RRGGBB` | สีที่ต้องการตัดออก |
| `similarity` | 0-1 | tolerance ของ chroma key |
| `blend` | 0-1 | softness ของขอบ mask |
| `despill` | 0-1 | ลดสีเขียว/สี key ที่ขอบตัวแบบ |
| `audio_source` | `product`, `background`, `none` | แหล่งเสียง; factory อาจเลือกตาม uploaded audio |

ค่าที่เหมาะกับงานจริงต้องตรวจภาพและเสียง ไม่ควรเปลี่ยน similarity หรือ blend จาก default โดยไม่ตรวจ edge quality

## 3. TC01 Chroma

เหมาะสำหรับงานพื้นฐานหนึ่ง product ต่อหนึ่ง background

```json
{
  "files": {
    "product": ["<product_id>"],
    "background": ["<background_id>"],
    "cover": ["<cover_id>"],
    "audio": ["<audio_id>"]
  },
  "settings": {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "encoder": "auto",
    "key_color": "#00FF00",
    "similarity": 0.29,
    "blend": 0.04,
    "despill": 0.32
  }
}
```

Cover และ uploaded audio เป็น optional แต่ต้องตรวจ duration และ stream ก่อนใช้งาน production

## 4. TC02 Reframe Matrix

TC02 ทำสอง stage:

1. reframe product ตาม lens/composition
2. ส่ง reframe outputs เข้า chroma pipeline

ค่า production ของ Mac M4 ที่ผ่าน acceptance:

```text
V3_TC02_STREAMING=1
V3_TC02_PRODUCERS=2
V3_TC02_CONSUMERS=3
```

TC02 สร้าง fixed lens/composition matrix โดย default 21 outputs ต่อ product การเปิด parallelism เพิ่มอาจทำให้ memory/CPU contention สูงขึ้นบนเครื่องอื่น จึงต้อง tune แยกตาม worker class

สำหรับ input แนวตั้ง 1080x1920 ให้พิจารณา `reframe_short_side=720` เมื่อยอมรับ trade-off ด้านความคมและขอบ mask ได้

## 5. TC03 Batch

TC03 ใช้ `segment_duration` และ `match_mode` เพื่อจับคู่ product/background/audio

```json
{
  "files": {
    "product": ["<product_id>"],
    "background": ["<background_id>"],
    "audio": ["<audio_id>"]
  },
  "values": {
    "segment_duration": 10.0,
    "match_mode": "no_repeat",
    "seed": 0
  }
}
```

จำนวน output อาจต่างจากจำนวนไฟล์ input เพราะขึ้นกับ duration และ segment matching

## 6. TC04 Rebatch

TC04 เป็น pipeline หลาย stage และใช้ audio-master ได้ จึงใช้เวลามากกว่า TC01:

```text
reframe -> validate intermediates -> batch split -> final chroma -> audio-master (ถ้ามี)
```

ต้องตรวจทั้ง stage result และ final output count อย่าตัดสินจาก HTTP `202` เพียงอย่างเดียว

## 7. TC05 Reframe-only

TC05 ต้องใช้ `source` หรือ `source_ids` และไม่ต้องส่ง background สำหรับ direct reframe

```json
{
  "files": {
    "source": ["<source_id>"]
  },
  "settings": {
    "width": 1080,
    "height": 1920,
    "encoder": "auto",
    "bitrate": "8000k",
    "ffmpeg_workers": 3
  }
}
```

## 8. TC06 Product Root

โครงสร้างที่รองรับ:

```text
product-root/
├── product/
├── bg/
└── audio/
```

- `product/` ต้องมี video
- `bg/` รับ video หรือ image ตาม contract
- `audio/` ต้องมี audio stream ที่อ่านได้
- ส่งเป็น `product_root` ZIP หรือ root ที่ Worker resolve ได้
- ZIP ต้องไม่มี path traversal เช่น `../outside.mp4`

## 9. Output Contract

ทุก pipeline ต้องคืนสถานะ truth-bearing และ output manifest:

```json
{
  "status": "succeeded",
  "expected": 21,
  "succeeded": 21,
  "failed": 0,
  "cancelled": 0,
  "output_files": ["<safe-output-name>.mp4"]
}
```

Acceptance ขั้นต่ำ:

- status เป็น `succeeded`
- output count ตรงกับ expected
- ทุกไฟล์มีขนาดมากกว่าศูนย์
- duration ตรงกับ contract/input
- codec, resolution, audio stream ตรงกับ settings

## 10. Dry-run

```bash
curl -fsS -X POST "$BASE/api/tc04/dry-run" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "files":{"product":["<product_id>"],"background":["<background_id>"]},
    "values":{"assume_duration_seconds":21,"segment_duration":10}
  }'
```

Dry-run เป็น estimate เท่านั้น ไม่แทน real-media acceptance และไม่รับประกัน output count หาก media probe ให้ duration ต่างจากค่าที่ assume
