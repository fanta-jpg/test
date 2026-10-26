from urllib.parse import urlparse

from flask import Blueprint, redirect, render_template, request
from flask_login import login_required

from .models import Calibration, Log, Animal
from .api import device_info

################
from datetime import date, datetime
from. import api
from . import LANGUAGES

views = Blueprint('views', __name__)

#Main page - Dashboard
@views.route('/')
@login_required
def dashboard():
    last_event = Log.query.order_by(Log.timestamp.desc()).first()
    animals = Animal.query.order_by(Animal.animal_number).all()
    animal_count = len(animals)
    curr_time = datetime.utcnow()
    device_ip = device_info['ip']
    device_rssi = device_info['rssi']
    device_up_time = device_info['up_time']
    return render_template("dashboard.html", last_event=last_event, animal_count=animal_count, curr_time=curr_time, system_status=api.systemState(), animals=animals)

#Animal overview page
@views.route('/animals')
@login_required
def animals():
    animals = Animal.query.all()
    for animal in animals:
        api.syncTodaysFeedings(animal)
    return render_template("animals.html", animals=animals)

#Calibration page
@views.route('/calibration')
@login_required
def calibration():
    cal = Calibration.query.order_by(Calibration.created_at.desc()).first()
    return render_template(
        "calibration.html",
        feed_rate=f"{cal.feed_rate:.2f}" if cal else None,
        last_calibration=cal.created_at.strftime('%Y/%m/%d %H:%M') if cal else None
    )

#Log page
#The page polls itself with ?after=<newest id shown> to pick up only new rows.
@views.route('/logs')
@login_required
def logs():
    query = Log.query
    after = request.args.get('after', type=int)
    if after is not None:
        query = query.filter(Log.id > after)
    logs = query.order_by(Log.timestamp.desc()).all()
    return render_template("logs.html", logs=logs)


#Language switcher (sidebar and sign-in page); works signed in or out.
#Goes back to the page it was clicked on -- only the path is kept, so the
#redirect can't be pointed at another site.
@views.route('/language', methods=['POST'])
def set_language():
    lang = request.form.get('lang')
    # lstrip: a path like "//evil.com" would otherwise be read as another site
    back = '/' + urlparse(request.referrer or '').path.lstrip('/')
    response = redirect(back)
    if lang in LANGUAGES:
        response.set_cookie('lang', lang, max_age=60 * 60 * 24 * 365, samesite='Lax')
    return response
