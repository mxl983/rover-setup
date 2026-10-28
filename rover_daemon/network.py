"""Network helpers: nmcli Wi-Fi + Tailscale."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

log = logging.getLogger("rover.network")

AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")

# Snapshot of NM Wi‑Fi profiles taken at WIPE_NET so a failed WIFI_CONFIG can
# restore connectivity instead of leaving the Pi stranded without Ethernet.
WIFI_BACKUP_DIR = "/var/lib/rover/nm-wifi-backup"
NM_CONNECTIONS_DIR = "/etc/NetworkManager/system-connections"
WIFI_CONNECT_RETRIES = 3
WIFI_CONNECT_RETRY_DELAY_SEC = 3.0
WIFI_IP_WAIT_SEC = 25.0
INTERNET_WAIT_SEC = 45.0
TAILSCALE_JOIN_RETRIES = 3
TAILSCALE_JOIN_RETRY_DELAY_SEC = 4.0


def _run(cmd: list[str], dry_run: bool, timeout: float = 60) -> Tuple[int, str, str]:
    log.info("$ %s", " ".join(cmd))
    if dry_run:
        log.info("[dry-run] skipped")
        return 0, "", ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"missing binary: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def wifi_already_connected() -> bool:
    """True if a Wi‑Fi device is in NM state connected."""
    if not shutil.which("nmcli"):
        code, out, _ = _run(["hostname", "-I"], dry_run=False, timeout=5)
        return code == 0 and bool(out.strip())
    code, out, _ = _run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev"],
        dry_run=False,
        timeout=10,
    )
    if code != 0:
        return False
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] == "wifi" and parts[2] == "connected":
            return True
    return False


def current_wifi_ssid() -> str:
    """Return the active Wi‑Fi SSID, or empty if not connected."""
    if not shutil.which("nmcli"):
        return ""
    code, out, _ = _run(
        ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
        dry_run=False,
        timeout=15,
    )
    if code != 0:
        return ""
    for line in out.splitlines():
        active, _, ssid = line.partition(":")
        if active == "yes" and ssid:
            return ssid
    return ""


def wait_for_wifi_connected(timeout_sec: float = WIFI_IP_WAIT_SEC) -> bool:
    """Poll until Wi‑Fi device is connected (has association / IP path)."""
    deadline = time.time() + max(timeout_sec, 1.0)
    while time.time() < deadline:
        if wifi_already_connected():
            return True
        time.sleep(0.5)
    return wifi_already_connected()


def has_default_route() -> bool:
    code, out, _ = _run(["ip", "-4", "route", "show", "default"], dry_run=False, timeout=5)
    return code == 0 and bool(out.strip())


def wait_for_internet(timeout_sec: float = INTERNET_WAIT_SEC) -> bool:
    """
    Wait until a default route exists and login.tailscale.com is reachable.

    Wi‑Fi association alone is not enough for Tailscale auth-key join.
    """
    deadline = time.time() + max(timeout_sec, 1.0)
    while time.time() < deadline:
        if has_default_route():
            # Cheap TCP/TLS check — auth key join needs this host anyway.
            try:
                urllib.request.urlopen(
                    "https://login.tailscale.com/",
                    timeout=5,
                )
                log.info("internet OK (login.tailscale.com reachable)")
                return True
            except Exception as exc:
                log.info("default route up but Tailscale login not reachable yet: %s", exc)
        time.sleep(1.0)
    ok = has_default_route()
    log.warning("internet wait timed out (default_route=%s)", ok)
    return ok


def _nmcli_profile_wifi_ssid(name: str) -> str:
    """
    SSID stored on one NM connection profile.

    Important: ``nmcli -f …,802-11-wireless.ssid connection show`` (list mode)
    is invalid on many NetworkManager builds — always query per-profile with -g.
    """
    if not name or not shutil.which("nmcli"):
        return ""
    code, out, _ = _run(
        ["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "id", name],
        dry_run=False,
        timeout=10,
    )
    if code != 0:
        return ""
    for line in (out or "").splitlines():
        value = line.strip()
        if value and value != "--":
            return value
    return ""


def _wifi_profile_names() -> list[str]:
    """Names of all NM Wi‑Fi connection profiles."""
    if not shutil.which("nmcli"):
        return []
    code, out, _ = _run(
        ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
        dry_run=False,
        timeout=15,
    )
    if code != 0:
        return []
    names: list[str] = []
    for line in out.splitlines():
        if not line:
            continue
        name, _, rest = line.partition(":")
        ctype = rest.split(":", 1)[0] if rest else ""
        if name and ctype in ("802-11-wireless", "wifi"):
            names.append(name)
    return names


def list_wifi_profiles() -> list[tuple[str, str]]:
    """Return [(profile_name, ssid), ...] for every Wi‑Fi NM profile."""
    result: list[tuple[str, str]] = []
    for name in _wifi_profile_names():
        result.append((name, _nmcli_profile_wifi_ssid(name)))
    return result


def find_wifi_profiles_for_ssid(ssid: str) -> list[str]:
    """NM profile names that belong to ``ssid`` (by stored SSID or profile name)."""
    if not ssid:
        return []
    names: list[str] = []
    for name, profile_ssid in list_wifi_profiles():
        if profile_ssid == ssid or name == ssid:
            names.append(name)
    return names


def _active_wifi_connection_ids() -> set[str]:
    """NM connection ids currently active on a Wi‑Fi device (must not be wiped)."""
    ids: set[str] = set()
    if not shutil.which("nmcli"):
        return ids
    code, out, _ = _run(
        ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
        dry_run=False,
        timeout=10,
    )
    if code == 0:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            name, ctype = parts[0], parts[1]
            if name and ctype in ("802-11-wireless", "wifi"):
                ids.add(name)
    # Device property is authoritative even when profile naming is odd (netplan-*).
    for dev in ("wlan0", "wlan1"):
        code2, out2, _ = _run(
            ["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", dev],
            dry_run=False,
            timeout=10,
        )
        if code2 != 0:
            continue
        conn = (out2 or "").strip().splitlines()
        if conn and conn[0] and conn[0] != "--":
            ids.add(conn[0])
    return ids


def wifi_enrollment_verified(ssid: str) -> bool:
    """True when a reboot-durable NM profile exists for ``ssid``."""
    return bool(find_wifi_profiles_for_ssid(ssid))


def ensure_persistent_wifi_enrollment(
    ssid: str, password: str, dry_run: bool
) -> bool:
    """
    Guarantee a reboot-persistent NM Wi‑Fi profile for ``ssid``.

    Works for both fresh joins and "already associated" (e.g. netplan-wlan0-*)
    so WIFI_CONFIG never ACKs success without a saved profile + autoconnect.
    """
    if not ssid:
        return False
    if dry_run:
        log.info("[dry-run] ensure persistent wifi enrollment ssid=%s", ssid)
        return True
    if not shutil.which("nmcli"):
        log.error("nmcli not found")
        return False

    names = find_wifi_profiles_for_ssid(ssid)
    if not names:
        log.info(
            "no saved Wi‑Fi profile for ssid=%s — creating persistent enrollment",
            ssid,
        )
        if current_wifi_ssid() == ssid and wifi_already_connected():
            # Add a saved profile without requiring a disconnect first.
            cmd = [
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "con-name",
                ssid,
                "ifname",
                "*",
                "ssid",
                ssid,
                "connection.autoconnect",
                "yes",
                "connection.autoconnect-priority",
                "100",
            ]
            if password:
                cmd += [
                    "wifi-sec.key-mgmt",
                    "wpa-psk",
                    "wifi-sec.psk",
                    password,
                ]
            code, out, err = _run(cmd, dry_run=False, timeout=30)
            if code != 0:
                log.warning(
                    "connection add failed for %s (%s %s) — trying device connect",
                    ssid,
                    out.strip(),
                    err.strip(),
                )
                if not _nmcli_wifi_connect_once(ssid, password, 45):
                    return False
                if not wait_for_wifi_connected(WIFI_IP_WAIT_SEC):
                    return False
        else:
            if not _nmcli_wifi_connect_once(ssid, password, 45):
                return False
            if not wait_for_wifi_connected(WIFI_IP_WAIT_SEC):
                return False
        names = find_wifi_profiles_for_ssid(ssid)

    if not names:
        log.error("wifi enrollment failed — still no NM profile for ssid=%s", ssid)
        return False

    for name in names:
        _run(
            [
                "nmcli",
                "connection",
                "modify",
                "id",
                name,
                "connection.autoconnect",
                "yes",
                "connection.autoconnect-priority",
                "100",
            ],
            dry_run=False,
            timeout=15,
        )
        if password:
            # Phone-provisioned PSK is the source of truth during WIFI_CONFIG.
            c2, _, e2 = _run(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    "id",
                    name,
                    "wifi-sec.key-mgmt",
                    "wpa-psk",
                    "wifi-sec.psk",
                    password,
                ],
                dry_run=False,
                timeout=15,
            )
            if c2 != 0:
                log.warning("wifi PSK update failed for profile %s: %s", name, e2)
        log.info("wifi enrolled profile=%s ssid=%s autoconnect=yes", name, ssid)

    ok = wifi_enrollment_verified(ssid)
    if not ok:
        log.error("wifi enrollment verification failed for ssid=%s", ssid)
    return ok


def connect_wifi(ssid: str, password: str, dry_run: bool, timeout_sec: int) -> bool:
    """
    Join Wi‑Fi and leave a reboot-durable NM profile.

    - Always enroll a persistent profile (create/update + autoconnect) before ACK.
    - If already on this SSID: keep the live link, enroll/update the saved profile,
      then drop *other* profiles only after enrollment is verified.
    - Never treat "associated right now" as success without a saved profile.
    - On failure: restore the WIPE_NET backup so previous Wi‑Fi comes back.
    """
    if not ssid:
        return False
    ssid = ssid.strip()
    password = password or ""
    if dry_run:
        log.info("[dry-run] connect wifi ssid=%s", ssid)
        return True
    if not shutil.which("nmcli"):
        log.error("nmcli not found")
        return False

    active = current_wifi_ssid()
    if active == ssid and wifi_already_connected():
        log.info(
            "already associated to ssid=%s — ensuring persistent NM enrollment",
            ssid,
        )
        if ensure_persistent_wifi_enrollment(ssid, password, dry_run=False):
            wipe_wifi_profiles(False, keep_ssid=ssid)
            if wifi_enrollment_verified(ssid):
                # Association may briefly report down while NM reloads; require
                # either still connected or a saved profile we can bring up.
                if wifi_already_connected() or wait_for_wifi_connected(10.0):
                    log.info("wifi enroll OK (already associated) ssid=%s", ssid)
                    return True
                # Profile exists — try bringing it up explicitly.
                for name in find_wifi_profiles_for_ssid(ssid):
                    _run(
                        ["nmcli", "connection", "up", "id", name],
                        dry_run=False,
                        timeout=float(timeout_sec),
                    )
                    if wait_for_wifi_connected(WIFI_IP_WAIT_SEC):
                        log.info("wifi enroll OK after connection up ssid=%s", ssid)
                        return True
            log.error(
                "wifi profile lost after enroll/wipe for ssid=%s — not ACKing success",
                ssid,
            )
        else:
            log.warning(
                "persistent enroll failed while associated — falling through to reconnect"
            )

    # Snapshot again right before we risk changing association.
    backup_wifi_profiles(False)

    last_ok = False
    for attempt in range(1, WIFI_CONNECT_RETRIES + 1):
        log.info(
            "wifi connect attempt %d/%d ssid=%s",
            attempt,
            WIFI_CONNECT_RETRIES,
            ssid,
        )
        # Rescan helps after a prior wipe / radio settle.
        _run(["nmcli", "dev", "wifi", "rescan"], dry_run=False, timeout=20)
        time.sleep(1.0)
        if _nmcli_wifi_connect_once(ssid, password, timeout_sec):
            if wait_for_wifi_connected(WIFI_IP_WAIT_SEC):
                last_ok = True
                break
            log.warning("nmcli reported connect but Wi‑Fi not associated yet")
        if attempt < WIFI_CONNECT_RETRIES:
            time.sleep(WIFI_CONNECT_RETRY_DELAY_SEC)

    if last_ok:
        if not ensure_persistent_wifi_enrollment(ssid, password, dry_run=False):
            log.error("wifi associated but persistent enrollment failed for ssid=%s", ssid)
            restore_wifi_profiles(False)
            return False
        # Only now drop other profiles — never before the new link is proven.
        wipe_wifi_profiles(False, keep_ssid=ssid)
        if not wifi_enrollment_verified(ssid):
            log.error(
                "wifi profile missing after wipe for ssid=%s — restoring backup",
                ssid,
            )
            restore_wifi_profiles(False)
            return False
        log.info("wifi join OK for ssid=%s", ssid)
        return True

    log.error("wifi connect failed for ssid=%s — restoring previous profiles", ssid)
    restore_wifi_profiles(False)
    return False


def backup_wifi_profiles(dry_run: bool) -> bool:
    """Copy current NM Wi‑Fi connection files so we can restore after a failed join."""
    if dry_run:
        log.info("[dry-run] backup wifi profiles → %s", WIFI_BACKUP_DIR)
        return True
    try:
        if os.path.isdir(WIFI_BACKUP_DIR):
            shutil.rmtree(WIFI_BACKUP_DIR)
        os.makedirs(WIFI_BACKUP_DIR, mode=0o700, exist_ok=True)
        if not os.path.isdir(NM_CONNECTIONS_DIR):
            log.warning("NM connections dir missing: %s", NM_CONNECTIONS_DIR)
            return True
        copied = 0
        for name in os.listdir(NM_CONNECTIONS_DIR):
            src = os.path.join(NM_CONNECTIONS_DIR, name)
            if not os.path.isfile(src):
                continue
            # Prefer wifi profiles; also keep anything that looks like a saved AP.
            # Reading secrets requires root (daemon runs as root).
            try:
                with open(src, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError as exc:
                log.warning("skip backup %s: %s", name, exc)
                continue
            if "wifi" not in body.lower() and "802-11-wireless" not in body:
                continue
            dest = os.path.join(WIFI_BACKUP_DIR, name)
            shutil.copy2(src, dest)
            os.chmod(dest, 0o600)
            copied += 1
        log.info("backed up %d Wi‑Fi profile(s) → %s", copied, WIFI_BACKUP_DIR)
        return True
    except OSError as exc:
        log.error("wifi profile backup failed: %s", exc)
        return False


def restore_wifi_profiles(dry_run: bool) -> bool:
    """Restore Wi‑Fi profiles from the last WIPE_NET backup and ask NM to reload."""
    if dry_run:
        log.info("[dry-run] restore wifi profiles from %s", WIFI_BACKUP_DIR)
        return True
    if not os.path.isdir(WIFI_BACKUP_DIR):
        log.warning("no wifi backup dir at %s — cannot restore", WIFI_BACKUP_DIR)
        return False
    try:
        os.makedirs(NM_CONNECTIONS_DIR, mode=0o700, exist_ok=True)
        restored = 0
        for name in os.listdir(WIFI_BACKUP_DIR):
            src = os.path.join(WIFI_BACKUP_DIR, name)
            if not os.path.isfile(src):
                continue
            dest = os.path.join(NM_CONNECTIONS_DIR, name)
            shutil.copy2(src, dest)
            os.chmod(dest, 0o600)
            restored += 1
        if restored == 0:
            log.warning("wifi backup dir empty — nothing to restore")
            return False
        _run(["nmcli", "connection", "reload"], dry_run=False, timeout=30)
        # Bring up any restored wifi profile that can autoconnect.
        code, out, _ = _run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            dry_run=False,
            timeout=15,
        )
        if code == 0:
            for line in out.splitlines():
                name, _, ctype = line.partition(":")
                if ctype not in ("802-11-wireless", "wifi") or not name:
                    continue
                c2, _, e2 = _run(
                    ["nmcli", "connection", "up", "id", name],
                    dry_run=False,
                    timeout=45,
                )
                if c2 == 0:
                    log.info("restored + activated Wi‑Fi profile %s", name)
                    break
                log.warning("could not activate restored profile %s: %s", name, e2)
        ok = wait_for_wifi_connected(WIFI_IP_WAIT_SEC)
        log.info(
            "restored %d Wi‑Fi profile(s); connected=%s", restored, ok
        )
        return ok or restored > 0
    except OSError as exc:
        log.error("wifi profile restore failed: %s", exc)
        return False


def wipe_wifi_profiles(dry_run: bool, *, keep_ssid: str = "") -> bool:
    """
    Delete NetworkManager Wi‑Fi profiles (optionally keep one SSID).

    Safety rules when ``keep_ssid`` is set (WIFI_CONFIG path):
    - Resolve each profile's SSID with per-connection ``nmcli -g`` (list-mode
      ``802-11-wireless.ssid`` is invalid on many NM builds).
    - Never delete the live/active Wi‑Fi connection.
    - Never delete a profile whose SSID cannot be resolved (refuse to guess).
    """
    if dry_run:
        log.info("[dry-run] wipe wifi profiles (keep_ssid=%r)", keep_ssid)
        return True
    if not shutil.which("nmcli"):
        log.error("nmcli not found")
        return False

    active_ids = _active_wifi_connection_ids() if keep_ssid else set()
    ok = True
    for name, profile_ssid in list_wifi_profiles():
        if keep_ssid and (name == keep_ssid or profile_ssid == keep_ssid):
            log.info(
                "keeping Wi‑Fi profile %s (ssid=%s)",
                name,
                profile_ssid or keep_ssid,
            )
            continue
        if keep_ssid and name in active_ids:
            log.warning(
                "keeping active Wi‑Fi profile %s (ssid=%r) — refuse to drop live link",
                name,
                profile_ssid,
            )
            continue
        if keep_ssid and not profile_ssid and name != keep_ssid:
            log.warning(
                "keeping Wi‑Fi profile %s (ssid unresolved; refuse to guess)",
                name,
            )
            continue
        c2, _, e2 = _run(
            ["nmcli", "connection", "delete", "id", name],
            dry_run=False,
        )
        if c2 != 0:
            log.error("delete %s failed: %s", name, e2)
            ok = False
    return ok


def _nmcli_wifi_connect_once(ssid: str, password: str, timeout_sec: int) -> bool:
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    code, out, err = _run(cmd, dry_run=False, timeout=float(timeout_sec))
    if code == 0:
        log.info("wifi connected: %s", out.strip() or "ok")
        return True
    log.error("wifi connect attempt failed: %s %s", out, err)
    return False


def ensure_wifi_autoconnect(ssid: str, dry_run: bool) -> None:
    """Mark every NM profile for this SSID to connect automatically on boot."""
    if dry_run or not ssid or not shutil.which("nmcli"):
        return
    names = find_wifi_profiles_for_ssid(ssid)
    if not names:
        log.warning("wifi autoconnect: no profile found for ssid=%s", ssid)
        return
    for name in names:
        code, _, err = _run(
            [
                "nmcli",
                "connection",
                "modify",
                "id",
                name,
                "connection.autoconnect",
                "yes",
                "connection.autoconnect-priority",
                "100",
            ],
            dry_run=False,
            timeout=15,
        )
        if code != 0:
            log.warning("wifi autoconnect set failed for profile %s: %s", name, err)
        else:
            log.info("wifi autoconnect enabled for profile %s (ssid=%s)", name, ssid)


def ensure_tailscaled_service(dry_run: bool) -> bool:
    """Ensure tailscaled is enabled/started without boot-time daemon-reload.

    ``systemctl enable`` on an already-enabled unit still triggers a systemd
    reload on this image and stalls NetworkManager (~seconds). Only enable
    when needed; only start when inactive.
    """
    if dry_run:
        log.info("[dry-run] would ensure tailscaled enabled+started")
        return True
    if not shutil.which("systemctl"):
        log.warning("systemctl not found — cannot ensure tailscaled on boot")
        return False

    code, out, err = _run(
        ["systemctl", "is-enabled", "tailscaled.service"],
        dry_run=False,
        timeout=10,
    )
    enabled = out.strip() == "enabled"
    # is-enabled returns 1 for disabled; still parse stdout.
    if not enabled:
        code, _, err = _run(
            ["systemctl", "enable", "tailscaled.service"],
            dry_run=False,
            timeout=30,
        )
        if code != 0:
            log.warning("systemctl enable tailscaled.service failed: %s", err)
            return False

    code, out, _ = _run(
        ["systemctl", "is-active", "tailscaled.service"],
        dry_run=False,
        timeout=10,
    )
    if out.strip() == "active":
        return True
    code, _, err = _run(
        ["systemctl", "start", "tailscaled.service"],
        dry_run=False,
        timeout=30,
    )
    if code != 0:
        log.warning("systemctl start tailscaled.service failed: %s", err)
        return False
    return True


def ensure_tailscale_enabled(
    dry_run: bool,
    *,
    authkey: Optional[str] = None,
    advertise_tag: str = "tag:rover",
) -> bool:
    """
    Bring Tailscale to Running and leave it that way across reboots.

    - Ensures tailscaled is enabled/started (systemd)
    - If already Running: success
    - If logged-in but stopped/needs reconnect: `tailscale up` (no browser)
    - If authkey given and not joined: join with tagged auth key
    """
    ensure_tailscaled_service(dry_run)
    if dry_run:
        log.info("[dry-run] would ensure Tailscale enabled")
        return True
    if not shutil.which("tailscale"):
        log.error("tailscale not found")
        return False

    if tailscale_is_connected(dry_run):
        log.info("Tailscale already Running — enabled")
        return True

    st = _tailscale_status_json(False) or {}
    backend = str(st.get("BackendState") or "")
    log.info("Tailscale BackendState=%s — bringing up", backend or "?")

    if authkey and backend in ("", "NeedsLogin", "NoState"):
        log.info("joining via auth key from ensure_tailscale_enabled")
        cmd = [
            "tailscale",
            "up",
            "--authkey",
            authkey,
            "--reset",
            "--timeout=60s",
            "--accept-dns=true",
        ]
        if advertise_tag:
            cmd.append(f"--advertise-tags={advertise_tag}")
        code, out, err = _run(cmd, dry_run=False, timeout=70)
        if code == 0 or tailscale_is_connected(False):
            log.info("Tailscale joined + enabled")
            return True
        log.error("auth-key join failed: %s %s", out, err)
        return False

    # Already has node state (or Stopped) — resume without a new login URL.
    cmd = ["tailscale", "up", "--timeout=60s", "--accept-dns=true"]
    if advertise_tag:
        cmd += [f"--advertise-tags={advertise_tag}"]
    code, out, err = _run(cmd, dry_run=False, timeout=70)
    if code == 0 or tailscale_is_connected(False):
        log.info("Tailscale enabled (up OK)")
        return True
    log.error("tailscale up failed: %s %s", out, err)
    return False


def tailscale_logout(dry_run: bool) -> None:
    log.info("logging out of Tailscale (clear node identity)")
    _run(["tailscale", "logout"], dry_run=dry_run, timeout=30)


def begin_setup_clean_slate(dry_run: bool) -> bool:
    """
    Prepare for ESP onboarding / re-pair WITHOUT dropping live connectivity.

    Critical safety invariant: never delete the active Wi‑Fi profile or log out
    of Tailscale until the new Wi‑Fi join is proven. Doing those first left the
    Pi unreachable (no LAN, no MagicDNS) when WIFI_CONFIG reconnect failed —
    Ethernet-only recovery.

    WIPE_NET now only snapshots NM Wi‑Fi profiles. Actual cleanup:
      - other Wi‑Fi profiles deleted after successful connect_wifi()
      - Tailscale logout deferred to prepare_tailscale_rejoin() after Wi‑Fi OK
    """
    log.info(
        "setup prepare: backup Wi‑Fi only (keep live Wi‑Fi + Tailscale until new join OK)"
    )
    return backup_wifi_profiles(dry_run)


def prepare_tailscale_rejoin(dry_run: bool) -> None:
    """
    Drop the previous Tailscale node identity only after Wi‑Fi is confirmed.

    Called immediately before auth-key join so a failed Wi‑Fi step never leaves
    the device logged out of the tailnet while also offline on Wi‑Fi.
    """
    if dry_run:
        log.info("[dry-run] would logout Tailscale for clean rejoin")
        return
    if not tailscale_is_connected(False) and not tailscale_already_up(False):
        st = _tailscale_status_json(False) or {}
        backend = str(st.get("BackendState") or "")
        if backend in ("NeedsLogin", "NoState", ""):
            log.info("Tailscale already logged out (BackendState=%s) — skip logout", backend or "?")
            return
    log.info("Wi‑Fi OK — logging out Tailscale for clean rejoin")
    tailscale_logout(dry_run)


def join_tailscale_with_authkey(
    dry_run: bool,
    authkey: str,
    advertise_tag: str = "tag:rover",
) -> bool:
    """
    Wait for internet, logout once, then auth-key join with retries.

    Returns True on success. On failure Wi‑Fi is left intact (caller stays
    reachable on LAN for another pairing attempt).
    """
    if not authkey:
        return False
    if dry_run:
        log.info("[dry-run] would join Tailscale with auth key")
        return True

    wait_for_internet(INTERNET_WAIT_SEC)
    prepare_tailscale_rejoin(dry_run)

    for attempt in range(1, TAILSCALE_JOIN_RETRIES + 1):
        log.info(
            "Tailscale auth-key join attempt %d/%d",
            attempt,
            TAILSCALE_JOIN_RETRIES,
        )
        if attempt > 1:
            wait_for_internet(20.0)
        url = fetch_tailscale_auth_url(
            dry_run,
            authkey=authkey,
            advertise_tag=advertise_tag,
        )
        if url is None and (
            tailscale_is_connected(False) or tailscale_already_up(False)
        ):
            log.info("Tailscale auth-key join succeeded")
            return True
        if url is None:
            # fetch returned None but not connected — treat as failure and retry
            log.warning("auth-key join returned no URL but Tailscale not Running yet")
        else:
            log.warning("auth-key join failed (detail=%s)", url)
        if attempt < TAILSCALE_JOIN_RETRIES:
            time.sleep(TAILSCALE_JOIN_RETRY_DELAY_SEC)

    log.error("Tailscale auth-key join failed after %d attempts", TAILSCALE_JOIN_RETRIES)
    return False


def _tailscale_status_json(dry_run: bool) -> Optional[dict]:
    if dry_run:
        return {"BackendState": "Running", "Self": {"ID": "dryrun"}}
    code, out, _ = _run(["tailscale", "status", "--json"], dry_run=False, timeout=15)
    if code != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def tailscale_already_up(dry_run: bool) -> bool:
    st = _tailscale_status_json(dry_run)
    if not st:
        return False
    return st.get("BackendState") == "Running"


def fetch_tailscale_auth_url(
    dry_run: bool,
    authkey: Optional[str] = None,
    advertise_tag: str = "tag:rover",
) -> Optional[str]:
    """
    Bring Tailscale up and leave it enabled (persists via tailscaled state).

    Returns:
      - auth URL string if interactive SSO is required
      - None if already connected / authkey join succeeded (no browser step)
      - "__AUTHKEY_FAILED__" if phone-supplied key join failed
    """
    ensure_tailscaled_service(dry_run)
    if dry_run:
        if authkey:
            log.info("[dry-run] would join with auth key (no SSO URL)")
            return None
        return "https://login.tailscale.com/a/DRYRUN"
    if not shutil.which("tailscale"):
        log.error("tailscale not found")
        return None

    if tailscale_already_up(dry_run):
        log.info("tailscale already up")
        ensure_tailscale_enabled(dry_run, advertise_tag=advertise_tag)
        return None

    # Preferred: tagged auth key → no browser; tagged devices default to no key expiry
    if authkey:
        log.info("joining tailnet with auth key (non-interactive)")
        cmd = [
            "tailscale",
            "up",
            "--authkey",
            authkey,
            "--reset",
            "--timeout=60s",
            "--accept-dns=true",
        ]
        if advertise_tag:
            cmd.append(f"--advertise-tags={advertise_tag}")
        code, out, err = _run(cmd, dry_run=False, timeout=70)
        if code == 0 or tailscale_already_up(False) or tailscale_is_connected(False):
            log.info("auth-key join OK — ensuring enabled on boot")
            ensure_tailscale_enabled(dry_run, advertise_tag=advertise_tag)
            return None
        # Never fall back to SSO URL when the phone supplied a key — report failure.
        log.error("auth-key join failed: %s %s (no SSO fallback)", out, err)
        return "__AUTHKEY_FAILED__"

    log.info("starting tailscale up (interactive SSO URL)")
    up_cmd = ["tailscale", "up", "--reset", "--timeout=60s", "--accept-dns=true"]
    if advertise_tag:
        up_cmd.append(f"--advertise-tags={advertise_tag}")
    try:
        proc = subprocess.Popen(
            up_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        return None

    url: Optional[str] = None
    assert proc.stdout is not None
    start = time.time()
    buf = ""
    while time.time() - start < 70:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if line:
            buf += line
            log.info("tailscale: %s", line.strip())
            m = AUTH_URL_RE.search(line)
            if m:
                url = m.group(0).rstrip(".,)")
                break
    if url is None:
        m = AUTH_URL_RE.search(buf)
        if m:
            url = m.group(0).rstrip(".,)")
    return url


def tailscale_is_connected(dry_run: bool) -> bool:
    if dry_run:
        return True
    st = _tailscale_status_json(False)
    if st and st.get("BackendState") == "Running":
        return True
    code, out, _ = _run(["tailscale", "status"], dry_run=False, timeout=15)
    if code != 0 or not out.strip():
        return False
    low = out.lower()
    if "logged out" in low:
        return False
    if "stopped" in low and "relay" not in low:
        return False
    return "100." in out or "online" in low


def tailscale_magic_dns(dry_run: bool) -> str:
    """
    Return MagicDNS name for this node, e.g. 'rover.tailnet.ts.net'.
    Empty string if unavailable.
    """
    if dry_run:
        return "rover-dryrun.tailnet.ts.net"
    st = _tailscale_status_json(False)
    if not st:
        return ""
    self_node = st.get("Self") or {}
    dns = str(self_node.get("DNSName") or "").strip()
    # Tailscale often appends a trailing dot
    dns = dns.rstrip(".")
    if dns:
        log.info("MagicDNS name: %s", dns)
    else:
        log.warning("no DNSName in tailscale status")
    return dns


def ensure_tailscale_key_never_expires(
    dry_run: bool,
    api_key: Optional[str],
    tailnet: Optional[str],
) -> bool:
    """
    Disable Tailscale *node key* expiry for this device via Admin API.

    Without this (or a tagged device), keys expire (~180 days by default) and
    the rover drops off the tailnet until someone re-auths — bad for customers.

    Docs: https://tailscale.com/docs/features/access-control/key-expiry
    """
    if dry_run:
        log.info("[dry-run] would disable Tailscale key expiry via API")
        return True
    if not api_key:
        log.warning(
            "ROVER_TAILSCALE_API_KEY not set — cannot auto-disable key expiry. "
            "Disable manually: Admin → Machines → ⋮ → Disable key expiry "
            "(or use a tagged auth key)."
        )
        return False

    st = _tailscale_status_json(False)
    if not st:
        log.error("cannot read tailscale status --json")
        return False
    self_node = st.get("Self") or {}
    node_id = self_node.get("ID") or self_node.get("StableID")
    if not node_id:
        log.error("no Self.ID in status JSON; cannot disable expiry via API")
        return False

    if _post_disable_expiry(api_key, str(node_id)):
        return True
    if tailnet:
        return _disable_expiry_via_device_list(api_key, tailnet, self_node)
    return False


def _post_disable_expiry(api_key: str, device_id: str) -> bool:
    url = f"https://api.tailscale.com/api/v2/device/{device_id}/key"
    body = json.dumps({"keyExpiryDisabled": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            log.info(
                "Tailscale key expiry DISABLED for device %s (%s)",
                device_id,
                raw[:200],
            )
            return True
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        log.error("Tailscale API disable-expiry HTTP %s: %s", exc.code, err)
        return False
    except Exception as exc:
        log.error("Tailscale API disable-expiry error: %s", exc)
        return False


def _disable_expiry_via_device_list(
    api_key: str, tailnet: str, self_node: dict
) -> bool:
    """Match this host in GET /tailnet/{tailnet}/devices then disable expiry."""
    list_url = f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices"
    req = urllib.request.Request(
        list_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.error("device list failed: %s", exc)
        return False

    host_name = (self_node.get("HostName") or self_node.get("DNSName") or "").split(
        "."
    )[0]
    dns = self_node.get("DNSName") or ""
    ips = set(self_node.get("TailscaleIPs") or [])

    device_id = None
    for d in data.get("devices") or []:
        if d.get("id") and (
            d.get("hostname") == host_name
            or d.get("name") == dns
            or ips.intersection(set(d.get("addresses") or []))
        ):
            device_id = d["id"]
            break
    if not device_id:
        log.error("could not match this node in tailnet device list")
        return False
    return _post_disable_expiry(api_key, device_id)


def factory_reset_network(dry_run: bool) -> None:
    tailscale_logout(dry_run)
    wipe_wifi_profiles(dry_run)
    if not dry_run and shutil.which("nmcli"):
        _run(["nmcli", "networking", "off"], dry_run=False, timeout=10)
        _run(["nmcli", "networking", "on"], dry_run=False, timeout=10)


def request_shutdown(dry_run: bool) -> None:
    log.warning("requesting OS shutdown")
    _run(["sudo", "shutdown", "now"], dry_run=dry_run, timeout=10)


# ---------- HTTPS certs for MagicDNS (MediaMTX / control-server) ----------

DEFAULT_CERT_DIR = "/var/lib/rover/certs"


def _cert_paths(cert_dir: str) -> Tuple[str, str]:
    return (
        os.path.join(cert_dir, "cert.crt"),
        os.path.join(cert_dir, "cert.key"),
    )


def _openssl_days_remaining(cert_path: str) -> Optional[int]:
    """Approximate days until notAfter; None if unreadable; -1 if expired."""
    if not os.path.isfile(cert_path):
        return None
    code, _, _ = _run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-checkend", "0"],
        dry_run=False,
        timeout=10,
    )
    if code != 0:
        return -1
    for days in (90, 60, 45, 30, 21, 14, 7, 3, 1):
        code_d, _, _ = _run(
            [
                "openssl",
                "x509",
                "-in",
                cert_path,
                "-noout",
                "-checkend",
                str(days * 86400),
            ],
            dry_run=False,
            timeout=10,
        )
        if code_d != 0:
            return days
    return 120


def _cert_matches_host(cert_path: str, dns: str) -> bool:
    if not dns or not os.path.isfile(cert_path):
        return False
    code, _, _ = _run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-checkhost", dns],
        dry_run=False,
        timeout=10,
    )
    if code == 0:
        return True
    # Older openssl without -checkhost: fall back to subject/SAN text
    code, out, _ = _run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-text"],
        dry_run=False,
        timeout=10,
    )
    return code == 0 and dns.lower() in out.lower()


def https_cert_needs_renewal(
    cert_dir: str,
    dns: str,
    renew_within_days: int = 21,
) -> bool:
    crt, key = _cert_paths(cert_dir)
    if not os.path.isfile(crt) or not os.path.isfile(key):
        return True
    if not _cert_matches_host(crt, dns):
        log.info("cert does not match MagicDNS %s — will reissue", dns)
        return True
    days = _openssl_days_remaining(crt)
    if days is None:
        return True
    if days < 0:
        log.warning("HTTPS cert already expired")
        return True
    if days <= renew_within_days:
        log.info("HTTPS cert renews soon (%s days left)", days)
        return True
    return False


def _mirror_certs_for_legacy_compose(cert_dir: str) -> None:
    """Keep docker-compose ./certs in sync when the rover tree is present."""
    candidates = [
        "/home/mango/rover/certs",
        "/opt/rover-server/certs",
    ]
    crt, key = _cert_paths(cert_dir)
    for dest_dir in candidates:
        parent = os.path.dirname(dest_dir)
        if not os.path.isdir(parent):
            continue
        try:
            os.makedirs(dest_dir, mode=0o755, exist_ok=True)
            for src, name in ((crt, "cert.crt"), (key, "cert.key")):
                dest = os.path.join(dest_dir, name)
                shutil.copy2(src, dest)
                os.chmod(dest, 0o644 if name.endswith(".crt") else 0o640)
            log.info("mirrored TLS certs → %s", dest_dir)
        except OSError as exc:
            log.warning("could not mirror certs to %s: %s", dest_dir, exc)


def notify_tls_consumers(dry_run: bool) -> None:
    """Best-effort reload of services that mount the certs."""
    units = [
        "rover-mediamtx.service",
        "rover-control.service",
        "mediamtx.service",
    ]
    for unit in units:
        _run(["systemctl", "try-reload-or-restart", unit], dry_run=dry_run, timeout=30)
    # Legacy docker compose stack (if present / running)
    compose_dir = "/home/mango/rover"
    compose_file = os.path.join(compose_dir, "docker-compose.yml")
    if os.path.isfile(compose_file) and shutil.which("docker"):
        # Only recreate if those containers exist / compose project is active.
        code, out, _ = _run(
            ["docker", "ps", "-q", "--filter", "name=mediamtx"],
            dry_run=False,
            timeout=15,
        )
        if code == 0 and out.strip():
            _run(
                [
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "up",
                    "-d",
                    "--force-recreate",
                    "mediamtx",
                    "control_server",
                ],
                dry_run=dry_run,
                timeout=180,
            )


def ensure_https_certs(
    dry_run: bool,
    cert_dir: str = DEFAULT_CERT_DIR,
    renew_within_days: int = 21,
    dns: Optional[str] = None,
) -> bool:
    """
    Issue/renew Tailscale HTTPS (Let's Encrypt) certs for this node's MagicDNS.

    Certs expire ~90 days; call this on NORMAL_OPERATION and from a weekly timer.
    Node *key* expiry is separate (see ensure_tailscale_key_never_expires).
    """
    if dry_run:
        log.info("[dry-run] would ensure HTTPS certs in %s", cert_dir)
        return True
    if not shutil.which("tailscale"):
        log.error("tailscale CLI missing — cannot issue HTTPS certs")
        return False
    if not shutil.which("openssl"):
        log.warning("openssl missing — host/expiry checks may be limited")

    name = (dns or tailscale_magic_dns(False) or "").strip().rstrip(".")
    if not name:
        log.warning("no MagicDNS yet — skip HTTPS cert ensure")
        return False

    os.makedirs(cert_dir, mode=0o755, exist_ok=True)
    crt, key = _cert_paths(cert_dir)

    if not https_cert_needs_renewal(cert_dir, name, renew_within_days):
        log.info("HTTPS certs OK for %s (dir=%s)", name, cert_dir)
        _mirror_certs_for_legacy_compose(cert_dir)
        return True

    log.info("issuing/renewing Tailscale HTTPS cert for %s → %s", name, cert_dir)
    code, out, err = _run(
        [
            "tailscale",
            "cert",
            "--cert-file",
            crt,
            "--key-file",
            key,
            name,
        ],
        dry_run=False,
        timeout=120,
    )
    if code != 0:
        log.error("tailscale cert failed (%s): %s %s", code, out, err)
        return False

    try:
        os.chmod(crt, 0o644)
        os.chmod(key, 0o640)
    except OSError:
        pass

    _mirror_certs_for_legacy_compose(cert_dir)
    notify_tls_consumers(False)
    log.info("HTTPS certs ready: %s , %s", crt, key)
    return True

