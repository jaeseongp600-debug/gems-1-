from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_cors import CORS
from functools import wraps
from datetime import datetime, timedelta
import random, math

app = Flask(__name__)
app.secret_key = 'gems-v4-secret-2024'
CORS(app)

# ─────────────────────────────────────────
# 계정 / 발전기 매핑 (계정당 최대 2대)
# ─────────────────────────────────────────
USERS = {
    'admin':    {'password': '1234',     'role': '관리자', 'name': '관리자',
                 'generators': ['GEN-001', 'GEN-002']},
    'operator': {'password': 'gems2024', 'role': '운영자', 'name': '운영자',
                 'generators': ['GEN-003', 'GEN-004']},
    'viewer':   {'password': 'view1234', 'role': '조회자', 'name': '조회자',
                 'generators': ['GEN-005', 'GEN-006']},
}

GENERATORS_DB = {
    'GEN-001': {'id':'GEN-001','name':'1호 디젤발전기','site':'김포 1공장','type':'디젤','capacity':500, 'model':'CAT C15',        'install':'2020-03-15'},
    'GEN-002': {'id':'GEN-002','name':'2호 디젤발전기','site':'김포 1공장','type':'디젤','capacity':500, 'model':'CAT C15',        'install':'2020-03-15'},
    'GEN-003': {'id':'GEN-003','name':'1호 가스발전기', 'site':'김포 2공장','type':'가스', 'capacity':800, 'model':'MAN E3262',    'install':'2019-07-20'},
    'GEN-004': {'id':'GEN-004','name':'2호 가스발전기', 'site':'김포 2공장','type':'가스', 'capacity':800, 'model':'MAN E3262',    'install':'2019-07-20'},
    'GEN-005': {'id':'GEN-005','name':'비상 디젤발전기','site':'김포 3공장','type':'디젤','capacity':300, 'model':'Cummins QSK19', 'install':'2021-11-01'},
    'GEN-006': {'id':'GEN-006','name':'피크컷 발전기',  'site':'김포 3공장','type':'가스', 'capacity':1000,'model':'Jenbacher J624','install':'2022-05-10'},
}

# ─── 발전기별 개별 설정 ────────────────────
GEN_TARGET_PEAK = {
    'GEN-001': 420, 'GEN-002': 450,
    'GEN-003': 680, 'GEN-004': 700,
    'GEN-005': 250, 'GEN-006': 850,
}

# ─── 수동 ON/OFF 상태 (None=자동, True=강제ON, False=강제OFF) ───
GEN_MANUAL_STATE = {gid: None for gid in GENERATORS_DB}

# ─── 피크제어 활성화 상태 (True=피크제어 ON, False=피크제어 OFF) ───
GEN_PEAK_CTRL = {gid: True for gid in GENERATORS_DB}

# ─── 피크운전 부하분담 모드 ───────────────────
# 'excess': 초과분 추종 모드 (predicted - target 만큼 발전기 공급)
# 'fixed' : 고정 부하율 모드 (capacity × load_pct / 100 만큼 고정 공급)
GEN_PEAK_MODE     = {gid: 'excess' for gid in GENERATORS_DB}
GEN_PEAK_LOAD_PCT = {gid: 50       for gid in GENERATORS_DB}  # 0~100 %

# ─── 오늘 최대전력 추적 ──────────────────────
GEN_TODAY_MAX = {}

SETTINGS = {'target_peak': 800}

# ─── 오피넷 유가 캐시 (경유 기준, 원/L) ─────────────────────
# 실제 오피넷 데이터 기반 최근 30일 경유 평균가
# 출처: 오피넷 평균판매가격 (opinet.co.kr) - 2026.03 기준
import datetime as _dt

def _gen_diesel_prices():
    """최근 30일 경유 가격 시뮬레이션 (오피넷 실데이터 기반)"""
    # 오피넷 2026-03-02 기준 경유 1,607.39 원/L
    base = 1607.39
    today = _dt.date.today()
    prices = {}
    for i in range(30, -1, -1):
        d = today - _dt.timedelta(days=i)
        # ±30원 이내 변동 (실제 시장 반영)
        import random as _r
        delta = _r.uniform(-30, 30)
        prices[d.strftime('%Y-%m-%d')] = round(base + delta, 2)
    return prices

DIESEL_PRICE_CACHE = _gen_diesel_prices()

# 발전기 연비 테이블 (L/h @ 정격부하 기준)
# 디젤: 약 capacity(kW) × 0.24 L/kWh × 평균부하율 ÷ 효율
GEN_FUEL_RATE = {
    'GEN-001': 85.0,   # 500kW 디젤 - 약 85 L/h (정격시)
    'GEN-002': 85.0,   # 500kW 디젤
    'GEN-003': 0,      # 가스발전기 (연료비 별도)
    'GEN-004': 0,      # 가스발전기
    'GEN-005': 52.0,   # 300kW 디젤 - 약 52 L/h
    'GEN-006': 0,      # 가스발전기
}

# ─────────────────────────────────────────
# 상태 시뮬레이터
# ─────────────────────────────────────────
def sim_generator(gid):
    g = dict(GENERATORS_DB[gid])
    cap = g['capacity']
    manual = GEN_MANUAL_STATE.get(gid)

    if manual is False:
        # 강제 OFF
        g['status'] = '정지 (수동)'; g['status_key'] = 'stop'
    elif manual == 'trial':
        # 시운전: 발전기 가동, ACB 개방 상태
        g['status'] = '시운전 중 (수동)'; g['status_key'] = 'trial'
    elif manual == 'manual':
        # 수동 모드: 발전기 켜져있지만 ACB 개방 (스탠바이)
        g['status'] = '수동 모드 (스탠바이)'; g['status_key'] = 'manual'
    elif manual is True:
        # 강제 ON: 피크/비상 조건 충족 시 ACB 투입
        g['status'] = '운전중 (수동)'; g['status_key'] = 'running'
    else:
        # 자동
        roll = random.random()
        if   roll < 0.50: g['status'] = '운전중';        g['status_key'] = 'running'
        elif roll < 0.62: g['status'] = '피크 운전 중';   g['status_key'] = 'peak'
        elif roll < 0.70: g['status'] = '경고: 과열';     g['status_key'] = 'warn'
        elif roll < 0.76: g['status'] = '경고: 연료 부족';g['status_key'] = 'warn'
        elif roll < 0.84: g['status'] = '정상 운전';      g['status_key'] = 'running'
        elif roll < 0.92: g['status'] = '대기 중';        g['status_key'] = 'idle'
        else:             g['status'] = '정지';           g['status_key'] = 'stop'

    running = g['status_key'] in ('running', 'peak', 'trial', 'manual')
    g['power']        = round(cap * random.uniform(0.65, 0.92), 1) if running else 0
    g['load_pct']     = round(g['power'] / cap * 100, 1) if running else 0
    g['efficiency']   = round(random.uniform(84, 91), 1) if running else 0
    g['temperature']  = round(random.uniform(72, 94)) if running else 25
    g['rpm']          = int(random.uniform(1480, 1520)) if running else 0
    g['voltage']      = round(random.uniform(377, 383), 1) if running else 0
    g['frequency']    = round(random.uniform(59.8, 60.2), 2) if running else 0
    g['oil_pressure'] = round(random.uniform(3.8, 4.6), 2)
    g['runtime']      = random.randint(2800, 7000)
    g['peak_runtime'] = random.randint(100, 500)
    g['fuel_level']   = random.randint(35, 95) if g['type'] == '디젤' else None
    g['next_maint']   = (datetime.today() + timedelta(days=random.randint(10, 90))).strftime('%Y-%m-%d')
    g['manual_state'] = manual  # None / True / False
    g['peak_ctrl']    = GEN_PEAK_CTRL.get(gid, True)  # 피크제어 활성화 여부
    return g

def get_user_gens(username):
    return [sim_generator(gid) for gid in USERS.get(username, {}).get('generators', [])]

def sim_peak_metrics(gid):
    """발전기별 개별 피크 지표 (부하분담 모드 반영)"""
    cap      = GENERATORS_DB[gid]['capacity']
    target   = GEN_TARGET_PEAK.get(gid, int(cap * 0.85))
    mode     = GEN_PEAK_MODE.get(gid, 'excess')
    load_pct = GEN_PEAK_LOAD_PCT.get(gid, 50)

    predicted   = round(cap * random.uniform(0.78, 0.96))
    today_key   = datetime.today().strftime('%Y%m%d') + gid
    if today_key not in GEN_TODAY_MAX:
        GEN_TODAY_MAX[today_key] = round(cap * random.uniform(0.82, 0.98))
    current_max = GEN_TODAY_MAX[today_key]
    if random.random() > 0.7:
        nv = round(cap * random.uniform(0.80, 0.99))
        if nv > current_max:
            GEN_TODAY_MAX[today_key] = nv; current_max = nv
    peak_runtime = random.randint(100, 520)
    status = 'over' if predicted > target else 'safe'
    pct    = round(predicted / target * 100, 1)

    # ── 부하분담 계산 ──
    if status == 'over':
        if mode == 'fixed':
            # 고정 부하율 모드: 발전기는 capacity × load_pct% 고정 공급
            gen_kw   = round(cap * load_pct / 100)
            kepco_kw = predicted - gen_kw  # 나머지는 한전
            # 정지 조건: kepco + gen < predicted (=gen < predicted-kepco → 항상 equal이므로
            # 실제 정지조건은 고정공급량으로는 부하를 감당 못할 때:
            # target + gen_kw < predicted)
            stop_needed = (target + gen_kw) < predicted
        else:
            # 초과분 추종 모드: predicted - target 만큼 발전기 공급 (용량 초과 시 cap으로 제한)
            gen_kw   = min(predicted - target, cap)
            kepco_kw = predicted - gen_kw
            stop_needed = False  # 항상 추종
    else:
        gen_kw      = 0
        kepco_kw    = predicted
        stop_needed = False

    return {
        'predicted':    predicted,
        'target':       target,
        'current_max':  current_max,
        'peak_runtime': peak_runtime,
        'status':       status,
        'pct':          pct,
        'peak_mode':    mode,
        'load_pct':     load_pct,
        'gen_kw':       gen_kw,
        'kepco_kw':     kepco_kw,
        'stop_needed':  stop_needed,
    }

# ─────────────────────────────────────────
# 차트 데이터
# ─────────────────────────────────────────
def chart_hourly():
    now = datetime.now(); labels, power, peak = [], [], []
    for h in range(now.hour + 1):
        labels.append(f'{h:02d}:00')
        b = 500 + 200 * math.sin(math.pi * h / 12)
        power.append(round(b * (1 + random.uniform(-0.08, 0.08))))
        peak.append(round(b * 1.15 * (1 + random.uniform(-0.05, 0.05))))
    return labels, power, peak

def chart_daily():
    labels, power, peak = [], [], []; today = datetime.today()
    for i in range(29, -1, -1):
        d = today - timedelta(days=i); labels.append(d.strftime('%m/%d'))
        b = 550 + 150 * math.sin(math.pi * (29 - i) / 15)
        power.append(round(b * (1 + random.uniform(-0.10, 0.10))))
        peak.append(round(b * 1.18 * (1 + random.uniform(-0.06, 0.06))))
    return labels, power, peak

def chart_monthly():
    labels, power, peak = [], [], []; today = datetime.today()
    for i in range(11, -1, -1):
        m = (today.month - i - 1) % 12 + 1
        y = today.year - ((today.month - i - 1) // 12)
        labels.append(f'{y}-{m:02d}')
        b = 600 + 100 * math.sin(math.pi * (11 - i) / 6)
        power.append(round(b * (1 + random.uniform(-0.08, 0.08))))
        peak.append(round(b * 1.20 * (1 + random.uniform(-0.05, 0.05))))
    return labels, power, peak

# ─────────────────────────────────────────
# 인증
# ─────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def deco(*a, **kw):
        if 'username' not in session: return redirect(url_for('login'))
        return f(*a, **kw)
    return deco

# ─────────────────────────────────────────
# 페이지 라우트
# ─────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'username' in session else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '').strip()
        if u in USERS and USERS[u]['password'] == p:
            session.update({'username': u, 'role': USERS[u]['role'], 'name': USERS[u]['name']})
            return redirect(url_for('dashboard'))
        error = '아이디 또는 비밀번호가 올바르지 않습니다.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    gens = get_user_gens(session['username'])
    return render_template('dashboard.html', user=session, generators=gens)

@app.route('/generator/<gen_id>')
@login_required
def generator_detail(gen_id):
    if gen_id not in USERS[session['username']]['generators']:
        return redirect(url_for('dashboard'))
    g    = sim_generator(gen_id)
    peak = sim_peak_metrics(gen_id)
    # 계정 소속 전체 발전기 (계통도용)
    all_gens = get_user_gens(session['username'])
    lh, vh, pkh = chart_hourly()
    ld, vd, pkd = chart_daily()
    lm, vm, pkm = chart_monthly()
    return render_template('generator_detail.html',
        user=session, g=g, peak=peak, all_gens=all_gens,
        lh=lh, vh=vh, pkh=pkh,
        ld=ld, vd=vd, pkd=pkd,
        lm=lm, vm=vm, pkm=pkm)

# ─── 보고서 페이지 ────────────────────────────
@app.route('/report')
@app.route('/report/<gen_id>')
@login_required
def report_page(gen_id=None):
    user_obj = USERS[session['username']]
    gens = get_user_gens(session['username'])
    if gen_id is None and gens:
        gen_id = gens[0]['id']
    selected_gen = next((g for g in gens if g['id'] == gen_id), gens[0] if gens else None)
    return render_template('report.html', user=user_obj, gens=gens, selected_gen=selected_gen)


@app.route('/api/alarm_count')
@login_required
def api_alarm_count():
    """미처리 알람 건수 반환"""
    gens = get_user_gens(session['username'])
    # 미확인 알람만 카운트 (랜덤으로 생성되지만 일관성을 위해 seed 고정)
    import hashlib
    day_seed = int(hashlib.md5(datetime.today().strftime('%Y%m%d').encode()).hexdigest()[:8], 16)
    rng = random.Random(day_seed)
    count = 0
    for g in gens:
        n = rng.randint(1, 3)
        for _ in range(n):
            ack = rng.random() > 0.5
            if not ack:
                count += 1
    return jsonify({'unread': count})

@app.route('/alarms')
@login_required
def alarms():
    gens = get_user_gens(session['username'])
    alarm_list = []
    msgs = ['출력 저하 감지 (정격 70% 이하)', '냉각수 온도 상승 (92°C)', '연료 잔량 부족 (30%)',
            '배터리 전압 저하', '오일 압력 경보', '주기 점검 예정', '통신 오류 감지', '과부하 경보']
    for g in gens:
        for _ in range(random.randint(1, 3)):
            lv = random.choice(['경고', '주의', '정상'])
            alarm_list.append({'gen_id': g['id'], 'gen_name': g['name'], 'level': lv,
                'msg': random.choice(msgs),
                'time': (datetime.now() - timedelta(minutes=random.randint(1, 480))).strftime('%Y-%m-%d %H:%M'),
                'ack': random.random() > 0.5})
    alarm_list.sort(key=lambda x: x['time'], reverse=True)
    return render_template('alarms.html', user=session, alarms=alarm_list)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    msg = None
    user_gens = USERS[session['username']]['generators']
    if request.method == 'POST':
        action   = request.form.get('action')
        parallel = 'parallel' in request.form
        sel_gen  = request.form.get('sel_gen', user_gens[0] if user_gens else None)
        if action == 'peak':
            val = int(request.form.get('target_peak', 800))
            if parallel:
                for gid in user_gens:
                    GEN_TARGET_PEAK[gid] = val
                msg = f'목표 피크량 {val} kW 이(가) 병렬 발전기 전체에 저장되었습니다.'
            else:
                if sel_gen and sel_gen in GEN_TARGET_PEAK:
                    GEN_TARGET_PEAK[sel_gen] = val
                    msg = f'{GENERATORS_DB[sel_gen]["name"]}의 목표 피크량이 {val} kW로 저장되었습니다.'
        elif action == 'peak_mode':
            # 피크운전 부하분담 모드 저장
            sel_gen  = request.form.get('sel_gen', user_gens[0] if user_gens else None)
            parallel = 'parallel' in request.form
            mode     = request.form.get('peak_mode_type', 'excess')
            lp       = max(0, min(100, int(request.form.get('peak_load_pct', 50))))
            target_gids = user_gens if parallel else ([sel_gen] if sel_gen else [])
            for gid in target_gids:
                if gid in GENERATORS_DB:
                    GEN_PEAK_MODE[gid]     = mode
                    GEN_PEAK_LOAD_PCT[gid] = lp
            names = ', '.join(GENERATORS_DB[g]['name'] for g in target_gids if g in GENERATORS_DB)
            mode_label = '초과분 추종' if mode == 'excess' else f'고정 부하율 {lp}%'
            msg = f'{names} 피크 부하분담 → {mode_label} 저장 완료'
        elif action == 'alarm':

            SETTINGS['alarm_output_drop'] = 'alarm_output_drop' in request.form
            SETTINGS['alarm_over_temp']   = 'alarm_over_temp'   in request.form
            SETTINGS['alarm_comm_loss']   = 'alarm_comm_loss'   in request.form
            SETTINGS['alarm_periodic']    = 'alarm_periodic'    in request.form
            SETTINGS['alarm_email']       = 'alarm_email'       in request.form
            SETTINGS['alarm_sms']         = 'alarm_sms'         in request.form
            msg = '알람 설정이 저장되었습니다.'
    gen_targets = {gid: GEN_TARGET_PEAK.get(gid, 800) for gid in user_gens}
    gen_names   = {gid: GENERATORS_DB[gid]['name'] for gid in user_gens}
    peak_modes    = {gid: GEN_PEAK_MODE.get(gid, 'excess')     for gid in user_gens}
    peak_load_pcts= {gid: GEN_PEAK_LOAD_PCT.get(gid, 50)       for gid in user_gens}
    gen_caps      = {gid: GENERATORS_DB[gid]['capacity']        for gid in user_gens}
    return render_template('settings.html', user=session, settings=SETTINGS,
                           msg=msg, user_gens=user_gens,
                           gen_targets=gen_targets, gen_names=gen_names,
                           peak_modes=peak_modes, peak_load_pcts=peak_load_pcts,
                           gen_caps=gen_caps)

# ─────────────────────────────────────────
# API
# ─────────────────────────────────────────
@app.route('/api/realtime')
@login_required
def api_realtime():
    gens = get_user_gens(session['username'])
    return jsonify({'generators': [{
        'id': g['id'], 'status': g['status'], 'status_key': g['status_key'],
        'power': g['power'], 'load_pct': g['load_pct'], 'efficiency': g['efficiency'],
        'temperature': g['temperature'], 'fuel_level': g['fuel_level'],
        'rpm': g['rpm'], 'voltage': g['voltage'], 'frequency': g['frequency'],
        'oil_pressure': g['oil_pressure'],
        'peak_ctrl':    g.get('peak_ctrl', True),
        'manual_state': g.get('manual_state', None),
    } for g in gens], 'ts': datetime.now().strftime('%H:%M:%S')})

@app.route('/api/chart')
@login_required
def api_chart():
    period = request.args.get('period', 'hourly')
    if period == 'daily':     l, v, pk = chart_daily()
    elif period == 'monthly': l, v, pk = chart_monthly()
    else:                     l, v, pk = chart_hourly()
    return jsonify({'labels': l, 'values': v, 'peaks': pk})

@app.route('/api/peak_metrics/<gen_id>')
@login_required
def api_peak_metrics(gen_id):
    if gen_id not in USERS[session['username']]['generators']:
        return jsonify({'error': '권한 없음'}), 403
    return jsonify(sim_peak_metrics(gen_id))

# ─── 목표 피크값 API (발전기별) ─────────────
@app.route('/api/gen_target/<gen_id>', methods=['POST'])
@login_required
def api_set_gen_target(gen_id):
    if gen_id not in USERS[session['username']]['generators']:
        return jsonify({'error': '권한 없음'}), 403
    data = request.get_json() or {}
    val  = int(data.get('target', GEN_TARGET_PEAK.get(gen_id, 800)))
    parallel = data.get('parallel', False)
    if parallel:
        for gid in USERS[session['username']]['generators']:
            GEN_TARGET_PEAK[gid] = val
    else:
        GEN_TARGET_PEAK[gen_id] = val
    return jsonify({'ok': True, 'target': val,
                    'targets': {gid: GEN_TARGET_PEAK[gid]
                                for gid in USERS[session['username']]['generators']}})

# ─── 수동 ON/OFF API ────────────────────────
@app.route('/api/generator/control/<gen_id>', methods=['POST'])
@login_required
def api_gen_control(gen_id):
    if gen_id not in USERS[session['username']]['generators']:
        return jsonify({'error': '권한 없음'}), 403
    data  = request.get_json() or {}
    action = data.get('action')  # 'on' | 'off' | 'auto'
    if action == 'on':
        GEN_MANUAL_STATE[gen_id] = True
    elif action == 'off':
        GEN_MANUAL_STATE[gen_id] = False
    elif action == 'trial':
        GEN_MANUAL_STATE[gen_id] = 'trial'
    elif action == 'manual':
        GEN_MANUAL_STATE[gen_id] = 'manual'
    else:
        GEN_MANUAL_STATE[gen_id] = None
    return jsonify({'ok': True, 'gen_id': gen_id,
                    'manual_state': GEN_MANUAL_STATE[gen_id]})

# ─── 피크 제어 ON/OFF API ─────────────────
@app.route('/api/generator/peak_ctrl/<gen_id>', methods=['POST'])
@login_required
def api_peak_ctrl(gen_id):
    """피크 제어 시스템 활성화/비활성화"""
    if gen_id not in USERS[session['username']]['generators']:
        return jsonify({'error': '권한 없음'}), 403
    data   = request.get_json() or {}
    enable = data.get('enable')   # True / False
    if isinstance(enable, bool):
        GEN_PEAK_CTRL[gen_id] = enable
    return jsonify({'ok': True, 'gen_id': gen_id,
                    'peak_ctrl': GEN_PEAK_CTRL[gen_id]})


# ─── 보고서 API ────────────────────────────────
@app.route('/api/report/<gen_id>', methods=['GET'])
@login_required
def api_report(gen_id):
    """발전기 보고서 데이터 반환"""
    import random
    from datetime import datetime, timedelta

    if gen_id not in GENERATORS_DB:
        return jsonify({'error': '발전기를 찾을 수 없습니다'}), 404

    g_info = dict(GENERATORS_DB[gen_id])
    g_sim  = sim_generator(gen_id)
    peak   = sim_peak_metrics(gen_id)
    today  = datetime.today()

    # ─ 가동 가능 여부 판단 로직
    checks = []
    oil_ok    = 3.5 <= g_sim['oil_pressure'] <= 5.0
    temp_ok   = g_sim['temperature'] < 95
    fuel_ok   = (g_sim['fuel_level'] is None) or (g_sim['fuel_level'] >= 20)
    volt_ok   = 370 <= g_sim['voltage'] <= 390 if g_sim['voltage'] > 0 else True
    maint_ok  = True
    try:
        next_m = datetime.strptime(g_sim['next_maint'], '%Y-%m-%d')
        maint_ok = (next_m - today).days > 0
    except: pass

    checks = [
        {'name': '유압 (오일 압력)',  'value': f"{g_sim['oil_pressure']} bar", 'ok': oil_ok,   'limit': '3.5~5.0 bar'},
        {'name': '냉각수 온도',        'value': f"{g_sim['temperature']} °C",   'ok': temp_ok,  'limit': '< 95 °C'},
        {'name': '연료 잔량',          'value': f"{g_sim['fuel_level']} %" if g_sim['fuel_level'] else 'N/A (가스)', 'ok': fuel_ok, 'limit': '≥ 20 %'},
        {'name': '전압 정상',          'value': f"{g_sim['voltage']} V" if g_sim['voltage'] > 0 else '정지 상태', 'ok': volt_ok, 'limit': '370~390 V'},
        {'name': '정비 유효 기간',     'value': g_sim['next_maint'], 'ok': maint_ok, 'limit': '만료 전'},
    ]
    operable = all(c['ok'] for c in checks)
    reason   = '모든 점검 항목 정상' if operable else ', '.join(c['name'] for c in checks if not c['ok']) + ' 이상'

    # ─ 누적 가동 이력 (30일 시뮬레이션)
    history = []
    fuel_rate = GEN_FUEL_RATE.get(gen_id, 0)  # L/h
    gen_type  = g_info.get('type', '')
    for i in range(30, 0, -1):
        d = today - timedelta(days=i)
        run_h = round(random.uniform(0, 8), 1)
        peak_h = round(random.uniform(0, min(run_h, 3)), 1) if run_h > 0 else 0
        kwh   = round(g_info['capacity'] * run_h * random.uniform(0.65, 0.92))
        date_str = d.strftime('%Y-%m-%d')
        # 유류비 계산 (디젤 발전기만)
        diesel_price = DIESEL_PRICE_CACHE.get(date_str, 1607.39)
        if gen_type == '디젤' and fuel_rate > 0 and run_h > 0:
            fuel_liter = round(fuel_rate * run_h, 1)
            fuel_cost  = round(fuel_liter * diesel_price)
        else:
            fuel_liter = 0
            fuel_cost  = 0
        history.append({
            'date':        date_str,
            'run_hours':   run_h,
            'peak_hours':  peak_h,
            'kwh':         kwh,
            'status':      '운전' if run_h > 0 else '정지',
            'fuel_liter':  fuel_liter,
            'fuel_cost':   fuel_cost,
            'diesel_price':round(diesel_price, 1),
        })

    # 누계 집계
    total_run   = round(sum(h['run_hours']  for h in history), 1)
    total_peak  = round(sum(h['peak_hours'] for h in history), 1)
    total_kwh   = sum(h['kwh'] for h in history)
    op_days     = sum(1 for h in history if h['run_hours'] > 0)
    total_fuel_cost  = sum(h['fuel_cost']  for h in history)
    total_fuel_liter = round(sum(h['fuel_liter'] for h in history), 1)
    avg_diesel_price = round(
        sum(h['diesel_price'] for h in history if h['run_hours'] > 0) /
        max(op_days, 1), 1
    )

    return jsonify({
        'ok': True,
        'gen_id':    gen_id,
        'gen_name':  g_info['name'],
        'gen_site':  g_info['site'],
        'gen_type':  g_info['type'],
        'capacity':  g_info['capacity'],
        'model':     g_info.get('model', '-'),
        'install':   g_info.get('install', '-'),
        'next_maint':g_sim['next_maint'],
        'generated_at': today.strftime('%Y-%m-%d %H:%M'),
        'operable':  operable,
        'reason':    reason,
        'checks':    checks,
        'runtime':   g_sim['runtime'],
        'peak_runtime': g_sim['peak_runtime'],
        'history':   history,
        'summary': {
            'total_run_hours':   total_run,
            'total_peak_hours':  total_peak,
            'total_kwh':         total_kwh,
            'op_days':           op_days,
            'avg_daily_kwh':     round(total_kwh / 30),
            'total_fuel_cost':   total_fuel_cost,
            'total_fuel_liter':  total_fuel_liter,
            'avg_diesel_price':  avg_diesel_price,
            'fuel_rate':         GEN_FUEL_RATE.get(gen_id, 0),
            'gen_type':          g_info.get('type', ''),
        },
        'current_status': g_sim['status_key'],
        'peak': peak,
    })

@app.route('/api/settings/peak_mode/<gen_id>', methods=['POST'])
@login_required
def api_set_peak_mode(gen_id):
    """발전기 피크 부하분담 모드 설정 API"""
    data     = request.get_json() or {}
    mode     = data.get('peak_mode', 'excess')
    load_pct = max(0, min(100, int(data.get('load_pct', 50))))
    parallel = data.get('parallel', False)
    user_gens = USERS[session['username']]['generators']
    if parallel:
        for gid in user_gens:
            GEN_PEAK_MODE[gid]     = mode
            GEN_PEAK_LOAD_PCT[gid] = load_pct
    elif gen_id in user_gens:
        GEN_PEAK_MODE[gen_id]     = mode
        GEN_PEAK_LOAD_PCT[gen_id] = load_pct
    return jsonify({'ok': True, 'peak_mode': mode, 'load_pct': load_pct})

@app.route('/api/settings/peak', methods=['POST'])
@login_required
def api_set_peak():
    data = request.get_json() or {}
    val  = int(data.get('target_peak', 800))
    gen_id = data.get('gen_id')
    parallel = data.get('parallel', False)
    user_gens = USERS[session['username']]['generators']
    if parallel:
        for gid in user_gens: GEN_TARGET_PEAK[gid] = val
    elif gen_id and gen_id in user_gens:
        GEN_TARGET_PEAK[gen_id] = val
    else:
        SETTINGS['target_peak'] = val
    return jsonify({'ok': True, 'target_peak': val})

if __name__ == '__main__':
    print("\n" + "=" * 52)
    print("  GEMS v4 - 발전기 에너지 관리 시스템")
    print("  서버: http://localhost:5000")
    print("  admin / 1234  |  operator / gems2024  |  viewer / view1234")
    print("=" * 52 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=True)
