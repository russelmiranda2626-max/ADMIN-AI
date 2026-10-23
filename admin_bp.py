"""
KusinaAI Admin - bahagi na ng main app (blueprint sa /admin).

Idagdag sa app.py, pagkatapos ng  app.register_blueprint(auth_bp):

    from admin_bp import init_admin
    init_admin(app, db, User, Favorite, BASE_DIR)

Mga setting sa .env:  ADMIN_USERNAME, ADMIN_PASSWORD   (GEMINI_API_KEY at DATABASE_URL ay gamit na ng main site)
Buksan:  /admin   (hal. http://127.0.0.1:5000/admin)
"""
import os
import re
import io
import csv
import json
import ssl
import time
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, jsonify, request, session, redirect, render_template
from sqlalchemy import func

bp = Blueprint('kusina_admin', __name__, url_prefix='/admin')

db = User = Favorite = None
CSV_PATH = ''
COLUMNS = ['id', 'title', 'category', 'ingredients', 'instructions', 'budget_level', 'best_for', 'image_url']
DAYS = ['Lunes', 'Martes', 'Miyerkules', 'Huwebes', 'Biyernes', 'Sabado', 'Linggo']
ADMIN_SESSION_SECONDS = 12 * 3600

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL = ssl.create_default_context()


def init_admin(app, _db, _User, _Favorite, base_dir):
    """Ikabit ang admin sa main app. Ginagamit ang parehong db at models ng main site."""
    global db, User, Favorite, CSV_PATH
    db, User, Favorite = _db, _User, _Favorite
    CSV_PATH = os.path.join(base_dir, 'recipes.csv')
    app.register_blueprint(bp)


# ---------------------------------------------------------------------------
# AUTH (sariling login ng admin; hiwalay na session key kaya hindi nito ginagalaw ang login ng users)
# ---------------------------------------------------------------------------
_fails = {}  # ip -> (bilang, oras)


def _env(name, default=''):
    """Basahin ang .env nang walang sobrang espasyo, panipi, o nakatagong BOM."""
    v = os.getenv(name)
    if v is None:
        for k in os.environ:
            if k.lstrip('\ufeff').strip() == name:
                v = os.environ[k]
                break
    if v is None:
        return default
    v = v.replace('\ufeff', '').strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
        v = v[1:-1]
    return v


def _locked(ip):
    n, t = _fails.get(ip, (0, 0))
    return n >= 5 and time.time() - t < 300


def _is_admin():
    return time.time() - float(session.get('kusina_admin_at', 0) or 0) < ADMIN_SESSION_SECONDS


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _is_admin():
            if request.path.startswith('/admin/api/'):
                return jsonify({'error': 'Mag-login muna.'}), 401
            return redirect('/admin/login')
        return fn(*a, **kw)
    return wrapper


@bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        return render_template('admin_login.html', configured=bool(_env('ADMIN_PASSWORD')))
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
    is_form = not request.is_json
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    want_user = _env('ADMIN_USERNAME', 'admin')
    want_pass = _env('ADMIN_PASSWORD', '')

    def fail(msg, code):
        if is_form:  # galing sa login form ng main site
            return redirect('/admin/login?err=' + ('lock' if code == 429 else 'bad'))
        return jsonify({'error': msg}), code

    if not want_pass:
        return fail('Wala pang ADMIN_PASSWORD sa .env.', 400)
    if _locked(ip):
        return fail('Masyadong maraming maling subok. Maghintay ng 5 minuto.', 429)
    got_user, got_pass = str(body.get('username', '')).strip(), str(body.get('password', ''))
    ok = (secrets.compare_digest(got_user.encode(), want_user.encode()) and
          secrets.compare_digest(got_pass.encode(), want_pass.encode()))
    if not ok:
        print(f"[ADMIN LOGIN GAGAL] username {'tama' if got_user == want_user else 'MALI'}; "
              f"password {'tama' if got_pass == want_pass else 'MALI'} "
              f"(haba ng password sa .env: {len(want_pass)}, inilagay: {len(got_pass)})")
        n, _ = _fails.get(ip, (0, 0))
        _fails[ip] = (n + 1, time.time())
        return fail('Maling username o password.', 401)
    _fails.pop(ip, None)
    session['kusina_admin_at'] = time.time()
    return redirect('/admin') if is_form else jsonify({'ok': True})


@bp.route('/logout')
def admin_logout():
    session.pop('kusina_admin_at', None)
    return redirect('/admin/login')


@bp.route('/')
@admin_required
def admin_home():
    return render_template('admin_panel.html', admin_name=_env('ADMIN_USERNAME', 'admin'))


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def iso(dt):
    if not dt:
        return ''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def load_store(u):
    """Ibabalik ang (kasalukuyang plano, {linggo: plano}) mula sa plan_json ng user."""
    try:
        raw = json.loads(u.plan_json) if u.plan_json else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get('v') == 2:
        return raw.get('plan') or {}, raw.get('history') or {}, raw.get('week') or ''
    return raw, {}, ''


def count_dishes(plan):
    n = 0
    if isinstance(plan, dict):
        for day in plan.values():
            if isinstance(day, dict):
                n += sum(1 for v in day.values() if v and v != '-')
    return n


def dishes_in(plan):
    out = []
    if isinstance(plan, dict):
        for day in plan.values():
            if isinstance(day, dict):
                out += [str(v).strip() for v in day.values() if v and v != '-']
    return out


# --- Opsyonal: kung hiwalay ang hosting ng admin, ang recipes.csv ay binabasa/sinusulatan sa GitHub repo ng main site ---
def _gh_conf():
    tok, repo = os.getenv('GITHUB_TOKEN', '').strip(), os.getenv('GITHUB_REPO', '').strip()
    if not (tok and repo):
        return None
    return {'token': tok, 'repo': repo, 'branch': os.getenv('GITHUB_BRANCH', 'main').strip() or 'main',
            'path': os.getenv('GITHUB_CSV_PATH', 'recipes.csv').strip() or 'recipes.csv'}


def _gh_request(conf, method='GET', payload=None):
    url = f"https://api.github.com/repos/{conf['repo']}/contents/{conf['path']}"
    if method == 'GET':
        url += '?ref=' + conf['branch']
    req = urllib.request.Request(
        url, method=method, data=json.dumps(payload).encode('utf-8') if payload else None,
        headers={'Authorization': 'Bearer ' + conf['token'], 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'KusinaAI-Admin', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
        return json.loads(r.read().decode('utf-8'))


def gh_get(conf):
    """Ibabalik ang (teksto ng CSV, sha) mula sa GitHub."""
    import base64
    d = _gh_request(conf)
    return base64.b64decode(d['content']).decode('utf-8-sig'), d['sha']


def gh_put(conf, text, sha, message):
    import base64
    _gh_request(conf, 'PUT', {'message': message, 'branch': conf['branch'], 'sha': sha,
                              'content': base64.b64encode(text.encode('utf-8')).decode('ascii')})


def _parse_rows(text):
    rows = list(csv.DictReader(text.splitlines(True)))
    for r in rows:
        try:
            r['id'] = int(r.get('id') or 0)
        except ValueError:
            r['id'] = 0
    return rows


def read_recipes():
    conf = _gh_conf()
    if conf:
        try:
            return _parse_rows(gh_get(conf)[0])
        except Exception as e:
            print('GitHub read error:', e)
            return []
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding='utf-8-sig', newline='') as f:
        return _parse_rows(f.read())


def extract_json(text):
    m = re.search(r'\{.*\}', text or '', re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GEMINI (maikling bersyon)
# ---------------------------------------------------------------------------
LAST_ERR = {'msg': ''}


def gemini(prompt):
    key = os.getenv('GEMINI_API_KEY', '').strip().strip('"').strip("'")
    if not key:
        LAST_ERR['msg'] = 'Walang GEMINI_API_KEY sa .env.'
        return None
    for model in ('gemini-3.5-flash', 'gemini-3-flash-preview', 'gemini-flash-latest'):
        req = urllib.request.Request(
            f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
            data=json.dumps({'contents': [{'parts': [{'text': prompt}]}]}).encode('utf-8'),
            headers={'x-goog-api-key': key, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=40, context=_SSL) as r:
                data = json.loads(r.read().decode('utf-8'))
            text = ''.join(p.get('text', '') for p in data['candidates'][0]['content']['parts']).strip()
            if text:
                return text
        except urllib.error.HTTPError as e:
            LAST_ERR['msg'] = f'Gemini error {e.code}'
            if e.code in (401, 403):
                break
        except Exception as e:
            LAST_ERR['msg'] = str(e)[:200]
    return None


# ---------------------------------------------------------------------------
# API: STATS, USERS, DISHES
# ---------------------------------------------------------------------------
@bp.get('/api/stats')
@admin_required
def api_stats():
    week = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    users = db.session.execute(db.select(User)).scalars().all()
    planning = sum(1 for u in users if count_dishes(load_store(u)[0]) or load_store(u)[1])
    return jsonify({
        'users': len(users),
        'new_week': sum(1 for u in users if u.created_at and u.created_at.replace(tzinfo=None) >= week),
        'planning': planning,
        'recipes': len(read_recipes()),
        'favorites': db.session.scalar(db.select(func.count(Favorite.id))) or 0,
    })


@bp.get('/api/users')
@admin_required
def api_users():
    q = request.args.get('q', '').strip().lower()
    users = db.session.execute(db.select(User).order_by(User.id.desc())).scalars().all()
    favs = dict(db.session.execute(db.select(Favorite.user_id, func.count(Favorite.id)).group_by(Favorite.user_id)).all())
    out = []
    for u in users:
        if q and q not in (u.name or '').lower() and q not in (u.email or '').lower():
            continue
        cur, hist, _ = load_store(u)
        out.append({'id': u.id, 'name': u.name, 'email': u.email, 'joined': iso(u.created_at),
                    'week_dishes': count_dishes(cur), 'total_dishes': count_dishes(cur) + sum(count_dishes(p) for p in hist.values()),
                    'favorites': favs.get(u.id, 0)})
    return jsonify({'users': out})


@bp.get('/api/users/<int:uid>')
@admin_required
def api_user(uid):
    u = db.session.get(User, uid)
    if not u:
        return jsonify({'error': 'Wala nang ganitong user.'}), 404
    cur, hist, week = load_store(u)
    return jsonify({'user': {'id': u.id, 'name': u.name, 'email': u.email, 'joined': iso(u.created_at)},
                    'week': week, 'plan': cur,
                    'history': [{'start': k, 'plan': v} for k, v in sorted(hist.items(), reverse=True) if count_dishes(v)]})


@bp.delete('/api/users/<int:uid>')
@admin_required
def api_delete_user(uid):
    u = db.session.get(User, uid)
    if not u:
        return jsonify({'error': 'Wala nang ganitong user.'}), 404
    db.session.execute(db.delete(Favorite).where(Favorite.user_id == uid))
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})


@bp.get('/api/dishes')
@admin_required
def api_dishes():
    """Mga ulam na pinaplano ng mga user (bilang), at ang pinakabagong sumali."""
    counts, labels = {}, {}
    for u in db.session.execute(db.select(User)).scalars().all():
        cur, hist, _ = load_store(u)
        for d in dishes_in(cur) + [x for p in hist.values() for x in dishes_in(p)]:
            k = d.lower()
            counts[k] = counts.get(k, 0) + 1
            labels.setdefault(k, d)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:25]
    recent = db.session.execute(db.select(User).order_by(User.id.desc()).limit(10)).scalars().all()
    return jsonify({'top': [{'dish': labels[k], 'count': n} for k, n in top],
                    'recent': [{'name': u.name, 'email': u.email, 'joined': iso(u.created_at)} for u in recent]})


# ---------------------------------------------------------------------------
# API: RECIPES (isinusulat sa recipes.csv ng main site)
# ---------------------------------------------------------------------------
@bp.get('/api/recipes')
@admin_required
def api_recipes():
    rows = read_recipes()
    cats = sorted({r.get('category', '') for r in rows if r.get('category')})
    latest = sorted(rows, key=lambda r: r['id'], reverse=True)[:30]
    return jsonify({'total': len(rows), 'categories': cats,
                    'latest': [{'id': r['id'], 'title': r.get('title', ''), 'category': r.get('category', '')} for r in latest],
                    'csv_found': bool(rows) or os.path.exists(CSV_PATH), 'source': 'github' if _gh_conf() else 'local'})


@bp.post('/api/recipes')
@admin_required
def api_add_recipe():
    d = request.get_json(silent=True) or {}
    title = str(d.get('title', '')).strip()[:120]
    ingredients = str(d.get('ingredients', '')).strip().replace('\n', ' ')[:1500]
    instructions = str(d.get('instructions', '')).strip()[:5000]
    if not title or not ingredients or not instructions:
        return jsonify({'error': 'Kailangan ang pangalan, sangkap, at paraan ng pagluluto.'}), 400
    rows = read_recipes()
    if any(r.get('title', '').strip().lower() == title.lower() for r in rows):
        return jsonify({'error': 'May resipe nang may ganitong pangalan.'}), 400
    img = str(d.get('image_url', '')).strip()[:300]
    if not img.startswith('/static/'):
        img = ''  # ang main site ay /static/ lang ang tinatanggap
    new_id = max([r['id'] for r in rows] + [0]) + 1
    row = [new_id, title, str(d.get('category', '')).strip()[:40] or 'Ulam', ingredients,
           instructions.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\\n'),
           str(d.get('budget_level', '')).strip()[:60] or 'Katamtaman', str(d.get('best_for', '')).strip()[:80], img]
    conf = _gh_conf()
    if conf:  # hiwalay na hosting: isusulat sa GitHub repo ng main site (magre-redeploy ito nang kusa)
        try:
            text, sha = gh_get(conf)
            buf = io.StringIO()
            csv.writer(buf, lineterminator='\n').writerow(row)
            if text and not text.endswith('\n'):
                text += '\n'
            gh_put(conf, text + buf.getvalue(), sha, f'Admin: dagdag na resipe - {title}')
        except Exception as e:
            return jsonify({'error': f'Hindi maisulat sa GitHub: {str(e)[:150]}'}), 502
        return jsonify({'ok': True, 'recipe': {'id': new_id, 'title': title}})
    exists = os.path.exists(CSV_PATH) and os.path.getsize(CSV_PATH) > 0
    needs_nl = False
    if exists:
        with open(CSV_PATH, 'rb') as f:
            f.seek(-1, os.SEEK_END)
            needs_nl = f.read(1) not in (b'\n', b'\r')
    with open(CSV_PATH, 'a', encoding='utf-8', newline='') as f:
        if needs_nl:
            f.write('\n')
        w = csv.writer(f)
        if not exists:
            w.writerow(COLUMNS)
        w.writerow(row)
    return jsonify({'ok': True, 'recipe': {'id': new_id, 'title': title}})


@bp.post('/api/ai_recipe')
@admin_required
def api_ai_recipe():
    title = str((request.get_json(silent=True) or {}).get('title', '')).strip()[:120]
    if not title:
        return jsonify({'error': 'Ilagay muna ang pangalan ng ulam.'}), 400
    cats = ', '.join(sorted({r.get('category', '') for r in read_recipes() if r.get('category')})) or 'Ulam, Sabaw, Gulay'
    prompt = (
        f'Gumawa ng resipeng Pinoy para sa ulam na "{title}". Ibalik LAMANG ang isang JSON object, walang code fence '
        'at walang ibang teksto: {"category": "...", "ingredients": "...", "instructions": "...", "budget_level": "...", "best_for": "..."}. '
        f'Ang category ay isa sa: {cats}. Ang ingredients ay mga pangalan ng sangkap na pinaghihiwalay ng espasyo, lowercase, '
        'walang bilang (halimbawa: manok toyo suka bawang sibuyas paminta). Ang instructions ay 4 hanggang 6 na hakbang sa Filipino, '
        'may numero (1. 2. 3.), bawat hakbang sa bagong linya. Ang budget_level ay isa sa: "Mura / Budget-Friendly", "Katamtaman", "Espesyal". '
        'Ang best_for ay maikli, halimbawa "Tanghalian / Pang-araw-araw".')
    obj = extract_json(gemini(prompt))
    if not isinstance(obj, dict):
        return jsonify({'error': LAST_ERR['msg'] or 'Hindi nakuha ang sagot ng AI.'}), 502
    return jsonify({'ok': True, 'recipe': {k: str(obj.get(k, '')).strip() for k in
                                          ('category', 'ingredients', 'instructions', 'budget_level', 'best_for')}})


# ---------------------------------------------------------------------------
# API: ADMIN BOT
# ---------------------------------------------------------------------------
@bp.post('/api/chat')
@admin_required
def api_chat():
    d = request.get_json(silent=True) or {}
    q = str(d.get('query', '')).strip()[:500]
    if not q:
        return jsonify({'reply': 'Magtanong ka ng tungkol sa site mo.'})
    users = db.session.execute(db.select(User).order_by(User.id.desc()).limit(50)).scalars().all()
    total = db.session.scalar(db.select(func.count(User.id))) or 0
    top = api_dishes().get_json()['top'][:10]
    ctx = (f"Kabuuang user: {total}. Kabuuang resipe: {len(read_recipes())}.\nMga user (pinakabago muna):\n" +
           '\n'.join(f"- {u.name} <{u.email}>, sumali {iso(u.created_at)[:10]}, {count_dishes(load_store(u)[0])} ulam ngayong linggo"
                     for u in users) +
           "\nPinakamadalas na ulam sa mga kalendaryo:\n" + '\n'.join(f"- {t['dish']}: {t['count']}" for t in top))
    hist = ''
    for h in (d.get('history') or [])[-6:]:
        if isinstance(h, dict):
            hist += f"{'Admin' if h.get('role') == 'user' else 'Assistant'}: {str(h.get('text', ''))[:500]}\n"
    prompt = ("Ikaw ang admin assistant ng KusinaAI (digital cookbook at meal planner ng mga Pinoy). Tinutulungan mo ang may-ari "
              "ng site. Sagutin sa parehong wika ng tanong, maikli at malinaw. Gamitin ang datos sa ibaba; huwag mag-imbento.\n\n"
              f"DATOS:\n{ctx}\n\n" + (f"Naunang usapan:\n{hist}\n" if hist else '') + f"Tanong: {q}")
    text = gemini(prompt)
    return jsonify({'reply': text or LAST_ERR['msg'] or 'Hindi maabot ang AI ngayon.'})