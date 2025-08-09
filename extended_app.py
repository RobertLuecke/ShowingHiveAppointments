"""
extended_app.py
================

This Flask application demonstrates a more feature‑rich showing‑management
platform branded as **ShowingHive**.  It draws inspiration from
Instashowing but is not affiliated with that company.  It is not intended for production use
but provides an example of how to implement core workflows such as
property management, showing scheduling, tour planning, lockbox code
generation and approval workflows with in‑memory data structures.  The
application intentionally avoids external integrations (e.g. mapping or
lockbox hardware APIs) and uses simplified logic to illustrate concepts
found on modern showing‑management platforms, such as customizable
approval settings, buyer tour creation and seller dashboards.

Key Features
------------

* **Property Management** – Create and list properties.  Each property has
  a unique identifier, a name and an address.  Properties can also have
  blocked time ranges during which showings cannot be scheduled.

* **Showing Scheduling** – Schedule showings for specific properties at
  given times.  When scheduling, the application checks for existing
  showings and any blocked times to avoid conflicts.  Showings start in
  a ``pending`` state and must be approved by a seller.

* **Approval and Rescheduling** – Sellers can approve, decline or
  reschedule a pending showing.  Upon approval the system issues a
  one‑time lockbox code (a random six‑digit string) that expires shortly
  after the scheduled start time.  Rescheduling updates the scheduled
  time and re‑generates a lockbox code.

* **Feedback Collection** – After a showing has occurred, clients can
  submit a rating and a comment.  Feedback is attached to the showing
  and viewable in the seller dashboard.

* **Buyer Tours** – Buyers can create tours comprising multiple approved
  showings.  The application sorts the selected showings by their
  scheduled start time to produce a simple itinerary.  In a real
  platform you would calculate travel times and distances; here we
  simply order by date/time to illustrate the concept.

* **Seller Dashboard** – View all showings for a given property,
  including their status, any blocked times and feedback.  Sellers can
  approve, decline or reschedule pending showings and add new blocked
  time ranges from this dashboard.

The application stores all data in Python dictionaries and lists for
demonstration purposes.  In production you would use a persistent
database and implement authentication/authorization.  All endpoints
return JSON responses so you can interact with the API using tools such
as ``curl`` or ``httpie``.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, render_template_string, send_file, render_template, redirect, url_for
from werkzeug.utils import secure_filename
import io
import smtplib
from email.mime.text import MIMEText

# Added imports for database and user authentication
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# -----------------------------------------------------------------------------
# Database configuration and initialization
#
# The extended application now uses a SQLite database to persist users,
# properties and showings.  The SQLAlchemy and Flask‑Login extensions are
# configured here.  Replace the secret key with a secure random value in
# production.
app.config["SECRET_KEY"] = "change-this-secret-key"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
# Redirect unauthenticated users to the login page
login_manager.login_view = "login"

# In‑memory data stores
properties: Dict[str, Dict[str, Any]] = {}
showings: Dict[str, Dict[str, Any]] = {}
feedback_store: Dict[str, List[Dict[str, Any]]] = {}
blocked_times: Dict[str, List[Tuple[datetime, datetime]]] = {}
tours: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# Database models
#
# The ``User`` model represents users who can authenticate to manage properties
# and showings.  The ``PropertyModel`` and ``ShowingModel`` models mirror the
# in‑memory ``properties`` and ``showings`` data structures but provide
# persistent storage.  During creation of a property or showing via the UI or
# API, the corresponding database record is created in addition to the
# in‑memory entry.  These models are simplified and do not include every field
# from the in‑memory dictionaries; they demonstrate how to begin migrating
# towards a persistent database.

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)


class PropertyModel(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(200), nullable=False)
    seller_name = db.Column(db.String(200))
    seller_phone = db.Column(db.String(50))
    seller_email = db.Column(db.String(200))
    agent_name = db.Column(db.String(200))
    agent_phone = db.Column(db.String(50))
    agent_email = db.Column(db.String(200))
    auto_approve_showings = db.Column(db.Boolean, default=False)
    requires_disclosure_approval = db.Column(db.Boolean, default=False)


class ShowingModel(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    property_id = db.Column(db.String(36), db.ForeignKey("property_model.id"), nullable=False)
    client_name = db.Column(db.String(200), nullable=False)
    client_phone = db.Column(db.String(50))
    client_email = db.Column(db.String(200))
    scheduled_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    lockbox_code = db.Column(db.String(20))
    code_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    property = db.relationship("PropertyModel", backref="showings")

# -----------------------------------------------------------------------------
# Helper to load database records into in‑memory structures

def load_db_into_memory() -> None:
    """Load persisted properties and showings from the database into the in‑memory dictionaries.

    This function queries the PropertyModel and ShowingModel tables and populates
    the ``properties`` and ``showings`` dictionaries, allowing the rest of the
    application to operate on the same in‑memory data structures it used before
    database support was added.
    """
    # Clear existing in‑memory data
    properties.clear()
    showings.clear()
    for prop in PropertyModel.query.all():
        properties[prop.id] = {
            "id": prop.id,
            "name": prop.name,
            "address": prop.address,
            "created_at": datetime.utcnow(),
            "seller_name": prop.seller_name,
            "seller_phone": prop.seller_phone,
            "seller_email": prop.seller_email,
            "agent_name": prop.agent_name,
            "agent_phone": prop.agent_phone,
            "agent_email": prop.agent_email,
            "auto_approve_showings": prop.auto_approve_showings,
            "requires_disclosure_approval": prop.requires_disclosure_approval,
        }
    for sh in ShowingModel.query.all():
        showings[sh.id] = {
            "id": sh.id,
            "property_id": sh.property_id,
            "client_name": sh.client_name,
            "client_phone": sh.client_phone,
            "client_email": sh.client_email,
            "scheduled_at": sh.scheduled_at,
            "status": sh.status,
            "lockbox_code": sh.lockbox_code,
            "code_expires_at": sh.code_expires_at,
            "created_at": sh.created_at,
        }

# -----------------------------------------------------------------------------
# User loader for Flask‑Login
#
@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    """
    Given a user ID (stored in the session), return the corresponding User
    object.  Flask‑Login uses this callback to reload the user from the
    database on each request.

    :param user_id: The primary key of the user as a string.
    :return: The ``User`` instance or ``None`` if not found.
    """
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Disclosure management and activity logging
#
# The following two in‑memory structures hold uploaded disclosure files and
# records of activity for each property.  Disclosure files are stored as
# byte streams keyed by property ID and filename.  The activity log is a
# chronological list of events (such as showing requests, approvals,
# declines, reschedules, feedback submissions and disclosure uploads or
# downloads) for each property.  These structures are kept in memory for
# demonstration purposes; a real system would persist them in a database
# or external storage.
disclosures: Dict[str, Dict[str, bytes]] = {}
activity_logs: Dict[str, List[Dict[str, Any]]] = {}

# Packages and sharing
# -----------------------------------------------------------------------------
# A package groups multiple disclosure files together into a single listing
# packet for a property.  Packages can be public or private.  A package may be
# shared with individual buyers via a unique share link, which allows buyers
# to download the files and automatically logs their activity.  Package
# definitions and shares are stored in the following in‑memory structures.
packages: Dict[str, Dict[str, Any]] = {}
package_shares: Dict[str, Dict[str, Any]] = {}

# Disclosure feedback storage
# -----------------------------------------------------------------------------
# Buyers can provide feedback on disclosure packages after reviewing them.  This
# dictionary stores feedback entries keyed by share ID.  Each entry contains
# a list of feedback objects with rating, comment and creation timestamp.
disclosure_feedback_store: Dict[str, List[Dict[str, Any]]] = {}

# Offers management
# -----------------------------------------------------------------------------
# Listing offers are stored in the ``offers`` dictionary keyed by property ID.
# Each offer includes an ID, the buyer's name, price and an optional set of
# terms.  Offers can be listed and created via dedicated endpoints.
offers: Dict[str, List[Dict[str, Any]]] = {}


def log_event(property_id: str, event_type: str, details: Dict[str, Any]) -> None:
    """
    Record an activity event for a property.  Each event includes a
    timestamp and arbitrary details.  Events are stored in reverse
    chronological order (most recent first).

    :param property_id: ID of the property the event relates to.
    :param event_type: A short string describing the type of event
        (e.g., ``showing_requested``, ``showing_approved``, ``feedback_submitted``,
        ``upload_disclosure``, ``download_disclosure``).
    :param details: Additional context about the event.
    """
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "type": event_type,
        "details": details,
    }
    activity_logs.setdefault(property_id, []).insert(0, entry)


def generate_lockbox_code() -> str:
    """Generate a random six‑digit lockbox code."""
    return f"{random.randint(0, 999999):06d}"

# -----------------------------------------------------------------------------
# Twilio integration
#
# The extended application can optionally send SMS notifications using Twilio.
# To avoid a hard dependency on the twilio package in environments where it is
# not installed, the import is wrapped in a try/except and a simple stub
# function will be used when the library isn't available.  An administrator can
# configure the account SID, auth token and sender number via the `/admin/twilio`
# endpoint.  These values are stored in the ``twilio_config`` dictionary.  If
# any of the values are missing, ``send_sms`` will simply log the message to
# the console instead of attempting to send it.
try:
    from twilio.rest import Client  # type: ignore
except Exception:
    Client = None  # type: ignore

# Holds Twilio credentials and default "from" number.  Administrators should
# populate these values via the `/admin/twilio` endpoint.
twilio_config: Dict[str, Optional[str]] = {
    "account_sid": None,
    "auth_token": None,
    "from_number": None,
}

def send_sms(to_number: str, message: str) -> None:
    """
    Send an SMS message using Twilio.  If the Twilio client or configuration
    values are not available, the message is printed to the console instead.

    :param to_number: Destination phone number (in E.164 format).
    :param message: Text message to send.
    """
    if Client is None:
        # Twilio is not installed; log instead of sending
        print(f"[SMS not sent] To {to_number}: {message}")
        return
    sid = twilio_config.get("account_sid")
    token = twilio_config.get("auth_token")
    from_number = twilio_config.get("from_number")
    if not sid or not token or not from_number:
        # Missing configuration; log instead of sending
        print(f"[SMS not sent] To {to_number}: {message} (Twilio config incomplete)")
        return
    try:
        client = Client(sid, token)
        client.messages.create(body=message, from_=from_number, to=to_number)
    except Exception as e:  # pragma: no cover - network errors are non-deterministic
        print(f"[SMS error] Could not send to {to_number}: {e}")


# -----------------------------------------------------------------------------
# Email integration
#
# This application can optionally send email notifications using a simple SMTP
# client.  Administrators configure the SMTP server, port, optional login
# credentials and sender email via the `/admin/email` endpoint.  The
# configuration values are stored in ``email_config``.  When any of the
# required values are missing, ``send_email`` will log the message to the
# console instead of attempting to send it.

email_config: Dict[str, Optional[str]] = {
    "smtp_server": None,
    "smtp_port": None,
    "smtp_username": None,
    "smtp_password": None,
    "from_email": None,
    "use_tls": "true",  # store as string for simplicity
}

def send_email(to_email: str, subject: str, message: str) -> None:
    """
    Send an email notification using the configured SMTP server.  If the
    configuration is incomplete, the email is printed to the console instead.

    :param to_email: Destination email address.
    :param subject: Email subject.
    :param message: Plain‑text email body.
    """
    server_host = email_config.get("smtp_server")
    server_port = email_config.get("smtp_port")
    from_addr = email_config.get("from_email")
    if not server_host or not server_port or not from_addr:
        print(f"[Email not sent] To {to_email}: {subject} - {message} (email config incomplete)")
        return
    try:
        port_num = int(server_port)
    except Exception:
        print(f"[Email not sent] Invalid SMTP port: {server_port}")
        return
    msg = MIMEText(message)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_email
    try:
        with smtplib.SMTP(server_host, port_num) as smtp:
            # use TLS if configured
            use_tls = (email_config.get("use_tls") or "false").lower() == "true"
            if use_tls:
                try:
                    smtp.starttls()
                except Exception:
                    pass
            user = email_config.get("smtp_username")
            pwd = email_config.get("smtp_password")
            if user and pwd:
                try:
                    smtp.login(user, pwd)
                except Exception:
                    pass
            smtp.send_message(msg)
    except Exception as e:
        print(f"[Email error] Could not send to {to_email}: {e}")


def is_time_blocked(property_id: str, start: datetime, end: datetime) -> bool:
    """
    Check whether the given time range overlaps any blocked period for the
    property.
    """
    for b_start, b_end in blocked_times.get(property_id, []):
        if start < b_end and end > b_start:
            return True
    return False


def has_conflict(property_id: str, start: datetime, end: datetime) -> bool:
    """
    Determine if the proposed showing conflicts with an existing showing for
    the same property.
    """
    for s in showings.values():
        if s["property_id"] != property_id or s["status"] == "declined":
            continue
        s_start = s["scheduled_at"]
        s_end = s_start + timedelta(hours=1)  # assume 1‑hour showings
        if start < s_end and end > s_start:
            return True
    return False


@app.route("/properties", methods=["GET", "POST"])
def property_list() -> Any:
    """
    List all properties or create a new property.  POST data should
    include ``name`` and ``address``.  Additional optional fields may
    specify seller or agent contact information:

    ``seller_name``, ``seller_phone``, ``seller_email`` – Contact information
        for the home seller.  At least one of phone or email should be
        provided if you want the seller to receive showing notifications.

    ``agent_name``, ``agent_phone``, ``agent_email`` – Contact information
        for the listing agent.  If provided, the agent will also
        receive notifications about showing requests.

    ``auto_approve_showings`` (boolean) – When set to true, any showing
        scheduled for this property will be automatically approved
        immediately.  The system will generate a lockbox code and send
        notifications to the buyer and property contacts without
        requiring manual approval.

    ``requires_disclosure_approval`` (boolean) – When true, buyers
        requesting disclosure packages must have their share approved by
        the seller or agent before they can download files.  Shares
        created under this setting start in a ``pending`` state and
        must be approved via ``POST /share/<share_id>/approve``.

    Returns JSON.
    """
    if request.method == "POST":
        data = request.json or {}
        name = data.get("name")
        address = data.get("address")
        if not name or not address:
            return jsonify({"error": "name and address are required"}), 400
        prop_id = str(uuid.uuid4())
        # capture optional contact details for seller and agent
        # Parse boolean flags for auto approval settings
        def parse_bool(val: Any) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in {"true", "1", "yes", "on"}
            return False
        properties[prop_id] = {
            "id": prop_id,
            "name": name,
            "address": address,
            "created_at": datetime.utcnow(),
            "seller_name": data.get("seller_name"),
            "seller_phone": data.get("seller_phone"),
            "seller_email": data.get("seller_email"),
            "agent_name": data.get("agent_name"),
            "agent_phone": data.get("agent_phone"),
            "agent_email": data.get("agent_email"),
            # If true, showings will automatically be approved upon request
            "auto_approve_showings": parse_bool(data.get("auto_approve_showings")),
            # If true, disclosure packages require explicit approval before download
            "requires_disclosure_approval": parse_bool(data.get("requires_disclosure_approval")),
        }
        return jsonify(properties[prop_id]), 201
    # GET
    return jsonify(list(properties.values()))


@app.route("/properties/<property_id>/blocks", methods=["GET", "POST"])
def manage_blocks(property_id: str) -> Any:
    """
    View or add blocked time ranges for a property.  POST data should
    include ``start`` and ``end`` ISO‑formatted datetimes.  Returns JSON.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    if request.method == "POST":
        data = request.json or {}
        try:
            start = datetime.fromisoformat(data.get("start"))
            end = datetime.fromisoformat(data.get("end"))
        except Exception:
            return jsonify({"error": "invalid start or end time"}), 400
        if end <= start:
            return jsonify({"error": "end must be after start"}), 400
        # Check overlap
        if is_time_blocked(property_id, start, end):
            return jsonify({"error": "time range overlaps existing block"}), 409
        blocked_times.setdefault(property_id, []).append((start, end))
        return jsonify({"start": start.isoformat(), "end": end.isoformat()}), 201
    # GET
    return jsonify([
        {"start": s.isoformat(), "end": e.isoformat()}
        for s, e in blocked_times.get(property_id, [])
    ])


@app.route("/showings", methods=["GET", "POST"])
def showing_list() -> Any:
    """
    List all showings or schedule a new showing.  POST data should include
    ``property_id``, ``scheduled_at`` (ISO datetime) and ``client_name``.
    A showing is scheduled for a fixed one‑hour window starting at
    ``scheduled_at``.  Returns JSON.
    """
    if request.method == "POST":
        data = request.json or {}
        prop_id = data.get("property_id")
        sched_str = data.get("scheduled_at")
        client_name = data.get("client_name")
        # Optional phone number and email for the client; used for notifications
        client_phone = data.get("client_phone")
        client_email = data.get("client_email")
        if not prop_id or prop_id not in properties:
            return jsonify({"error": "invalid property_id"}), 400
        if not sched_str or not client_name:
            return jsonify({"error": "scheduled_at and client_name are required"}), 400
        try:
            start = datetime.fromisoformat(sched_str)
        except Exception:
            return jsonify({"error": "invalid date format"}), 400
        end = start + timedelta(hours=1)
        # Check blocks and conflicts
        if is_time_blocked(prop_id, start, end):
            return jsonify({"error": "requested time is blocked"}), 409
        if has_conflict(prop_id, start, end):
            return jsonify({"error": "requested time conflicts with another showing"}), 409
        showing_id = str(uuid.uuid4())
        showings[showing_id] = {
            "id": showing_id,
            "property_id": prop_id,
            "client_name": client_name,
            "client_phone": client_phone,
            "client_email": client_email,
            "scheduled_at": start,
            "status": "pending",
            "lockbox_code": None,
            "code_expires_at": None,
            "created_at": datetime.utcnow(),
        }
        # Notify the buyer that their request was received
        if client_phone:
            try:
                prop = properties.get(prop_id)
                prop_name = prop.get("name") if prop else prop_id
                when = start.strftime("%Y-%m-%d %H:%M")
                send_sms(client_phone, f"Your showing request for {prop_name} on {when} has been received and is pending approval.")
            except Exception:
                pass
        if client_email:
            try:
                prop = properties.get(prop_id)
                prop_name = prop.get("name") if prop else prop_id
                when = start.strftime("%Y-%m-%d %H:%M")
                send_email(
                    client_email,
                    "Showing request received",
                    f"Hello {client_name},\n\nYour showing request for {prop_name} on {when} has been received and is pending approval.\n\nThank you."
                )
            except Exception:
                pass
        # Notify the seller and/or agent about the pending showing
        try:
            prop = properties.get(prop_id, {})
            prop_name = prop.get("name", prop_id)
            when = start.strftime("%Y-%m-%d %H:%M")
            seller_phone = prop.get("seller_phone")
            seller_email = prop.get("seller_email")
            agent_phone = prop.get("agent_phone")
            agent_email = prop.get("agent_email")
            # Prepare the message with instructions
            msg = (
                f"New showing request for {prop_name}: {client_name} has requested to view the property on {when}.\n"
                f"Use your dashboard or the API to approve, decline or reschedule this showing.\n"
                f"Showing ID: {showing_id}"
            )
            subj = f"New showing request for {prop_name}"
            # Send to seller
            if seller_phone:
                send_sms(seller_phone, msg)
            if seller_email:
                send_email(seller_email, subj, msg)
            # Also send to agent if provided
            if agent_phone:
                send_sms(agent_phone, msg)
            if agent_email:
                send_email(agent_email, subj, msg)
        except Exception:
            pass
        # Log the showing request as an activity event
        try:
            log_event(prop_id, "showing_requested", {
                "showing_id": showing_id,
                "client_name": client_name,
                "scheduled_at": start.isoformat(),
            })
        except Exception:
            pass

        # Auto‑approve the showing if the property is configured to do so
        try:
            prop = properties.get(prop_id, {})
            if prop.get("auto_approve_showings"):
                # mimic the approve_showing logic
                s = showings.get(showing_id)
                if s and s["status"] == "pending":
                    code = generate_lockbox_code()
                    s["lockbox_code"] = code
                    s["code_expires_at"] = s["scheduled_at"] + timedelta(hours=1, minutes=15)
                    s["status"] = "approved"
                    # notify buyer about approval
                    client_phone = s.get("client_phone")
                    client_email2 = s.get("client_email")
                    prop_name2 = prop.get("name", prop_id)
                    when2 = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
                    code_str = s["lockbox_code"]
                    expires_str = s["code_expires_at"].strftime("%Y-%m-%d %H:%M") if s.get("code_expires_at") else ""
                    sms_msg2 = f"Your showing for {prop_name2} at {when2} has been approved. Lockbox code: {code_str} (expires {expires_str})."
                    email_subj2 = "Showing approved"
                    email_body2 = f"Hello {s['client_name']},\n\nYour showing for {prop_name2} at {when2} has been approved.\nYour lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you."
                    if client_phone:
                        send_sms(client_phone, sms_msg2)
                    if client_email2:
                        send_email(client_email2, email_subj2, email_body2)
                    # notify seller/agent about auto approval
                    seller_phone2 = prop.get("seller_phone")
                    seller_email2 = prop.get("seller_email")
                    agent_phone2 = prop.get("agent_phone")
                    agent_email2 = prop.get("agent_email")
                    notif_msg = (
                        f"Showing for {prop_name2} on {when2} was automatically approved.\n"
                        f"Buyer: {s['client_name']}. Lockbox code: {code_str} (expires {expires_str}).\n"
                        f"Showing ID: {showing_id}"
                    )
                    notif_subj = f"Showing auto‑approved for {prop_name2}"
                    if seller_phone2:
                        send_sms(seller_phone2, notif_msg)
                    if seller_email2:
                        send_email(seller_email2, notif_subj, notif_msg)
                    if agent_phone2:
                        send_sms(agent_phone2, notif_msg)
                    if agent_email2:
                        send_email(agent_email2, notif_subj, notif_msg)
                    # log approval event
                    log_event(prop_id, "showing_approved", {
                        "showing_id": showing_id,
                        "client_name": s["client_name"],
                        "scheduled_at": s["scheduled_at"].isoformat(),
                        "lockbox_code": s["lockbox_code"],
                        "auto": True,
                    })
        except Exception:
            pass
        return jsonify(showings[showing_id]), 201
    # GET
    return jsonify(list(showings.values()))


@app.route("/showings/<showing_id>/approve", methods=["POST"])
def approve_showing(showing_id: str) -> Any:
    """
    Approve a pending showing.  Generates a lockbox code valid until one
    hour after the scheduled start time.  Returns the updated showing.
    """
    s = showings.get(showing_id)
    if not s:
        return jsonify({"error": "showing not found"}), 404
    if s["status"] != "pending":
        return jsonify({"error": "only pending showings can be approved"}), 400
    code = generate_lockbox_code()
    s["lockbox_code"] = code
    s["code_expires_at"] = s["scheduled_at"] + timedelta(hours=1, minutes=15)
    s["status"] = "approved"
    # Send approval notifications to the buyer
    client_phone = s.get("client_phone")
    client_email = s.get("client_email")
    try:
        prop = properties.get(s["property_id"])
        prop_name = prop.get("name") if prop else s["property_id"]
        when = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
        code_str = s["lockbox_code"]
        expires_str = s["code_expires_at"].strftime("%Y-%m-%d %H:%M") if s.get("code_expires_at") else ""
        sms_msg = f"Your showing for {prop_name} at {when} has been approved. Lockbox code: {code_str} (expires {expires_str})."
        email_subj = "Showing approved"
        email_body = f"Hello {s['client_name']},\n\nYour showing for {prop_name} at {when} has been approved.\nYour lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you."
        if client_phone:
            send_sms(client_phone, sms_msg)
        if client_email:
            send_email(client_email, email_subj, email_body)
    except Exception:
        pass
    # Notify seller/agent that the showing has been approved (manual)
    try:
        prop = properties.get(s["property_id"], {})
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        prop_name = prop.get("name", s["property_id"])
        when = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
        code_str = s.get("lockbox_code") or ""
        expires_str = s.get("code_expires_at").strftime("%Y-%m-%d %H:%M") if s.get("code_expires_at") else ""
        msg_notify = (
            f"Showing for {prop_name} on {when} has been approved.\n"
            f"Buyer: {s['client_name']}. Lockbox code: {code_str} (expires {expires_str}).\n"
            f"Showing ID: {showing_id}"
        )
        subj_notify = f"Showing approved for {prop_name}"
        if seller_phone:
            send_sms(seller_phone, msg_notify)
        if seller_email:
            send_email(seller_email, subj_notify, msg_notify)
        if agent_phone:
            send_sms(agent_phone, msg_notify)
        if agent_email:
            send_email(agent_email, subj_notify, msg_notify)
    except Exception:
        pass
    # Log the approval event
    try:
        log_event(s["property_id"], "showing_approved", {
            "showing_id": showing_id,
            "client_name": s["client_name"],
            "scheduled_at": s["scheduled_at"].isoformat(),
            "lockbox_code": s["lockbox_code"],
        })
    except Exception:
        pass
    return jsonify(s)


@app.route("/showings/<showing_id>/decline", methods=["POST"])
def decline_showing(showing_id: str) -> Any:
    """
    Decline a pending showing.  Returns the updated showing.
    """
    s = showings.get(showing_id)
    if not s:
        return jsonify({"error": "showing not found"}), 404
    if s["status"] != "pending":
        return jsonify({"error": "only pending showings can be declined"}), 400
    s["status"] = "declined"
    # Notify the client of the decline via SMS/email if contact info is available
    client_phone = s.get("client_phone")
    client_email = s.get("client_email")
    try:
        prop = properties.get(s["property_id"])
        prop_name = prop.get("name") if prop else s["property_id"]
        when = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
        sms_msg = f"Your showing request for {prop_name} on {when} has been declined."
        email_subj = "Showing declined"
        email_body = f"Hello {s['client_name']},\n\nYour showing request for {prop_name} on {when} has been declined.\n\nThank you."
        if client_phone:
            send_sms(client_phone, sms_msg)
        if client_email:
            send_email(client_email, email_subj, email_body)
    except Exception:
        pass
    # Log the decline event
    try:
        log_event(s["property_id"], "showing_declined", {
            "showing_id": showing_id,
            "client_name": s["client_name"],
            "scheduled_at": s["scheduled_at"].isoformat(),
        })
    except Exception:
        pass
    # Notify seller/agent of the declined showing
    try:
        prop = properties.get(s["property_id"], {})
        prop_name = prop.get("name", s["property_id"])
        when = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
        msg_notify = (
            f"Showing for {prop_name} on {when} has been declined.\n"
            f"Buyer: {s['client_name']}. Showing ID: {showing_id}"
        )
        subj_notify = f"Showing declined for {prop_name}"
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if seller_phone:
            send_sms(seller_phone, msg_notify)
        if seller_email:
            send_email(seller_email, subj_notify, msg_notify)
        if agent_phone:
            send_sms(agent_phone, msg_notify)
        if agent_email:
            send_email(agent_email, subj_notify, msg_notify)
    except Exception:
        pass
    return jsonify(s)


@app.route("/showings/<showing_id>/reschedule", methods=["POST"])
def reschedule_showing(showing_id: str) -> Any:
    """
    Reschedule an approved or pending showing.  POST data should include
    ``scheduled_at`` (ISO datetime).  Generates a new lockbox code when
    rescheduling an approved showing.  Returns the updated showing.
    """
    s = showings.get(showing_id)
    if not s:
        return jsonify({"error": "showing not found"}), 404
    data = request.json or {}
    sched_str = data.get("scheduled_at")
    if not sched_str:
        return jsonify({"error": "scheduled_at is required"}), 400
    try:
        start = datetime.fromisoformat(sched_str)
    except Exception:
        return jsonify({"error": "invalid date format"}), 400
    end = start + timedelta(hours=1)
    prop_id = s["property_id"]
    if is_time_blocked(prop_id, start, end):
        return jsonify({"error": "requested time is blocked"}), 409
    if has_conflict(prop_id, start, end):
        return jsonify({"error": "requested time conflicts with another showing"}), 409
    s["scheduled_at"] = start
    # Re‑generate lockbox code if already approved
    regenerated = False
    if s["status"] == "approved":
        s["lockbox_code"] = generate_lockbox_code()
        s["code_expires_at"] = start + timedelta(hours=1, minutes=15)
        regenerated = True
    # Notify the client about the new schedule via SMS/email
    client_phone = s.get("client_phone")
    client_email = s.get("client_email")
    try:
        prop = properties.get(prop_id)
        prop_name = prop.get("name") if prop else prop_id
        when_str = start.strftime("%Y-%m-%d %H:%M")
        if regenerated:
            code_str = s.get("lockbox_code")
            expires_str = s.get("code_expires_at").strftime("%Y-%m-%d %H:%M") if s.get("code_expires_at") else ""
            sms_msg = f"Your showing for {prop_name} has been rescheduled to {when_str}. New lockbox code: {code_str} (expires {expires_str})."
            email_subj = "Showing rescheduled"
            email_body = f"Hello {s['client_name']},\n\nYour showing for {prop_name} has been rescheduled to {when_str}.\nYour new lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you."
        else:
            sms_msg = f"Your showing request for {prop_name} has been rescheduled to {when_str} and is pending approval."
            email_subj = "Showing rescheduled"
            email_body = f"Hello {s['client_name']},\n\nYour showing request for {prop_name} has been rescheduled to {when_str} and is pending approval.\n\nThank you."
        if client_phone:
            send_sms(client_phone, sms_msg)
        if client_email:
            send_email(client_email, email_subj, email_body)
    except Exception:
        pass
    # Log the reschedule event
    try:
        log_event(prop_id, "showing_rescheduled", {
            "showing_id": showing_id,
            "client_name": s["client_name"],
            "new_scheduled_at": start.isoformat(),
        })
    except Exception:
        pass
    # Notify seller/agent about the reschedule
    try:
        prop = properties.get(prop_id, {})
        prop_name = prop.get("name", prop_id)
        # Determine message based on whether new code was generated
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        msg_notify = (
            f"Showing for {prop_name} has been rescheduled to {when_str}.\n"
            f"Buyer: {s['client_name']}. Showing ID: {showing_id}"
        )
        subj_notify = f"Showing rescheduled for {prop_name}"
        if seller_phone:
            send_sms(seller_phone, msg_notify)
        if seller_email:
            send_email(seller_email, subj_notify, msg_notify)
        if agent_phone:
            send_sms(agent_phone, msg_notify)
        if agent_email:
            send_email(agent_email, subj_notify, msg_notify)
    except Exception:
        pass
    return jsonify(s)


@app.route("/showings/<showing_id>/feedback", methods=["POST"])
def submit_feedback(showing_id: str) -> Any:
    """
    Submit feedback for a showing.  POST data should include ``rating`` (1–5)
    and ``comment``.  Returns the stored feedback entry.
    """
    s = showings.get(showing_id)
    if not s:
        return jsonify({"error": "showing not found"}), 404
    data = request.json or {}
    try:
        rating = int(data.get("rating"))
    except Exception:
        return jsonify({"error": "rating must be an integer"}), 400
    comment = data.get("comment")
    if rating < 1 or rating > 5 or not comment:
        return jsonify({"error": "rating must be 1–5 and comment required"}), 400
    entry = {
        "id": str(uuid.uuid4()),
        "rating": rating,
        "comment": comment,
        "created_at": datetime.utcnow(),
    }
    feedback_store.setdefault(showing_id, []).append(entry)
    # Log feedback submission
    try:
        property_id = s["property_id"]  # type: ignore[name-defined]
        log_event(property_id, "feedback_submitted", {
            "showing_id": showing_id,
            "rating": rating,
            "comment": comment,
        })
    except Exception:
        pass
    # Notify seller/agent of the feedback
    try:
        prop = properties.get(s["property_id"], {})  # type: ignore[name-defined]
        prop_name = prop.get("name", s["property_id"])  # type: ignore[name-defined]
        msg_notify = (
            f"New feedback received for showing ID {showing_id} on {prop_name}.\n"
            f"Rating: {rating}, Comment: {comment}"
        )
        subj_notify = f"Showing feedback for {prop_name}"
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if seller_phone:
            send_sms(seller_phone, msg_notify)
        if seller_email:
            send_email(seller_email, subj_notify, msg_notify)
        if agent_phone:
            send_sms(agent_phone, msg_notify)
        if agent_email:
            send_email(agent_email, subj_notify, msg_notify)
    except Exception:
        pass
    return jsonify(entry), 201


@app.route("/showings/<showing_id>/code", methods=["GET"])
def get_lockbox_code(showing_id: str) -> Any:
    """
    Retrieve the lockbox code for an approved showing if it is still
    valid.  Returns the code and its expiration time.
    """
    s = showings.get(showing_id)
    if not s:
        return jsonify({"error": "showing not found"}), 404
    if s["status"] != "approved" or not s["lockbox_code"]:
        return jsonify({"error": "showing is not approved"}), 400
    if s["code_expires_at"] and datetime.utcnow() > s["code_expires_at"]:
        return jsonify({"error": "code expired"}), 410
    return jsonify({
        "lockbox_code": s["lockbox_code"],
        "expires_at": s["code_expires_at"].isoformat(),
    })


@app.route("/tours", methods=["GET", "POST"])
def tour_list() -> Any:
    """
    List all tours or create a new tour.  POST data should include a
    ``showing_ids`` list referencing approved showings.  The system sorts
    the showings by scheduled start time to create the itinerary.
    Returns JSON.
    """
    if request.method == "POST":
        data = request.json or {}
        ids = data.get("showing_ids", [])
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "showing_ids must be a non‑empty list"}), 400
        selected: List[Dict[str, Any]] = []
        for sid in ids:
            s = showings.get(sid)
            if not s:
                return jsonify({"error": f"showing {sid} not found"}), 404
            if s["status"] != "approved":
                return jsonify({"error": f"showing {sid} is not approved"}), 400
            selected.append(s)
        selected.sort(key=lambda x: x["scheduled_at"])
        tour_id = str(uuid.uuid4())
        tours[tour_id] = {
            "id": tour_id,
            "showings": [s["id"] for s in selected],
            "itinerary": [
                {
                    "showing_id": s["id"],
                    "property_id": s["property_id"],
                    "scheduled_at": s["scheduled_at"].isoformat(),
                    "address": properties[s["property_id"]]["address"],
                }
                for s in selected
            ],
            "created_at": datetime.utcnow(),
        }
        return jsonify(tours[tour_id]), 201
    # GET
    return jsonify(list(tours.values()))


@app.route("/properties/<property_id>/dashboard", methods=["GET"])
def property_dashboard(property_id: str) -> Any:
    """
    Seller dashboard for a property.  Returns the property details,
    upcoming showings, blocked times and feedback.  All times are in
    ISO format.
    """
    prop = properties.get(property_id)
    if not prop:
        return jsonify({"error": "property not found"}), 404
    upcoming = [
        s for s in showings.values() if s["property_id"] == property_id
    ]
    dashboard = {
        "property": prop,
        "showings": [
            {
                **{
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in s.items()
                    if k != "property_id"
                },
                "property_id": s["property_id"],
                "feedback": feedback_store.get(s["id"], []),
            }
            for s in upcoming
        ],
        "blocked_times": [
            {"start": s.isoformat(), "end": e.isoformat()}
            for s, e in blocked_times.get(property_id, [])
        ],
    }
    return jsonify(dashboard)


# -----------------------------------------------------------------------------
# Disclosure upload, download and activity reporting endpoints
#
# These routes allow sellers or agents to upload disclosure packages for a
# property, retrieve a list of uploaded files, download individual
# disclosures, view an activity log and generate summary reports.  Files
# are stored in memory for demonstration purposes and will be lost when
# the application restarts.

@app.route("/properties/<property_id>/disclosures", methods=["GET", "POST"])
def property_disclosures(property_id: str) -> Any:
    """
    Upload a disclosure file for a property or list existing disclosures.

    * POST: Accepts a multipart/form upload with a single ``file`` field.  The
      file is stored in memory and associated with the property.  Returns
      JSON containing the filename.  Logs an ``upload_disclosure`` event.

    * GET: Returns a JSON list of filenames representing disclosures
      uploaded for the property.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    if request.method == "POST":
        if "file" not in request.files:
            return jsonify({"error": "file is required"}), 400
        file = request.files["file"]
        filename = secure_filename(file.filename or "")
        if not filename:
            return jsonify({"error": "invalid filename"}), 400
        data = file.read()
        disclosures.setdefault(property_id, {})[filename] = data
        # Log the upload event
        try:
            log_event(property_id, "upload_disclosure", {"filename": filename})
        except Exception:
            pass
        return jsonify({"filename": filename}), 201
    # GET
    files = list(disclosures.get(property_id, {}).keys())
    return jsonify(files)


@app.route("/properties/<property_id>/disclosures/<path:filename>", methods=["GET"])
def download_disclosure(property_id: str, filename: str) -> Any:
    """
    Download a specific disclosure file for a property.  Logs a
    ``download_disclosure`` event.  Returns 404 if the file does not exist.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    # Ensure the filename is safe
    safe_name = secure_filename(filename)
    data = disclosures.get(property_id, {}).get(safe_name)
    if data is None:
        return jsonify({"error": "file not found"}), 404
    # Log download event
    try:
        log_event(property_id, "download_disclosure", {"filename": safe_name})
    except Exception:
        pass
    return send_file(
        io.BytesIO(data),
        download_name=safe_name,
        as_attachment=True,
    )


@app.route("/properties/<property_id>/activity", methods=["GET"])
def get_activity_log(property_id: str) -> Any:
    """
    Retrieve the activity log for a property.  Returns a list of events in
    reverse chronological order.  Each event contains a timestamp, type
    and details.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    return jsonify(activity_logs.get(property_id, []))


@app.route("/properties/<property_id>/report", methods=["GET"])
def property_report(property_id: str) -> Any:
    """
    Generate a simple summary report for a property.  The report counts
    occurrences of each event type recorded in the activity log and
    returns the totals along with basic information about the property and
    number of uploaded disclosures.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    events = activity_logs.get(property_id, [])
    counts: Dict[str, int] = {}
    for ev in events:
        counts[ev["type"]] = counts.get(ev["type"], 0) + 1
    report = {
        "property": properties[property_id],
        "event_counts": counts,
        "disclosure_count": len(disclosures.get(property_id, {})),
        "package_count": sum(1 for pkg in packages.values() if pkg["property_id"] == property_id),
        "share_count": sum(1 for sh in package_shares.values() if sh["property_id"] == property_id),
        "offers_count": len(offers.get(property_id, [])),
        "total_showings": sum(1 for s in showings.values() if s["property_id"] == property_id),
        "showings_by_status": {
            status: sum(1 for s in showings.values() if s["property_id"] == property_id and s["status"] == status)
            for status in {"pending", "approved", "declined"}
        },
        "feedback_count": sum(len(feedback_store.get(sid, [])) for sid, s in showings.items() if s["property_id"] == property_id),
        "disclosure_feedback_count": sum(
            len(disclosure_feedback_store.get(share_id, []))
            for share_id, share in package_shares.items()
            if share.get("property_id") == property_id
        ),
    }
    return jsonify(report)


# -----------------------------------------------------------------------------
# Package and Share Endpoints
#
# Packages group multiple disclosure files into a single listing information
# package.  Users can create packages, list them for a property and retrieve
# details of a specific package.  Packages may be shared with buyers via
# unique share links, which track download activity and contribute to buyer
# interest reports.

@app.route("/properties/<property_id>/packages", methods=["GET", "POST"])
def manage_packages(property_id: str) -> Any:
    """
    Create or list listing packages for a property.

    POST data should include ``name`` (string), ``files`` (list of filenames
    already uploaded to the property via the disclosures endpoint) and
    optional ``is_public`` (boolean, defaults to False).  Returns the new
    package definition.  Logs a ``package_created`` event.

    GET returns a list of packages for the specified property.  Each entry
    includes the package ID, name, file list and public/private flag.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    if request.method == "POST":
        data = request.json or {}
        name = data.get("name")
        files = data.get("files") or []
        is_public = bool(data.get("is_public", False))
        if not name or not isinstance(files, list) or not files:
            return jsonify({"error": "name and non‑empty files list required"}), 400
        # Validate file names exist for the property
        prop_files = disclosures.get(property_id, {})
        for fn in files:
            safe_fn = secure_filename(fn)
            if safe_fn not in prop_files:
                return jsonify({"error": f"file {fn} not found for property"}), 400
        pkg_id = str(uuid.uuid4())
        packages[pkg_id] = {
            "id": pkg_id,
            "property_id": property_id,
            "name": name,
            "files": [secure_filename(fn) for fn in files],
            "is_public": is_public,
            "created_at": datetime.utcnow().isoformat(),
        }
        # Log package creation
        try:
            log_event(property_id, "package_created", {"package_id": pkg_id, "name": name, "files": files, "is_public": is_public})
        except Exception:
            pass
        return jsonify(packages[pkg_id]), 201
    # GET: list packages
    return jsonify([
        {k: v for k, v in pkg.items() if k != "property_id"}
        for pkg in packages.values() if pkg["property_id"] == property_id
    ])


@app.route("/packages/<package_id>", methods=["GET"])
def package_detail(package_id: str) -> Any:
    """
    Retrieve the details of a specific package.  Returns the package fields or
    404 if not found.
    """
    pkg = packages.get(package_id)
    if not pkg:
        return jsonify({"error": "package not found"}), 404
    return jsonify(pkg)


@app.route("/packages/<package_id>/share", methods=["POST"])
def create_share(package_id: str) -> Any:
    """
    Create a share link for a buyer to access a package.  POST data should
    include ``buyer_name``.  Returns a share ID which can be used to
    download the package files.  Logs a ``share_created`` event.
    """
    pkg = packages.get(package_id)
    if not pkg:
        return jsonify({"error": "package not found"}), 404
    data = request.json or {}
    buyer_name = data.get("buyer_name")
    if not buyer_name:
        return jsonify({"error": "buyer_name is required"}), 400
    # Capture optional buyer contact information for notifications
    buyer_phone = data.get("buyer_phone")
    buyer_email = data.get("buyer_email")
    share_id = str(uuid.uuid4())
    prop_id = pkg["property_id"]
    prop = properties.get(prop_id, {})
    # Determine whether this share is automatically approved based on property setting
    auto = not prop.get("requires_disclosure_approval")
    package_shares[share_id] = {
        "id": share_id,
        "package_id": package_id,
        "property_id": prop_id,
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "buyer_email": buyer_email,
        "downloads": [],  # list of dicts {filename, timestamp}
        "approved": auto,
    }
    # Log share creation
    try:
        log_event(prop_id, "share_created", {"share_id": share_id, "package_id": package_id, "buyer_name": buyer_name, "auto": auto})
    except Exception:
        pass
    # Notify seller/agent of the share request.
    try:
        prop_name = prop.get("name", prop_id)
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if auto:
            # Auto‑approved share
            msg = (
                f"Disclosure package '{pkg['name']}' for {prop_name} was automatically shared with buyer {buyer_name}."
                f" (Share ID: {share_id})"
            )
            subj = f"Disclosure package shared for {prop_name}"
        else:
            # Approval required
            msg = (
                f"Buyer {buyer_name} has requested access to disclosure package '{pkg['name']}' for {prop_name}.\n"
                f"Approve the share via POST /share/{share_id}/approve."
            )
            subj = f"Disclosure access request for {prop_name}"
        if seller_phone:
            send_sms(seller_phone, msg)
        if seller_email:
            send_email(seller_email, subj, msg)
        if agent_phone:
            send_sms(agent_phone, msg)
        if agent_email:
            send_email(agent_email, subj, msg)
    except Exception:
        pass
    # Notify the buyer about the share status
    try:
        if auto:
            # If the share is auto approved, tell the buyer they can download the package
            buyer_msg = (
                f"You have been granted access to disclosure package '{pkg['name']}' for {prop_name}.\n"
                f"Use your share ID {share_id} to download the files."
            )
            buyer_subj = f"Disclosure package available for {prop_name}"
        else:
            # Otherwise inform them that approval is pending
            buyer_msg = (
                f"Your request to access disclosure package '{pkg['name']}' for {prop_name} has been received and is pending approval.\n"
                f"You will be notified when access is granted."
            )
            buyer_subj = f"Disclosure access request received for {prop_name}"
        if buyer_phone:
            send_sms(buyer_phone, buyer_msg)
        if buyer_email:
            send_email(buyer_email, buyer_subj, buyer_msg)
    except Exception:
        pass
    return jsonify({"share_id": share_id, "approved": auto}), 201


@app.route("/share/<share_id>/files", methods=["GET"])
def share_file_list(share_id: str) -> Any:
    """
    List the files available to a buyer via a share link.  Returns the
    filenames from the underlying package, or 404 if the share or package
    does not exist.
    """
    share = package_shares.get(share_id)
    if not share:
        return jsonify({"error": "share not found"}), 404
    pkg = packages.get(share["package_id"])
    if not pkg:
        return jsonify({"error": "package not found"}), 404
    return jsonify(pkg["files"])


@app.route("/share/<share_id>/files/<path:filename>", methods=["GET"])
def share_download(share_id: str, filename: str) -> Any:
    """
    Allow a buyer to download a specific file from a package via a share link.
    Logs the download in the share record and creates a ``share_download``
    activity event for the property.  Returns 404 if the share, package or
    file does not exist.
    """
    share = package_shares.get(share_id)
    if not share:
        return jsonify({"error": "share not found"}), 404
    pkg = packages.get(share["package_id"])
    if not pkg:
        return jsonify({"error": "package not found"}), 404
    safe_fn = secure_filename(filename)
    if safe_fn not in pkg["files"]:
        return jsonify({"error": "file not found in package"}), 404
    # Check approval status; if not approved, return 403
    if not share.get("approved", False):
        return jsonify({"error": "download not approved"}), 403
    prop_id = pkg["property_id"]
    data = disclosures.get(prop_id, {}).get(safe_fn)
    if data is None:
        return jsonify({"error": "file not found"}), 404
    # Record download in share
    timestamp = datetime.utcnow().isoformat()
    share["downloads"].append({"filename": safe_fn, "timestamp": timestamp})
    # Log activity event
    try:
        log_event(prop_id, "share_download", {"share_id": share_id, "buyer_name": share["buyer_name"], "filename": safe_fn})
    except Exception:
        pass
    return send_file(
        io.BytesIO(data),
        download_name=safe_fn,
        as_attachment=True,
    )

# -----------------------------------------------------------------------------
# Disclosure Request Endpoint
#
# Buyers or their agents can use this endpoint to request access to a specific
# disclosure package.  Unlike the generic share creation endpoint (which is
# typically used by listing agents), this route is meant for buyers.  It
# captures the buyer's contact information and notifies the seller and agent
# that a request has been made.  The system automatically determines whether
# the share is approved based on the property's ``requires_disclosure_approval``
# setting.  The buyer will receive a notification indicating whether their
# request is pending or immediately available.

@app.route("/properties/<property_id>/disclosures/request", methods=["POST"])
def request_disclosure(property_id: str) -> Any:
    """
    Request access to a disclosure package for a property.  POST data must
    include ``package_id`` (identifying which package to access) and
    ``buyer_name``.  Optional ``buyer_phone`` and ``buyer_email`` provide
    contact details for notifications.  Returns the share ID and approval
    status.  Logs a ``disclosure_requested`` event.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    data = request.json or {}
    pkg_id = data.get("package_id")
    buyer_name = data.get("buyer_name")
    if not pkg_id or not buyer_name:
        return jsonify({"error": "package_id and buyer_name are required"}), 400
    pkg = packages.get(pkg_id)
    if not pkg or pkg.get("property_id") != property_id:
        return jsonify({"error": "package not found for property"}), 404
    buyer_phone = data.get("buyer_phone")
    buyer_email = data.get("buyer_email")
    # Determine auto approval based on property settings
    prop = properties.get(property_id, {})
    auto = not prop.get("requires_disclosure_approval")
    share_id = str(uuid.uuid4())
    package_shares[share_id] = {
        "id": share_id,
        "package_id": pkg_id,
        "property_id": property_id,
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "buyer_email": buyer_email,
        "downloads": [],
        "approved": auto,
    }
    # Log disclosure request
    try:
        log_event(property_id, "disclosure_requested", {
            "share_id": share_id,
            "package_id": pkg_id,
            "buyer_name": buyer_name,
            "auto": auto,
        })
    except Exception:
        pass
    # Notify seller/agent
    try:
        prop_name = prop.get("name", property_id)
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if auto:
            msg = (
                f"Disclosure package '{pkg['name']}' for {prop_name} was automatically shared with buyer {buyer_name}."
                f" (Share ID: {share_id})"
            )
            subj = f"Disclosure package shared for {prop_name}"
        else:
            msg = (
                f"Buyer {buyer_name} has requested access to disclosure package '{pkg['name']}' for {prop_name}.\n"
                f"Approve the share via POST /share/{share_id}/approve."
            )
            subj = f"Disclosure access request for {prop_name}"
        if seller_phone:
            send_sms(seller_phone, msg)
        if seller_email:
            send_email(seller_email, subj, msg)
        if agent_phone:
            send_sms(agent_phone, msg)
        if agent_email:
            send_email(agent_email, subj, msg)
    except Exception:
        pass
    # Notify buyer about the status
    try:
        prop_name = prop.get("name", property_id)
        if auto:
            buyer_msg = (
                f"You have been granted access to disclosure package '{pkg['name']}' for {prop_name}.\n"
                f"Use your share ID {share_id} to download the files."
            )
            buyer_subj = f"Disclosure package available for {prop_name}"
        else:
            buyer_msg = (
                f"Your request to access disclosure package '{pkg['name']}' for {prop_name} has been received and is pending approval.\n"
                f"You will be notified when access is granted."
            )
            buyer_subj = f"Disclosure access request received for {prop_name}"
        if buyer_phone:
            send_sms(buyer_phone, buyer_msg)
        if buyer_email:
            send_email(buyer_email, buyer_subj, buyer_msg)
    except Exception:
        pass
    return jsonify({"share_id": share_id, "approved": auto}), 201


@app.route("/properties/<property_id>/interest", methods=["GET"])
def buyer_interest(property_id: str) -> Any:
    """
    Generate a simple buyer interest report summarizing disclosure download
    activity by share.  Returns a list of buyers with the count of files
    downloaded.  This approximates the buyer interest report provided by
    listing‑management tools.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    report = []
    for share in package_shares.values():
        if share["property_id"] == property_id:
            report.append({
                "buyer_name": share["buyer_name"],
                "downloads": len(share.get("downloads", [])),
            })
    return jsonify(report)

# -----------------------------------------------------------------------------
# Disclosure Feedback Endpoint
#
# Buyers can provide feedback on the contents of a disclosure package they
# downloaded via a share.  Feedback includes a rating (1–5) and a comment.
# Feedback entries are stored in ``disclosure_feedback_store`` keyed by
# share ID.  This endpoint logs a ``share_feedback_submitted`` event and
# notifies the seller and agent about the new feedback.

@app.route("/share/<share_id>/feedback", methods=["POST"])
def share_feedback(share_id: str) -> Any:
    """
    Submit feedback for a disclosure share.  POST data must include
    ``rating`` (integer 1–5) and ``comment`` (non‑empty string).  Returns
    the stored feedback entry.  Logs a ``share_feedback_submitted`` event.
    """
    share = package_shares.get(share_id)
    if not share:
        return jsonify({"error": "share not found"}), 404
    data = request.json or {}
    try:
        rating = int(data.get("rating"))
    except Exception:
        return jsonify({"error": "rating must be an integer"}), 400
    comment = data.get("comment")
    if rating < 1 or rating > 5 or not comment:
        return jsonify({"error": "rating must be 1–5 and comment required"}), 400
    entry = {
        "id": str(uuid.uuid4()),
        "rating": rating,
        "comment": comment,
        "buyer_name": share.get("buyer_name"),
        "created_at": datetime.utcnow().isoformat(),
    }
    disclosure_feedback_store.setdefault(share_id, []).append(entry)
    # Log feedback event
    try:
        prop_id = share.get("property_id")
        log_event(prop_id, "share_feedback_submitted", {
            "share_id": share_id,
            "buyer_name": share.get("buyer_name"),
            "rating": rating,
            "comment": comment,
        })
    except Exception:
        pass
    # Notify seller/agent about feedback
    try:
        prop_id = share.get("property_id")
        prop = properties.get(prop_id, {})
        prop_name = prop.get("name", prop_id)
        msg_notify = (
            f"New disclosure feedback received for {prop_name} (share ID {share_id}).\n"
            f"Rating: {rating}, Comment: {comment}"
        )
        subj_notify = f"Disclosure feedback for {prop_name}"
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if seller_phone:
            send_sms(seller_phone, msg_notify)
        if seller_email:
            send_email(seller_email, subj_notify, msg_notify)
        if agent_phone:
            send_sms(agent_phone, msg_notify)
        if agent_email:
            send_email(agent_email, subj_notify, msg_notify)
    except Exception:
        pass
    return jsonify(entry), 201


# -----------------------------------------------------------------------------
# Share Approval Endpoint
#
# When a property requires disclosure approval, shares are created in a
# not‑approved state.  Sellers or agents can call this endpoint to
# approve a share, enabling the buyer to download disclosure files.
@app.route("/share/<share_id>/approve", methods=["POST"])
def approve_share(share_id: str) -> Any:
    """
    Approve a disclosure share so the buyer can download the files.
    Returns the updated share record or 404 if the share does not exist.
    """
    share = package_shares.get(share_id)
    if not share:
        return jsonify({"error": "share not found"}), 404
    if share.get("approved"):
        return jsonify(share), 200
    share["approved"] = True
    # Log approval event
    prop_id = share.get("property_id")
    try:
        log_event(prop_id, "share_approved", {"share_id": share_id, "buyer_name": share.get("buyer_name")})
    except Exception:
        pass
    # Notify seller/agent and buyer that the share has been approved
    try:
        prop = properties.get(prop_id, {})
        prop_name = prop.get("name", prop_id)
        buyer_name = share.get("buyer_name")
        msg_notify = (
            f"Disclosure package share (ID: {share_id}) for {prop_name} has been approved.\n"
            f"Buyer: {buyer_name}."
        )
        subj_notify = f"Disclosure share approved for {prop_name}"
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if seller_phone:
            send_sms(seller_phone, msg_notify)
        if seller_email:
            send_email(seller_email, subj_notify, msg_notify)
        if agent_phone:
            send_sms(agent_phone, msg_notify)
        if agent_email:
            send_email(agent_email, subj_notify, msg_notify)
        # Notify the buyer that access has been granted
        buyer_phone = share.get("buyer_phone")
        buyer_email = share.get("buyer_email")
        buyer_msg = (
            f"Your request to access disclosure package for {prop_name} has been approved.\n"
            f"Use your share ID {share_id} to download the files."
        )
        buyer_subj = f"Disclosure package approved for {prop_name}"
        if buyer_phone:
            send_sms(buyer_phone, buyer_msg)
        if buyer_email:
            send_email(buyer_email, buyer_subj, buyer_msg)
    except Exception:
        pass
    return jsonify(share)


# -----------------------------------------------------------------------------
# Offer Endpoints
#
# Offers allow buyers to submit purchase proposals for a property.  Each
# offer includes a price and optional terms.  Sellers can view all offers and
# retrieve a report summarizing them.

@app.route("/properties/<property_id>/offers", methods=["GET", "POST"])
def property_offers(property_id: str) -> Any:
    """
    Create or list offers for a property.

    POST data must include ``buyer_name`` and ``price`` (numeric).  Optional
    ``terms`` may describe contingencies or notes.  Returns the created offer
    entry and logs an ``offer_submitted`` event.

    GET returns a list of all offers for the specified property.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    if request.method == "POST":
        data = request.json or {}
        buyer_name = data.get("buyer_name")
        price = data.get("price")
        terms = data.get("terms")
        if not buyer_name or price is None:
            return jsonify({"error": "buyer_name and price are required"}), 400
        try:
            price_val = float(price)
        except Exception:
            return jsonify({"error": "price must be numeric"}), 400
        offer_id = str(uuid.uuid4())
        offer_entry = {
            "id": offer_id,
            "buyer_name": buyer_name,
            "price": price_val,
            "terms": terms,
            "created_at": datetime.utcnow().isoformat(),
        }
        offers.setdefault(property_id, []).append(offer_entry)
        # Log offer submission
        try:
            log_event(property_id, "offer_submitted", {"offer_id": offer_id, "buyer_name": buyer_name, "price": price_val})
        except Exception:
            pass
        return jsonify(offer_entry), 201
    # GET
    return jsonify(offers.get(property_id, []))


@app.route("/properties/<property_id>/offers/report", methods=["GET"])
def offers_report(property_id: str) -> Any:
    """
    Produce a summary of offers for a property.  The report sorts offers
    descending by price and includes the top offer, average price and the
    number of offers.  Returns 404 if no offers exist.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    prop_offers = offers.get(property_id)
    if not prop_offers:
        return jsonify({"error": "no offers for property"}), 404
    sorted_offers = sorted(prop_offers, key=lambda x: x["price"], reverse=True)
    total = sum(o["price"] for o in sorted_offers)
    avg = total / len(sorted_offers)
    report = {
        "offers": sorted_offers,
        "top_offer": sorted_offers[0],
        "average_price": avg,
        "count": len(sorted_offers),
    }
    return jsonify(report)


@app.route("/properties/<property_id>/prospects", methods=["GET"])
def property_prospects(property_id: str) -> Any:
    """
    Generate statistics about potential buyers and their agents for a listing.

    This endpoint aggregates activity from showings, disclosure share downloads
    and offers to provide insight into how engaged each buyer (or buyer
    agent) is.  The response returns a dictionary keyed by buyer name with
    counts of scheduled showings, approved showings, declined showings,
    downloads from listing packages and submitted offers.
    """
    if property_id not in properties:
        return jsonify({"error": "property not found"}), 404
    stats: Dict[str, Dict[str, int]] = {}
    # Aggregate showings
    for s in showings.values():
        if s["property_id"] != property_id:
            continue
        buyer = s.get("client_name") or "Unknown"
        rec = stats.setdefault(buyer, {
            "showings_requested": 0,
            "showings_approved": 0,
            "showings_declined": 0,
            "downloads": 0,
            "offers": 0,
        })
        rec["showings_requested"] += 1
        status = s.get("status")
        if status == "approved":
            rec["showings_approved"] += 1
        elif status == "declined":
            rec["showings_declined"] += 1
    # Aggregate downloads from shares
    for share in package_shares.values():
        if share["property_id"] != property_id:
            continue
        buyer = share.get("buyer_name") or "Unknown"
        rec = stats.setdefault(buyer, {
            "showings_requested": 0,
            "showings_approved": 0,
            "showings_declined": 0,
            "downloads": 0,
            "offers": 0,
        })
        rec["downloads"] += len(share.get("downloads", []))
    # Aggregate offers
    for offer in offers.get(property_id, []):
        buyer = offer.get("buyer_name") or "Unknown"
        rec = stats.setdefault(buyer, {
            "showings_requested": 0,
            "showings_approved": 0,
            "showings_declined": 0,
            "downloads": 0,
            "offers": 0,
        })
        rec["offers"] += 1
    return jsonify(stats)


# -----------------------------------------------------------------------------
# Administration UI
#
# A simple form for configuring Twilio credentials (account SID, auth token and
# default sender number).  This endpoint renders a plain HTML page using
# ``render_template_string`` and accepts POST submissions.  It does not
# include authentication; in a real application you would restrict access.
@app.route("/admin/twilio", methods=["GET", "POST"])
def twilio_admin() -> Any:
    message = ""
    if request.method == "POST":
        twilio_config["account_sid"] = request.form.get("account_sid") or None
        twilio_config["auth_token"] = request.form.get("auth_token") or None
        twilio_config["from_number"] = request.form.get("from_number") or None
        message = "Configuration updated successfully."
    html = """
    <h1>Twilio Configuration</h1>
    {% if message %}<p>{{ message }}</p>{% endif %}
    <form method="post">
        <label>Account SID: <input type="text" name="account_sid" value="{{ config.account_sid or '' }}"></label><br>
        <label>Auth Token: <input type="text" name="auth_token" value="{{ config.auth_token or '' }}"></label><br>
        <label>From Number: <input type="text" name="from_number" value="{{ config.from_number or '' }}"></label><br>
        <button type="submit">Save</button>
    </form>
    """
    return render_template_string(html, config=twilio_config, message=message)


# Email configuration page
#
# Similar to the Twilio admin page, this endpoint exposes a simple HTML form
# that allows administrators to configure the SMTP server, port, optional
# username/password and the default "from" address used for sending emails.
# The ``use_tls`` option determines whether the connection should be wrapped in
# STARTTLS.
@app.route("/admin/email", methods=["GET", "POST"])
def email_admin() -> Any:
    msg = ""
    if request.method == "POST":
        email_config["smtp_server"] = request.form.get("smtp_server") or None
        email_config["smtp_port"] = request.form.get("smtp_port") or None
        email_config["smtp_username"] = request.form.get("smtp_username") or None
        email_config["smtp_password"] = request.form.get("smtp_password") or None
        email_config["from_email"] = request.form.get("from_email") or None
        # store TLS value as string ("true" or "false")
        use_tls_val = request.form.get("use_tls") or "true"
        email_config["use_tls"] = use_tls_val.lower()
        msg = "Email configuration updated successfully."
    page = """
    <h1>Email Configuration</h1>
    {% if message %}<p>{{ message }}</p>{% endif %}
    <form method="post">
        <label>SMTP Server: <input type="text" name="smtp_server" value="{{ cfg.smtp_server or '' }}"></label><br>
        <label>SMTP Port: <input type="text" name="smtp_port" value="{{ cfg.smtp_port or '' }}"></label><br>
        <label>SMTP Username: <input type="text" name="smtp_username" value="{{ cfg.smtp_username or '' }}"></label><br>
        <label>SMTP Password: <input type="password" name="smtp_password" value="{{ cfg.smtp_password or '' }}"></label><br>
        <label>From Email: <input type="text" name="from_email" value="{{ cfg.from_email or '' }}"></label><br>
        <label>Use TLS: <select name="use_tls">
            <option value="true" {% if cfg.use_tls == 'true' %}selected{% endif %}>Yes</option>
            <option value="false" {% if cfg.use_tls == 'false' %}selected{% endif %}>No</option>
        </select></label><br>
        <button type="submit">Save</button>
    </form>
    """
    return render_template_string(page, cfg=email_config, message=msg)


# -----------------------------------------------------------------------------
# Front‑end (UI) Routes
#
# The following routes provide a simple web interface for interacting with the
# showing and disclosure management system.  They render Jinja2 templates
# located in the ``templates`` directory.  These forms call the same in‑memory
# functions used by the API endpoints to ensure the front‑end and API stay
# synchronized.

@app.route("/register", methods=["GET", "POST"])
def register() -> Any:
    """Render a registration form and create a new user."""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if not username or not password:
            return render_template("register.html", error="Username and password are required")
        # check if user already exists
        existing = User.query.filter_by(username=username).first()
        if existing:
            return render_template("register.html", error="Username already exists")
        new_user = User(username=username, password=password)
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for("ui_home"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Render a login form and authenticate the user."""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        user = User.query.filter_by(username=username, password=password).first()
        if user:
            login_user(user)
            return redirect(url_for("ui_home"))
        return render_template("login.html", error="Invalid username or password")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout() -> Any:
    """Log out the current user and redirect to home page."""
    logout_user()
    return redirect(url_for("ui_home"))

@app.route("/")
def ui_home() -> Any:
    """Render a homepage listing all properties with links to view details."""
    return render_template("home.html", properties=properties)


@app.route("/properties/new", methods=["GET", "POST"])
@login_required
def ui_create_property() -> Any:
    """Render a form to create a new property and handle submission."""
    if request.method == "POST":
        # Use same logic as API to parse and create property
        name = request.form.get("name")
        address = request.form.get("address")
        if not name or not address:
            return render_template("create_property.html", error="Name and address are required")
        # optional contacts
        seller_name = request.form.get("seller_name")
        seller_phone = request.form.get("seller_phone")
        seller_email = request.form.get("seller_email")
        agent_name = request.form.get("agent_name")
        agent_phone = request.form.get("agent_phone")
        agent_email = request.form.get("agent_email")
        # parse boolean flags
        def parse_bool(val: Any) -> bool:
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in {"true", "1", "yes", "on"}
            return False
        auto_approve = parse_bool(request.form.get("auto_approve_showings"))
        req_disc_approval = parse_bool(request.form.get("requires_disclosure_approval"))
        prop_id = str(uuid.uuid4())
        properties[prop_id] = {
            "id": prop_id,
            "name": name,
            "address": address,
            "created_at": datetime.utcnow(),
            "seller_name": seller_name,
            "seller_phone": seller_phone,
            "seller_email": seller_email,
            "agent_name": agent_name,
            "agent_phone": agent_phone,
            "agent_email": agent_email,
            "auto_approve_showings": auto_approve,
            "requires_disclosure_approval": req_disc_approval,
        }
        # Persist the property in the database
        db_prop = PropertyModel(
            id=prop_id,
            name=name,
            address=address,
            seller_name=seller_name,
            seller_phone=seller_phone,
            seller_email=seller_email,
            agent_name=agent_name,
            agent_phone=agent_phone,
            agent_email=agent_email,
            auto_approve_showings=auto_approve,
            requires_disclosure_approval=req_disc_approval,
        )
        db.session.add(db_prop)
        db.session.commit()
        return redirect(url_for("ui_property_detail", property_id=prop_id))
    return render_template("create_property.html")


@app.route("/properties/<property_id>")
@login_required
def ui_property_detail(property_id: str) -> Any:
    """Display details for a single property, including showings and packages."""
    prop = properties.get(property_id)
    if not prop:
        return "Property not found", 404
    # Gather showings for this property
    property_showings = [s for s in showings.values() if s["property_id"] == property_id]
    # Sort showings by scheduled time
    property_showings.sort(key=lambda s: s["scheduled_at"])
    # Gather packages and shares for this property
    property_packages = [pkg for pkg in packages.values() if pkg["property_id"] == property_id]
    property_shares = [sh for sh in package_shares.values() if sh["property_id"] == property_id]
    # List uploaded disclosure files
    files = list(disclosures.get(property_id, {}).keys())
    return render_template(
        "property_detail.html",
        property=prop,
        showings=property_showings,
        packages=property_packages,
        shares=property_shares,
        files=files,
    )


@app.route("/properties/<property_id>/schedule_showing", methods=["POST"])
def ui_schedule_showing(property_id: str) -> Any:
    """Handle scheduling a showing from the UI form."""
    # reuse API logic to schedule showing
    prop = properties.get(property_id)
    if not prop:
        return "Property not found", 404
    # parse form fields
    client_name = request.form.get("client_name")
    scheduled_at = request.form.get("scheduled_at")
    client_phone = request.form.get("client_phone")
    client_email = request.form.get("client_email")
    if not client_name or not scheduled_at:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    # call underlying showing_list POST logic directly
    # convert to JSON-like data and reuse existing function
    # create new showing id
    try:
        start = datetime.fromisoformat(scheduled_at)
    except Exception:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    end = start + timedelta(hours=1)
    # Check conflict
    if is_time_blocked(property_id, start, end) or has_conflict(property_id, start, end):
        # Could set flash message; skip for simplicity
        return redirect(url_for("ui_property_detail", property_id=property_id))
    showing_id = str(uuid.uuid4())
    showings[showing_id] = {
        "id": showing_id,
        "property_id": property_id,
        "client_name": client_name,
        "client_phone": client_phone,
        "client_email": client_email,
        "scheduled_at": start,
        "status": "pending",
        "lockbox_code": None,
        "code_expires_at": None,
        "created_at": datetime.utcnow(),
    }
    # Persist the showing to the database
    db_showing = ShowingModel(
        id=showing_id,
        property_id=property_id,
        client_name=client_name,
        client_phone=client_phone,
        client_email=client_email,
        scheduled_at=start,
        status="pending",
        lockbox_code=None,
        code_expires_at=None,
        created_at=datetime.utcnow(),
    )
    db.session.add(db_showing)
    db.session.commit()
    # send notifications and log event (reuse code from API)
    try:
        # notify buyer
        if client_phone:
            send_sms(client_phone, f"Your showing request for {prop['name']} on {start.strftime('%Y-%m-%d %H:%M')} has been received and is pending approval.")
        if client_email:
            send_email(
                client_email,
                "Showing request received",
                f"Hello {client_name},\n\nYour showing request for {prop['name']} on {start.strftime('%Y-%m-%d %H:%M')} has been received and is pending approval.\n\nThank you."
            )
        # notify seller/agent
        msg = (
            f"New showing request for {prop['name']}: {client_name} has requested to view the property on {start.strftime('%Y-%m-%d %H:%M')}.\n"
            f"Use your dashboard or the API to approve, decline or reschedule this showing.\n"
            f"Showing ID: {showing_id}"
        )
        subj = f"New showing request for {prop['name']}"
        if prop.get("seller_phone"):
            send_sms(prop.get("seller_phone"), msg)
        if prop.get("seller_email"):
            send_email(prop.get("seller_email"), subj, msg)
        if prop.get("agent_phone"):
            send_sms(prop.get("agent_phone"), msg)
        if prop.get("agent_email"):
            send_email(prop.get("agent_email"), subj, msg)
        # log event
        log_event(property_id, "showing_requested", {
            "showing_id": showing_id,
            "client_name": client_name,
            "scheduled_at": start.isoformat(),
        })
        # auto approve if configured
        if prop.get("auto_approve_showings"):
            s = showings[showing_id]
            code = generate_lockbox_code()
            s["lockbox_code"] = code
            s["code_expires_at"] = s["scheduled_at"] + timedelta(hours=1, minutes=15)
            s["status"] = "approved"
            # notify buyer
            when2 = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
            code_str = s["lockbox_code"]
            expires_str = s["code_expires_at"].strftime("%Y-%m-%d %H:%M")
            if s.get("client_phone"):
                send_sms(s.get("client_phone"), f"Your showing for {prop['name']} at {when2} has been approved. Lockbox code: {code_str} (expires {expires_str}).")
            if s.get("client_email"):
                send_email(s.get("client_email"), "Showing approved", f"Hello {s['client_name']},\n\nYour showing for {prop['name']} at {when2} has been approved.\nYour lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you.")
            # notify property contacts of auto approval
            notif_msg = (
                f"Showing for {prop['name']} on {when2} was automatically approved.\n"
                f"Buyer: {s['client_name']}. Lockbox code: {code_str} (expires {expires_str}).\n"
                f"Showing ID: {showing_id}"
            )
            notif_subj = f"Showing auto‑approved for {prop['name']}"
            if prop.get("seller_phone"):
                send_sms(prop.get("seller_phone"), notif_msg)
            if prop.get("seller_email"):
                send_email(prop.get("seller_email"), notif_subj, notif_msg)
            if prop.get("agent_phone"):
                send_sms(prop.get("agent_phone"), notif_msg)
            if prop.get("agent_email"):
                send_email(prop.get("agent_email"), notif_subj, notif_msg)
            # log approval
            log_event(property_id, "showing_approved", {
                "showing_id": showing_id,
                "client_name": s["client_name"],
                "scheduled_at": s["scheduled_at"].isoformat(),
                "lockbox_code": s["lockbox_code"],
                "auto": True,
            })
    except Exception:
        pass
    return redirect(url_for("ui_property_detail", property_id=property_id))


@app.route("/showings/<showing_id>/approve_ui", methods=["POST"])
def ui_approve_showing(showing_id: str) -> Any:
    """Approve a showing from the UI and redirect to the property detail page."""
    s = showings.get(showing_id)
    if not s:
        return "Showing not found", 404
    prop_id = s["property_id"]
    # reuse approval logic
    if s["status"] == "pending":
        code = generate_lockbox_code()
        s["lockbox_code"] = code
        s["code_expires_at"] = s["scheduled_at"] + timedelta(hours=1, minutes=15)
        s["status"] = "approved"
        # send notifications
        try:
            prop = properties.get(prop_id)
            prop_name = prop.get("name") if prop else prop_id
            when = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
            code_str = s["lockbox_code"]
            expires_str = s["code_expires_at"].strftime("%Y-%m-%d %H:%M")
            # buyer
            if s.get("client_phone"):
                send_sms(s.get("client_phone"), f"Your showing for {prop_name} at {when} has been approved. Lockbox code: {code_str} (expires {expires_str}).")
            if s.get("client_email"):
                send_email(s.get("client_email"), "Showing approved", f"Hello {s['client_name']},\n\nYour showing for {prop_name} at {when} has been approved.\nYour lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you.")
            # seller/agent
            msg_notify = (
                f"Showing for {prop_name} on {when} has been approved.\n"
                f"Buyer: {s['client_name']}. Lockbox code: {code_str} (expires {expires_str}).\n"
                f"Showing ID: {showing_id}"
            )
            subj_notify = f"Showing approved for {prop_name}"
            if prop.get("seller_phone"):
                send_sms(prop.get("seller_phone"), msg_notify)
            if prop.get("seller_email"):
                send_email(prop.get("seller_email"), subj_notify, msg_notify)
            if prop.get("agent_phone"):
                send_sms(prop.get("agent_phone"), msg_notify)
            if prop.get("agent_email"):
                send_email(prop.get("agent_email"), subj_notify, msg_notify)
            # log event
            log_event(prop_id, "showing_approved", {
                "showing_id": showing_id,
                "client_name": s["client_name"],
                "scheduled_at": s["scheduled_at"].isoformat(),
                "lockbox_code": s["lockbox_code"],
            })
        except Exception:
            pass
    return redirect(url_for("ui_property_detail", property_id=prop_id))


@app.route("/showings/<showing_id>/decline_ui", methods=["POST"])
def ui_decline_showing(showing_id: str) -> Any:
    """Decline a showing from the UI."""
    s = showings.get(showing_id)
    if not s:
        return "Showing not found", 404
    prop_id = s["property_id"]
    if s["status"] == "pending":
        s["status"] = "declined"
        try:
            prop = properties.get(prop_id)
            prop_name = prop.get("name") if prop else prop_id
            when = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
            # notify buyer
            if s.get("client_phone"):
                send_sms(s.get("client_phone"), f"Your showing request for {prop_name} on {when} has been declined.")
            if s.get("client_email"):
                send_email(s.get("client_email"), "Showing declined", f"Hello {s['client_name']},\n\nYour showing request for {prop_name} on {when} has been declined.\n\nThank you.")
            # notify seller/agent
            msg_notify = (
                f"Showing for {prop_name} on {when} has been declined.\n"
                f"Buyer: {s['client_name']}. Showing ID: {showing_id}"
            )
            subj_notify = f"Showing declined for {prop_name}"
            if prop.get("seller_phone"):
                send_sms(prop.get("seller_phone"), msg_notify)
            if prop.get("seller_email"):
                send_email(prop.get("seller_email"), subj_notify, msg_notify)
            if prop.get("agent_phone"):
                send_sms(prop.get("agent_phone"), msg_notify)
            if prop.get("agent_email"):
                send_email(prop.get("agent_email"), subj_notify, msg_notify)
            # log decline
            log_event(prop_id, "showing_declined", {
                "showing_id": showing_id,
                "client_name": s["client_name"],
                "scheduled_at": s["scheduled_at"].isoformat(),
            })
        except Exception:
            pass
    return redirect(url_for("ui_property_detail", property_id=prop_id))


@app.route("/showings/<showing_id>/reschedule_ui", methods=["POST"])
def ui_reschedule_showing(showing_id: str) -> Any:
    """Reschedule a showing from the UI."""
    s = showings.get(showing_id)
    if not s:
        return "Showing not found", 404
    prop_id = s["property_id"]
    new_time = request.form.get("new_time")
    if not new_time:
        return redirect(url_for("ui_property_detail", property_id=prop_id))
    try:
        start = datetime.fromisoformat(new_time)
    except Exception:
        return redirect(url_for("ui_property_detail", property_id=prop_id))
    end = start + timedelta(hours=1)
    if is_time_blocked(prop_id, start, end) or has_conflict(prop_id, start, end):
        return redirect(url_for("ui_property_detail", property_id=prop_id))
    s["scheduled_at"] = start
    regenerated = False
    if s["status"] == "approved":
        s["lockbox_code"] = generate_lockbox_code()
        s["code_expires_at"] = start + timedelta(hours=1, minutes=15)
        regenerated = True
    # send notifications
    try:
        prop = properties.get(prop_id)
        prop_name = prop.get("name") if prop else prop_id
        when_str = start.strftime("%Y-%m-%d %H:%M")
        if regenerated:
            code_str = s["lockbox_code"]
            expires_str = s["code_expires_at"].strftime("%Y-%m-%d %H:%M") if s.get("code_expires_at") else ""
            sms_msg = f"Your showing for {prop_name} has been rescheduled to {when_str}. New lockbox code: {code_str} (expires {expires_str})."
            email_body = f"Hello {s['client_name']},\n\nYour showing for {prop_name} has been rescheduled to {when_str}.\nYour new lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you."
        else:
            sms_msg = f"Your showing request for {prop_name} has been rescheduled to {when_str} and is pending approval."
            email_body = f"Hello {s['client_name']},\n\nYour showing request for {prop_name} has been rescheduled to {when_str} and is pending approval.\n\nThank you."
        if s.get("client_phone"):
            send_sms(s.get("client_phone"), sms_msg)
        if s.get("client_email"):
            send_email(s.get("client_email"), "Showing rescheduled", email_body)
        # notify seller/agent
        msg_notify = (
            f"Showing for {prop_name} has been rescheduled to {when_str}.\n"
            f"Buyer: {s['client_name']}. Showing ID: {showing_id}"
        )
        subj_notify = f"Showing rescheduled for {prop_name}"
        if prop.get("seller_phone"):
            send_sms(prop.get("seller_phone"), msg_notify)
        if prop.get("seller_email"):
            send_email(prop.get("seller_email"), subj_notify, msg_notify)
        if prop.get("agent_phone"):
            send_sms(prop.get("agent_phone"), msg_notify)
        if prop.get("agent_email"):
            send_email(prop.get("agent_email"), subj_notify, msg_notify)
        # log event
        log_event(prop_id, "showing_rescheduled", {
            "showing_id": showing_id,
            "client_name": s["client_name"],
            "new_scheduled_at": start.isoformat(),
        })
    except Exception:
        pass
    return redirect(url_for("ui_property_detail", property_id=prop_id))


# UI helpers for disclosures and packages
@app.route("/properties/<property_id>/create_package_ui", methods=["POST"])
def ui_create_package(property_id: str) -> Any:
    """Create a listing package from a form submission."""
    prop = properties.get(property_id)
    if not prop:
        return "Property not found", 404
    name = request.form.get("name")
    files_field = request.form.get("files") or ""
    files_list = [f.strip() for f in files_field.split(",") if f.strip()]
    is_public = bool(request.form.get("is_public"))
    if not name or not files_list:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    # validate that files exist
    prop_files = disclosures.get(property_id, {})
    for fn in files_list:
        safe_fn = secure_filename(fn)
        if safe_fn not in prop_files:
            return redirect(url_for("ui_property_detail", property_id=property_id))
    pkg_id = str(uuid.uuid4())
    packages[pkg_id] = {
        "id": pkg_id,
        "property_id": property_id,
        "name": name,
        "files": [secure_filename(fn) for fn in files_list],
        "is_public": is_public,
        "created_at": datetime.utcnow().isoformat(),
    }
    # log event
    try:
        log_event(property_id, "package_created", {
            "package_id": pkg_id,
            "name": name,
            "files": files_list,
            "is_public": is_public,
        })
    except Exception:
        pass
    return redirect(url_for("ui_property_detail", property_id=property_id))


@app.route("/properties/<property_id>/request_disclosure_ui", methods=["POST"])
def ui_request_disclosure(property_id: str) -> Any:
    """Handle disclosure request from UI."""
    prop = properties.get(property_id)
    if not prop:
        return "Property not found", 404
    package_id = request.form.get("package_id")
    buyer_name = request.form.get("buyer_name")
    buyer_phone = request.form.get("buyer_phone")
    buyer_email = request.form.get("buyer_email")
    if not package_id or not buyer_name:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    pkg = packages.get(package_id)
    if not pkg or pkg.get("property_id") != property_id:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    # Determine auto approval
    auto = not prop.get("requires_disclosure_approval")
    share_id = str(uuid.uuid4())
    package_shares[share_id] = {
        "id": share_id,
        "package_id": package_id,
        "property_id": property_id,
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "buyer_email": buyer_email,
        "downloads": [],
        "approved": auto,
    }
    # log event
    try:
        log_event(property_id, "disclosure_requested", {
            "share_id": share_id,
            "package_id": package_id,
            "buyer_name": buyer_name,
            "auto": auto,
        })
    except Exception:
        pass
    # notify seller/agent
    try:
        prop_name = prop.get("name", property_id)
        seller_phone = prop.get("seller_phone")
        seller_email = prop.get("seller_email")
        agent_phone = prop.get("agent_phone")
        agent_email = prop.get("agent_email")
        if auto:
            msg = (
                f"Disclosure package '{pkg['name']}' for {prop_name} was automatically shared with buyer {buyer_name}. (Share ID: {share_id})"
            )
            subj = f"Disclosure package shared for {prop_name}"
        else:
            msg = (
                f"Buyer {buyer_name} has requested access to disclosure package '{pkg['name']}' for {prop_name}.\n"
                f"Approve the share via POST /share/{share_id}/approve."
            )
            subj = f"Disclosure access request for {prop_name}"
        if seller_phone:
            send_sms(seller_phone, msg)
        if seller_email:
            send_email(seller_email, subj, msg)
        if agent_phone:
            send_sms(agent_phone, msg)
        if agent_email:
            send_email(agent_email, subj, msg)
    except Exception:
        pass
    # notify buyer
    try:
        prop_name = prop.get("name", property_id)
        if auto:
            buyer_msg = (
                f"You have been granted access to disclosure package '{pkg['name']}' for {prop_name}.\nUse your share ID {share_id} to download the files."
            )
            buyer_subj = f"Disclosure package available for {prop_name}"
        else:
            buyer_msg = (
                f"Your request to access disclosure package '{pkg['name']}' for {prop_name} has been received and is pending approval.\nYou will be notified when access is granted."
            )
            buyer_subj = f"Disclosure access request received for {prop_name}"
        if buyer_phone:
            send_sms(buyer_phone, buyer_msg)
        if buyer_email:
            send_email(buyer_email, buyer_subj, buyer_msg)
    except Exception:
        pass
    return redirect(url_for("ui_property_detail", property_id=property_id))


@app.route("/properties/<property_id>/upload_disclosure_ui", methods=["POST"])
def ui_upload_disclosure(property_id: str) -> Any:
    """Handle disclosure file upload from UI."""
    if property_id not in properties:
        return "Property not found", 404
    file = request.files.get("file")
    if not file:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    filename = secure_filename(file.filename or "")
    if not filename:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    data_bytes = file.read()
    disclosures.setdefault(property_id, {})[filename] = data_bytes
    # log upload event
    try:
        log_event(property_id, "upload_disclosure", {"filename": filename})
    except Exception:
        pass
    return redirect(url_for("ui_property_detail", property_id=property_id))


@app.route("/share/<share_id>/approve_ui", methods=["POST"])
def ui_approve_share(share_id: str) -> Any:
    """Approve a disclosure share from the UI."""
    share = package_shares.get(share_id)
    if not share:
        return "Share not found", 404
    prop_id = share.get("property_id")
    if not share.get("approved"):
        share["approved"] = True
        # log event
        try:
            log_event(prop_id, "share_approved", {"share_id": share_id, "buyer_name": share.get("buyer_name")})
        except Exception:
            pass
        # notify buyer
        try:
            prop = properties.get(prop_id, {})
            prop_name = prop.get("name", prop_id)
            buyer_phone = share.get("buyer_phone")
            buyer_email = share.get("buyer_email")
            buyer_msg = (
                f"Your request to access disclosure package for {prop_name} has been approved.\nUse your share ID {share_id} to download the files."
            )
            buyer_subj = f"Disclosure package approved for {prop_name}"
            if buyer_phone:
                send_sms(buyer_phone, buyer_msg)
            if buyer_email:
                send_email(buyer_email, buyer_subj, buyer_msg)
        except Exception:
            pass
    return redirect(url_for("ui_property_detail", property_id=prop_id))

# Only run the development server if this module is executed directly.
if __name__ == "__main__":
    # Ensure database tables exist and load any existing records into memory
    with app.app_context():
        db.create_all()
        load_db_into_memory()
    # Run the development server on port 3000 for demonstration purposes
    app.run(host="0.0.0.0", port=3000, debug=True)