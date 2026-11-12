# Automation Idea: Story-to-Scene Google Flow Pipeline

## Tujuan

Membuat pipeline otomatis untuk menghasilkan konten berbasis cerita dengan alur:

1. Membuat ide cerita.
2. Memecah cerita menjadi beberapa scene.
3. Menghasilkan prompt detail untuk setiap scene menggunakan AI.
4. Menyimpan prompt secara lokal.
5. Membuka Google Flow dalam kondisi sudah login.
6. Mengambil prompt satu per satu dari lokal.
7. Paste prompt ke Google Flow.
8. Menunggu proses generate selesai sekitar 50-60 menit.
9. Mengambil hasil.
10. Melanjutkan ke scene berikutnya sampai semua selesai.

---

## Gambaran Arsitektur

```text
User Input
   ↓
Story Generator
   ↓
Scene Splitter
   ↓
Prompt Generator
   ↓
Local Prompt Queue
   ↓
Google Flow Automation Worker
   ↓
Generate / Wait / Collect Result
   ↓
Result Storage
   ↓
Next Scene
```

---

## Komponen Utama

### 1. Story Generator

Komponen ini bertugas membuat ide cerita utama berdasarkan input user.

Input contoh:

```json
{
  "topic": "anak kecil menemukan robot tua di hutan",
  "genre": "sci-fi adventure",
  "duration": "60 seconds",
  "style": "cinematic, emotional, family friendly"
}
```

Output contoh:

```json
{
  "title": "The Forest Robot",
  "logline": "Seorang anak menemukan robot rusak di hutan dan membantunya hidup kembali.",
  "synopsis": "Cerita pendek emosional tentang persahabatan antara anak kecil dan robot tua."
}
```

---

### 2. Scene Splitter

Komponen ini memecah cerita menjadi beberapa scene.

Output contoh:

```json
[
  {
    "scene_id": 1,
    "title": "The Discovery",
    "duration": "8s",
    "description": "Anak kecil berjalan di hutan berkabut dan melihat benda logam tersembunyi di balik semak."
  },
  {
    "scene_id": 2,
    "title": "First Contact",
    "duration": "10s",
    "description": "Anak itu menyentuh robot tua dan mata robot mulai menyala redup."
  }
]
```

---

### 3. Prompt Generator

Setiap scene diubah menjadi prompt siap pakai untuk Google Flow.

Prompt harus konsisten secara karakter, visual style, camera movement, lighting, dan mood.

Struktur prompt:

```text
Scene {scene_id}: {scene_title}

Visual Description:
{detailed visual description}

Character Consistency:
{character details}

Camera:
{camera movement, lens, shot type}

Lighting:
{lighting style}

Mood:
{emotional tone}

Style:
{global visual style}

Negative Prompt:
{things to avoid}
```

Output lokal disimpan sebagai JSON:

```json
{
  "project_id": "forest_robot_20260914",
  "status": "queued",
  "scenes": [
    {
      "scene_id": 1,
      "status": "pending",
      "prompt": "cinematic shot of a young child walking through a misty forest...",
      "result_path": null,
      "started_at": null,
      "completed_at": null,
      "error": null
    }
  ]
}
```

---

## Struktur Folder Lokal

```text
v2fun_data/
└── google_flow_projects/
    └── forest_robot_20260914/
        ├── project.json
        ├── story.json
        ├── scenes.json
        ├── prompts/
        │   ├── scene_001.txt
        │   ├── scene_002.txt
        │   └── scene_003.txt
        ├── results/
        │   ├── scene_001.mp4
        │   ├── scene_002.mp4
        │   └── scene_003.mp4
        ├── screenshots/
        │   ├── scene_001_submitted.png
        │   └── scene_001_completed.png
        └── logs/
            └── automation.log
```

---

## Automation Worker

Automation worker bertugas menjalankan Google Flow secara browser automation.

### Asumsi

- Browser sudah login ke akun Google.
- Session/profile browser disimpan dan digunakan ulang.
- Google Flow tidak perlu login ulang setiap scene.
- Prompt tersedia di folder lokal.
- Worker hanya memproses satu scene dalam satu waktu agar stabil.

---

## Browser Session Strategy

Gunakan persistent browser context agar login Google tetap tersimpan.

Contoh konsep Playwright:

```python
context = await playwright.chromium.launch_persistent_context(
    user_data_dir="v2fun_data/browser_profiles/google_flow_main",
    headless=False,
    viewport={"width": 1920, "height": 1080},
    args=[
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox"
    ]
)
```

Keuntungan:

- Login Google tetap tersimpan.
- Tidak perlu OAuth berulang.
- Lebih stabil untuk long-running automation.
- Bisa dicek manual jika flow berhenti.

---

## Scene Processing Flow

```text
Start Worker
   ↓
Load project.json
   ↓
Find first scene with status = pending
   ↓
Open Google Flow
   ↓
Wait until editor ready
   ↓
Read local prompt file
   ↓
Paste prompt into input/editor
   ↓
Click generate/submit
   ↓
Mark scene = processing
   ↓
Wait 50-60 minutes
   ↓
Check result status
   ↓
If completed:
      download/export result
      save to results/scene_xxx.mp4
      mark scene = completed
   ↓
If failed/timeout:
      save screenshot
      mark scene = failed
   ↓
Move to next pending scene
```

---

## Status Lifecycle

Setiap scene memiliki status:

```text
pending → processing → completed
                    ↘ failed
                    ↘ timeout
                    ↘ manual_review
```

### Status Meaning

| Status | Meaning |
|--------|---------|
| `pending` | Prompt sudah ada tapi belum diproses |
| `processing` | Sudah dikirim ke Google Flow |
| `completed` | Hasil berhasil diambil |
| `failed` | Generate gagal |
| `timeout` | Tidak selesai dalam batas waktu |
| `manual_review` | Butuh cek manual |

---

## project.json Schema

```json
{
  "project_id": "forest_robot_20260914",
  "title": "The Forest Robot",
  "created_at": "2026-09-14T18:35:00",
  "status": "processing",
  "current_scene": 1,
  "settings": {
    "wait_minutes": 60,
    "max_retry_per_scene": 2,
    "browser_profile": "google_flow_main",
    "headless": false
  },
  "scenes": [
    {
      "scene_id": 1,
      "title": "The Discovery",
      "prompt_file": "prompts/scene_001.txt",
      "status": "pending",
      "attempts": 0,
      "started_at": null,
      "completed_at": null,
      "result_file": null,
      "google_flow_url": null,
      "error": null
    }
  ]
}
```

---

## Polling dan Waiting Strategy

Karena proses Google Flow bisa 50-60 menit, automation sebaiknya tidak hanya menggunakan `sleep(3600)` tanpa checkpoint.

Gunakan checkpoint loop:

```text
Total wait: 60 minutes
Check interval: 2 minutes
Max checks: 30
```

Pseudo flow:

```python
for check in range(30):
    await asyncio.sleep(120)
    status = await check_google_flow_status(page)

    if status == "completed":
        await download_result(page, scene_id)
        break

    if status == "failed":
        mark_failed(scene_id)
        break

    save_checkpoint(scene_id, check)
```

Keuntungan:

- Bisa tahu progress.
- Bisa screenshot berkala.
- Bisa recover jika browser crash.
- Tidak kehilangan state.

---

## Result Collection

Metode pengambilan hasil tergantung UI Google Flow.

Prioritas metode:

1. Klik tombol download/export jika tersedia.
2. Ambil URL video/image dari DOM/network response.
3. Download file dari resource URL.
4. Jika tidak bisa otomatis, simpan screenshot dan tandai `manual_review`.

Output result:

```json
{
  "scene_id": 1,
  "status": "completed",
  "result_file": "results/scene_001.mp4",
  "completed_at": "2026-09-14T19:42:00",
  "duration_minutes": 57
}
```

---

## Retry Strategy

Jika scene gagal:

```text
Attempt 1 failed
   ↓
Save screenshot + error log
   ↓
Refresh Google Flow
   ↓
Retry same prompt
   ↓
If attempt 2 failed
   ↓
Mark manual_review
   ↓
Continue next scene
```

Konfigurasi:

```json
{
  "max_retry_per_scene": 2,
  "retry_delay_minutes": 5,
  "continue_on_failure": true
}
```

---

## Recovery Strategy

Jika script berhenti atau komputer restart:

1. Baca ulang `project.json`.
2. Cari scene dengan status `processing`.
3. Cek apakah hasil sudah muncul di Google Flow.
4. Jika hasil ada, download dan mark `completed`.
5. Jika tidak ada dan sudah melewati timeout, mark `timeout` atau retry.
6. Lanjut ke scene berikutnya.

Dengan ini automation bisa dilanjutkan tanpa mulai dari awal.

---

## Logging

Semua aktivitas dicatat ke:

```text
v2fun_data/google_flow_projects/{project_id}/logs/automation.log
```

Contoh log:

```text
[2026-09-14 18:35:00] Project started: forest_robot_20260914
[2026-09-14 18:35:10] Scene 001 loaded
[2026-09-14 18:35:15] Prompt pasted to Google Flow
[2026-09-14 18:35:20] Generate clicked
[2026-09-14 18:37:20] Checkpoint 1/30: still processing
[2026-09-14 19:31:20] Scene 001 completed
[2026-09-14 19:31:25] Result saved: results/scene_001.mp4
```

---

## Main Modules Proposal

```text
v2fun_scripts/
├── google_flow_story_pipeline.py      # Main orchestrator
├── google_flow_prompt_generator.py    # Story → scenes → prompts
├── google_flow_worker.py              # Browser automation worker
├── google_flow_storage.py             # Project state / JSON handling
├── google_flow_downloader.py          # Result extraction/download
└── google_flow_recovery.py            # Resume interrupted projects
```

---

## CLI Commands Proposal

### Create Project

```bash
python v2fun_scripts/google_flow_story_pipeline.py create \
  --topic "anak kecil menemukan robot tua di hutan" \
  --genre "sci-fi adventure" \
  --scenes 8
```

### Generate Prompts Only

```bash
python v2fun_scripts/google_flow_story_pipeline.py prompts \
  --project forest_robot_20260914
```

### Run Automation

```bash
python v2fun_scripts/google_flow_story_pipeline.py run \
  --project forest_robot_20260914
```

### Resume Automation

```bash
python v2fun_scripts/google_flow_story_pipeline.py resume \
  --project forest_robot_20260914
```

### Check Status

```bash
python v2fun_scripts/google_flow_story_pipeline.py status \
  --project forest_robot_20260914
```

---

## Technical Risks

| Risk | Mitigation |
|------|------------|
| Google Flow UI berubah | Gunakan selector berlapis dan screenshot debug |
| Session Google expired | Gunakan persistent profile dan manual relogin fallback |
| Generate sangat lama | Gunakan checkpoint loop, bukan sleep panjang tunggal |
| Browser crash | Simpan state setelah setiap step |
| Download button tidak terdeteksi | Fallback ke network capture/resource URL |
| Rate limit | Tambah delay antar scene dan multi-profile rotation |

---

## Recommended MVP

Tahap pertama cukup buat MVP dengan fitur:

1. Input topic cerita.
2. Generate scene list + prompt lokal.
3. Buka Google Flow dengan persistent login.
4. Paste prompt scene pertama.
5. Klik generate.
6. Wait dengan checkpoint per 2 menit.
7. Ambil hasil/manual review.
8. Lanjut scene berikutnya.
9. Resume dari `project.json` jika berhenti.

---

## MVP Execution Loop

```python
async def run_project(project_id):
    project = load_project(project_id)
    context = await open_google_flow_profile(project["settings"]["browser_profile"])
    page = await open_google_flow(context)

    for scene in project["scenes"]:
        if scene["status"] == "completed":
            continue

        if scene["status"] in ["pending", "timeout", "failed"]:
            await process_scene(page, project, scene)
            save_project(project)

    project["status"] = "completed"
    save_project(project)
```

---

## Kesimpulan

Skema automation ini memungkinkan dan cocok dibuat sebagai pipeline terpisah dari V2Fun automation saat ini.

Kunci teknisnya adalah:

1. Gunakan persistent browser profile agar Google Flow tetap login.
2. Simpan semua prompt dan state secara lokal.
3. Proses scene satu per satu agar stabil.
4. Gunakan checkpoint setiap beberapa menit.
5. Jangan close browser/popup sebelum result benar-benar selesai.
6. Buat resume system agar automation tahan crash.

Dengan rancangan ini, automation bisa berjalan panjang selama beberapa jam untuk menyelesaikan semua scene secara bertahap.
