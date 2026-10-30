# ============================================================================
# NONCE GENESIS MINT - AGENT INSTRUCTIONS (self-contained)
# ============================================================================
# MISSION : Mint 1 NONCE Genesis NFT (free + BNB gas only) on BNB Smart Chain
#           for the wallet below. Keep retrying until minted (failed attempts
#           do not consume quota). Stop only when supply runs out.
# WALLET  : 0x4648e4f44Aedc55092FFEEea8e7cE5091edC213d (Binance Agentic Wallet,
#           holds .ai Agent ID "puiyi.ai")
# USAGE   : python3 nonce_solver_v9.py <WALLET_BSC> <TARGET_MINT=1>
#           env WALKERS=<n> to override worker count (default: CPU cores, max 16)
#
# PREREQUISITES:
#   1. baw CLI (Binance Agentic Wallet): npm i -g @binance/agentic-wallet
#      - sign in: baw auth signin --json  (scan QR in Binance App)
#                baw auth verify --qrCodeId <id> --json
#      - Developer Mode ON (Binance App -> Agentic Wallet -> Settings)
#   2. gcc (auto-compiles walksat5.c in this folder)
#   3. python packages: requests
#
# WHAT IT DOES (fully automatic, no agent thinking needed):
#   auth (MCP token, auto-refresh 15min before expiry)
#   -> prepare_attempt -> sign EIP-712 via baw -> start_attempt
#   -> fetch all fragments (PARALLEL 8 threads, saved to ~/.nonce/)
#   -> stage 0: spectral guess test (discarded if not better than random)
#   -> stage 1: N parallel C solvers (probSAT/Sparrow/Novelty+/intensify),
#      best merged with Python ground-truth recount, best file persistent
#   -> stage escalation: best no-improve 60s -> intensify -> cube-and-conquer
#   -> submit ONLY when 0 unsat AND verified locally
#   -> mint_transaction -> verify on-chain (Transfer log) -> report
#   -> on failure: new attempt automatically (quota is not consumed)
#
# STOP: touch ~/.nonce/STOP for clean exit. Reports every development to stdout.
# NOTE: MCP server may return 429 "Service busy" - the script retries with
#       jitter automatically, exactly as the server instructs. Be patient.
# ============================================================================

# NONCE Genesis mint v13 (28 Okt 2026) - v11 + RETRYABLE lengkap (dari v12 agent lain); 256 thread; warm-start DIBUANG (instance beda tiap attempt)
# Install: pip install web3 requests eth-abi ; npm i -g @binance/agentic-wallet ; butuh gcc
# Pairing: baw auth signin --json ; baw auth verify --qrCodeId <id> --json
# Jalankan: python3 nonce_solver_v4.py <WALLET_BSC> <TARGET_MINT=1>
#
# Alur: auth (refresh proaktif menit ke-45) -> prepare (simpan file sebelum sign) ->
#       start_attempt -> ambil semua fragment (simpan per-fragment + klausa) ->
#       tahap 0 spektral (power iteration, bandingin vs acak) ->
#       stage 1: 4 worker walksat2 paralel (mode normal), best digabung + recount python ->
#       stage 2 (best gak improve 120s): mode intensify (noise off, flip terarah klausa unsat tersering) ->
#       stage 3: cube-and-conquer 8 variabel tersering (256 cube, 4 worker bagi range) ->
#       submit cuma kalau 0 unsat & verified -> mint. Attempt gagal = attempt baru otomatis.
#       STOP: sentuh file ~/.nonce/STOP buat exit bersih.
# Mint gratis, cuma gas BNB. Risiko ditanggung sendiri.
import json, subprocess, sys, time, uuid, random, requests
import os, shutil

B = os.environ.get("BAW_BIN") or (os.path.expanduser("~/.npm-global/bin/baw")
    if os.path.exists(os.path.expanduser("~/.npm-global/bin/baw")) else "baw")
E = "https://mcp.nonceprotocol.xyz"
W = sys.argv[1]
FREEMINT = "0x7538ba8C1A82E9706dB6d2463C15f69ADc01a17e"
DATA = os.path.expanduser('~/.nonce'); os.makedirs(DATA, exist_ok=True)
AUTH = os.path.join(DATA, 'auth_%s.json' % W.lower()[:10])
STOP = os.path.join(DATA, 'STOP')
HERE = os.path.dirname(os.path.abspath(__file__))
C_SRC = os.path.join(HERE, 'walksat5.c')
C_BIN = os.path.join(DATA, 'walksat5')
ROUND_SECS = int(os.environ.get('ROUND_SECS', '120'))
# Default: 2x CPU cores (oversubscribe = portfolio diversity, CPU tetap 100%)
WORKERS = int(os.environ.get("WALKERS", str(min((os.cpu_count() or 4) * 2, 32))))
TOKEN_TTL = 45 * 60          # refresh proaktif menit ke-45

def log(*a): print(*a, flush=True)

def baw(*a):
    r = subprocess.run([B, *a, "--json"], capture_output=True, text=True, timeout=300)
    try: return json.loads(r.stdout)
    except Exception: return {"raw": (r.stdout + r.stderr)[-400:]}

def post(path, body, tries=30):
    for i in range(tries):
        try:
            r = requests.post(E + path, json=body, timeout=40)
            j = r.json()
            if r.status_code >= 500 or 'busy' in json.dumps(j).lower() or r.status_code in (429, 503):
                time.sleep(random.uniform(3, 8)); continue
            return j
        except Exception:
            time.sleep(random.uniform(10, 20))
    raise SystemExit("POST failed " + path)

TOKEN = None
AUTH_AT = 0.0
_EXP = 0.0
CRED = ('credential', 'unauthorized', 'expired', 'invalid token', 'not authenticated', 'unauthenticated')

def reauth():
    global TOKEN, AUTH_AT, _EXP
    log('credential expired -> re-auth ...')
    try: os.remove(AUTH)
    except OSError: pass
    TOKEN = None; _EXP = 0.0
    auth()

RETRYABLE = ('busy', 'rate', 'too many', 'retry-after', 'throttle', 'slow down', 'try again', 'retry',
             'wait', 'current requests', 'finish', 'service busy', 'keep the same')

def _is_retryable(em):
    return any(k in em for k in RETRYABLE)

def call(name, args, auth=True, tries=400):
    global TOKEN
    reauth_n = 0
    for i in range(tries):
        # refresh proaktif: 15 menit sebelum expiry absolut, jangan tunggu 401
        if auth and TOKEN and _EXP and time.time() > _EXP - 900:
            reauth()
        h = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if auth: h["authorization"] = "Bearer " + (TOKEN or "")
        try:
            r = requests.post(E + "/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}, timeout=25)
            if r.status_code >= 500:
                time.sleep(random.uniform(3, 8)); continue
            if r.status_code in (401, 403):
                if auth and reauth_n < 3:
                    reauth_n += 1; reauth(); continue
                return {'_error': 'http %d after reauth' % r.status_code}
            t = r.text
            if "data:" in t[:30] or t.startswith("event:"):
                t = [l[5:] for l in t.splitlines() if l.startswith("data:")][-1]
            j = json.loads(t)
        except Exception:
            time.sleep(random.uniform(3, 8)); continue
        if 'error' in j:
            em = json.dumps(j['error']).lower()
            if any(k in em for k in CRED):
                if auth and reauth_n < 3:
                    reauth_n += 1; reauth(); continue
                return {'_error': j['error']}
            if _is_retryable(em):
                time.sleep(random.uniform(3, 8)); continue
            return {'_error': j['error']}
        if 'result' not in j:
            time.sleep(random.uniform(3, 8)); continue
        res = j['result']; txt = res['content'][0]['text']
        try: out = json.loads(txt)
        except Exception: out = txt
        if res.get('isError'):
            s = json.dumps(out).lower()
            if _is_retryable(s):
                time.sleep(random.uniform(3, 8)); continue
            return {'_isError': out}
        return out
    raise SystemExit("call failed " + name)

def sign712(wallet_request):
    p = baw("sign-message", "preview", "--binanceChainId", "56", "--signType", "EIP712", "--message", json.dumps(wallet_request))
    rid = (p.get('data') or {}).get('requestId')
    if not rid: raise SystemExit("sign preview failed " + json.dumps(p)[:400])
    e = baw("sign-message", "execute", "--requestId", rid)
    d = e.get('data') or {}
    sig = d.get('signature')
    if not sig and d.get('orderId'):
        for i in range(60):
            time.sleep(5)
            r = baw("sign-message", "result", "--order-id", d['orderId'])
            sig = (r.get('data') or {}).get('signature')
            if sig: break
    if not sig: raise SystemExit("sign failed " + json.dumps(e)[:400])
    return sig, d.get('signatureRecovery')

def auth():
    global TOKEN, AUTH_AT, _EXP
    try:
        a = json.load(open(AUTH))
        if a.get('token') and time.time() < (a.get('_exp', 0) - 1500):
            TOKEN = a['token']; AUTH_AT = time.time(); _EXP = a.get('_exp', 0); return
    except Exception: pass
    ch = post("/auth/challenge", {"wallet": W})
    td = ch['typedData']
    assert td['domain']['name'] == 'NONCE MCP Sign In' and int(td['domain']['chainId']) == 56
    assert td['message']['wallet'].lower() == W.lower(), "wallet mismatch"
    sig, rec = sign712(ch['walletRequest'])
    body = {"wallet": td['message']['wallet'], "challenge": ch['challenge'], "signature": sig}
    if rec is not None: body['signatureRecovery'] = rec
    v = post("/auth/verify", body)
    if not v.get('token'): raise SystemExit("verify failed " + json.dumps({k: v[k] for k in v if k != 'token'})[:300])
    v['_exp'] = time.time() + 3600
    fd = os.open(AUTH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.write(fd, json.dumps(v).encode()); os.close(fd)
    TOKEN = v['token']; AUTH_AT = time.time(); _EXP = v['_exp']; log("auth ok (token fresh)")

# ---------------- solver infra ----------------

def ensure_binary():
    need = (not os.path.exists(C_BIN)) or (os.path.exists(C_SRC) and os.path.getmtime(C_SRC) > os.path.getmtime(C_BIN))
    if need:
        log("compiling", os.path.basename(C_SRC), "...")
        subprocess.run(['gcc', '-O3', '-march=native', '-funroll-loops', '-o', C_BIN, C_SRC, '-lm'], check=True)
    os.chmod(C_BIN, 0o755)

def write_cnf(clauses, n, path):
    with open(path, 'w') as f:
        f.write('%d %d\n' % (n, len(clauses)))
        for c in clauses:
            f.write(' '.join(str(int(l)) for l in c) + '\n')

def unsat_count(assign, clauses):
    ms = set(i + 1 for i, ch in enumerate(assign) if ch == '1')
    u = 0
    for c in clauses:
        if not any((l > 0 and l in ms) or (l < 0 and -l not in ms) for l in c):
            u += 1
    return u

def unsat_clauses(assign, clauses):
    ms = set(i + 1 for i, ch in enumerate(assign) if ch == '1')
    return [c for c in clauses if not any((l > 0 and l in ms) or (l < 0 and -l not in ms) for l in c)]

# ---------------- tahap 0: tebakan spektral ----------------

def spectral_guess(clauses, n):
    """Graf bertanda antar variabel (searah +1 / berlawanan -1), power iteration adjacency."""
    w = {}
    for c in clauses:
        k = len(c)
        for i in range(k):
            for j in range(i + 1, k):
                a, b = abs(c[i]), abs(c[j])
                key = (a, b) if a < b else (b, a)
                w[key] = w.get(key, 0.0) + (1.0 if (c[i] > 0) == (c[j] > 0) else -1.0)
    x = [0.0] * (n + 1)
    for i in range(1, n + 1): x[i] = random.random() - 0.5
    for _ in range(60):
        y = [0.0] * (n + 1)
        for (i, j), v in w.items():
            y[i] += v * x[j]; y[j] += v * x[i]
        norm = sum(abs(t) for t in y[1:]) or 1.0
        x = [0.0] + [t / norm for t in y[1:]]
    pos = ''.join('1' if x[i + 1] > 0 else '0' for i in range(n))
    neg = ''.join('1' if x[i + 1] <= 0 else '0' for i in range(n))
    return (pos, unsat_count(pos, clauses)) if unsat_count(pos, clauses) <= unsat_count(neg, clauses) else (neg, unsat_count(neg, clauses))

# ---------------- solver utama (stage 1/2/3) ----------------

def _parse_worker(out):
    u, a = None, None
    for line in out.splitlines():
        if line.startswith('RESULT'): u = int(line.split()[2])
        elif line.startswith('ASSIGN'): a = line.split()[1]
        elif line.startswith('STATS'): log('  solver', line)
    return u, a

def _profiles(stage):
    """Portfolio: probSAT (5, beta) + Sparrow (4) + Novelty+ (3) + WalkSAT (1) + intensify (2) + DLM (6).
    Beta probSAT diskala per k=5 (dari bundel Ray): b1=2.6, b2=4.0, b3=1.9."""
    if stage == 1:
        return [('2', 4, 0, 0, 0), ('2', 4, 0, 0, 0), ('6', 4, 0, 0, 0), ('5', 8, 1, 2, 2.6),
                ('4', 8, 1, 2, 0), ('3', 8, 1, 8, 0), ('2', 4, 0, 0, 0), ('6', 4, 0, 0, 0)]
    if stage == 2:
        return [('2', 4, 0, 0, 0), ('6', 4, 0, 0, 0), ('2', 4, 0, 0, 0), ('6', 4, 0, 0, 0),
                ('5', 8, 1, 2, 1.9), ('4', 6, 1, 4, 0), ('3', 6, 1, 4, 0), ('2', 4, 0, 0, 0)]
    return [('2', 4, 0, 0, 0), ('6', 4, 0, 0, 0), ('2', 4, 0, 0, 0), ('6', 4, 0, 0, 0),
            ('4', 6, 1, 4, 0), ('1', 4, 0, 0, 0), ('3', 6, 1, 8, 0), ('1', 2, 0, 0, 0)]

def solve(aid, cap, n, fc, bm=None, t_attempt0=None):
    # --- ambil semua fragment, simpan per-fragment ke disk sambil jalan ---
    clauses = [None] * fc
    def _fetch(i):
        r = call('inspect_fragment', {'attemptId': aid, 'capability': cap, 'fragmentId': i})
        cl = (r.get('fragment') or {}).get('clauses') if isinstance(r, dict) else None
        if cl is None: raise SystemExit("frag err %d %s" % (i, json.dumps(r)[:300]))
        json.dump(cl, open(os.path.join(DATA, 'frag_%s_%d.json' % (aid[:8], i)), 'w'))
        return i, cl
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=256) as ex:
        for i, cl in ex.map(_fetch, range(fc)):
            clauses[i] = cl
    clauses = [c for part in clauses for c in part]
    if t_attempt0 is not None:
        budget = max(60.0, 870.0 - (time.time() - t_attempt0) - 30.0)
    else:
        budget = 860.0
    log("fragments", fc, "clauses", len(clauses), "vars", n, "workers", WORKERS, "budget %.0fs" % budget)
    if bm and bm.get('clauses') and int(bm['clauses']) != len(clauses):
        log('WARN benchmark clauses', bm['clauses'], '!= fetched', len(clauses))
    json.dump(clauses, open(os.path.join(DATA, 'cl_%s.json' % aid[:8]), 'w'))
    ensure_binary()
    cnf = os.path.join(DATA, 'cl_%s.cnf' % aid[:8])
    write_cnf(clauses, n, cnf)

    shared = os.path.join(DATA, 'best_%s.txt' % aid[:8])
    wfiles = [os.path.join(DATA, 'best_%s_w%d.txt' % (aid[:8], i)) for i in range(WORKERS)]
    for pth in [shared] + wfiles:
        try: os.remove(pth)
        except OSError: pass

    # --- tahap 0: spektral vs acak ---
    spec, u_spec = spectral_guess(clauses, n)
    rnd = ''.join(random.choice('01') for _ in range(n))
    u_rnd = unsat_count(rnd, clauses)
    log('TAHAP 0 spektral: unsat spektral=%d vs acak=%d' % (u_spec, u_rnd))
    if u_spec < u_rnd:
        with open(shared, 'w') as f: f.write(spec)
        log('spektral lebih bagus -> dipakai sebagai seed awal')
    else:
        log('spektral tidak terbukti -> dibuang, mulai dari acak')

    best, best_u = None, None
    deadline = time.time() + budget
    stage = 1
    last_improve = time.time()
    rnd_i = 0
    while time.time() < deadline:
        if os.path.exists(STOP): raise SystemExit('STOP file detected')
        rnd_i += 1
        left = int(deadline - time.time())
        if left < 3: break
        secs = min(left, ROUND_SECS)          # round dibatasi 120s biar eskalasi tahap beneran kepakai
        profs = _profiles(stage)
        cubes = None
        if stage >= 3:
            base = best if best is not None else (open(shared).read().strip() if os.path.exists(shared) else None)
            if base and len(base) == n:
                uc = unsat_clauses(base, clauses)
                freqv = {}
                for c in uc:
                    for l in c: freqv[abs(l)] = freqv.get(abs(l), 0) + 1
                top = sorted(freqv, key=lambda v: -freqv[v])[:10]
            else:
                top = list(range(1, 11))
            cubef = os.path.join(DATA, 'cube_%s.txt' % aid[:8])
            with open(cubef, 'w') as f:
                for mask in range(1024):
                    f.write('10 ' + ' '.join(map(str, top)) + ' ' + ' '.join('1' if (mask >> i) & 1 else '0' for i in range(10)) + '\n')
            cubes = cubef
            log('stage 3 cube-and-conquer: top vars', top)
        if os.path.exists(shared):
            for wf in wfiles:
                try: shutil.copyfile(shared, wf)
                except OSError: pass
        if getattr(solve, '_win', None) is not None:
            profs = list(profs); profs[0] = _profiles(stage)[solve._win % len(_profiles(stage))]
        procs, seeds = [], []
        for i in range(WORKERS):
            s = random.getrandbits(48); seeds.append(s)
            mode, nshift, wnum, wden, beta = profs[i % len(profs)]
            cmd = [C_BIN, cnf, str(s), str(secs), '50000', wfiles[i], mode]
            if cubes:
                lo = (1024 * i) // WORKERS; hi = (1024 * (i + 1)) // WORKERS
                cmd += [cubes, str(lo), str(hi)]
            else:
                cmd += ['', '', '', str(nshift)]
                if mode == '3': cmd += [str(wnum), str(wden)]
                if mode == '5': cmd += [str(beta)]
            procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
        log('round', rnd_i, 'stage', stage, 'secs', secs, 'profiles', [p[0] for p in profs[:WORKERS]], 'seeds', seeds)
        solved = None
        # poll-based: worker yang solve bikin sisanya di-kill INSTANT (gak nunggu round penuh)
        while any(p.poll() is None for p in procs):
            for p in procs:
                if p.poll() is not None and not getattr(p, '_read', False):
                    p._read = True
                    try: out = p.stdout.read() or ''
                    except Exception: out = ''
                    u, a = _parse_worker(out)
                    if a is not None and len(a) == n and u == 0 and unsat_count(a, clauses) == 0:
                        solved = a; break
            if solved: break
            time.sleep(0.3)
        for p in procs:
            if p.poll() is None:
                p.kill(); p.wait()
        for p in procs:
            if getattr(p, '_read', False): continue
            p._read = True
            try: out = p.stdout.read() or ''
            except Exception: out = ''
            u, a = _parse_worker(out)
        # gabung best: kandidat dari stdout + file worker, ground truth python
        cands = {}
        if best is not None: cands[best] = best_u
        for wf in wfiles:
            try: txt = open(wf).read().strip()
            except OSError: continue
            if len(txt) == n and txt not in cands:
                cands[txt] = unsat_count(txt, clauses)
        if solved is not None and solved not in cands: cands[solved] = 0
        if cands:
            best, best_u = min(cands.items(), key=lambda kv: kv[1])
            with open(shared, 'w') as f: f.write(best)
            for i, wf in enumerate(wfiles):   # catat profil pemegang best -> diunggulkan next round
                try:
                    if open(wf).read().strip() == best: solve._win = i % WORKERS; break
                except OSError: pass
        improved = best_u is not None and best_u < getattr(solve, '_last_u', 1 << 30)
        if improved:
            solve._last_u = best_u
            last_improve = time.time()
            log('round', rnd_i, 'BEST BARU unsat', best_u)
        else:
            log('round', rnd_i, 'merged best unsat', best_u, '(no improve %.0fs)' % (time.time() - last_improve))
        if best_u == 0:
            log('SOLVED 0 unsat (verified) -> submit')
            return best
        if time.time() - last_improve > 30 and stage < 3:
            stage += 1
            last_improve = time.time()
            log('=== PINDAH TAHAP -> stage', stage, '(best diam 30s)')
    raise SystemExit('solver timeout, best unsat %s' % best_u)

# ---------------- mint flow ----------------

def one_mint():
    if os.path.exists(STOP): raise SystemExit('STOP file detected')
    auth()
    ident = call('agent_identity', {'wallet': W}); log("identity", json.dumps(ident)[:300])
    if '_error' in ident or '_isError' in ident: raise SystemExit("identity " + json.dumps(ident)[:300])
    st = call('wallet_status', {'wallet': W}); log("status", json.dumps(st)[:400])
    if '_error' in st or '_isError' in st: raise SystemExit("status " + json.dumps(st)[:300])
    ids = ident.get('identities') or [ident]
    me = [i for i in ids if (i.get('authorizedWallet') or i.get('owner') or '').lower() == W.lower()] or ids
    agentId = str(me[0]['agentId'])
    if int(st.get('remainingMints', 0)) < 1: raise SystemExit('quota habis')
    ensure_binary()   # solver siap SEBELUM start_attempt (timer mulai di start)
    key = str(uuid.uuid4())
    for _ in range(60):
        prep = call('prepare_attempt', {'wallet': W, 'beneficiary': W, 'agentId': agentId, 'idempotencyKey': key})
        if 'ATTEMPT_PENDING' in json.dumps(prep): log("pending old attempt, waiting"); time.sleep(30); auth(); continue
        break
    json.dump({'key': key, 'prep': prep}, open(os.path.join(DATA, 'prep_last.json'), 'w'))  # sebelum tanda tangan
    if isinstance(prep, dict) and ('_isError' in prep or '_error' in prep): raise SystemExit("prepare " + json.dumps(prep)[:500])
    aid = prep.get('attemptId'); bm = prep.get('benchmark') or {}
    log("attempt", aid, "version", prep.get('version'), json.dumps(bm)[:300])
    swr = prep.get('startWalletRequest')
    dom = (prep.get('startAuthorization') or {}).get('domain') or {}
    assert dom.get('name') == 'NONCE Free Mint' and dom.get('verifyingContract', '').lower() == FREEMINT.lower(), "bad start domain"
    sig, rec = sign712(swr)
    sargs = {'attemptId': aid, 'signature': sig}
    if rec is not None: sargs['signatureRecovery'] = rec
    t_attempt0 = time.time()
    sa = call('start_attempt', sargs)
    if isinstance(sa, dict) and ('_isError' in sa or '_error' in sa): raise SystemExit("start " + json.dumps(sa)[:500])
    cap = sa.get('capability'); b2 = sa.get('benchmark') or bm
    n = int(b2.get('variables')); fc = int(b2.get('fragmentCount'))
    solve._last_u = 1 << 30
    a = solve(aid, cap, n, fc, bm=b2, t_attempt0=t_attempt0)
    res = call('submit_artifact', {'attemptId': aid, 'capability': cap, 'artifact': {'assignment': a}, 'nonce': '0'})
    log("submit", json.dumps(res)[:400])
    if 'VERIFIED_WIN' not in json.dumps(res):
        # format berubah / error server: simpan raw, jangan nebak
        rawp = os.path.join(DATA, 'submit_raw_%s.json' % aid[:8])
        json.dump(res, open(rawp, 'w'))
        log('submit bukan VERIFIED_WIN -> raw disimpan', rawp)
        if isinstance(res, dict) and ('_isError' in res or '_error' in res):
            raise SystemExit("submit error " + json.dumps(res)[:300])
        raise SystemExit("not a win")
    mt = call('mint_transaction', {'attemptId': aid})
    tx = mt.get('transaction') or mt.get('tx') or {}
    to = tx.get('to', '')
    val = int(str(tx.get('value', '0')), 0) if str(tx.get('value', '0')).startswith('0x') else int(tx.get('value', 0) or 0)
    data = tx.get('data') or tx.get('input')
    callee = None
    try:
        d = data or ''
        callee = '0x' + d[10:74][-40:]
    except Exception:
        pass
    assert to.lower() == FREEMINT.lower() and val == 0 and data and data[:10] != '0x095ea7b3', "bad mint tx " + json.dumps(tx)[:300]
    log('mint tx ok: to=%s value=%d datalen=%d callee-in-data=%s' % (to, val, len(data or ''), callee))
    if wallet_disconnected(): raise SystemExit("BAW disconnected before mint")
    p = baw("contract-call", "preview", "--binanceChainId", "56", "--from", W, "--to", FREEMINT, "--value", "0", "--inputData", data)
    rid = (p.get('data') or {}).get('requestId')
    if not rid: raise SystemExit("mint preview fail " + json.dumps(p)[:400])
    log("preview risks", json.dumps((p.get('data') or {}).get('risks'))[:300])
    e = baw("contract-call", "execute", "--requestId", rid)
    log("mint exec", json.dumps(e.get('data'))[:300])
    h = (e.get('data') or {}).get('txHash')
    if h:
        rc = None
        for _ in range(36):   # tunggu receipt maksimal ~3 menit
            time.sleep(5)
            try:
                rr = requests.post("https://api.404.build/api/v1/rpc/bsc",
                                   json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [h]},
                                   timeout=20).json()
                rc = rr.get('result')
                if rc: break
            except Exception: pass
        if rc:
            T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
            tid = [int(l['topics'][3], 16) for l in rc.get('logs', []) if len(l.get('topics', [])) == 4 and l['topics'][0] == T]
            gas_bnb = int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16) / 1e18
            log("MINTED", h, "status", int(rc.get('status', '0x0'), 16), "tokenIds", tid, "gasBNB", gas_bnb)
        else:
            log("MINT broadcast, receipt belum muncul:", h)
    return h

def wallet_disconnected():
    return (baw("wallet", "status").get('data') or {}).get('status') != 'CONNECTED'

if __name__ == "__main__":
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    done = 0; attempt_no = 0
    while done < target:
        if os.path.exists(STOP):
            log('STOP file -> exit bersih'); break
        attempt_no += 1
        log('=== attempt', attempt_no, '(mint sukses', done, '/', target, ')')
        try:
            h = one_mint()
            if h:
                done += 1
                log('=== MINT', done, 'SUKSES:', h)
                if done >= target:
                    log('target', target, 'tercapai -> BERHENTI, lapor BOZ'); break
        except SystemExit as e:
            log('attempt', attempt_no, 'gagal:', e)
            if 'quota habis' in str(e):
                log('=== SUPPLY HABIS (remainingMints=0) -> BERHENTI'); break
            time.sleep(5)
        except Exception as e:
            log('attempt', attempt_no, 'exception:', repr(e))
            json.dump({'err': repr(e)}, open(os.path.join(DATA, 'exc_%d.json' % attempt_no), 'w'))
            time.sleep(5)
    log('=== SELESAI. total mint:', done)
