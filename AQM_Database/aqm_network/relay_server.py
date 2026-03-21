import asyncio
import json
import logging
import websockets
from AQM_Database.aqm_network.protocol import parse_message , frame_message

logger = logging.getLogger("aqm.relay")

class RelayServer:
    def __init__(self , host , port):
        self.host = host
        self.port = port
        self.connected_clients = {}
        self.mailbox = {}

    async def start(self):
        async with websockets.serve(self.handle_connection , self.host , self.port):
            await asyncio.Future()

    async def handle_connection(self , websocket) -> None:
        message = await websocket.recv()
        msg_type, payload = parse_message(message)
        if not payload.get("user_id") or msg_type != 'AUTH':
            await websocket.send(frame_message("ERROR", {"reason": "auth required"}))
            await websocket.close()
            return

        user_id = payload["user_id"]
        self.connected_clients[user_id] = websocket
        await self.deliver_pending(user_id , websocket)

        try:
            async for message in websocket:
                # Check for GROUP_PARCEL first (uses its own format, not protocol.py)
                if self._is_group_parcel(message):
                    await self.handle_group_parcel(user_id, message)
                    continue
                msg_type, payload = parse_message(message)
                if msg_type == 'PARCEL':
                    await self.route_parcel(user_id, payload["recipient_id"] , message)
        finally:
            if user_id in self.connected_clients:
                del self.connected_clients[user_id]

    async def route_parcel(self, sender_id, recipient_id, raw_frame):
        if recipient_id in self.connected_clients:
            await self.connected_clients[recipient_id].send(raw_frame)
        else:
            self.store_parcel(recipient_id , raw_frame)

    def store_parcel(self, recipient_id, raw_frame):
        self.mailbox.setdefault(recipient_id, []).append(raw_frame)

    async def deliver_pending(self, user_id, websocket):
        if user_id not in self.mailbox:
            return
        pending = self.mailbox.pop(user_id)
        for parcel in pending:
            await websocket.send(parcel)

    # ── Group Parcel Fan-Out (Blind Star Graph, D7) ──────────────────

    def _is_group_parcel(self, raw_message: str) -> bool:
        """
        Check if a raw message is a GROUP_PARCEL without full parsing.
        Why: GROUP_PARCEL uses its own wire format (group_parcel.py),
        not protocol.py's MESSAGE_TYPE set. We peek at msg_type to route.
        """
        try:
            data = json.loads(raw_message)
            return data.get("msg_type") == "GROUP_PARCEL"
        except (json.JSONDecodeError, TypeError):
            return False

    async def handle_group_parcel(self, sender_id: str, raw_message: str) -> None:
        """
        Fan out a group parcel to all recipients (Blind Star Graph, D7).

        Why: the sender uploads the parcel exactly once. The relay reads
        only the routing header (group_id, recipient_ids) and duplicates
        the encrypted blob to each member. Server is zero-knowledge —
        it cannot read the payload, branch keys, or member tiers.

        The same encrypted blob is sent to every recipient. Each recipient
        decrypts their own leaf using their private key or HOT ratchet.
        """
        try:
            data = json.loads(raw_message)
            header = data.get("header", {})
            recipient_ids = header.get("recipient_ids", [])
            group_id = header.get("group_id", "?")

            logger.info("GROUP_PARCEL fan-out: group=%s sender=%s recipients=%d",
                        group_id, sender_id, len(recipient_ids))

            for recipient_id in recipient_ids:
                await self._deliver_or_mailbox(recipient_id, raw_message,
                                               group_id=group_id)

        except Exception as e:
            logger.error("handle_group_parcel failed: %s", e)

    async def _deliver_or_mailbox(self, recipient_id: str, raw_message: str,
                                  group_id: str = None) -> None:
        """
        Deliver to connected client or store in mailbox for later.

        Why: D7 — offline delivery reuses the existing per-user mailbox.
        The optional group_id parameter is for logging/future mailbox
        extension (migration 003).
        """
        if recipient_id in self.connected_clients:
            await self.connected_clients[recipient_id].send(raw_message)
        else:
            self.store_parcel(recipient_id, raw_message)
            if group_id:
                logger.debug("Mailboxed group parcel for %s (group=%s)",
                             recipient_id, group_id)