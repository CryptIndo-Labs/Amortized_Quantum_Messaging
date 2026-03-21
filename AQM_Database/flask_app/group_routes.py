"""
Flask Blueprint for AQM Group Chat (PDF §8.1).

Adds group chat routes under /group prefix. Registered on the main Flask
app via app.register_blueprint(). No existing routes are modified.

Implements: D5 (creator-only groups), D7 (fan-out once), D10 (client-only history).
"""

import json
import logging
import os
import queue
import time
import uuid

from flask import Blueprint, jsonify, request, render_template

logger = logging.getLogger("aqm.flask.group")

group_bp = Blueprint("group", __name__, url_prefix="/group")


def init_group_routes(
    user_id,
    group_db,
    contacts_db,
    inventory,
    vault,
    crypto,
    crypto_lock,
    hot_edge_tracker,
    sse_queue,
    known_contacts,
    login_required_decorator,
    contact_ports=None,
):
    """
    Initialize group routes with the necessary dependencies.

    Why: Flask Blueprints can't easily access module-level state from app.py,
    so we inject dependencies at registration time. This avoids modifying
    any existing Flask routes or global state.
    """
    from AQM_Database.aqm_group.group_orchestrator import GroupOrchestrator
    from AQM_Database.aqm_group.group_parcel import parse_parcel

    sent_parcels = []

    def send_parcel(raw):
        """
        Send parcel to relay. For MVP: forward to each member's Flask instance
        directly (same as 1:1 messages). D7: called exactly once per send.
        """
        sent_parcels.append(raw)
        # Fan-out via HTTP to each member (mirrors _forward_to_partner pattern)
        try:
            header, _ = parse_parcel(raw)
            for recipient_id in header.recipient_ids:
                _forward_group_to_member(recipient_id, raw)
        except Exception as e:
            logger.warning("Group parcel fan-out error: %s", e)

    def _notify_group_created(recipient_id, group_id, group_name, creator_id, all_member_ids):
        """Notify a member that they've been added to a new group."""
        import urllib.request, urllib.error
        port = (contact_ports or {}).get(recipient_id, 5000)
        base_host = os.environ.get("AQM_CONTACT_HOST_TEMPLATE", "localhost")
        if base_host == "docker":
            url = f"http://{recipient_id}:{port}/group/api/notify_create"
        else:
            url = f"http://{base_host}:{port}/group/api/notify_create"

        payload = json.dumps({
            "group_id": group_id,
            "group_name": group_name,
            "creator_id": creator_id,
            "member_ids": all_member_ids,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2)
        except Exception as e:
            logger.debug("Could not notify %s of group create: %s", recipient_id, e)

    def _forward_group_to_member(recipient_id, raw_parcel):
        """Forward group parcel to a member's Flask instance."""
        import urllib.request, urllib.error
        port = (contact_ports or {}).get(recipient_id, 5000)
        base_host = os.environ.get("AQM_CONTACT_HOST_TEMPLATE", "localhost")
        if base_host == "docker":
            url = f"http://{recipient_id}:{port}/group/api/receive"
        else:
            url = f"http://{base_host}:{port}/group/api/receive"

        payload = json.dumps({"parcel": raw_parcel}).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=2)
        except Exception as e:
            logger.debug("Could not forward group parcel to %s: %s", recipient_id, e)

    orchestrator = GroupOrchestrator(
        user_id=user_id,
        group_db=group_db,
        contacts_db=contacts_db,
        inventory=inventory,
        vault=vault,
        crypto=crypto,
        hot_edge_tracker=hot_edge_tracker,
        send_parcel_fn=send_parcel,
    )

    # ── Routes ────────────────────────────────────────────────────────

    @group_bp.route("/")
    @login_required_decorator
    def group_index():
        """Group chat page."""
        return render_template("group.html",
                               user_id=user_id,
                               known_contacts=known_contacts)

    @group_bp.route("/api/create", methods=["POST"])
    @login_required_decorator
    def api_create_group():
        """
        Create a new group. D5: creator-only, creator is ADMIN.
        """
        data = request.get_json(force=True) or {}
        name = data.get("name", "").strip()
        member_ids = data.get("members", [])

        if not name:
            return jsonify({"error": "group name required"}), 400
        if not member_ids:
            return jsonify({"error": "at least one member required"}), 400

        try:
            group = orchestrator.create_group(name, member_ids)
            # Notify other members so the group appears immediately
            for mid in member_ids:
                _notify_group_created(mid, group.group_id, name, user_id, member_ids)
            return jsonify({
                "ok": True,
                "group": {
                    "group_id": group.group_id,
                    "name": group.name,
                    "my_role": group.my_role,
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @group_bp.route("/api/send", methods=["POST"])
    @login_required_decorator
    def api_send_group():
        """
        Send a message to a group. G5: fan-out called exactly once.
        """
        data = request.get_json(force=True) or {}
        group_id = data.get("group_id", "").strip()
        message = data.get("message", "").strip()

        if not group_id or not message:
            return jsonify({"error": "group_id and message required"}), 400

        try:
            with crypto_lock:
                orchestrator.send_group_message(group_id, message)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @group_bp.route("/api/receive", methods=["POST"])
    def api_receive_group():
        """
        Receive a group parcel from another member's Flask instance.
        No auth required (called server-to-server).
        """
        data = request.get_json(force=True) or {}
        raw_parcel = data.get("parcel", "")

        if not raw_parcel:
            return jsonify({"error": "missing parcel"}), 400

        try:
            with crypto_lock:
                plaintext = orchestrator.receive_group_message(raw_parcel)

            if plaintext is not None:
                # Push to SSE for UI update
                try:
                    sse_queue.put_nowait({
                        "type": "group_message",
                        "data": {
                            "text": plaintext,
                            "sender": "unknown",  # extracted from parcel header
                            "ts": time.time(),
                        },
                    })
                except queue.Full:
                    pass

            return jsonify({"ok": True})
        except Exception as e:
            logger.warning("Group receive error: %s", e)
            return jsonify({"error": str(e)}), 500

    @group_bp.route("/api/notify_create", methods=["POST"])
    def api_notify_group_created():
        """
        Receive notification that this user was added to a new group.
        No auth required (called server-to-server).
        """
        data = request.get_json(force=True) or {}
        group_id = data.get("group_id", "")
        group_name = data.get("group_name", "")
        creator_id = data.get("creator_id", "")
        member_ids = data.get("member_ids", [])

        if not group_id:
            return jsonify({"error": "missing group_id"}), 400

        # Create group locally if it doesn't exist
        existing = group_db.get_group(group_id)
        if existing is None:
            group_db.create_group(group_id, group_name or group_id[:8], role="MEMBER")
            # Add creator as member
            group_db.add_member(group_id, creator_id, creator_id, priority="STRANGER")
            # Add all other members
            for mid in member_ids:
                if mid != creator_id:
                    group_db.add_member(group_id, mid, mid, priority="STRANGER")
            logger.info("Auto-created group %s (%s) from notify", group_id, group_name)

            # Push SSE so UI updates
            try:
                sse_queue.put_nowait({
                    "type": "group_created",
                    "data": {"group_id": group_id, "name": group_name},
                })
            except queue.Full:
                pass

        return jsonify({"ok": True})

    @group_bp.route("/api/groups")
    @login_required_decorator
    def api_list_groups():
        """List all groups this user belongs to."""
        groups = group_db.get_all_groups()
        return jsonify({
            "groups": [
                {
                    "group_id": g.group_id,
                    "name": g.name,
                    "my_role": g.my_role,
                    "created_at": g.created_at,
                }
                for g in groups
            ]
        })

    @group_bp.route("/api/groups/<group_id>/messages")
    @login_required_decorator
    def api_group_messages(group_id):
        """Get message history for a group. D10: client-only."""
        messages = group_db.get_messages(group_id)
        return jsonify({"messages": messages})

    @group_bp.route("/api/groups/<group_id>/members")
    @login_required_decorator
    def api_group_members(group_id):
        """Get members of a group."""
        members = group_db.get_members(group_id)
        return jsonify({
            "members": [
                {
                    "member_id": m.member_id,
                    "display_name": m.display_name,
                    "priority": m.priority,
                }
                for m in members
            ]
        })

    return orchestrator
