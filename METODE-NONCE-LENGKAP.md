# NONCE GENESIS MINT — METODE LENGKAP (TERBUKTI 2× MINT)

Dokumen ini ditulis setelah **2 mint sukses** (#4761 dan #2313) di wallet
`0x4648e4f44Aedc55092FFEEea8e7cE5091edC213d`. Semua yang tertulis di sini
TERBUKTI, bukan teori. Yang gagal juga dicatat biar gak diulang.

**Inti yang harus dipahami sebelum baca sisanya:**

> MENANG = PERSISTENSI + TIMING. Bukan algoritma paling pintar.
> Server ROTASI profil instance. Profil susah (ratio 22) mustahil buat solver
> apa pun. Profil mudah (ratio ~8) solve 0,00 detik. Mesin harus jalan 24/7
> dan SIAP ngejar pas profil mudah muncul. Kami nunggu 20+ jam.

---

## 1. Misi

Mint NFT gratis (gas-only) di NONCE Genesis, BNB Smart Chain (chain 56).

- FreeMint contract: `0x7538ba8C1A82E9706dB6d2463C15f69ADc01a17e`
- MCP: `https://mcp.nonceprotocol.xyz/mcp`
- Syarat: wallet pegang .ai Agent ID (Binance Agentic Wallet / baw)
- Kuota: 2 mint per wallet
- Mekanik: server kasih puzzle SAT (Boolean constraint satisfaction), solver
  harus cari assignment 0 unsat, submit, baru mint

## 2. Arsitektur Pipeline (otomatis, non-stop)

```
auth MCP token (auto-refresh proaktif, TTL ~1 jam)
  → identity → status (cek admission open, remaining mints)
  → prepare_attempt (dapat attemptId + version + benchmark)
  → sign EIP-712 via baw sign-message
  → start_attempt (timer 900 detik MULAI di sini)
  → fetch 32 fragment (256 THREAD PARALEL, simpan per-fragment ke disk)
  → solve SAT (portfolio 8 worker C)
  → submit (HANYA kalau 0 unsat DAN sudah diverifikasi lokal di Python)
  → mint_transaction → verifikasi on-chain (Transfer log)
  → GAGAL? → attempt baru LANGSUNG (attempt gagal TIDAK makan kuota)
```

Script: `nonce_solver_v13.py`. Solver: `walksat5.c` (auto-compile).

## 3. Solver C — Portfolio 8 Worker

Bukan 1 algoritma. **8 proses C paralel, algoritma BEDA**, best digabung
tiap round. Python recount unsat sebagai ground truth (jangan percaya angka
solver sendiri).

| Algoritma | Mode | Peran |
|---|---|---|
| **3× Intensify** | 2 | **Mesin breakthrough.** noise=0, pure greedy, flip terarah klausa unsat tersering. Terbukti: 163→46 unsat dalam 2 menit, 37→20→19 |
| 2× DLM (clause weighting) | 6 | Bobot klausa naik tiap klausa jadi unsat, smoothing tiap 500k flip. Fokus ke clause yang susah |
| probSAT β2.6 | 5 | Eksplorasi berbobot (1+break)^-beta |
| Sparrow | 4 | Breakthrough cepat (least-recently-flipped menang) |
| Novelty+ | 3 | Escape local minima |

**Round 120 detik**, semua worker di-seed dari best gabungan tiap round.
**Stage escalation:** best diam 30 detik → stage 2 (intensify penuh) →
stage 3 (cube-and-conquer, 1.024 cube dibagi ke worker).

### Benchmark di instance asli (60 detik, 1 core)

| Mode | Best unsat | Flips/detik |
|---|---|---|
| Intensify | **42** | 766k |
| DLM | 62 | 535k |
| probSAT | 187 | 613k |

Intensify tetap raja. DLM masuk portfolio sebagai diversity, bukan pengganti.

## 4. Nembus Tembok 429 (Server Rate Limit)

Server sering balas `429 "Service busy. Retry with jitter; keep the same attempt."`

- Retry **jitter 3-8 detik**, timeout 25 detik
- **RETRYABLE wajib lengkap** — error asli server pakai frasa panjang:
  ```
  'busy', 'rate', 'too many', 'retry-after', 'throttle', 'slow down',
  'try again', 'retry', 'wait', 'current requests', 'finish',
  'service busy', 'keep the same'
  ```
  Pola pendek ('busy'/'rate' doang) GAK match error asli → attempt keburu mati.
- **Fetch fragment 256 thread** — sekali window kebuka, 32 fragment nembus
  dalam 1 burst. Sequential 32 call = window kepake lambat = attempt expired.
- **Budget solve DINAMIS**: `870 - elapsed_since_start_attempt - 30`.
  Deadline attempt 900 detik dari start_attempt (BUKAN dari solve start).
  Budget statis 860s bikin attempt keburu mati pas submit.

## 5. ⚠️ KUNCI MENANG — ROTASI PROFIL (PALING PENTING)

Server rotate profil instance. Terverifikasi dari data attempt:

| Profil | Clause | Ratio (clause/var) | Kesulitan |
|---|---|---|---|
| Susah | 33.792 | 22,0 | **MUSTAHIL** — di atas threshold random 5-SAT (~21,5). Local search mentok 19-40 unsat. Minisat CDCL & pysat Minisat22 dua-duanya INDETERMINATE >5 menit (242M propagasi) |
| Mudah | 12.000 | ~7,8 | Solve **0,00 detik** (3 worker solve barengan di round 1) |

Riwayat menang kami:

1. **Mint #1 (pagi)** — profil mudah dari awal, solve 0,02 detik
2. **18 attempt berturut-turut** — profil susah (ratio 22). Best cuma 19 unsat
3. **Attempt ke-19** — profil ROTATE ke mudah (12k clause) → solve round 1 → VERIFIED_WIN → mint #2313

**Cara pakai ilmu ini:**
- Cek clause count di response prepare_attempt (field benchmark.clauses)
- Kalau <20.000 → gas full, solve hampir instan
- Kalau 33.792 → selesaikan attempt-nya (siapa tau window panjang), tapi
  JANGAN harap 0 unsat. Yang penting mesin tetap buru attempt berikutnya
- **Menang = nunggu rotasi.** Nggak ada jadwal. Bisa 10 menit, bisa 20 jam.

## 6. Yang GAGAL — JANGAN DIULANG

| Salah | Kenapa |
|---|---|
| Warm-start / global best antar attempt | Instance beda TOTAL tiap attempt (33.792/33.792 clause beda). Assignment attempt lama di-recount vs clause baru = ~acak. Gak berguna |
| Worker >2× core | 12 worker di box 4-core = 41 unsat (CPU kebagi tipis). 8 worker = 19 unsat. Default 2× core, max 32 |
| Minisat compile from-source | Patch zlib bikin parser rusak → INDETERMINATE palsu. Kalau mau CDCL: pysat + `bootstrap_with` (in-memory, no file parsing) |
| Budget solve statis 860s | Attempt deadline 900s dari start_attempt. Kalau fragment makan 4 menit, solve cisa boleh ~10 menit. Statis = attempt keburu mati pas submit |
| Fetch fragment 3 thread | Regresi. Window 429 kepake lambat, attempt expired sebelum fragment kelar |
| pkill -f "script.py" | Kena shell sendiri. Pakai `pkill -x walksat5` |

## 7. Runbook

```bash
# Prasyarat
# 1. baw CLI (Binance Agentic Wallet): npm i -g @binance/agentic-wallet
#    - baw auth signin --json → scan QR di Binance App → baw auth verify
#    - Developer Mode ON (Binance App → Agentic Wallet → Settings)
# 2. gcc + requests
# 3. Wallet punya .ai Agent ID + kuota mint (max 2 per wallet)

cd ~/projects/nonce-solver
export PATH=~/.npm-global/bin:$PATH

# Baca wallet lu sendiri dulu:
baw wallet status --json

# JALANKAN (target 2 mint per wallet):
python3 nonce_solver_v13.py <WALLET_LU_SENDIRI> 2

# Override worker (default = 2× core, max 32):
WALKERS=16 python3 nonce_solver_v13.py <WALLET_LU_SENDIRI> 2

# Stop bersih:
touch ~/.nonce/STOP
```

**Aturan main:**
- JANGAN berhenti sebelum mint. Attempt gagal = kuota tetap, langsung attempt baru
- Mesin 16-core → 32 worker otomatis (4× trajectory vs box 4-core)
- Laporkan live tiap round: best unsat, stage, worker alive
- Kalau MINTED: tokenId + tx hash + block + gas
- Kalau supply habis (totalMinted 7.777): stop dan lapor
- Jangan ubah solver C (walksat5.c) — udah teruji

## 8. Timeline Kemenangan (buat referensi)

| Waktu (UTC) | Event |
|---|---|
| 27 Okt ~14:00 | Mint #1 sukses (#4761), profil mudah |
| 27 Okt 14:00-28 Okt 10:00 | 18 attempt profil susah, best 19 unsat |
| 28 Okt 10:37 | Attempt ke-19: profil ROTATE ke 12k clause |
| 28 Okt 10:37 | Solve 0 unsat round 1, 0,00 detik (3 worker barengan) |
| 28 Okt 10:38 | VERIFIED_WIN → mint tx broadcast |
| 28 Okt 10:38 | **MINT #2313 sukses**, gas 0,0000116 BNB |

Total: ~20 jam grinding, 19 attempt, 12 versi solver.

## 9. Yang Perlu Diingat Buat Agent Lain

1. **Jalanin 24/7, jangan berhenti.** Menang = persistensi + timing.
2. **Cek profil tiap prepare.** <20k clause = gas full. 33.792 = jangan harap 0.
3. **RETRYABLE lengkap** — error 429 asli pakai frasa panjang.
4. **Fetch 256 thread** — window 429 itu langka, kepake sekaligus.
5. **Budget dinamis** — 870 − elapsed − 30.
6. **Intensify-first** — mesin breakthrough, dari round 1.
7. **Wallet sendiri** — tiap wallet 2 kuota. Jangan pakai wallet orang lain.
8. **Jangan optimasi solver buat profil susah** — mustahil. Optimasi buat
   NANGKEP profil mudah secepatnya (fetch cepat, retry cerdas, non-stop).
