"""JSON API for the ESP32 feeder and the web frontend.

All routes are registered under the ``/api`` prefix (see ``website/__init__.py``).
The server is the single source of truth: the ESP32 asks a question on every
RFID read and the server answers ``feed`` or ``reject``.

NOTE: times are compared in UTC to match the ``Log.timestamp`` default. If the
feeding intervals are meant to be farm-local time, convert before comparing.
"""

import re
from datetime import datetime, timedelta

import hmac
from functools import wraps

from flask import Blueprint, current_app, jsonify, request
from flask_babel import gettext as _
from flask_login import login_required

from . import csrf
from .models import Animal, Calibration, Log, db


api = Blueprint('api', __name__)

# Motor run time (seconds) for a calibration test run.
CALIBRATION_RUN_TIME = 10.0

# How long a feeding / calibration holds the "busy" lock before it auto-clears,
# in case the ESP32 or the browser never reports back.
FEEDING_TIMEOUT = timedelta(seconds=120)
CALIBRATION_TIMEOUT = timedelta(seconds=10)

# Coarse, in-memory system state. A module global is fine for the single-process
# dev server; move it to the DB or a real job queue before running multiple
# workers or multiple feeders.
_pending_command = None       # one-shot motor command the ESP32 polls for
_feeding_until = None         # datetime; feeder is dispensing until this moment
_calibrating_since = None     # datetime; a calibration is running / awaiting its weight
device_info = {
        'last_seen': None,
        'up_time': None,
        'ip': None,
        'rssi': None,
    }
OFFLINE_AFTER = timedelta(seconds=20)


def requireDeviceKey(f):
    """Reject requests that don't carry the feeder's X-Device-Key header."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        sent = request.headers.get('X-Device-Key', '').encode()
        expected = current_app.config['FEEDER_API_KEY'].encode()
        if not hmac.compare_digest(sent, expected):
            return jsonify({'error': 'unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


def overallStatus():
    """'offline' if the feeder isn't reporting, otherwise _system_state()."""
    return _system_state() if deviceOnline() else 'offline'

def _system_state():
    """One of 'idle', 'feeding', 'calibrating'."""
    now = datetime.utcnow()
    #if _pending_command is not None:
    #    return 'calibrating'
    if _calibrating_since is not None and now - _calibrating_since < CALIBRATION_TIMEOUT:
        return 'calibrating'
    if _feeding_until is not None and now < _feeding_until:
        return 'feeding'
    return 'idle'


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def addLog(event_type, message=None):
    db.session.add(Log(event_type=event_type, message=message))
    db.session.commit()


def currentFeedRate():
    cal = Calibration.query.order_by(Calibration.created_at.desc()).first()
    return cal.feed_rate if cal else None


def withinInterval(now_t, start, end):
    #check if now_t fall between start & end
    if start <= end:
        return start <= now_t <= end
    #handle cases through midnight
    return now_t >= start or now_t <= end



def syncTodaysFeedings(animal):
    """Reset an animal's daily feeding counter when its feeding day has rolled over.

    The count lives on the Animal row itself (not derived from the Log
    table), so a new feeding day needs an explicit reset here before it's
    read. Uses UTC "now" to match the interval check in ``withinInterval``.

    For a window that crosses midnight (start > end, e.g. 22:00-02:00), the
    post-midnight tail of the window still belongs to the feeding day the
    window opened on, not the new calendar day -- otherwise the counter
    would reset mid-window and let an animal be fed roughly twice the daily
    limit across one continuous overnight window.
    """
    now = datetime.utcnow()
    today = now.date()

    start, end = animal.feeding_interval_start, animal.feeding_interval_end
    feeding_day = today
    if start is not None and end is not None and start > end and now.time() <= end:
        feeding_day = today - timedelta(days=1)

    if animal.feedings_today_date != feeding_day:
        animal.feedings_today = 0
        animal.feedings_today_date = feeding_day
        db.session.commit()

#def syncTodaysFeedings(animal):
#    """Reset an animal's daily feeding counter when the day has rolled over.
#
#    The count lives on the Animal row itself (not derived from the Log
#    table), so a new day needs an explicit reset here before it's read.
#    """
#    today = date.today()
#    if animal.feedings_today_date != today:
#        animal.feedings_today = 0
#        animal.feedings_today_date = today
#        db.session.commit()


# --------------------------------------------------------------------------- #
# ESP32: feeding
# --------------------------------------------------------------------------- #
@api.route('/feeding-request', methods=['POST'])
@csrf.exempt
@requireDeviceKey
def feeding_request():
    """ESP32 read an RFID tag; decide whether to feed and how much."""
    data = request.get_json(silent=True) or {}
    rfid_tag = (data.get('rfid_tag') or '').strip()

    animal = Animal.query.filter_by(rfid_tag=rfid_tag).first()

    if animal is None:
        #####TODO: send email or sms alert?
        addLog('unknown_tag', message=f'Unknown RFID tag: {rfid_tag}')
        return jsonify({'decision': 'reject', 'reason': 'unknown_tag'})

    now_t = datetime.utcnow().time()
    if not withinInterval(now_t, animal.feeding_interval_start, animal.feeding_interval_end):
        addLog('feeding_rejected',message=f'{animal.animal_number}: outside allowed feeding interval')

        return jsonify({'decision': 'reject', 'reason': 'outside_interval'})

    syncTodaysFeedings(animal)
    if animal.feedings_today >= animal.max_feedings_per_day:
        addLog('feeding_rejected', message=f'{animal.animal_number}: daily feeding limit reached')

        return jsonify({'decision': 'reject', 'reason': 'daily_limit_reached'})

    if animal.last_fed_at is not None and animal.min_minutes_between_feedings > 0:
        since_last = datetime.utcnow() - animal.last_fed_at
        min_gap = timedelta(minutes=animal.min_minutes_between_feedings)
        if since_last < min_gap:
            addLog('feeding_rejected',
                   message=f'{animal.animal_number}: fed too recently, '
                           f'{animal.min_minutes_between_feedings} min required between feedings')
            return jsonify({'decision': 'reject', 'reason': 'too_soon_since_last_feeding'})

    global _feeding_until
    _feeding_until = datetime.utcnow() + FEEDING_TIMEOUT

    feed_rate = currentFeedRate()
    response = {
        'decision': 'feed',
        'animal_number': animal.animal_number,
        'grams': animal.feed_amount,
    }
    if feed_rate:
        response['run_seconds'] = round(animal.feed_amount / feed_rate, 2)
    return jsonify(response)


@api.route('/feeding-complete', methods=['POST'])
@csrf.exempt
@requireDeviceKey
def feeding_complete():
    """ESP32 finished dispensing; record the completed feeding."""
    data = request.get_json(silent=True) or {}
    rfid_tag = (data.get('rfid_tag') or '').strip()
    grams = data.get('grams')

    animal = Animal.query.filter_by(rfid_tag=rfid_tag).first()
    animal_number = animal.animal_number if animal else rfid_tag

    global _feeding_until
    _feeding_until = None

    if animal is not None:
        syncTodaysFeedings(animal)
        animal.feedings_today += 1
        animal.last_fed_at = datetime.utcnow()
        db.session.commit()

    addLog('feeding', message=f'{animal_number}: fed {grams}g'
         if grams is not None else f'{animal_number}: feeding completed')
    return jsonify({'status': 'logged'})


@api.route('/status', methods=['GET'])
@login_required
def status():
    """Coarse system state, used by the UI to gate actions like calibration."""
    return jsonify({'status': overallStatus()})


@api.route('/logs/latest', methods=['GET'])
@login_required
def latest_log():
    """Most recent log entry, polled by the dashboard's "Last Event" tile."""
    log = Log.query.order_by(Log.timestamp.desc()).first()
    return jsonify(log.to_dict() if log else None)

def deviceOnline():
    last_seen = device_info['last_seen']
    return last_seen is not None and datetime.utcnow() - last_seen < OFFLINE_AFTER

@api.route('/device/status', methods=['POST'])
@csrf.exempt
@requireDeviceKey
def get_device_status():
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    rssi = data.get('rssi')
    up_time = data.get('up_time')

    #if device was offline before, device just came back online
    if not deviceOnline():
        addLog('device_online', message=f"Feeder online, IP {ip}")
    #device was online before, so check up_time in case device rebooted
    elif up_time is not None and device_info['up_time'] is not None and up_time < device_info['up_time']:
        addLog('device_rebooted', message='Feeder rebooted')
    device_info.update(last_seen=datetime.utcnow(), up_time=up_time, ip=ip, rssi=rssi)

    return jsonify({'status': 'ok'})

@api.route('/device/info', methods=['GET'])
@login_required
def device_info_view():
    """Feeder connection info, polled by the dashboard."""
    return jsonify({'online': deviceOnline(), 'ip': device_info['ip'],
                    'rssi': device_info['rssi'], 'up_time': device_info['up_time']})


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
@api.route('/calibration/run', methods=['POST'])
@login_required
def calibration_run():
    """Frontend pressed the calibration button; queue a motor run for the ESP32.

    Refused with 409 unless the feeder is idle.
    """
    global _pending_command, _calibrating_since
    state = _system_state()
    if state != 'idle':
        return jsonify({'error': 'busy', 'state': state}), 409

    _pending_command = {'action': 'run_motor', 'seconds': CALIBRATION_RUN_TIME}
    _calibrating_since = datetime.utcnow()
    return jsonify({'status': 'queued', 'seconds': CALIBRATION_RUN_TIME})


@api.route('/calibration/cancel', methods=['POST'])
@login_required
def calibration_cancel():
    """Frontend closed the calibration dialog without submitting a weight."""
    global _pending_command, _calibrating_since
    _pending_command = None
    _calibrating_since = None
    return jsonify({'status': 'cancelled'})


@api.route('/calibration/check_command', methods=['GET'])
@csrf.exempt
@requireDeviceKey
def calibration_command():
    """ESP32 polls this; returns a queued motor command exactly once."""
    global _pending_command
    if _pending_command is None:
        return jsonify({'action': 'none'})
    command, _pending_command = _pending_command, None
    return jsonify(command)


@api.route('/calibration/submit', methods=['POST'])
@login_required
def submit_calibration():
    """Frontend submits the measured weight; compute and store the feed rate."""
    data = request.get_json(silent=True) or {}
    run_seconds = data.get('run_seconds')
    measured_grams = data.get('measured_grams')

    if not run_seconds or measured_grams is None:
        return jsonify(
            {'error': 'run_seconds and measured_grams are required'}), 400

    feed_rate = float(measured_grams) / float(run_seconds)
    db.session.add(Calibration(feed_rate=feed_rate, run_seconds=run_seconds,
                               measured_grams=measured_grams))
    db.session.commit()
    addLog('calibration', message=f'New feed rate: {feed_rate:.2f} g/s')

    global _pending_command, _calibrating_since
    _pending_command = None
    _calibrating_since = None

    return jsonify({'feed_rate': round(feed_rate, 3)})


# --------------------------------------------------------------------------- #
# Animals
# --------------------------------------------------------------------------- #

def _validate_animal_payload(data):
    """Shared validation for the add/edit animal forms.

    Returns (fields, values). ``fields`` maps field name -> error message and
    is empty when everything parsed; ``values`` holds the parsed values for
    whichever fields did parse.
    """
    animal_number = (data.get('animal_number') or '').strip()
    rfid_tag = (data.get('rfid_tag') or '').strip()
    interval_start = (data.get('feeding_interval_start') or '').strip()
    interval_end = (data.get('feeding_interval_end') or '').strip()

    fields = {}

    if not animal_number:
        fields['animal_number'] = _('Enter an animal number.')

    if not re.fullmatch(r'\d{10}', rfid_tag):
        fields['rfid_tag'] = _('The RFID tag must be exactly 10 digits.')

    try:
        feed_amount = float(data.get('feed_amount'))
        if feed_amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        feed_amount = None
        fields['feed_amount'] = _('Enter a feed amount greater than 0.')

    try:
        max_feedings = int(data.get('max_feedings_per_day'))
        if max_feedings < 1:
            raise ValueError
    except (TypeError, ValueError):
        max_feedings = None
        fields['max_feedings_per_day'] = _('Enter at least 1 feeding per day.')

    try:
        min_minutes_between_feedings = int(data.get('min_minutes_between_feedings'))
        if min_minutes_between_feedings < 0:
            raise ValueError
    except (TypeError, ValueError):
        min_minutes_between_feedings = None
        fields['min_minutes_between_feedings'] = _('Enter 0 or more minutes between feedings.')

    try:
        start_t = datetime.strptime(interval_start, '%H:%M').time()
        end_t = datetime.strptime(interval_end, '%H:%M').time()
    except ValueError:
        start_t = end_t = None
        fields['feeding_interval_start'] = _('Set a valid feeding window.')

    values = {
        'animal_number': animal_number,
        'rfid_tag': rfid_tag,
        'feed_amount': feed_amount,
        'max_feedings_per_day': max_feedings,
        'min_minutes_between_feedings': min_minutes_between_feedings,
        'feeding_interval_start': start_t,
        'feeding_interval_end': end_t,
    }
    return fields, values


@api.route('/animals/add', methods=['POST'])
@login_required
def create_animal():
    """Frontend "Add animal" dialog submits here; validate and store one animal."""
    data = request.get_json(silent=True) or {}
    fields, values = _validate_animal_payload(data)

    if fields:
        return jsonify({'error': 'validation_failed', 'fields': fields}), 400

    if Animal.query.filter_by(animal_number=values['animal_number']).first():
        return jsonify({'error': 'duplicate', 'fields': {
            'animal_number': _('An animal with this number already exists.')}}), 409
    if Animal.query.filter_by(rfid_tag=values['rfid_tag']).first():
        return jsonify({'error': 'duplicate', 'fields': {
            'rfid_tag': _('This RFID tag is already registered.')}}), 409

    animal = Animal(**values)
    db.session.add(animal)
    db.session.commit()
    addLog('animal_updated', message=f'Added animal {values["animal_number"]}')
    #RETURNS ANIMAL JSON, NOT USED
    return jsonify(animal.to_dict()), 201


@api.route('/animals/<int:animal_id>/update', methods=['POST'])
@login_required
def update_animal(animal_id):
    """Frontend "Edit animal" dialog submits here; validate and update one animal."""
    animal = Animal.query.get(animal_id)
    if animal is None:
        return jsonify({'error': 'not_found'}), 404

    data = request.get_json(silent=True) or {}
    fields, values = _validate_animal_payload(data)

    if fields:
        return jsonify({'error': 'validation_failed', 'fields': fields}), 400

    duplicate_number = Animal.query.filter_by(animal_number=values['animal_number']).first()
    if duplicate_number and duplicate_number.id != animal_id:
        return jsonify({'error': 'duplicate', 'fields': {
            'animal_number': _('An animal with this number already exists.')}}), 409
    duplicate_rfid = Animal.query.filter_by(rfid_tag=values['rfid_tag']).first()
    if duplicate_rfid and duplicate_rfid.id != animal_id:
        return jsonify({'error': 'duplicate', 'fields': {
            'rfid_tag': _('This RFID tag is already registered.')}}), 409

    for key, value in values.items():
        setattr(animal, key, value)
    db.session.commit()
    addLog('animal_updated', message=f'Updated animal {values["animal_number"]}')
    return jsonify(animal.to_dict())


@api.route('/animals/<int:animal_id>/delete', methods=['POST'])
@login_required
def delete_animal(animal_id):
    """Frontend "Delete" button in the edit dialog submits here."""
    animal = Animal.query.get(animal_id)
    if animal is None:
        return jsonify({'error': 'not_found'}), 404

    animal_number = animal.animal_number
    db.session.delete(animal)
    db.session.commit()
    addLog('animal_updated', message=f'Deleted animal {animal_number}')
    return jsonify({'status': 'deleted'})
