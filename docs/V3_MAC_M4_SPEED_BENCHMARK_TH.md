# V3 Cursor API: Mac M4 Speed Benchmark

วันที่ทดสอบ: 2026-08-19 (เวลาไทย)

> **Historical snapshot:** รายงานนี้เป็นผล acceptance ของ M4 ที่ release `aa671b5` โดยใช้ H.264 VideoToolbox. Live M4 snapshot ที่ตรวจเมื่อ 2026-08-20 ยัง healthy แต่ preferred encoder เปลี่ยนเป็น `hevc_videotoolbox`; ตัวเลขในรายงานนี้จึงไม่ใช่ HEVC baseline ปัจจุบัน

## Baseline ก่อน optimize

Mac M4 worker (`m4-mlx`) ที่ release `f6299fa` ใช้ sequential TC02 และ CPU budget default 50%:

| Pipeline | Input | Outputs | เวลา | ผลลัพธ์ |
|---|---:|---:|---:|---|
| TC01 | 10 วินาที | 1 | เฉลี่ย 2.592 วินาที | 3/3 สำเร็จ |
| TC02 | 10 วินาที | 21 | 154.805 วินาที | 21/21 สำเร็จ |

## ผลหลัง optimize

ผล acceptance ผ่าน Worker API จริงที่ release `aa671b5`:

| Pipeline | Input | Outputs | เวลา | ผลลัพธ์ |
|---|---:|---:|---:|---|
| TC01 | 10 วินาที | 1 | 2.147 วินาที | succeeded |
| TC02 | 10 วินาที | 21 | 34.692 วินาที | 21/21 สำเร็จ |

ผล TC02 ใหม่เร็วกว่า baseline `154.805` วินาทีประมาณ `77.6%` และ logs ยืนยัน `h264_videotoolbox` กับ streaming `2+3` ใน historical run

## Environment

- Worker: `m4-mlx`
- Platform: Apple M4
- Worker version: `1.2.0`
- Release commit: `aa671b5`
- Encoder ที่ทดสอบ: H.264 VideoToolbox พร้อม `-prio_speed 1`
- Input resolution: `1280x720`
- Input codec: H.264/AAC
- Input duration: `10.000` วินาที
- Worker concurrency: `2`
- CPU budget: `100%` (`~/.green_pc/cpu_percent.txt`)
- Input hwaccel: `-hwaccel videotoolbox`
- TC02 streaming: เปิด `2 producers + 3 consumers`

## Test Method

การทดสอบส่งงานตรงไปยัง Worker API บน Mac M4 โดยใช้ fixture ที่มีอยู่จริงใน job storage:

- อัปโหลด product และ background เข้า job ใหม่
- เริ่ม render ผ่าน Worker endpoint
- Poll `/v1/jobs/{job_id}/status` จนเป็น terminal state
- ตรวจ output manifest
- ใช้ `ffprobe` ตรวจ codec, resolution, duration และไฟล์ที่มีขนาดมากกว่าศูนย์
- ตรวจ worker health ให้เป็น `vt_ready=true` และ preferred encoder เป็น `h264_videotoolbox` ใน historical run

Current live health ต้องตรวจซ้ำและคาดหวัง `hevc_videotoolbox` ตาม current-state audit

การจับเวลาเริ่มหลัง upload เสร็จและเริ่ม render request จึงไม่รวม latency จาก public Gateway, network ภายนอก และ PostgreSQL

## TC01 Results: Baseline ก่อน optimize

ทดสอบซ้ำ 3 รอบด้วย input เดียวกัน:

| Run | Render time | Output size | Status |
|---:|---:|---:|---|
| 1 | 2.597 วินาที | 726,704 bytes | succeeded |
| 2 | 2.601 วินาที | 726,704 bytes | succeeded |
| 3 | 2.577 วินาที | 726,704 bytes | succeeded |

สถิติ:

- Average: `2.592` วินาที
- Minimum: `2.577` วินาที
- Maximum: `2.601` วินาที
- Effective speed: ประมาณ `3.86x realtime`
- Output duration: `10.000` วินาที
- Output streams: H.264 video และ AAC audio

## TC02 Results: Baseline ก่อน optimize

TC02 สร้าง reframe/chroma matrix จำนวน 21 outputs จาก product เดียว:

- Elapsed time: `154.805` วินาที
- Expected outputs: `21`
- Succeeded outputs: `21`
- Failed outputs: `0`
- Progress: `100%`
- Output size รวม: `12,637,463` bytes
- Average: `7.372` วินาทีต่อ output
- Aggregate output speed: ประมาณ `1.36x realtime` เมื่อคิดจาก 21 outputs x 10 วินาที

## Output Validation

ตรวจ output ทั้ง 21 ไฟล์ด้วย `ffprobe`:

- Manifest count: `21`
- Valid count: `21`
- Invalid count: `0`
- Duration ต่ำสุด: `10.000` วินาที
- Duration สูงสุด: `10.000` วินาที
- Codec: H.264 ทุกไฟล์
- Resolution: `1280x720` ทุกไฟล์
- ไม่มีไฟล์ขนาดศูนย์หรือขาด video stream

## Interpretation

- TC01 เหมาะใช้เป็น smoke benchmark หลัง deploy เพราะใช้เวลาประมาณ 3 วินาทีและตรวจทั้ง queue, render, status และ output ได้ครบ
- TC02 เป็น workload ที่เหมาะใช้เทียบประสิทธิภาพ reframe matrix เพราะสร้าง 21 outputs และสะท้อนต้นทุนจริงของ pipeline มากกว่า TC01
- ความเร็ว TC02 ในรายงานนี้เป็น sequential production default ไม่ใช่ opt-in streaming benchmark
- Output size ของ TC01 และผลสำเร็จคงที่ทั้ง 3 รอบ แสดงว่า benchmark มีความสม่ำเสมอในสภาวะ idle

## Caveats

- การทดสอบตรง Worker ไม่ได้วัด Gateway dispatch, PostgreSQL state update หรือ public network latency
- ยังไม่ได้เปรียบเทียบกับ RTX5060Ti ใน release เดียวกัน
- ยังไม่ได้วัด TC03-TC06 ด้วย real-media fixture ชุดเดียวกัน
- ระหว่างเริ่ม TC02 ครั้งแรก Mac launchd มีช่วง restart overlap ทำให้ connection refused ก่อนเริ่ม render; รอบที่รันหลัง service stable ผ่านครบ 21/21
- ไม่ควรใช้ตัวเลขนี้เป็น SLA จนกว่าจะทดสอบหลายขนาดไฟล์, codec, audio และ concurrent jobs

## สิ่งที่เปลี่ยนในการ optimize

- แก้ VideoToolbox smoke test จาก invalid `-allow_sw_hw` เป็น `-allow_sw` สำหรับ FFmpeg 8
- แก้ Apple input acceleration จาก invalid `h264_videotoolbox` decoder เป็น `-hwaccel videotoolbox`
- ตั้ง M4 CPU budget เป็น `100%`
- เปิด `V3_TC02_STREAMING=1`, `V3_TC02_PRODUCERS=2`, `V3_TC02_CONSUMERS=3`
- เพิ่ม `-prio_speed 1` ใน VideoToolbox render command
- แก้ telemetry ให้แสดง `GPU/VideoToolbox` และรายงานจำนวน producer/consumer จริง

## Baseline สำหรับรอบถัดไป

ใช้ค่าเหล่านี้เป็น baseline เมื่อเปลี่ยน code, encoder หรือ worker configuration:

```text
Before: TC01 10s input -> 1 output -> average 2.592s
Before: TC02 10s input -> 21 outputs -> 154.805s -> 21/21 valid
Current: TC01 10s input -> 1 output -> 2.147s -> GPU/VideoToolbox
Current: TC02 10s input -> 21 outputs -> 34.692s -> 21/21 valid -> streaming 2+3
```

ควรถือว่า benchmark regression เกิดขึ้นเมื่อ:

- TC01 average ช้าลงเกิน 20% โดยใช้ fixture และ settings เดิม
- TC02 output count ไม่เท่ากับ 21
- TC02 มี output invalid, duration ต่างจาก input หรือ status ไม่ใช่ `succeeded`
- Worker health ไม่ตอบระหว่าง render
