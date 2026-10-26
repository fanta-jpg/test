from flask import Blueprint, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required, login_user, logout_user

from .models import User

auth = Blueprint('auth', __name__)


@auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('views.dashboard'))

    error = None
    username = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(request.form.get('password', '')):
            login_user(user, remember=True)
            return redirect(url_for('views.dashboard'))
        # Same message for unknown user and wrong password, so the page
        # doesn't reveal which usernames exist.
        error = _('Wrong username or password.')
    return render_template('login.html', error=error, username=username)


@auth.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))
