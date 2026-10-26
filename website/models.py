from datetime import date, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(db.Model, UserMixin):
    """A login account. Created with ``flask create-user``; there is no sign-up page."""
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Animal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    animal_number = db.Column(db.String(50), unique=True, nullable=False)
    rfid_tag = db.Column(db.String(10), unique=True, nullable=False)
    feed_amount = db.Column(db.Float, nullable=False)
    max_feedings_per_day = db.Column(db.Integer, nullable=False)
    min_minutes_between_feedings = db.Column(db.Integer, nullable=False, default=0)
    feeding_interval_start = db.Column(db.Time, nullable=False)
    feeding_interval_end = db.Column(db.Time, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Running count of today's feedings and the day it belongs to, so the
    # count can be reset once that day has passed without a scheduled job.
    feedings_today = db.Column(db.Integer, default=0, nullable=False)
    feedings_today_date = db.Column(db.Date, default=date.today, nullable=False)

    # When the animal was last actually fed, so a minimum gap between
    # feedings can be enforced even within the daily count/interval limits.
    last_fed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'animal_number': self.animal_number,
            'rfid_tag': self.rfid_tag,
            'feed_amount': self.feed_amount,
            'max_feedings_per_day': self.max_feedings_per_day,
            'min_minutes_between_feedings': self.min_minutes_between_feedings,
            'feeding_interval_start': self.feeding_interval_start.strftime('%H:%M')
            if self.feeding_interval_start else None,
            'feeding_interval_end': self.feeding_interval_end.strftime('%H:%M')
            if self.feeding_interval_end else None,
            'feedings_today': self.feedings_today,
            'last_fed_at': self.last_fed_at.isoformat() if self.last_fed_at else None,
        }


class Calibration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_rate = db.Column(db.Float, nullable=False)  # grams per second
    run_seconds = db.Column(db.Float)
    measured_grams = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'feed_rate': round(self.feed_rate, 3),
            'run_seconds': self.run_seconds,
            'measured_grams': self.measured_grams,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Log(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    message = db.Column(db.String(255))

    def to_dict(self):
        return {
            'id': self.id,
            'event_type': self.event_type,
            'message': self.message,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }
