"""
KusinaAI Admin - HIWALAY na hosting.
Gumagamit ng PAREHONG database (DATABASE_URL) ng main site, kaya makikita nito ang mga account.

Lokal na pagsubok:  python admin_app.py      ->  http://127.0.0.1:5001
Sa hosting:         gunicorn admin_app:app

Pagsusuri ng koneksyon: mag-login sa admin, tapos buksan ang /admin/dbcheck
"""
import os
import hashlib
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, redirect, jsonify
from flask_sqlalchemy import SQLAlchemy

HERE = os.path.dirname(os.path.abspath(__file__))
# override=False: ang environment variables ng hosting (Render) ang mananaig sa .env.
# (Dati override=True, kaya nabubura ng .env file ang DATABASE_URL ng Render.)
load_dotenv(os.path.join(HERE, '.env'), override=False, encoding='utf-8-sig')

app = Flask(__name__, template_folder=os.path.join(HERE, 'templates'))

app.config.update(
    SECRET_KEY=os.getenv('ADMIN_SECRET_KEY') or hashlib.sha256(('kusina-admin:' + os.getenv('ADMIN_PASSWORD', '').strip()).encode()).hexdigest(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

_db_url = (os.getenv('DATABASE_URL') or '').strip().strip('"').strip("'")

if not _db_url:
    print("BABALA: Walang DATABASE_URL! Gagamit ng lokal na kusina.db na WALANG laman. "
          "I-set ang DATABASE_URL sa Environment ng admin service (kapareho ng main site).")
    _db_url = 'sqlite:///' + os.path.join(HERE, 'kusina.db')
elif _db_url.lower().startswith('sqlite') and ('C:' in _db_url or 'c:' in _db_url or '\\' in _db_url):
    print("BABALA: Windows path ang DATABASE_URL (galing sa lokal na .env?). Hindi ito gagana sa hosting.")
    _db_url = 'sqlite:///' + os.path.join(HERE, 'kusina.db')

if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True, 'pool_recycle': 280}
db = SQLAlchemy(app)


# Kapareho ng main site. HINDI gumagawa ng bagong table (walang create_all).
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80))
    email = db.Column(db.String(255))
    created_at = db.Column(db.DateTime)
    plan_json = db.Column(db.Text)


class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    recipe_id = db.Column(db.Integer)


from admin_bp import init_admin, admin_required  # noqa: E402
init_admin(app, db, User, Favorite, HERE)


@app.route('/admin/dbcheck')
@admin_required
def dbcheck():
    """Diagnostic: ipinapakita kung saang database nakakonekta ang admin at ilan ang users."""
    url = db.engine.url
    info = {'dialect': db.engine.dialect.name, 'host': url.host or '(lokal na file)',
            'database': url.database, 'ok': False}
    try:
        info['users'] = db.session.scalar(db.select(db.func.count(User.id)))
        info['favorites'] = db.session.scalar(db.select(db.func.count(Favorite.id)))
        info['ok'] = True
    except Exception as e:
        db.session.rollback()
        info['error'] = str(e)[:400]
    return jsonify(info)


@app.route('/')
def root():
    return redirect('/admin')


if __name__ == '__main__':
    print('Database:', _db_url.split('@')[-1] if '@' in _db_url else _db_url)
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', 5001)), debug=False)