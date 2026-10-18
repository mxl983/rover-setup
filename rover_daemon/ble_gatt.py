"""BlueZ GATT peripheral for ESP setup (J2-Pi).

Service UUID:   6a2b0001-b5a3-f393-e0a9-e50e24dcca9e
Write (ESP→Pi): 6a2b0002-b5a3-f393-e0a9-e50e24dcca9e
Notify (Pi→ESP): 6a2b0003-b5a3-f393-e0a9-e50e24dcca9e

Requires: ``sudo apt install python3-dbus python3-gi`` (system-site-packages venv).
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from typing import Callable, Optional

log = logging.getLogger("rover.ble")

SERVICE_UUID = "6a2b0001-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_UUID = "6a2b0002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_UUID = "6a2b0003-b5a3-f393-e0a9-e50e24dcca9e"

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"


def _require_dbus() -> None:
    try:
        import dbus  # noqa: F401
        import dbus.mainloop.glib  # noqa: F401
        import dbus.service  # noqa: F401
        from gi.repository import GLib  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "BLE needs python3-dbus + python3-gi "
            "(sudo apt install python3-dbus python3-gi; venv --system-site-packages)"
        ) from exc


class BleGattServer:
    """Advertise as ``local_name`` (must start with J2-Pi) and exchange JSON lines."""

    def __init__(
        self,
        local_name: str = "J2-Pi",
        on_line: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not local_name.startswith("J2-Pi"):
            raise ValueError("BLE local name must start with 'J2-Pi'")
        self.local_name = local_name
        self._on_line = on_line
        self._rx_queue: queue.Queue[str] = queue.Queue()
        self._rx_buf = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._mainloop = None
        self._notify_chr = None
        self._adapter_path: Optional[str] = None
        self._app = None
        self._adv = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None

    def start(self) -> None:
        _require_dbus()
        if self._thread and self._thread.is_alive():
            return
        self._prepare_adapter()
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run_loop, name="rover-ble-gatt", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=25.0):
            raise RuntimeError(
                f"BLE GATT failed to start: {self._error or 'timeout'}"
            )
        if self._error is not None:
            raise RuntimeError(f"BLE GATT failed to start: {self._error}")

    def stop(self) -> None:
        self._stop.set()
        if self._mainloop is not None:
            try:
                self._mainloop.quit()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None

    def send_line(self, data: bytes) -> None:
        if self._notify_chr is None:
            return
        payload = bytes(data)
        if not payload.endswith(b"\n"):
            payload += b"\n"
        try:
            from gi.repository import GLib

            GLib.idle_add(self._notify_chr.send_notification, payload)
        except Exception as exc:
            log.error("BLE notify failed: %s", exc)

    def pop_lines(self, max_items: int = 32) -> list[str]:
        out: list[str] = []
        while len(out) < max_items:
            try:
                out.append(self._rx_queue.get_nowait())
            except queue.Empty:
                break
        return out

    @staticmethod
    def _prepare_adapter() -> None:
        for cmd in (
            ["rfkill", "unblock", "bluetooth"],
            ["bluetoothctl", "power", "on"],
        ):
            try:
                subprocess.run(
                    cmd,
                    check=False,
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    def _handle_write(self, value: bytes) -> None:
        try:
            text = bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return
        self._rx_buf += text
        while "\n" in self._rx_buf:
            line, self._rx_buf = self._rx_buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            self._rx_queue.put(line)
            if self._on_line:
                try:
                    self._on_line(line)
                except Exception:
                    log.exception("on_line handler failed")

    def _run_loop(self) -> None:
        import dbus
        import dbus.mainloop.glib
        import dbus.service
        from gi.repository import GLib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()

        class Application(dbus.service.Object):
            def __init__(inner):
                inner.path = "/org/rover/ble/app"
                inner.services = []
                dbus.service.Object.__init__(inner, bus, inner.path)
                svc = Service(0)
                write_chr = WriteCharacteristic(0, svc)
                notify_chr = NotifyCharacteristic(1, svc)
                svc.add_characteristic(write_chr)
                svc.add_characteristic(notify_chr)
                inner.services.append(svc)
                inner.notify_chr = notify_chr

            def get_path(inner):
                return dbus.ObjectPath(inner.path)

            @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
            def GetManagedObjects(inner):
                response = {}
                for svc in inner.services:
                    response[svc.get_path()] = svc.get_properties()
                    for chrc in svc.characteristics:
                        response[chrc.get_path()] = chrc.get_properties()
                return response

        class Service(dbus.service.Object):
            PATH_BASE = "/org/rover/ble/service"

            def __init__(inner, index: int):
                inner.path = inner.PATH_BASE + str(index)
                inner.uuid = SERVICE_UUID
                inner.primary = True
                inner.characteristics = []
                dbus.service.Object.__init__(inner, bus, inner.path)

            def get_properties(inner):
                return {
                    GATT_SERVICE_IFACE: {
                        "UUID": inner.uuid,
                        "Primary": inner.primary,
                        "Characteristics": dbus.Array(
                            [c.get_path() for c in inner.characteristics],
                            signature="o",
                        ),
                    }
                }

            def get_path(inner):
                return dbus.ObjectPath(inner.path)

            def add_characteristic(inner, chrc) -> None:
                inner.characteristics.append(chrc)

        class Characteristic(dbus.service.Object):
            def __init__(inner, index: int, service, uuid: str, flags):
                inner.path = service.path + "/char" + str(index)
                inner.uuid = uuid
                inner.service = service
                inner.flags = flags
                inner.notifying = False
                dbus.service.Object.__init__(inner, bus, inner.path)

            def get_properties(inner):
                return {
                    GATT_CHRC_IFACE: {
                        "Service": inner.service.get_path(),
                        "UUID": inner.uuid,
                        "Flags": inner.flags,
                    }
                }

            def get_path(inner):
                return dbus.ObjectPath(inner.path)

            @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
            def GetAll(inner, interface):
                if interface != GATT_CHRC_IFACE:
                    raise dbus.exceptions.DBusException(
                        "org.freedesktop.DBus.Error.InvalidArgs"
                    )
                return inner.get_properties()[GATT_CHRC_IFACE]

        class WriteCharacteristic(Characteristic):
            def __init__(inner, index: int, service):
                super().__init__(
                    index, service, WRITE_UUID, ["write", "write-without-response"]
                )

            @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
            def WriteValue(inner, value, options):
                self._handle_write(bytes(value))

        class NotifyCharacteristic(Characteristic):
            def __init__(inner, index: int, service):
                super().__init__(index, service, NOTIFY_UUID, ["notify"])

            @dbus.service.method(GATT_CHRC_IFACE)
            def StartNotify(inner):
                if inner.notifying:
                    return
                inner.notifying = True
                log.info("ESP subscribed to BLE notify")

            @dbus.service.method(GATT_CHRC_IFACE)
            def StopNotify(inner):
                if not inner.notifying:
                    return
                inner.notifying = False
                log.info("ESP unsubscribed from BLE notify")

            @dbus.service.signal(DBUS_PROP_IFACE, signature="sa{sv}as")
            def PropertiesChanged(inner, interface, changed, invalidated):
                pass

            def send_notification(inner, payload: bytes) -> bool:
                arr = dbus.Array([dbus.Byte(b) for b in payload], signature="y")
                try:
                    inner.PropertiesChanged(GATT_CHRC_IFACE, {"Value": arr}, [])
                except Exception as exc:
                    log.error("PropertiesChanged notify failed: %s", exc)
                return False

        class Advertisement(dbus.service.Object):
            PATH_BASE = "/org/rover/ble/advertisement"

            def __init__(inner, index: int):
                inner.path = inner.PATH_BASE + str(index)
                inner.ad_type = "peripheral"
                inner.service_uuids = [SERVICE_UUID]
                inner.local_name = self.local_name
                inner.include_tx_power = True
                dbus.service.Object.__init__(inner, bus, inner.path)

            def get_properties(inner):
                return {
                    LE_ADVERTISEMENT_IFACE: {
                        "Type": inner.ad_type,
                        "ServiceUUIDs": dbus.Array(
                            inner.service_uuids, signature="s"
                        ),
                        "LocalName": dbus.String(inner.local_name),
                        "IncludeTxPower": dbus.Boolean(inner.include_tx_power),
                    }
                }

            def get_path(inner):
                return dbus.ObjectPath(inner.path)

            @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
            def GetAll(inner, interface):
                if interface != LE_ADVERTISEMENT_IFACE:
                    raise dbus.exceptions.DBusException(
                        "org.freedesktop.DBus.Error.InvalidArgs"
                    )
                return inner.get_properties()[LE_ADVERTISEMENT_IFACE]

            @dbus.service.method(LE_ADVERTISEMENT_IFACE)
            def Release(inner):
                log.debug("Advertisement released")

        try:
            adapter_path = self._find_adapter(bus)
            self._adapter_path = adapter_path
            self._power_adapter(bus, adapter_path)

            app = Application()
            self._app = app
            self._notify_chr = app.notify_chr
            adv = Advertisement(0)
            self._adv = adv

            service_manager = dbus.Interface(
                bus.get_object(BLUEZ, adapter_path), GATT_MANAGER_IFACE
            )
            ad_manager = dbus.Interface(
                bus.get_object(BLUEZ, adapter_path), LE_ADVERTISING_MANAGER_IFACE
            )

            mainloop = GLib.MainLoop()
            self._mainloop = mainloop
            registered = {"app": False, "adv": False}

            def maybe_ready():
                if registered["app"] and registered["adv"]:
                    self._ready.set()

            def on_register_app(*_a):
                log.info("GATT application registered on %s", adapter_path)
                registered["app"] = True
                maybe_ready()

            def on_register_app_err(err):
                self._error = RuntimeError(f"RegisterApplication failed: {err}")
                log.error("%s", self._error)
                self._ready.set()
                mainloop.quit()

            def on_register_ad(*_a):
                log.info(
                    "BLE advertising as %r (service %s)",
                    self.local_name,
                    SERVICE_UUID,
                )
                registered["adv"] = True
                maybe_ready()

            def on_register_ad_err(err):
                self._error = RuntimeError(f"RegisterAdvertisement failed: {err}")
                log.error("%s", self._error)
                self._ready.set()
                mainloop.quit()

            service_manager.RegisterApplication(
                app.get_path(),
                {},
                reply_handler=on_register_app,
                error_handler=on_register_app_err,
            )
            ad_manager.RegisterAdvertisement(
                adv.get_path(),
                {},
                reply_handler=on_register_ad,
                error_handler=on_register_ad_err,
            )

            def poll_stop():
                if self._stop.is_set():
                    mainloop.quit()
                    return False
                return True

            GLib.timeout_add(200, poll_stop)
            mainloop.run()
        except Exception as exc:
            self._error = exc
            log.exception("BLE main loop error")
            self._ready.set()
        finally:
            try:
                if self._adapter_path and self._adv is not None:
                    ad_manager = dbus.Interface(
                        bus.get_object(BLUEZ, self._adapter_path),
                        LE_ADVERTISING_MANAGER_IFACE,
                    )
                    ad_manager.UnregisterAdvertisement(self._adv.get_path())
                    self._adv.remove_from_connection()
            except Exception:
                pass
            try:
                if self._adapter_path and self._app is not None:
                    service_manager = dbus.Interface(
                        bus.get_object(BLUEZ, self._adapter_path),
                        GATT_MANAGER_IFACE,
                    )
                    service_manager.UnregisterApplication(self._app.get_path())
            except Exception:
                pass
            log.info("BLE GATT stopped")

    @staticmethod
    def _find_adapter(bus) -> str:
        import dbus

        om = dbus.Interface(bus.get_object(BLUEZ, "/"), DBUS_OM_IFACE)
        objects = om.GetManagedObjects()
        for path, ifaces in objects.items():
            if GATT_MANAGER_IFACE in ifaces and ADAPTER_IFACE in ifaces:
                return str(path)
        raise RuntimeError("No BlueZ adapter with GATT manager found")

    @staticmethod
    def _power_adapter(bus, adapter_path: str) -> None:
        import dbus

        props = dbus.Interface(bus.get_object(BLUEZ, adapter_path), DBUS_PROP_IFACE)
        try:
            props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(1))
        except Exception as exc:
            log.warning("could not power adapter: %s", exc)
        try:
            props.Set(ADAPTER_IFACE, "Alias", dbus.String("J2-Pi"))
        except Exception:
            pass
