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
from typing import Any, Dict, List, Optional, Tuple, Set

from flask import Flask, jsonify, request, render_template_string, send_file, render_template, redirect, url_for
from werkzeug.utils import secure_filename
import io
import smtplib
from email.mime.text import MIMEText
from sqlalchemy import or_

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

# Profile pictures uploaded by users.  Each entry maps a user ID to a dict
# containing the original filename and the binary content of the uploaded
# image.  This is kept in memory only for demonstration; a production
# implementation should store files on disk or in a blob storage service.
profile_pics: Dict[int, Dict[str, Any]] = {}

# Favorites store
#
# A mapping from user ID to a set of property IDs that the user has
# marked as favourites.  Buyers can save properties to their favourites
# list from the public listing or property pages.  Sellers and
# agents may ignore this field.  This is an in‑memory store; in a
# production system you would persist favourites in the database.
favorites: Dict[int, set] = {}

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
    # Role distinguishes between agents and sellers. Agents can create and manage
    # properties, while sellers can approve showings and disclosures for their own
    # properties. Buyers do not authenticate and therefore do not have a role.
    role = db.Column(db.String(20), nullable=False, default="agent")

    # Additional profile fields
    # Email address for contact.  Required for agents and sellers; optional for other roles.
    email = db.Column(db.String(200))
    # Mailing or contact address.  Optional.
    address = db.Column(db.String(200))
    # License number for agents (both listing and buyer agents).  Optional; sellers
    # and buyers may leave this blank.
    license_number = db.Column(db.String(200))
    # Filename of the uploaded avatar/profile image.  The actual file content is stored
    # in the ``profile_pics`` in‑memory dictionary keyed by user ID.
    avatar_filename = db.Column(db.String(200))


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
    """Render a registration form and create a new user.

    The registration form collects the user's email, password and role.  The
    email serves as the unique login identifier ("username") for the account.
    Address and license number are no longer collected at registration and
    can be set later on the profile page.
    """
    if request.method == "POST":
        # Use email as the unique username
        email = request.form.get("email")
        password = request.form.get("password")
        role = request.form.get("role") or "agent"
        # Validate required fields
        if not email or not password:
            return render_template("register.html", error="Email and password are required")
        # Use the email as the username internally
        username = email
        # Check if a user with this email/username already exists
        existing = User.query.filter_by(username=username).first()
        if existing:
            return render_template("register.html", error="An account with this email already exists")
        # Create the new user; address and license number are optional and will
        # default to empty strings.  Users can edit these later on their
        # profile page.
        new_user = User(
            username=username,
            password=password,
            role=role,
            email=email,
            address="",  # set to empty; editable later
            license_number="",  # set to empty; editable later
        )
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for("ui_dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    """Render a login form and authenticate the user."""
    if request.method == "POST":
        # Use email as the username identifier
        email = request.form.get("email")
        password = request.form.get("password")
        # Look up the user by either their username (stored as the email) or
        # the separate email column (older schemas may have kept these
        # distinct).  Use SQLAlchemy's ``or_`` operator to match either
        # condition along with the provided password.
        user = User.query.filter(
            or_(User.username == email, User.email == email),
            User.password == password,
        ).first()
        if user:
            login_user(user)
            # After logging in, take the user to their dashboard if they have one
            return redirect(url_for("ui_dashboard"))
        return render_template("login.html", error="Invalid email or password")
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
    # Pass current year for footer in home template to avoid UndefinedError
    from datetime import datetime
    current_year = datetime.now().year
    return render_template("home.html", properties=properties, current_year=current_year)

# --------------------------------------------------------------------------
# Additional routes and aliases to support navigation links
#
# Some templates reference 'home', 'manage_showings', and 'tasks' endpoints.
# Register these endpoints here to avoid BuildError exceptions.

# Alias '/' with endpoint name 'home' to map to ui_home
app.add_url_rule("/", endpoint="home", view_func=ui_home)

# ------------------------------ Dashboard -----------------------------------
@app.route("/dashboard")
@login_required
def ui_dashboard():
    """Display a dashboard summarizing the current user's properties, showings,
    disclosure packages, and feedback.

    Agents and sellers can use this view to manage their listings and monitor
    activity. Buyers do not have a dashboard and are redirected to the home
    page.
    """
    # Ensure the current user is an agent or seller; buyers are redirected
    if hasattr(current_user, "role") and current_user.role not in {"seller", "listing_agent", "buyer_agent", "both_agent", "agent"}:
        return redirect(url_for("ui_home"))

    # Collect properties owned or managed by the current user
    my_props = []
    for prop in properties.values():
        # Each property record may store seller_id or agent_username depending on how it was created
        seller_id = prop.get("seller_id")
        agent_username = prop.get("agent_username")
        if seller_id is not None and seller_id == current_user.id:
            my_props.append(prop)
        elif agent_username is not None and agent_username == getattr(current_user, "username", None):
            my_props.append(prop)

    # Gather property IDs for lookups
    prop_ids = {p["id"] for p in my_props}

    # Filter showings for these properties.  Note: showings is a dict; iterate over values.
    my_showings = [show for show in showings.values() if show.get("property_id") in prop_ids]
    # Sort showings chronologically by scheduled time (ISO string comparison suffices)
    my_showings = sorted(my_showings, key=lambda s: s.get("scheduled_at", ""))

    # Filter disclosure packages and package share requests for these properties.  The
    # global variable ``packages`` stores package definitions keyed by package ID,
    # and ``package_shares`` stores share records keyed by share ID.  These lists
    # replace the previously undefined disclosure_packages and disclosure_requests.
    my_packages = [pkg for pkg in packages.values() if pkg.get("property_id") in prop_ids]
    my_pkg_requests = [req for req in package_shares.values() if req.get("property_id") in prop_ids]

    # Filter feedback for these properties
    my_feedback = [fb for fb in feedback if fb.get("property_id") in prop_ids]

    # Compute simple statistics
    def _avg(values):
        # Helper to compute average ignoring None
        vals = [v for v in values if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    stats = {
        "properties": len(my_props),
        "showings_total": len(my_showings),
        "disclosures_total": len(my_pkg_requests),
        "feedback_total": len(my_feedback),
        "avg_rating_house": _avg([fb.get("rating_house") for fb in my_feedback]),
        "avg_rating_price": _avg([fb.get("rating_price") for fb in my_feedback]),
        "avg_rating_quality": _avg([fb.get("rating_quality") for fb in my_feedback]),
    }

    # Determine favourites for buyers
    my_favorites: List[Dict[str, Any]] = []
    if hasattr(current_user, "role") and current_user.role == "buyer":
        fav_ids = favorites.get(current_user.id, set())
        # Build list of property dicts for the user's favourites
        for pid in fav_ids:
            prop = properties.get(pid)
            if prop:
                my_favorites.append(prop)
    return render_template(
        "dashboard.html",
        properties=my_props,
        showings=my_showings,
        packages=my_packages,
        package_requests=my_pkg_requests,
        feedback_list=my_feedback,
        stats=stats,
        favorites=my_favorites,
    )

# ------------------------------ Public Listing -------------------------------
#
# Provide a public page where buyers and buyer agents can browse all
# properties and access their showing calendars and disclosure packages
# without needing to log in.  Each property is listed with a button
# linking to its public token page.  The page is accessible via
# `/public` and displays the property name and address.  It uses the
# existing ``public_property`` route for individual properties.
@app.route("/public")
def public_list() -> Any:
    """Render a public listing page showing all properties.

    Buyers and buyer agents can access this page without an account to
    browse available properties and request showings or download
    disclosure packages.  Properties are listed in alphabetical
    order by name and show their address.  Each listing links to the
    public property page via its token.  The current year is passed
    for the footer in the template.
    """
    from datetime import datetime
    current_year = datetime.now().year
    # Sort properties by name for display
    sorted_properties = sorted(
        properties.values(), key=lambda p: p.get("name", "")
    )
    # Determine the current user's favourite properties if authenticated buyer
    fav_set = set()
    if current_user.is_authenticated and getattr(current_user, "role", None) == "buyer":
        fav_set = favorites.get(current_user.id, set())
    return render_template(
        "public_list.html",
        properties=sorted_properties,
        current_year=current_year,
        favorites_set=fav_set,
    )


# -----------------------------------------------------------------------------
# User Profile
#
# Authenticated users can view and update their profile information.  Sellers
# and agents may supply an email address, mailing address, and license number,
# and optionally upload a profile picture.  The uploaded image is stored in
# memory and referenced by a filename on the User record.  The profile page
# also displays the user's properties and upcoming showings in a simple list.

@app.route("/profile", methods=["GET", "POST"])
@login_required
def ui_profile() -> Any:
    """Render and handle updates to the logged‑in user's profile."""
    user: User = current_user  # type: ignore[assignment]
    if request.method == "POST":
        # Update user fields from form
        # Email is required for agents and sellers but optional for other roles
        email = request.form.get("email")
        address = request.form.get("address")
        license_number = request.form.get("license")
        # Update the SQLAlchemy model
        user.email = email
        user.address = address
        user.license_number = license_number
        # Handle profile picture upload
        file = request.files.get("picture")
        if file and file.filename:
            filename = secure_filename(file.filename)
            user.avatar_filename = filename
            profile_pics[user.id] = {"filename": filename, "content": file.read()}
        db.session.commit()
        # Reload in‑memory structures from DB to reflect changes
        load_db_into_memory()
    # Collect properties owned or managed by the current user
    my_props = []
    for prop in properties.values():
        seller_id = prop.get("seller_id")
        agent_username = prop.get("agent_username")
        if seller_id is not None and seller_id == user.id:
            my_props.append(prop)
        elif agent_username is not None and agent_username == getattr(user, "username", None):
            my_props.append(prop)
    prop_ids = {p["id"] for p in my_props}
    # Gather upcoming showings across all managed properties
    upcoming_showings = [s for s in showings if s.get("property_id") in prop_ids]
    # Sort showings by scheduled time ascending
    upcoming_showings = sorted(
        upcoming_showings,
        key=lambda s: s.get("scheduled_at", "")
    )
    # Determine if a profile picture exists for this user
    pic_info = profile_pics.get(user.id)
    # Prepare base64‑encoded image data for the template if a picture exists
    img_data = None
    if pic_info:
        try:
            import base64  # late import to avoid unused import when no image
            img_data = (
                "data:image/" + pic_info.get("filename", "").split(".")[-1] + ";base64," +
                base64.b64encode(pic_info.get("content", b" ")).decode("utf-8")
            )
        except Exception:
            img_data = None
    # Determine favourite properties for this user
    fav_ids = favorites.get(user.id, set())
    fav_props: List[Dict[str, Any]] = []
    for pid in fav_ids:
        prop = properties.get(pid)
        if prop:
            fav_props.append(prop)
    return render_template(
        "profile.html",
        user=user,
        properties=my_props,
        showings=upcoming_showings,
        picture=img_data,
        favorites=fav_props,
    )

# -------------------------------------------------------------------------
# Guest request flow
#
# Buyers and buyers' agents who do not wish to create an account can request
# access to a property's showing calendar and disclosure packages by
# providing only their name and phone number.  These "guest requests"
# are stored in memory and presented to the listing agent or seller for
# approval.  When a request is approved, the requester receives a
# shareable link to the property's public page.  If declined, the
# requester is notified accordingly.  This feature allows agents to
# post a simple link in the MLS remarks for buyers to request access
# without signing up.

# In‑memory store for guest requests.  Each entry is keyed by a
# UUID string and contains fields: id, property_id, name, phone,
# role (buyer or buyer_agent), email (optional), status (pending,
# approved, declined), created_at ISO timestamp, and access_link when
# approved.  In a production system these would be persisted in a
# database.
guest_requests: Dict[str, Dict[str, Any]] = {}

# Utility to find a property record by its public token.  Returns
# the property dictionary or None if not found.
def _find_property_by_token(token: str) -> Optional[Dict[str, Any]]:
    for prop in properties.values():
        if prop.get("public_token") == token:
            return prop
    return None

@app.route("/request_access/<public_token>", methods=["GET", "POST"])
def request_access(public_token: str) -> Any:
    """Display or handle a guest access request for a property.

    Buyers and buyers' agents can access this page without logging in.  On
    GET, it shows a form for the requester to enter their name, phone
    number, role (Buyer or Buyer Agent) and optional email.  On POST,
    the request is recorded in the global ``guest_requests`` store and
    the listing agent or seller is notified via SMS/email.  The page
    then displays a confirmation message to the requester.

    Args:
        public_token: Unique token that identifies the property.

    Returns:
        Rendered HTML page.
    """
    # Look up property by token
    prop = _find_property_by_token(public_token)
    if not prop:
        return "Property not found", 404
    # Determine current year for footer
    from datetime import datetime
    current_year = datetime.now().year
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role", "buyer").strip()  # default to buyer
        email = request.form.get("email", "").strip()
        if not name or not phone:
            return render_template(
                "request_access.html",
                property=prop,
                error="Name and phone number are required.",
                current_year=current_year,
            )
        import uuid
        req_id = uuid.uuid4().hex
        guest_requests[req_id] = {
            "id": req_id,
            "property_id": prop["id"],
            "public_token": public_token,
            "name": name,
            "phone": phone,
            "role": role,
            "email": email or None,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
            "access_link": None,
        }
        # Notify listing agent/seller (if configured) via SMS/email
        # Determine contact details for seller/agent
        seller_id = prop.get("seller_id")
        agent_username = prop.get("agent_username")
        contact_name = None
        contact_phone = None
        contact_email = None
        if seller_id:
            seller = User.query.get(seller_id)
            if seller:
                contact_name = seller.username
                contact_phone = getattr(seller, "seller_phone", None)
                contact_email = seller.email
        elif agent_username:
            # Agent username corresponds to a user with that username
            agent = User.query.filter_by(username=agent_username).first()
            if agent:
                contact_name = agent.username
                contact_phone = getattr(agent, "agent_phone", None)
                contact_email = agent.email
        # Compose message
        msg = (
            f"New access request for property {prop.get('name')} by {name}. "
            f"Phone: {phone}. Role: {role}."
        )
        if contact_email:
            send_email(contact_email, f"New access request for {prop.get('name')}", msg)
        if contact_phone:
            send_sms(contact_phone, msg)
        return render_template(
            "request_access.html",
            property=prop,
            submitted=True,
            current_year=current_year,
        )
    # GET: render form
    return render_template(
        "request_access.html",
        property=prop,
        error=None,
        submitted=False,
        current_year=current_year,
    )

@app.route("/guest_requests")
@login_required
def list_guest_requests() -> Any:
    """Display all guest access requests for the current user's properties.

    Only sellers, listing agents, buyer agents or both agents can view
    requests.  Buyers are redirected to the home page.
    """
    if not hasattr(current_user, "role") or current_user.role not in {
        "seller", "listing_agent", "buyer_agent", "both_agent", "agent"
    }:
        return redirect(url_for("ui_home"))
    # Build a list of requests where the property belongs to current user
    # Determine property IDs managed by current user
    prop_ids: Set[str] = set()
    for prop in properties.values():
        seller_id = prop.get("seller_id")
        agent_username = prop.get("agent_username")
        if seller_id and seller_id == current_user.id:
            prop_ids.add(prop["id"])
        elif agent_username and agent_username == getattr(current_user, "username", None):
            prop_ids.add(prop["id"])
    # Filter guest requests for these properties
    requests_for_user = [r for r in guest_requests.values() if r.get("property_id") in prop_ids]
    # Sort by created_at descending
    requests_for_user.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    from datetime import datetime
    current_year = datetime.now().year
    return render_template(
        "guest_requests.html",
        requests=requests_for_user,
        properties=properties,
        current_year=current_year,
    )

@app.route("/guest_requests/<req_id>/approve", methods=["POST"])
@login_required
def approve_guest_request(req_id: str) -> Any:
    """Approve a pending guest access request.

    Marks the request as approved, generates an access link, and
    notifies the requester via SMS/email if provided.  Only
    sellers/agents can approve.  Buyers attempting to access this
    endpoint will be redirected to home.  After approval, the user
    is redirected back to the guest requests page.
    """
    if not hasattr(current_user, "role") or current_user.role not in {
        "seller", "listing_agent", "buyer_agent", "both_agent", "agent"
    }:
        return redirect(url_for("ui_home"))
    req = guest_requests.get(req_id)
    if not req:
        return "Request not found", 404
    # Ensure property belongs to current user
    prop = properties.get(req.get("property_id"))
    if not prop:
        return "Property not found", 404
    seller_id = prop.get("seller_id")
    agent_username = prop.get("agent_username")
    if not (
        (seller_id and seller_id == current_user.id)
        or (agent_username and agent_username == getattr(current_user, "username", None))
    ):
        return "Unauthorized", 403
    # Approve if pending
    if req.get("status") == "pending":
        req["status"] = "approved"
        # Construct public link for property
        public_token = req.get("public_token")
        access_link = url_for("public_property", public_token=public_token, _external=True)
        req["access_link"] = access_link
        # Notify requester via email or SMS
        msg = (
            f"Your request for property {prop.get('name')} has been approved. "
            f"You can access the showing calendar and disclosures here: {access_link}"
        )
        if req.get("email"):
            send_email(req["email"], f"Access approved for {prop.get('name')}", msg)
        # Use phone if available for SMS
        send_sms(req.get("phone"), msg)
    return redirect(url_for("list_guest_requests"))

@app.route("/guest_requests/<req_id>/decline", methods=["POST"])
@login_required
def decline_guest_request(req_id: str) -> Any:
    """Decline a pending guest access request.

    Marks the request as declined and notifies the requester if
    possible.  Only sellers/agents can decline.  After declining,
    redirects back to the guest requests page.
    """
    if not hasattr(current_user, "role") or current_user.role not in {
        "seller", "listing_agent", "buyer_agent", "both_agent", "agent"
    }:
        return redirect(url_for("ui_home"))
    req = guest_requests.get(req_id)
    if not req:
        return "Request not found", 404
    # Ensure property belongs to current user
    prop = properties.get(req.get("property_id"))
    if not prop:
        return "Property not found", 404
    seller_id = prop.get("seller_id")
    agent_username = prop.get("agent_username")
    if not (
        (seller_id and seller_id == current_user.id)
        or (agent_username and agent_username == getattr(current_user, "username", None))
    ):
        return "Unauthorized", 403
    if req.get("status") == "pending":
        req["status"] = "declined"
        msg = (
            f"Your request for property {prop.get('name')} has been declined. "
            "Please contact the listing agent for further information."
        )
        if req.get("email"):
            send_email(req["email"], f"Access declined for {prop.get('name')}", msg)
        send_sms(req.get("phone"), msg)
    return redirect(url_for("list_guest_requests"))

@app.route("/manage-showings")
@login_required
def manage_showings() -> Any:
    """
    Redirect to the home/dashboard page for managing showings and disclosures.
    This route exists to satisfy navigation links in the templates.
    """
    return redirect(url_for("ui_home"))

# --------------------------------------------------------------------------
# Public listing routes
#
# Buyers and buyer agents do not need to authenticate to request showings or
# disclosure packages.  Each property has a unique ``public_token`` that
# generates a public URL.  The routes below render a simplified schedule
# calendar and disclosure package list for buyers.  Buyers can select an
# available slot to request a showing or request a disclosure package via
# a simple form.  Seller/agent approvals and notifications use the same
# underlying logic as the authenticated UI routes.

@app.route("/public/property/<public_token>")
def public_property(public_token: str) -> Any:
    """Display a public schedule and disclosure list for a property.

    This route does not require authentication.  It looks up the property by
    ``public_token`` and, if found, builds a 7‑day schedule (8am–8pm) to
    display as clickable slots.  Only packages marked ``is_public`` are shown
    in the disclosure request form.
    """
    # Find the property by its public token
    prop_id = None
    for pid, prop in properties.items():
        if prop.get("public_token") == public_token:
            prop_id = pid
            break
    if not prop_id:
        return "Property not found", 404
    prop = properties.get(prop_id)
    # Determine if the logged‑in user (buyer) has favourited this property
    is_favorite = False
    if current_user.is_authenticated and getattr(current_user, "role", None) == "buyer":
        fav_set = favorites.get(current_user.id, set())
        is_favorite = prop_id in fav_set
    # Build weekly slots (8am–8pm) like the seller view
    from datetime import date, time
    today = date.today()
    week_slots: List[Dict[str, Any]] = []
    for offset in range(7):
        day_date = today + timedelta(days=offset)
        day_label = day_date.strftime("%a %b %d")
        times_list: List[Dict[str, Any]] = []
        for hour in range(8, 20):  # 8am to 7pm start times
            slot_dt = datetime.combine(day_date, time(hour, 0))
            iso_ts = slot_dt.strftime("%Y-%m-%dT%H:%M")
            available = True
            # Check existing showings
            for s in showings.values():
                if s["property_id"] == prop_id:
                    start_dt = s["scheduled_at"] if isinstance(s["scheduled_at"], datetime) else datetime.fromisoformat(str(s["scheduled_at"]))
                    end_dt = start_dt + timedelta(hours=1)
                    if start_dt <= slot_dt < end_dt:
                        available = False
                        break
            # Check blocked times
            if available:
                for b_start, b_end in blocked_times.get(prop_id, []):
                    if b_start <= slot_dt < b_end:
                        available = False
                        break
            times_list.append({
                "iso": iso_ts,
                "label": slot_dt.strftime("%-I:%M %p"),
                "available": available,
            })
        week_slots.append({"date": day_label, "times": times_list})
    # Filter packages for this property that are marked public
    property_packages = [pkg for pkg in packages.values() if pkg["property_id"] == prop_id and pkg.get("is_public")]
    return render_template(
        "public_property.html",
        property=prop,
        week_slots=week_slots,
        packages=property_packages,
        is_favorite=is_favorite,
    )


@app.route("/public/property/<public_token>/schedule-slot/<scheduled_at>", methods=["GET", "POST"])
def public_schedule_slot(public_token: str, scheduled_at: str) -> Any:
    """Allow buyers to request a showing via a public link.

    The GET request shows a simple form asking for buyer name and contact info.
    The POST request schedules the showing if the slot is still available and
    triggers notifications/approvals as in the authenticated flow.
    """
    # Find property by token
    prop_id = None
    for pid, prop in properties.items():
        if prop.get("public_token") == public_token:
            prop_id = pid
            break
    if not prop_id:
        return "Property not found", 404
    prop = properties.get(prop_id)
    try:
        slot_dt = datetime.fromisoformat(scheduled_at)
    except Exception:
        return redirect(url_for("public_property", public_token=public_token))
    if request.method == "POST":
        client_name = request.form.get("client_name")
        client_phone = request.form.get("client_phone")
        client_email = request.form.get("client_email")
        # Ratings (optional) – new fields for house/price/quality
        rating_house = request.form.get("rating_house")
        rating_price = request.form.get("rating_price")
        rating_quality = request.form.get("rating_quality")
        if not client_name:
            return render_template(
                "schedule_slot.html",
                property=prop,
                property_id=prop_id,
                scheduled_at=scheduled_at,
                error="Name is required",
            )
        start = slot_dt
        end = start + timedelta(hours=1)
        # Check availability
        if is_time_blocked(prop_id, start, end) or has_conflict(prop_id, start, end):
            return render_template(
                "schedule_slot.html",
                property=prop,
                property_id=prop_id,
                scheduled_at=scheduled_at,
                error="This slot is no longer available",
            )
        # Create showing
        showing_id = str(uuid.uuid4())
        showings[showing_id] = {
            "id": showing_id,
            "property_id": prop_id,
            "scheduled_at": start,
            "status": "pending",
            "client_name": client_name,
            "client_phone": client_phone,
            "client_email": client_email,
        }
        # Persist to DB
        db_showing = ShowingModel(
            id=showing_id,
            property_id=prop_id,
            client_name=client_name,
            client_phone=client_phone,
            client_email=client_email,
            scheduled_at=start,
            status="pending",
        )
        db.session.add(db_showing)
        db.session.commit()
        # Send notifications and log event using existing code
        try:
            buyer_msg = f"Your showing request for {prop['name']} on {start.strftime('%Y-%m-%d %H:%M')} has been received and is pending approval."
            if client_phone:
                send_sms(client_phone, buyer_msg)
            if client_email:
                send_email(client_email, "Showing request received", buyer_msg)
            contact_msg = f"New showing request for {prop['name']} at {start.strftime('%Y-%m-%d %H:%M')} from {client_name}."
            for contact in [prop.get("seller_phone"), prop.get("agent_phone")]:
                if contact:
                    send_sms(contact, contact_msg)
            for contact in [prop.get("seller_email"), prop.get("agent_email")]:
                if contact:
                    send_email(contact, "New showing request", contact_msg)
            log_event(prop_id, "showing_requested", {"showing_id": showing_id, "client_name": client_name, "scheduled_at": start.isoformat()})
            # Auto approve if configured
            if prop.get("auto_approve_showings"):
                s = showings[showing_id]
                code = generate_lockbox_code()
                s["lockbox_code"] = code
                s["code_expires_at"] = s["scheduled_at"] + timedelta(hours=1, minutes=15)
                s["status"] = "approved"
                when2 = s["scheduled_at"].strftime("%Y-%m-%d %H:%M")
                code_str = s["lockbox_code"]
                expires_str = s["code_expires_at"].strftime("%Y-%m-%d %H:%M")
                if client_phone:
                    send_sms(client_phone, f"Your showing for {prop['name']} at {when2} has been approved. Lockbox code: {code_str} (expires {expires_str}).")
                if client_email:
                    send_email(client_email, "Showing approved", f"Hello {client_name},\n\nYour showing for {prop['name']} at {when2} has been approved.\nYour lockbox code is {code_str} and will expire at {expires_str}.\n\nThank you.")
                msg_notify = (
                    f"Showing for {prop['name']} on {when2} was automatically approved.\n"
                    f"Buyer: {client_name}. Lockbox code: {code_str} (expires {expires_str}).\n"
                    f"Showing ID: {showing_id}"
                )
                subj_notify = f"Showing auto‑approved for {prop['name']}"
                for contact in [prop.get("seller_phone"), prop.get("agent_phone")]:
                    if contact:
                        send_sms(contact, msg_notify)
                for contact in [prop.get("seller_email"), prop.get("agent_email")]:
                    if contact:
                        send_email(contact, subj_notify, msg_notify)
                log_event(prop_id, "showing_approved", {"showing_id": showing_id, "client_name": client_name, "scheduled_at": start.isoformat(), "lockbox_code": code, "auto": True})
        except Exception:
            pass
        # Optionally store ratings for the property (if provided).  We attach them
        # as feedback entries keyed by showing ID so sellers can see buyer
        # sentiment later.
        if rating_house or rating_price or rating_quality:
            feedback_store.setdefault(showing_id, []).append({
                "rating_house": rating_house,
                "rating_price": rating_price,
                "rating_quality": rating_quality,
                "comment": None,
                "timestamp": datetime.utcnow(),
            })
        return redirect(url_for("public_property", public_token=public_token))
    # GET: show form
    return render_template(
        "schedule_slot.html",
        property=prop,
        property_id=prop_id,
        scheduled_at=scheduled_at,
        form_action=url_for("public_schedule_slot", public_token=public_token, scheduled_at=scheduled_at),
        back_link=url_for("public_property", public_token=public_token),
    )


@app.route("/public/property/<public_token>/request-package", methods=["POST"])
def public_request_package(public_token: str) -> Any:
    """Handle disclosure package requests from the public page.

    Buyers choose a public package and provide their name and contact info.
    The property settings control whether the request is auto‑approved or
    requires manual approval.  Notifications are sent to the seller/agent and
    buyer accordingly.
    """
    # Find property by token
    prop_id = None
    for pid, prop in properties.items():
        if prop.get("public_token") == public_token:
            prop_id = pid
            break
    if not prop_id:
        return "Property not found", 404
    prop = properties.get(prop_id)
    pkg_id = request.form.get("package_id")
    buyer_name = request.form.get("buyer_name")
    buyer_phone = request.form.get("buyer_phone")
    buyer_email = request.form.get("buyer_email")
    if not pkg_id or not buyer_name:
        return redirect(url_for("public_property", public_token=public_token))
    pkg = packages.get(pkg_id)
    if not pkg or pkg.get("property_id") != prop_id:
        return redirect(url_for("public_property", public_token=public_token))
    # Determine auto approval based on property setting
    auto = not prop.get("requires_disclosure_approval")
    share_id = str(uuid.uuid4())
    package_shares[share_id] = {
        "id": share_id,
        "package_id": pkg_id,
        "property_id": prop_id,
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "buyer_email": buyer_email,
        "downloads": [],
        "approved": auto,
    }
    # Notify seller/agent
    try:
        prop_name = prop.get("name", prop_id)
        if auto:
            msg = (
                f"Disclosure package '{pkg['name']}' for {prop_name} was automatically shared with buyer {buyer_name}."
                f" (Share ID: {share_id})"
            )
            subj = f"Disclosure package shared for {prop_name}"
        else:
            msg = (
                f"Buyer {buyer_name} has requested access to disclosure package '{pkg['name']}' for {prop_name}.\n"
                f"Approve the share via your dashboard. Share ID: {share_id}."
            )
            subj = f"Disclosure access request for {prop_name}"
        for contact in [prop.get("seller_phone"), prop.get("agent_phone")]:
            if contact:
                send_sms(contact, msg)
        for contact in [prop.get("seller_email"), prop.get("agent_email")]:
            if contact:
                send_email(contact, subj, msg)
    except Exception:
        pass
    # Notify buyer
    try:
        if auto:
            buyer_msg = (
                f"You have been granted access to disclosure package '{pkg['name']}' for {prop['name']}'.\n"
                f"Use your share ID {share_id} to download the files."
            )
            buyer_subj = f"Disclosure package available for {prop['name']}"
        else:
            buyer_msg = (
                f"Your request to access disclosure package '{pkg['name']}' for {prop['name']}' has been received and is pending approval.\n"
                f"You will be notified when access is granted."
            )
            buyer_subj = f"Disclosure access request received for {prop['name']}"
        if buyer_phone:
            send_sms(buyer_phone, buyer_msg)
        if buyer_email:
            send_email(buyer_email, buyer_subj, buyer_msg)
    except Exception:
        pass
    # Log share creation
    try:
        log_event(prop_id, "share_created", {"share_id": share_id, "package_id": pkg_id, "buyer_name": buyer_name, "auto": auto})
    except Exception:
        pass
    return redirect(url_for("public_property", public_token=public_token))

# Tasks page (placeholder)
@app.route("/tasks")
def tasks_page() -> Any:
    """
    Placeholder page for posting or doing tasks.
    ShowingHive focuses on showings and disclosures; task management is not
    implemented yet.
    """
    return render_template_string(
        """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Tasks – Coming Soon</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 2rem; }
        h1 { color: #00529B; }
    </style>
</head>
<body>
    <h1>Task Marketplace Coming Soon</h1>
    <p>This section of ShowingHive will allow you to post and claim real estate
    tasks in the future. Stay tuned!</p>
    <p><a href="{{ url_for('ui_home') }}">Back to Home</a></p>
</body>
</html>"""
    )


# -----------------------------------------------------------------------------
# Favourites
#
# Buyers can save properties to their favourites list via a POST request.  When a
# buyer toggles a favourite, the property ID is added to or removed from the
# ``favorites`` dictionary keyed by their user ID.  This route requires
# authentication and is intended for buyers only.  After toggling the
# favourite, the user is redirected back to the referring page or the public
# property view.
@app.route("/favorite/<property_id>", methods=["POST"])
@login_required
def favorite_property(property_id: str) -> Any:
    """Toggle a property in the current buyer's favourites list.

    Only users with role ``buyer`` may favourite properties.  If the user
    attempts to favourite a property with any other role, they are redirected
    to the home page.  The function adds the property ID to the set of
    favourites for the current user (creating the set if necessary) or
    removes it if it already exists.  After toggling, the user is
    redirected to the page they came from if available, otherwise to the
    public view of the property.
    """
    # Only allow buyers to favourite properties
    if getattr(current_user, "role", None) != "buyer":
        return redirect(url_for("ui_home"))
    # Ensure the property exists
    if property_id not in properties:
        return "Property not found", 404
    # Get or create the favourites set for this user
    fav_set = favorites.setdefault(current_user.id, set())
    # Toggle membership
    if property_id in fav_set:
        fav_set.remove(property_id)
    else:
        fav_set.add(property_id)
    # Redirect back to referrer or fallback to public property
    ref = request.referrer
    if ref:
        return redirect(ref)
    # Fallback if no referrer
    prop = properties[property_id]
    return redirect(url_for("public_property", public_token=prop.get("public_token")))


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
        # Generate a unique public token for buyers to access the public schedule
        public_token = uuid.uuid4().hex
        # Determine seller for this property.  If the logged‑in user is a seller,
        # assign them; otherwise assign the current agent (so sellers can later
        # approve showings/disclosures).
        seller_id = current_user.id if current_user.role == "seller" else current_user.id
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
            "seller_id": seller_id,
            "public_token": public_token,
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
    # Build a weekly schedule (next 7 days, default 8am-8pm) to display as calendar slots
    # If you wish to change the default hours, adjust the range below.
    from datetime import datetime, timedelta, time, date
    today = date.today()
    week_slots: List[Dict[str, Any]] = []
    for offset in range(7):
        day_date = today + timedelta(days=offset)
        day_label = day_date.strftime("%a %b %d")
        times_list: List[Dict[str, Any]] = []
        # Hours 8 through 19 (8am to 7pm start times). Adjust end+1 for inclusive end.
        for hour in range(8, 20):
            slot_dt = datetime.combine(day_date, time(hour, 0))
            iso_ts = slot_dt.strftime("%Y-%m-%dT%H:%M")
            available = True
            # Check conflicts with existing showings (one‑hour duration)
            for s in showings.values():
                if s["property_id"] == property_id:
                    start_dt = datetime.fromisoformat(s["scheduled_at"])
                    end_dt = start_dt + timedelta(hours=1)
                    if start_dt <= slot_dt < end_dt:
                        available = False
                        break
            # Check blocked times
            if available:
                for b_start, b_end in blocked_times.get(property_id, []):
                    # b_start and b_end are datetime objects
                    if b_start <= slot_dt < b_end:
                        available = False
                        break
            times_list.append({
                "iso": iso_ts,
                "label": slot_dt.strftime("%-I:%M %p"),
                "available": available,
            })
        week_slots.append({"date": day_label, "times": times_list})
    return render_template(
        "property_detail.html",
        property=prop,
        showings=property_showings,
        packages=property_packages,
        shares=property_shares,
        files=files,
        week_slots=week_slots,
        blocks=blocked_times.get(property_id, []),
    )


# -----------------------------------------------------------------------------
# UI route for adding blocked times

@app.route("/properties/<property_id>/block", methods=["POST"])
@login_required
def ui_add_block_time(property_id: str) -> Any:
    """
    Add a blocked time range for a property from the UI.  Expects
    form fields ``start`` and ``end`` as ISO datetimes (e.g. from
    ``datetime-local`` inputs).  After adding the block, the user is
    redirected back to the property detail page.  Overlapping blocks
    are silently ignored (no new block is added).
    """
    prop = properties.get(property_id)
    if not prop:
        return "Property not found", 404
    start_str = request.form.get("start")
    end_str = request.form.get("end")
    try:
        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str)
    except Exception:
        # Invalid inputs; just redirect back
        return redirect(url_for("ui_property_detail", property_id=property_id))
    if end_dt <= start_dt:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    # Check overlap with existing blocks
    overlaps = False
    for b_start, b_end in blocked_times.get(property_id, []):
        if start_dt < b_end and end_dt > b_start:
            overlaps = True
            break
    if not overlaps:
        blocked_times.setdefault(property_id, []).append((start_dt, end_dt))
        # Optionally sort blocked ranges for easier reading
        blocked_times[property_id].sort(key=lambda x: x[0])
    return redirect(url_for("ui_property_detail", property_id=property_id))


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

# -----------------------------------------------------------------------------
# UI endpoint for scheduling a showing via a specific time slot
#
# This route is used when a user clicks on a calendar slot in the property
# detail page.  The GET request displays a form asking for client name and
# contact information; the POST request schedules the showing using the
# provided details.
@app.route("/properties/<property_id>/schedule-slot/<scheduled_at>", methods=["GET", "POST"])
def ui_schedule_slot(property_id: str, scheduled_at: str) -> Any:
    prop = properties.get(property_id)
    if not prop:
        return "Property not found", 404
    # Parse scheduled_at param
    try:
        slot_dt = datetime.fromisoformat(scheduled_at)
    except Exception:
        return redirect(url_for("ui_property_detail", property_id=property_id))
    if request.method == "POST":
        client_name = request.form.get("client_name")
        client_phone = request.form.get("client_phone")
        client_email = request.form.get("client_email")
        if not client_name:
            return render_template(
                "schedule_slot.html",
                property=prop,
                property_id=property_id,
                scheduled_at=scheduled_at,
                error="Client name is required",
            )
        # Check availability (re-use conflict logic)
        start = slot_dt
        end = start + timedelta(hours=1)
        if is_time_blocked(property_id, start, end) or has_conflict(property_id, start, end):
            return render_template(
                "schedule_slot.html",
                property=prop,
                property_id=property_id,
                scheduled_at=scheduled_at,
                error="This slot is no longer available",
            )
        # Create showing
        showing_id = str(uuid.uuid4())
        showings[showing_id] = {
            "id": showing_id,
            "property_id": property_id,
            "scheduled_at": scheduled_at,
            "status": "pending",
            "client_name": client_name,
            "client_phone": client_phone,
            "client_email": client_email,
        }
        # Notify buyer and property contacts
        buyer_msg = f"Your showing request for {prop['name']} on {slot_dt.strftime('%Y-%m-%d %I:%M %p')} has been received and is pending approval."
        if client_phone:
            send_sms(client_phone, buyer_msg)
        if client_email:
            send_email(client_email, "Showing request received", buyer_msg)
        # Notify seller/agent
        contact_msg = f"New showing request for {prop['name']} at {slot_dt.strftime('%Y-%m-%d %I:%M %p')} from {client_name}."
        for contact in [prop.get("seller_phone"), prop.get("agent_phone")]:
            if contact:
                send_sms(contact, contact_msg)
        for contact in [prop.get("seller_email"), prop.get("agent_email")]:
            if contact:
                send_email(contact, "New showing request", contact_msg)
        log_event("showing_requested", property_id, showing_id, details={"client_name": client_name})
        # Auto-approve if property has auto_approve_showings
        if prop.get("auto_approve_showings"):
            showings[showing_id]["status"] = "approved"
            code, expires = generate_lockbox_code(showing_id)
            showings[showing_id]["code"] = code
            showings[showing_id]["expires_at"] = expires.isoformat()
            log_event("showing_approved", property_id, showing_id)
            # Notify buyer with lockbox code
            approval_msg = f"Your showing at {prop['name']} on {slot_dt.strftime('%Y-%m-%d %I:%M %p')} has been approved.\nLockbox code: {code} (expires {expires.strftime('%Y-%m-%d %I:%M %p')})."
            if client_phone:
                send_sms(client_phone, approval_msg)
            if client_email:
                send_email(client_email, "Showing approved", approval_msg)
            # Notify seller/agent
            for contact in [prop.get("seller_phone"), prop.get("agent_phone")]:
                if contact:
                    send_sms(contact, approval_msg)
            for contact in [prop.get("seller_email"), prop.get("agent_email")]:
                if contact:
                    send_email(contact, "Showing auto-approved", approval_msg)
        return redirect(url_for("ui_property_detail", property_id=property_id))
    # GET: show schedule form
    return render_template(
        "schedule_slot.html",
        property=prop,
        property_id=property_id,
        scheduled_at=scheduled_at,
    )


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
    """
    When executed directly, initialize the database schema and run the development server.

    This block also performs a simple schema migration check: if the existing SQLite
    database is missing the `role` column on the `user` table (introduced in
    later versions of the app), it will drop all tables and recreate them with
    the current schema. This destructive operation is intended for development
    environments only. For production use, integrate a proper migration tool
    such as Flask-Migrate/Alembic.
    """
    with app.app_context():
        from sqlalchemy import inspect

        inspector = inspect(db.engine)
        # Attempt to retrieve column names for the user table. If the table
        # doesn't exist yet, this will simply return an empty list.
        try:
            user_columns = [col["name"] for col in inspector.get_columns("user")]
        except Exception:
            user_columns = []
        # If any of the expected User columns are missing, drop and recreate tables.
        # This simple schema migration is only suitable for development.  In a
        # production system, use Alembic or another migration tool.
        required_user_columns = {"role", "email", "address", "license_number", "avatar_filename"}
        missing_cols = required_user_columns.difference(set(user_columns))
        if missing_cols:
            db.drop_all()
            print(
                "Database schema outdated or missing columns {} on user table; dropping and "
                "recreating all tables with the updated schema. All existing data "
                "will be lost.".format(
                    ", ".join(sorted(missing_cols))
                )
            )
        db.create_all()
        # Load any existing records into in-memory structures for the demo
        load_db_into_memory()
    # Run the development server on port 3000 for demonstration purposes
    app.run(host="0.0.0.0", port=3000, debug=True)