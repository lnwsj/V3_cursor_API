# V3 Cursor API: Operations Runbook

คู่มือสำหรับ deploy, monitor, canary และแก้ incident ใน production

## 1. หลักการปฏิบัติ

- Deploy release เดียวกันเป็นชุด อย่าแก้ไฟล์ใน production แบบไม่บันทึก release marker
- ตรวจ `health` ก่อนส่งงาน และตรวจ output manifest หลัง render
- ใช้ worker registry เป็น source ของ enabled/disabled state
- ปิด worker ก่อน deploy ถ้าไม่ต้องการให้รับงานใหม่
- ห้ามใช้ token จริงใน command history, issue หรือเอกสาร
- อย่าใช้คำสั่ง destructive เช่น `git reset --hard` กับ worktree ที่มี changes ของ operator

## 2. Health Checklist

### Gateway

```bash
curl -fsS "$GATEWAY_BASE/healthz"
curl -fsS "$GATEWAY_BASE/api/version"
curl -fsS "$GATEWAY_BASE/api/cluster/health"
```

### Worker

```bash
curl -fsS "$WORKER_BASE/health"
curl -fsS "$WORKER_BASE/v1/capabilities" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN"
curl -fsS "$WORKER_BASE/v1/active_jobs" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN"
```

Health ที่ดีควรมี `ok=true`, worker อยู่ใน enabled registry, queue ไม่ค้าง และ encoder ที่ต้องการอยู่ใน `gpu.available` หรือ `encoder`

สำหรับ Apple Silicon ตรวจเพิ่มเติม:

```text
gpu.vt_ready = true
encoder[0] = h264_videotoolbox
encoder args มี -prio_speed 1 เมื่อเปิด optimized profile
```

## 3. Install และ Service

### Linux systemd

```bash
sudo bash deploy/install.sh gateway
sudo bash deploy/install.sh worker
sudo systemctl status v3-cursor-api-gateway.service
sudo systemctl status v3-cursor-api-worker.service
sudo journalctl -u v3-cursor-api-gateway.service -n 200 --no-pager
sudo journalctl -u v3-cursor-api-worker.service -n 200 --no-pager
```

ก่อนติดตั้งต้องกำหนด secret ผ่าน environment หรือ secret store:

```bash
export CUTDEE_INTERNAL_TOKEN='<INTERNAL_TOKEN>'
export CUTDEE_API_KEYS='<API_KEY_1>,<API_KEY_2>'
sudo -E bash deploy/install.sh gateway
```

ตรวจ Python version ด้วย `python3 --version`; project dependency ชุดปัจจุบันควรใช้ Python ไม่เกิน 3.13

### macOS LaunchAgent

M4 worker ใช้ LaunchAgent และต้อง reload plist หลังเปลี่ยน EnvironmentVariables:

```bash
PLIST="$HOME/Library/LaunchAgents/com.cutdee.v3-worker.plist"
launchctl bootout "gui/$(id -u)/com.cutdee.v3-worker" || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl print "gui/$(id -u)/com.cutdee.v3-worker"
```

ค่า optimize ที่ผ่าน acceptance บน M4:

```text
V3_APPLE_HWACCEL=1
V3_TC02_STREAMING=1
V3_TC02_PRODUCERS=2
V3_TC02_CONSUMERS=3
~/.green_pc/cpu_percent.txt = 100
```

ต้องใช้ `-hwaccel videotoolbox` สำหรับ input acceleration; ห้ามใช้ `h264_videotoolbox` เป็น decoder เพราะเป็น encoder name

## 4. Worker Registry

ตัวอย่างโดยใช้ placeholder:

```json
{
  "workers": [
    {
      "id": "m4-01",
      "name": "Apple M4",
      "url": "http://<worker-host>:8789",
      "tier": "high",
      "max_concurrent": 2,
      "enabled": true
    }
  ]
}
```

หลังแก้ registry:

```bash
curl -fsS -X POST "$GATEWAY_BASE/api/cluster/workers/reload" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN"
curl -fsS "$GATEWAY_BASE/api/cluster/health"
```

การปิด worker เพื่อ canary:

```bash
curl -fsS -X PATCH "$GATEWAY_BASE/api/cluster/workers/m4-01" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled":false}'
```

อย่าลบ worker ที่มี active job โดยไม่ตรวจ job state ก่อน

## 5. Release และ Canary

1. ตรวจ `git status`, `git diff`, `git log` และยืนยันว่าไม่มี secret
2. รัน `python3 -m pytest -q`
3. รัน `python3 -m compileall -q gateway worker tests`
4. สร้าง release marker จาก commit ที่ deploy จริง
5. ปิดหรือจำกัด worker ที่จะเปลี่ยน
6. deploy source และ restart service
7. ตรวจ Gateway/Worker health และ encoder
8. รัน TC01 smoke หนึ่งงาน
9. รัน TC02 หรือ workload representative
10. ตรวจ status, output count, codec, resolution, duration และ audio
11. เปิด worker กลับเมื่อ canary ผ่าน

เกณฑ์ผ่าน M4 benchmark fixture เดิม:

```text
TC01: succeeded, H.264/AAC, 1280x720, 10s
TC02: 21/21 valid, 1280x720, 10s ต่อ output
```

## 6. Incident: Worker Unhealthy

ตรวจตามลำดับ:

```bash
curl -fsS "$WORKER_BASE/health"
launchctl print "gui/$(id -u)/com.cutdee.v3-worker"  # macOS
systemctl status v3-cursor-api-worker.service       # Linux
df -h
```

สาเหตุที่พบบ่อย:

- FFmpeg ไม่อยู่ใน PATH ของ service
- internal token ไม่ตรงกับ Gateway
- port `8789` ถูก process เก่าจับอยู่
- disk ใกล้เต็ม
- encoder smoke test fail แล้ว fallback เป็น CPU
- worker process ถูก restart ระหว่าง render

หลังแก้ให้รอ health stable แล้วค่อย enable worker กลับ

## 7. Incident: Job ค้างหรือ Queue เต็ม

```bash
curl -fsS "$WORKER_BASE/v1/active_jobs" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN"
curl -fsS "$GATEWAY_BASE/api/cluster/jobs/live" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN"
```

ขั้นตอน:

1. ตรวจว่า job เป็น `queued`, `running` หรือ `cancelling`
2. ตรวจ worker load และ log tail
3. ส่ง cancel ผ่าน Gateway ถ้าเป็นงานที่ยกเลิกได้
4. รอ terminal state ก่อน restart service
5. ถ้า worker restart งาน active จะถูก mark เป็น failed ตาม recovery policy
6. ตรวจ partial files และ output manifest ก่อน cleanup

อย่าเพิ่ม `WORKER_MAX_CONCURRENT` เพียงเพื่อแก้ queue โดยไม่ benchmark CPU, memory และ disk I/O

## 8. Incident: Output Missing หรือ Download ไม่ได้

ตรวจ:

- job status เป็น `succeeded` หรือไม่
- `output_files` มีชื่อไฟล์จริงหรือไม่
- ชื่อไฟล์ไม่มี path separator และตรงกับ manifest หรือไม่
- Worker ยังมีไฟล์อยู่หรือไม่
- Gateway เลือก worker ID เดิมได้หรือไม่
- ownership ของ user ตรงกับ job หรือไม่

ใช้ output filename ที่ server คืนเท่านั้น ห้ามสร้างชื่อจาก pattern เอง

## 9. Cleanup และ Disk

Worker cleanup ใช้ internal endpoint:

```bash
curl -fsS -X POST "$WORKER_BASE/v1/admin/cleanup?days=7" \
  -H "X-Cutdee-Internal: $INTERNAL_TOKEN"
```

ก่อน cleanup ต้องตรวจว่าไม่มี active job และรักษา logs/manifest ที่จำเป็นต่อ audit

## 10. Rollback

1. ปิด worker release ใหม่ใน registry
2. รอ active jobs จบหรือ cancel อย่างตั้งใจ
3. restore release artifact ที่ผ่าน acceptance ก่อนหน้า
4. restart service และตรวจ health
5. รัน TC01 smoke
6. เปิด worker กลับเมื่อผ่าน

Rollback ต้องใช้ tagged artifact หรือ known commit ไม่ใช่การแก้ไฟล์แบบเดาสุ่มใน production
