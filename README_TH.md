# TikTok Parcel Manager — Deploy HTTPS บน VPS

## สถานะและข้อจำกัด

ชุดนี้เป็นต้นแบบที่มี Docker Compose, FastAPI, SQLite persistent volume และ Caddy สำหรับ HTTPS อัตโนมัติ **ยังไม่ได้ deploy จริง** และ **ยังไม่ได้ยืนยันการเชื่อม J&T/ETrackings ด้วย API Key จริง**. ไม่สามารถดึงคำสั่งซื้อจาก TikTok บัญชีผู้ซื้ออัตโนมัติได้; ต้องนำเลขพัสดุเข้าระบบเอง/CSV. ใช้เฉพาะเลขพัสดุที่มีสิทธิ์เข้าถึง.

## สิ่งที่ต้องมี

- VPS Ubuntu 24.04 ที่มี public IPv4, Docker Engine + Docker Compose plugin (คำสั่ง `docker compose version` ต้องใช้ได้)
- โดเมน เช่น `parcel.example.com` และสิทธิ์ตั้งค่า DNS
- เปิด inbound TCP 80, TCP 443 (และ UDP 443 ถ้าต้องการ HTTP/3) ที่ firewall/cloud firewall; เปิด SSH เฉพาะที่จำเป็น
- ห้ามนำ `.env` ขึ้น Git หรือส่ง API token ให้ผู้อื่น

## ขั้นตอนติดตั้ง

1. แตก ZIP แล้วอัปโหลดโฟลเดอร์นี้ไปที่ VPS ผ่าน SCP/SFTP
2. สร้าง DNS A record: `parcel.example.com` → public IP ของ VPS และรอ DNS ทำงาน
3. SSH เข้า VPS แล้วเข้าโฟลเดอร์ที่มี `compose.yaml`
4. `cp .env.example .env`
5. สร้าง token: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` แล้วนำผลไปแทนค่า `APP_API_TOKEN` ใน `.env` และเปลี่ยน `DOMAIN` เป็นโดเมนจริง
6. ป้องกันไฟล์: `chmod 600 .env`
7. เริ่มระบบ: `docker compose up -d --build`
8. ตรวจสอบ: `docker compose ps` และ `docker compose logs --tail=80 caddy`
9. เปิด `https://parcel.example.com/ui/` และกรอก `APP_API_TOKEN` เพื่อใช้งาน
10. ทดสอบ `https://parcel.example.com/health` ควรได้ `ok:true`; `provider_configured:false` เป็นปกติถ้ายังไม่มี API key

Caddy จะขอและต่ออายุใบรับรอง TLS ให้อัตโนมัติเมื่อ DNS และพอร์ต 80/443 พร้อมใช้งาน. หากโดเมนยังไม่ชี้มาที่เซิร์ฟเวอร์ HTTPS อาจยังไม่สำเร็จ.

## นำเข้าข้อมูล

สร้าง CSV แบบ UTF-8:

```csv
account,tracking_no
buyer_001,TRACKING_NUMBER_1
buyer_002,TRACKING_NUMBER_2
```

นำเข้าผ่านหน้าเว็บ. ระบบใช้ token เดียวสำหรับทุกคน ไม่ใช่ระบบผู้ใช้หลายสิทธิ์ จึงควรจำกัดผู้ใช้งานไว้เฉพาะเจ้าของก่อน.

## API J&T

กรอก `ETRACKINGS_API_KEY`, `ETRACKINGS_KEY_SECRET`, `ETRACKINGS_JNT_COURIER` เฉพาะเมื่อได้รับและยืนยัน API endpoint, auth header, payload, รหัส carrier และ response schema กับผู้ให้บริการจริง. โค้ด `fetch_tracking` เป็นตัวเชื่อมต้นแบบที่อาจต้องปรับ. เริ่มจาก `ENABLE_POLLING=false` และทดสอบ 1 เลขพัสดุก่อนเปิด `true`. การตั้ง `POLL_MINUTES=60` และ `POLL_BATCH_SIZE=25` ไม่รับประกันว่าตรวจครบทุกพัสดุภายในหนึ่งชั่วโมงเมื่อมีรายการมาก.

## สำรองข้อมูล

ตรวจชื่อ volume: `docker volume ls` แล้วสำรอง SQLite โดยใช้ SQLite backup API หรือหยุด app ก่อนคัดลอก volume (อย่าคัดลอกเฉพาะไฟล์ `.sqlite3` ระหว่างที่ WAL ยังทำงาน). ทดสอบการกู้คืนเป็นระยะ.

## ข้อควรทำก่อนเปิดให้ใช้งานวงกว้าง

เพิ่มระบบล็อกอินรายผู้ใช้, rate limit, audit logs, จัดการข้อมูลส่วนบุคคล, กำหนด retention, จำกัดขนาดไฟล์ CSV, แยก scheduler เป็น worker และเปลี่ยนฐานข้อมูลเป็น PostgreSQL หากมีหลาย instance. ปัจจุบัน API token เป็น shared secret ที่กรอกใน browser และ backend ไม่มี rate limiting; ห้ามเผยแพร่ token สู่สาธารณะ.
