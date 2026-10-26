"""Смена пароля Steam.

Методы SteamClient перенесены без изменений из SteamHub (Pictures/SteamHub/client.py, crypto.py):
вход с ожиданием токена, jwt/finalizelogin на все домены, мастер HelpChangePassword
с подтверждением через мобильный аутентификатор (identity_secret).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import struct
import time
from urllib.parse import quote, urlparse, parse_qs

from curl_cffi import requests as cfreq

log = logging.getLogger("steam")

API = "https://api.steampowered.com"
COMMUNITY = "https://steamcommunity.com"
HELP = "https://help.steampowered.com"


class SteamGuardRequired(Exception):
    def __init__(self, client_id, steam_id, request_id, code_type, message):
        super().__init__(message)
        self.client_id = client_id
        self.steam_id = steam_id
        self.request_id = request_id
        self.code_type = code_type


# ---------- crypto.py ----------

GUARD_CHARS = "23456789BCDFGHJKMNPQRTVWXY"


def steam_totp(shared_secret_b64):
    key = base64.b64decode(shared_secret_b64)
    msg = struct.pack(">Q", int(time.time()) // 30)
    mac = hmac.new(key, msg, hashlib.sha1).digest()
    offset = mac[19] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return "".join(GUARD_CHARS[code // 26 ** i % 26] for i in range(5))


def rsa_encrypt(plaintext, mod_hex, exp_hex):
    n, e = int(mod_hex, 16), int(exp_hex, 16)
    key_len = (n.bit_length() + 7) // 8
    msg = plaintext.encode()
    pad_len = key_len - len(msg) - 3
    padding = b""
    while len(padding) < pad_len:
        padding += bytes(b for b in os.urandom(pad_len - len(padding)) if b != 0)
    block = b"\x00\x02" + padding[:pad_len] + b"\x00" + msg
    encrypted = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(encrypted.to_bytes(key_len, "big")).decode()


def confirmation_key(identity_secret_b64, tag, timestamp=None):
    ts = timestamp or int(time.time())
    key = base64.b64decode(identity_secret_b64)
    msg = struct.pack(">Q", ts) + tag.encode()
    return base64.b64encode(hmac.new(key, msg, hashlib.sha1).digest()).decode()


# ---------- client.py ----------

class SteamClient:
    def __init__(self):
        self.http = cfreq.Session(impersonate="chrome120")
        self.steam_id = None
        self.access_token = None
        self.refresh_token = None
        self.session_id = None
        self.login_name = None
        self.identity_secret = None
        self.shared_secret = None
        self._password = None
        self._on_token_refreshed = None

    def login(self, username, password, shared_secret=None):
        log.info("Authenticating as %s...", username)
        self.login_name = username

        for _ in range(5):
            rsa = self.http.get(
                f"{API}/IAuthenticationService/GetPasswordRSAPublicKey/v1",
                params={"account_name": username},
            ).json()["response"]

            r = self.http.post(
                f"{API}/IAuthenticationService/BeginAuthSessionViaCredentials/v1",
                data={
                    "account_name": username,
                    "encrypted_password": rsa_encrypt(
                        password, rsa["publickey_mod"], rsa["publickey_exp"]
                    ),
                    "encryption_timestamp": rsa["timestamp"],
                    "remember_login": "true",
                    "platform_type": "2",
                    "persistence": "1",
                    "website_id": "Store",
                },
            )

            if r.status_code == 429 or r.headers.get("x-eresult") == "87":
                log.warning("Rate limit, waiting 180s...")
                time.sleep(180)
                continue

            auth = r.json().get("response", {})
            if "client_id" in auth:
                break

            log.warning("Empty response, waiting 90s...")
            time.sleep(90)
        else:
            raise Exception("Authentication failed after retries")

        self.steam_id = auth["steamid"]
        client_id = str(auth["client_id"])
        request_id = auth["request_id"]

        confirmations = auth.get("allowed_confirmations", [])
        if shared_secret:
            self.http.post(
                f"{API}/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1",
                data={
                    "client_id": client_id,
                    "steamid": self.steam_id,
                    "code": steam_totp(shared_secret),
                    "code_type": "3",
                },
            )
        elif confirmations:
            types = {c.get("confirmation_type") for c in confirmations}
            if 3 in types:
                code_type, msg = "3", "Enter Steam Guard TOTP code"
            elif 2 in types:
                code_type, msg = "2", "Enter Steam Guard email code"
            else:
                code_type, msg = str(next(iter(types))), "Enter Steam Guard code"
            raise SteamGuardRequired(client_id, self.steam_id, request_id, code_type, msg)

        for attempt in range(15):
            poll = self.http.post(
                f"{API}/IAuthenticationService/PollAuthSessionStatus/v1",
                data={"client_id": client_id, "request_id": request_id},
            ).json()["response"]
            if poll.get("access_token") or poll.get("refresh_token"):
                break
            time.sleep(2)

        self.refresh_token = poll.get("refresh_token")
        self.access_token = poll.get("access_token")
        if not self.access_token and self.refresh_token:
            self._generate_access_token()

        if not self.access_token:
            raise Exception("Failed to obtain access token")

        self._password = password
        self.session_id = secrets.token_hex(12)
        self._apply_cookies()
        self._finalize_login()
        log.info("Logged in (steam_id=%s)", self.steam_id)

    def _generate_access_token(self):
        gen = self.http.post(
            f"{API}/IAuthenticationService/GenerateAccessTokenForApp/v1",
            data={"refresh_token": self.refresh_token, "steamid": self.steam_id},
        ).json().get("response", {})
        token = gen.get("access_token")
        if token:
            self.access_token = token
            self._apply_cookies()
            return True
        return False

    def refresh_web_session(self):
        """Mint a fresh WEB access_token from the refresh_token via Steam's
        finalizelogin/settoken flow. Returns (access_token, session_id) or None."""
        if not self.refresh_token:
            return None
        from urllib.parse import unquote
        sessionid = self.session_id or secrets.token_hex(12)
        try:
            r = self.http.post(
                "https://login.steampowered.com/jwt/finalizelogin",
                data={
                    "nonce": self.refresh_token,
                    "sessionid": sessionid,
                    "redir": "https://steamcommunity.com/login/home/?goto=",
                },
                headers={
                    "Origin": "https://steamcommunity.com",
                    "Referer": "https://steamcommunity.com/",
                },
            )
            data = r.json()
        except Exception:
            return None

        transfers = data.get("transfer_info") or []
        if not transfers:
            return None
        steamid = data.get("steamID", self.steam_id)
        for tr in transfers:
            url = tr.get("url")
            if not url:
                continue
            params = dict(tr.get("params") or {})
            params["steamID"] = steamid
            try:
                self.http.post(url, data=params, timeout=15)
            except Exception:
                pass

        try:
            val = self.http.cookies.get("steamLoginSecure", domain="steamcommunity.com")
        except Exception:
            val = None
        if not val:
            return None
        val = unquote(val)
        if "||" in val:
            self.access_token = val.split("||", 1)[1]
        self.session_id = sessionid
        if self._on_token_refreshed:
            self._on_token_refreshed(self)
        return self.access_token, self.session_id

    @staticmethod
    def _token_exp(tok):
        try:
            p = tok.split(".")[1]
            p += "=" * (-len(p) % 4)
            return json.loads(base64.b64decode(p)).get("exp", 0)
        except Exception:
            return 0

    def _try_refresh(self):
        if self.refresh_token and self._token_exp(self.refresh_token) > time.time():
            # Preferred: mint a fresh WEB token from the refresh token. This is the
            # path that actually works for web sessions (GenerateAccessTokenForApp
            # is denied for web), so try it first.
            try:
                if self.refresh_web_session():   # sets tokens + fires _on_token_refreshed
                    log.info("Access token refreshed (web session)")
                    return True
            except Exception:
                log.debug("web-session refresh failed")
            try:
                if self._generate_access_token():
                    log.info("Access token refreshed via refresh_token")
                    if self._on_token_refreshed:
                        self._on_token_refreshed(self)
                    return True
            except Exception:
                log.debug("Refresh via refresh_token failed")
        if self._password and self.login_name:
            try:
                ss = self.shared_secret
                log.info("Re-authenticating as %s...", self.login_name)
                old_http = self.http
                self.http = cfreq.Session(impersonate="chrome120")
                self.login(self.login_name, self._password, ss)
                if self._on_token_refreshed:
                    self._on_token_refreshed(self)
                return True
            except Exception:
                self.http = old_http
                log.debug("Full re-login failed")
        return False

    def _apply_cookies(self):
        cookie = quote(f"{self.steam_id}||{self.access_token}")
        for domain in [
            "store.steampowered.com",
            "help.steampowered.com",
            "login.steampowered.com",
            "steamcommunity.com",
        ]:
            self.http.cookies.set("steamLoginSecure", cookie, domain=domain, path="/", secure=True)
            self.http.cookies.set("sessionid", self.session_id, domain=domain, path="/")

    def _finalize_login(self):
        try:
            r = self.http.post(
                "https://login.steampowered.com/jwt/finalizelogin",
                data={
                    "nonce": self.refresh_token,
                    "sessionid": self.session_id,
                    "redir": "https://store.steampowered.com/login/?redir=&redir_ssl=1",
                },
            )
            data = r.json()
            if not data.get("steamID"):
                log.debug("finalizelogin failed: %s", data)
                return
            for info in data.get("transfer_info", []):
                url = info.get("url")
                params = info.get("params", {})
                if url and params:
                    self.http.post(url, data=params)
            log.debug("finalizelogin: sessions set on %d domains",
                       len(data.get("transfer_info", [])))
        except Exception as e:
            log.debug("finalizelogin error: %s", e)

    def _device_id(self):
        h = hashlib.sha1(self.steam_id.encode()).hexdigest()
        return f"android:{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

    def change_password(self, password, new_password, shared_secret, identity_secret):
        hdr = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": HELP, "X-Requested-With": "XMLHttpRequest",
        }
        device_id = self._device_id()

        def _get_wizard_params():
            r = self.http.get(
                f"{HELP}/wizard/HelpChangePassword?redir=store/account/",
                allow_redirects=True,
            )
            p = {k: v[0] for k, v in parse_qs(urlparse(str(r.url)).query).items()}
            return p

        params = _get_wizard_params()
        if "s" not in params:
            self.http.get(
                "https://login.steampowered.com/jwt/refresh"
                "?redir=https%3A%2F%2Fhelp.steampowered.com%2F",
                allow_redirects=True,
            )
            params = _get_wizard_params()
        if "s" not in params:
            if not self._try_refresh():
                raise Exception("Session expired — re-login required")
            params = _get_wizard_params()
        if "s" not in params:
            raise Exception("Session expired — re-login required")
        s, account = params["s"], params.get("account", "")
        hsid = self.http.cookies.get("sessionid", domain="help.steampowered.com")

        self.http.get(
            f"{HELP}/en/wizard/HelpWithLoginInfoEnterCode",
            params={
                "s": s, "account": account, "reset": "1", "lost": "0",
                "issueid": "406", "sessionid": hsid,
                "wizard_ajax": "1", "gamepad": "0",
            },
        )
        self.http.post(
            f"{HELP}/en/wizard/AjaxSendAccountRecoveryCode",
            data={
                "sessionid": hsid, "wizard_ajax": "1", "gamepad": "0",
                "s": s, "method": "8", "link": "", "n": "1",
            },
            headers={**hdr, "Referer": f"{HELP}/en/wizard/HelpWithLoginInfoEnterCode?s={s}"},
        )

        confirmed = False
        for _ in range(10):
            ts = int(time.time())
            data = self.http.get(
                f"{COMMUNITY}/mobileconf/getlist",
                params={
                    "p": device_id, "a": self.steam_id,
                    "k": confirmation_key(identity_secret, "conf", ts),
                    "t": str(ts), "m": "android", "tag": "conf",
                },
            ).json()
            confs = data.get("conf", [])
            if confs:
                c = confs[0]
                ts2 = int(time.time())
                r = self.http.get(
                    f"{COMMUNITY}/mobileconf/ajaxop",
                    params={
                        "op": "allow", "p": device_id, "a": self.steam_id,
                        "k": confirmation_key(identity_secret, "allow", ts2),
                        "t": str(ts2), "m": "android", "tag": "allow",
                        "cid": c["id"], "ck": c["nonce"],
                    },
                )
                if r.json().get("success"):
                    confirmed = True
                    break
            time.sleep(3)

        if not confirmed:
            raise Exception("Failed to confirm password change")

        self.http.post(
            f"{HELP}/en/wizard/AjaxPollAccountRecoveryConfirmation",
            data={
                "sessionid": hsid, "wizard_ajax": "1", "s": s,
                "reset": "1", "method": "8", "issueid": "406",
            },
            headers=hdr,
        )
        self.http.get(
            f"{HELP}/en/wizard/AjaxVerifyAccountRecoveryCode",
            params={
                "code": "", "s": s, "reset": "1", "method": "8",
                "issueid": "406", "sessionid": hsid, "wizard_ajax": "1",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.http.post(
            f"{HELP}/en/wizard/AjaxAccountRecoveryGetNextStep",
            data={
                "sessionid": hsid, "wizard_ajax": "1", "s": s,
                "account": account, "reset": "1", "issueid": "406", "lost": "2",
            },
            headers=hdr,
        )

        rsa = self.http.post(
            f"{HELP}/en/login/getrsakey/",
            data={"sessionid": hsid, "username": self.login_name},
            headers=hdr,
        ).json()
        self.http.post(
            f"{HELP}/en/wizard/AjaxAccountRecoveryVerifyPassword/",
            data={
                "sessionid": hsid, "s": s, "lost": "2", "reset": "1",
                "password": rsa_encrypt(password, rsa["publickey_mod"], rsa["publickey_exp"]),
                "rsatimestamp": str(rsa["timestamp"]),
            },
            headers=hdr,
        )

        rsa = self.http.post(
            f"{HELP}/en/login/getrsakey/",
            data={"sessionid": hsid, "username": self.login_name},
            headers=hdr,
        ).json()
        result = self.http.post(
            f"{HELP}/en/wizard/AjaxAccountRecoveryChangePassword/",
            data={
                "sessionid": hsid, "wizard_ajax": "1", "s": s,
                "account": account,
                "password": rsa_encrypt(new_password, rsa["publickey_mod"], rsa["publickey_exp"]),
                "rsatimestamp": str(rsa["timestamp"]),
            },
            headers=hdr,
        ).json()

        if "Complete" in result.get("hash", "") and not result.get("errorMsg"):
            log.info("Password changed successfully")
            return {"success": True}
        raise Exception(result.get("errorMsg", "Unknown error"))


# ---------- обёртки для бота ----------

def generate_password(length=14):
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        # Steam требует буквы и цифры
        if any(c.isdigit() for c in password) and any(c.isalpha() for c in password):
            return password


def change_password(login, password, shared_secret, identity_secret, new_password):
    """Входит в аккаунт и меняет пароль. Бросает исключение при неудаче."""
    client = SteamClient()
    client.shared_secret = shared_secret
    client.identity_secret = identity_secret
    client.login(login, password, shared_secret)
    client.change_password(password, new_password, shared_secret, identity_secret)
    return True
