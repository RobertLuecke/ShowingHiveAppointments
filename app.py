"""
Simple real estate showing management app.

This Flask application demonstrates a handful of core features found in
commercial showing-management platforms like Instashowing. It's not intended
for production use, but it provides a working example of how to:

* Manage property data (create and list properties)
* Schedule showings for specific properties at given times
* Approve or decline showing requests
* Collect feedback from clients after a showing occurs

Data is stored in memory for demonstration purposes only. In a real
application you would likely use a database.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Dict, Any
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "replace-this-with-a-secure-secret-key"

properties: List[Dict[str, Any]] = []
showings: List[Dict[str, Any]] = []
feedback_store: Dict[str, List[Dict[str, Any]]] = {}

@app.route("/")
def home() -> str:
    return render_template("home.html")

@app.route("/properties")
def list_properties() -> str:
    return render_template("properties.html", properties=properties)

@app.route("/properties/add", methods=["GET", "POST"])
def add_property() -> str:
    if request.method == "POST":
        name = request.form.get("name")
        address = request.form.get("address")
        if not name or not address:
            flash("Name and address are required.", "error")
        else:
            properties.append({
                "id": str(uuid.uuid4()),
                "name": name,
                "address": address,
                "created_at": datetime.utcnow(),
            })
            flash("Property added successfully.", "success")
            return redirect(url_for("list_properties"))
    return render_template("add_property.html")

@app.route("/showings")
def list_showings() -> str:
    display_showings = []
    for showing in showings:
        prop = next((p for p in properties if p["id"] == showing["property_id"]), None)
        feedback_list = feedback_store.get(showing["id"], [])
        display_showings.append({
            **showing,
            "property": prop,
            "feedback": feedback_list,
        })
    return render_template("showings.html", showings=display_showings)

@app.route("/showings/add", methods=["GET", "POST"])
def add_showing() -> str:
    if not properties:
        flash("Add a property first before scheduling a showing.", "error")
        return redirect(url_for("list_properties"))
    if request.method == "POST":
        property_id = request.form.get("property_id")
        scheduled_at_str = request.form.get("scheduled_at")
        client_name = request.form.get("client_name")
        if not property_id or not scheduled_at_str or not client_name:
            flash("All fields are required.", "error")
        else:
            try:
                scheduled_at = datetime.strptime(scheduled_at_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("Invalid date/time format.", "error")
                return redirect(url_for("add_showing"))
            new_showing = {
                "id": str(uuid.uuid4()),
                "property_id": property_id,
                "scheduled_at": scheduled_at,
                "client_name": client_name,
                "status": "pending",
                "created_at": datetime.utcnow(),
            }
            showings.append(new_showing)
            flash("Showing scheduled successfully.", "success")
            return redirect(url_for("list_showings"))
    return render_template("add_showing.html", properties=properties)

@app.route("/showings/<showing_id>/approve")
def approve_showing(showing_id: str) -> str:
    for s in showings:
        if s["id"] == showing_id:
            s["status"] = "approved"
            flash("Showing approved.", "success")
            break
    return redirect(url_for("list_showings"))

@app.route("/showings/<showing_id>/decline")
def decline_showing(showing_id: str) -> str:
    for s in showings:
        if s["id"] == showing_id:
            s["status"] = "declined"
            flash("Showing declined.", "success")
            break
    return redirect(url_for("list_showings"))

@app.route("/showings/<showing_id>/feedback", methods=["GET", "POST"])
def showing_feedback(showing_id: str) -> str:
    showing = next((s for s in showings if s["id"] == showing_id), None)
    if not showing:
        flash("Showing not found.", "error")
        return redirect(url_for("list_showings"))
    prop = next((p for p in properties if p["id"] == showing["property_id"]), None)
    if request.method == "POST":
        rating = request.form.get("rating")
        comment = request.form.get("comment")
        try:
            rating_int = int(rating) if rating else None
        except ValueError:
            rating_int = None
        if rating_int is None or rating_int < 1 or rating_int > 5 or not comment:
            flash("Please provide a rating (1-5) and a comment.", "error")
        else:
            entry = {
                "id": str(uuid.uuid4()),
                "rating": rating_int,
                "comment": comment,
                "created_at": datetime.utcnow(),
            }
            feedback_store.setdefault(showing_id, []).append(entry)
            flash("Feedback submitted.", "success")
            return redirect(url_for("showing_feedback", showing_id=showing_id))
    feedback_list = feedback_store.get(showing_id, [])
    return render_template(
        "feedback.html",
        showing=showing,
        property=prop,
        feedback=feedback_list,
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)
