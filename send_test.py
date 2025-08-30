import os
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

load_dotenv()

sid   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
from_whatsapp = (os.getenv("WHATSAPP_FROM") or "").strip()
to_whatsapp   = (os.getenv("WHATSAPP_TO") or "").strip()

print("SID repr:", repr(sid), "len:", len(sid))
print("TOK last4:", token[-4:], "len:", len(token))

try:
    Client(sid, token).api.accounts(sid).fetch()
    print("Login OK ✅")
except TwilioRestException as e:
    print("AUTH ERROR:", e)
    raise SystemExit()

from twilio.rest import Client
msg = Client(sid, token).messages.create(
    from_=from_whatsapp,
    to=to_whatsapp,
    body="Twilio OK, credenciales válidas."
)
print("Mensaje enviado. SID:", msg.sid)
