import os
from datetime import timedelta

import click
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_babel import Babel
from flask_babel import gettext as _
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import User, db

csrf = CSRFProtect()

# Languages the site is translated into; English is the source language in
# the code, Danish lives in website/translations/da/LC_MESSAGES/messages.po.
LANGUAGES = ['en', 'da']


def select_locale():
    """The language picked with the switcher (cookie), else the browser's."""
    lang = request.cookies.get('lang')
    if lang in LANGUAGES:
        return lang
    return request.accept_languages.best_match(LANGUAGES) or 'en'


def create_app():
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config['SECRET_KEY'] = os.environ['SECRET_KEY']
    if not app.config['SECRET_KEY']:
        raise RuntimeError('SECRET_KEY is not set')
    app.config['FEEDER_API_KEY'] = os.environ['FEEDER_API_KEY']
    if not app.config['FEEDER_API_KEY']:
        raise RuntimeError('FEEDER_API_KEY is not set')

    # Set by deploy/setup.sh when nginx serves the site over HTTPS: trust its
    # X-Forwarded-* headers and only send cookies over HTTPS.
    if os.environ.get('BEHIND_HTTPS_PROXY') == '1':
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.config['SESSION_COOKIE_SECURE'] = True
        app.config['REMEMBER_COOKIE_SECURE'] = True

    # Session / "remember me" cookies.
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
    app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
    # CSRF tokens live as long as the session, so a page left open for hours
    # (e.g. calibration) can still submit.
    app.config['WTF_CSRF_TIME_LIMIT'] = None

    os.makedirs(app.instance_path, exist_ok=True)
    DB_NAME = os.path.join(app.instance_path, 'farmgaard.sqlite3')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_NAME}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    csrf.init_app(app)
    Babel(app, locale_selector=select_locale)

    @app.context_processor
    def inject_language():
        return {'current_lang': select_locale(), 'languages': LANGUAGES}

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        # fetch() calls get JSON, page visits get sent to the sign-in page
        if request.path.startswith('/api/'):
            return jsonify({'error': 'login_required'}), 401
        return redirect(url_for('auth.login'))

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        if request.path.startswith('/api/'):
            # CSRF is checked before @login_required, so a request from an
            # expired session lands here; report it as signed out.
            if not current_user.is_authenticated:
                return jsonify({'error': 'login_required'}), 401
            return jsonify({'error': 'csrf_failed', 'reason': e.description}), 400
        # A form outside /api (sign in, sign out, language) had a stale token;
        # show the sign-in page with the reason under the password field.
        return render_template('login.html',
                               error=_('Your session expired. Sign in again.'),
                               username=request.form.get('username', '').strip()), 400

    @app.cli.command('create-user')
    @click.argument('username')
    @click.option('--admin', is_flag=True, help='Give the account admin rights.')
    @click.password_option()
    def create_user(username, admin, password):
        """Create a login account (there is no public sign-up)."""
        if User.query.filter_by(username=username).first():
            raise click.ClickException(f'User {username} already exists')
        user = User(username=username, is_admin=admin)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f'Created user {username}')

    @app.cli.command('set-password')
    @click.argument('username')
    @click.password_option()
    def set_password(username, password):
        """Change an existing account's password."""
        user = User.query.filter_by(username=username).first()
        if user is None:
            raise click.ClickException(f'User {username} does not exist')
        user.set_password(password)
        db.session.commit()
        click.echo(f'Password updated for {username}')

    from .views import views
    from . import api as api_module
    from .api import api
    from .auth import auth

    app.register_blueprint(views, url_prefix='/')
    app.register_blueprint(api, url_prefix='/api')
    app.register_blueprint(auth, url_prefix='/')

    with app.app_context():
        db.create_all()
        api_module.backfillFeedings()

    return app
