"""The key a push is signed with (VAPID, RFC 8292).

A push service takes a message for a subscription only from whoever holds the key
the subscription was made for. Beacon's is an ECDSA P-256 key, and it is no outside
service's: Beacon makes one itself the first time it is needed and keeps it in the
push directory, readable by the server's user only, so the subscriptions made for it
go on working when the server is started again.

**The mock store and the live store have a key each** (`mock.vapid.key`,
`live.vapid.key`), and no setting names one key for both. A device shows the
sentence it was handed when it subscribed, and a subscription takes a push only from
the key it was made for: so a device that subscribed to the invented scenario can be
woken only by the invented scenario's server, and shows that server's words.

Nothing is encrypted here, because a push carries no content: the key signs a
short token that says which push service the message is for and until when.
"""

import base64
import binascii
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from beacon.errors import raise_bare
from beacon.files import Mode

SUBJECT_VARIABLE = "BEACON_VAPID_SUBJECT"
# How long a token is good for. A push service takes one of at most a day.
TOKEN_LIFE = timedelta(hours=12)

_SCALAR_BYTES = 32
_BAD_KEY = (
    "The VAPID key file in the push directory does not hold a key Beacon made: 32 "
    "bytes in URL-safe base64. It is left as it is. The subscriptions made for the "
    "key it held take a push from no other key."
)


class VapidError(Exception):
    """The key a push is signed with cannot be had. The text is fixed, and holds
    nothing of the key."""


@dataclass(frozen=True)
class Vapid:
    """The key, and the contact a push service may be given with it."""

    _key: ec.EllipticCurvePrivateKey
    # `mailto:` or `https:`, or None to name nobody.
    subject: str | None = None

    @property
    def public_key(self) -> str:
        """What a browser subscribes with (`applicationServerKey`): the public
        point, uncompressed, in URL-safe base64 without padding."""
        point = self._key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return _b64(point)

    def authorization(self, audience: str, now: datetime) -> str:
        """The `Authorization` header of a push to the push service at `audience`
        (its origin): a token signed with the key, good for `TOKEN_LIFE`, and the
        public key."""
        claims: dict[str, object] = {
            "aud": audience,
            "exp": int((now + TOKEN_LIFE).timestamp()),
        }
        if self.subject is not None:
            claims["sub"] = self.subject
        signed = ".".join(
            _b64(json.dumps(part, separators=(",", ":")).encode())
            for part in ({"typ": "JWT", "alg": "ES256"}, claims)
        )
        r, s = decode_dss_signature(self._key.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(_SCALAR_BYTES, "big") + s.to_bytes(_SCALAR_BYTES, "big")
        return f"vapid t={signed}.{_b64(signature)}, k={self.public_key}"

    @classmethod
    def generate(cls, subject: str | None = None) -> "Vapid":
        return cls(ec.generate_private_key(ec.SECP256R1()), subject)

    @classmethod
    def from_text(cls, text: str, subject: str | None = None) -> "Vapid | None":
        """The key that `text` is, or None when it is not one."""
        try:
            scalar = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
        except (binascii.Error, ValueError):
            return None
        number = int.from_bytes(scalar, "big")
        if len(scalar) != _SCALAR_BYTES or number == 0:
            return None
        try:
            return cls(ec.derive_private_key(number, ec.SECP256R1()), subject)
        except ValueError:
            return None

    def _text(self) -> str:
        number = self._key.private_numbers().private_value
        return _b64(number.to_bytes(_SCALAR_BYTES, "big"))


def key_file(mode: Mode) -> str:
    return f"{mode}.vapid.key"


def open_vapid(env: Mapping[str, str], directory: Path, mode: Mode) -> Vapid:
    """The key the `mode` store's server signs with: the one in `directory`, made
    now when there is none.

    Raises VapidError for a subject that cannot be used, and for a key file that
    cannot be read or made. A file that holds no key is left as it is."""
    subject = env.get(SUBJECT_VARIABLE, "").strip() or None
    if subject is not None and not subject.startswith(("mailto:", "https://")):
        raise VapidError(
            f"{SUBJECT_VARIABLE} is whom a push service may write to about this "
            "server: a mailto: address or an https:// one."
        )
    path = directory / key_file(mode)
    problem: str | None = None
    found: Vapid | None = None
    try:
        try:
            # Made for this server's user only, and only when there is none: a
            # second process that got here first keeps its key.
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            found = Vapid.from_text(path.read_text(encoding="ascii"), subject)
            if found is None:
                problem = _BAD_KEY
        else:
            found = Vapid.generate(subject)
            with os.fdopen(handle, "w", encoding="ascii") as file:
                file.write(found._text() + "\n")
    except (OSError, UnicodeError) as error:
        # Only the kind of error: nothing of a key is in what is raised.
        problem = f"The VAPID key file could not be read or made ({type(error).__name__})."
    if found is None:
        raise_bare(VapidError(problem or _BAD_KEY))
    return found


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
