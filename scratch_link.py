#!/usr/bin/env python
import select
import struct

"""Scratch link on bluepy"""

import asyncio
import pathlib
import ssl
import websockets
import json
import base64

# for Bluetooth (e.g. Lego EV3)
import bluetooth

# for BLESession (e.g. BBC micro:bit)
from bluepy.btle import Scanner, UUID, Peripheral, DefaultDelegate
from bluepy.btle import BTLEDisconnectError

import threading
import time

# for logging
import logging
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
logger.setLevel(logging.INFO)
logger.addHandler(handler)
logger.propagate = False

class Session():
    """Base class for BTSession and BLESession"""
    def __init__(self, websocket, loop):
        self.websocket = websocket
        self.loop = loop
        self.lock = threading.RLock()
        self.notification = None

    async def recv_request(self):
        """
        Handle a request from Scratch through websocket.
        Return True when the sessino should end.
        """
        logger.debug("start recv_request")
        req = await self.websocket.recv()
        logger.debug(f"request: {req}")
        jsonreq = json.loads(req)
        if jsonreq['jsonrpc'] != '2.0':
            logger.error("error: jsonrpc versino is not 2.0")
            return
        jsonres = self.handle_request(jsonreq['method'], jsonreq['params'])
        if 'id' in jsonreq:
            jsonres['id'] = jsonreq['id']
        response = json.dumps(jsonres)
        logger.debug(f"response: {response}")
        await self.websocket.send(response)
        if self.end_request():
            return True
        return False

    def handle_request(self, method, params):
        """Default request handler"""
        logger.debug(f"default handle_request: {method}, {params}")

    def end_request(self):
        """
        Default callback at request end. This callback is required to
        allow other websocket usage out of the request handler.
        Return true when the session should end.
        """
        logger.debug("default end_request")
        return False

    def notify(self, method, params):
        """
        Notify BT/BLE device events to scratch.
        """
        logger.debug("start to notify")

        jsonn = { 'jsonrpc': "2.0", 'method': method }
        jsonn['params'] = params
        notification = json.dumps(jsonn)
        logger.debug(f"notification: {notification}")

        future = asyncio.run_coroutine_threadsafe(
            self.websocket.send(notification), self.loop)
        result = future.result()

    async def handle(self):
        logger.debug("start session hanlder")
        await self.recv_request()
        await asyncio.sleep(0.1)
        while True:
            if await self.recv_request():
                break
            logger.debug("in handle loop")

class BTSession(Session):
    """Manage a session for Bluetooth device"""

    INITIAL = 1
    DISCOVERY = 2
    DISCOVERY_COMPLETE = 3
    CONNECTED = 4
    DONE = 5

    # Split this into discovery thread and communication thread
    # discovery thread should auto-terminate

    class BTThread(threading.Thread):
        """
        Separated thread to control notifications to Scratch.
        It handles device discovery notification in DISCOVERY status
        and notifications from bluetooth devices in CONNECTED status.
        """

        class BTDiscoverer(bluetooth.DeviceDiscoverer):

            def __init__(self, major_class, minor_class):
                super().__init__()
                self.major_class = major_class
                self.minor_class = minor_class
                self.found_devices = {}
                self.done = False

            def pre_inquiry(self):
                self.done = False

            def device_discovered(self, address, device_class, rssi, name):
                logger.debug(f"Found device {name} addr={address} class={device_class} rssi={rssi}")
                major_class = (device_class & 0x1F00) >> 8
                minor_class = (device_class & 0xFF) >> 2
                if major_class == self.major_class and minor_class == self.minor_class:
                    self.found_devices[address] = (name, device_class, rssi)

            def inquiry_complete(self):
                self.done = True

        def __init__(self, session, major_device_class, minor_device_class):
            threading.Thread.__init__(self)
            self.session = session
            self.major_device_class = major_device_class
            self.minor_device_class = minor_device_class
            self.cancel_discovery = False
            self.ping_time = None

        def discover(self):
            discoverer = self.BTDiscoverer(self.major_device_class, self.minor_device_class)
            discoverer.find_devices(lookup_names=True)
            while self.session.status == self.session.DISCOVERY and not discoverer.done and not self.cancel_discovery:
                readable = select.select([discoverer], [], [], 0.5)[0]
                if discoverer in readable:
                    discoverer.process_event()
                    for addr, (device_name, device_class, rssi) in discoverer.found_devices.items():
                        logger.debug(f"notifying discovered {addr}: {device_name}")
                        params = {"rssi": rssi, 'peripheralId': addr, 'name': device_name.decode("utf-8")}
                        self.session.notify('didDiscoverPeripheral', params)
                    discoverer.found_devices.clear()

            if not discoverer.done:
                discoverer.cancel_inquiry()

        def run(self):
            while self.session.status != self.session.DONE:

                logger.debug("loop in BT thread")
                current_time = int(round(time.time()))

                if self.session.status == self.session.DISCOVERY and not self.cancel_discovery:
                    logger.debug("in discovery status:")
                    try:
                        self.discover()
                        self.ping_time = current_time + 5
                    finally:
                        self.session.status = self.session.DISCOVERY_COMPLETE

                elif self.session.status == self.session.CONNECTED:
                    logger.debug("in connected status:")
                    sock = self.session.sock
                    try:
                        ready = select.select([sock], [], [], 1)
                        if ready[0]:
                            header = sock.recv(2)
                            [msg_len] = struct.unpack("<H", header)
                            msg_data = sock.recv(msg_len)
                            data = header + msg_data
                            params = {'message': base64.standard_b64encode(data).decode('utf-8'), "encoding": "base64"}
                            self.session.notify('didReceiveMessage', params)
                            self.ping_time = current_time + 5

                    except Exception as e:
                            logger.error(e)
                            self.session.close()
                            break

                    # To avoid repeated lock by this single thread,
                    # yield CPU to other lock waiting threads.
                    time.sleep(0)
                else:
                    # Nothing to do:
                    time.sleep(1)

                # Terminate if we have lost websocket connection to Scratch (e.g. browser closed)
                if self.ping_time is None or self.ping_time <= current_time:
                    try:
                        self.session.notify('ping', {})
                        self.ping_time = current_time + 5
                    except Exception as e:
                        logger.error(e)
                        self.session.close()
                        break

    def __init__(self, websocket, loop):
        super().__init__(websocket, loop)
        self.status = self.INITIAL
        self.sock = None
        self.bt_thread = None

    def close(self):
        self.status = self.DONE
        if self.sock:
            logger.info(f"disconnect to BT socket: {self.sock}")
            self.sock.close()

    def __del__(self):
        self.close()

    def handle_request(self, method, params):
        """Handle requests from Scratch"""
        logger.debug("handle request to BT device")
        logger.debug(method)
        if len(params) > 0:
            logger.debug(params)

        res = { "jsonrpc": "2.0" }

        if self.status == self.INITIAL and method == 'discover':
            logger.debug("Starting async discovery")
            self.status = self.DISCOVERY
            self.bt_thread = self.BTThread(self, params["majorDeviceClass"], params["minorDeviceClass"])
            self.bt_thread.start()
            res["result"] = None

        elif self.status in [self.DISCOVERY, self.DISCOVERY_COMPLETE] and method == 'connect':

            # Cancel discovery
            while self.status == self.DISCOVERY:
                logger.debug("Cancelling discovery")
                self.bt_thread.cancel_discovery = True
                time.sleep(1)

            addr = params['peripheralId']
            logger.debug(f"connecting to the BT device {addr}")
            try:
                self.sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
                self.sock.connect((addr, 1))
                logger.info(f"connected to BT device: {addr}")
            except bluetooth.BluetoothError as e:
                logger.error(f"failed to connect to BT device: {e}", exc_info=e)
                self.status = self.DONE
                self.sock = None

            if self.sock:
                res["result"] = None
                self.status = self.CONNECTED
            else:
                err_msg = f"BT connect failed: {addr}"
                res["error"] = { "message": err_msg }
                self.status = self.DONE

        elif self.status == self.CONNECTED and method == 'send':
            logger.debug("handle send request")
            if params['encoding'] != 'base64':
                logger.error("encoding other than base 64 is not "
                                 "yet supported: ", params['encoding'])
            msg_bstr = params['message'].encode('ascii')
            data = base64.standard_b64decode(msg_bstr)
            self.sock.send(data)
            res['result'] = len(data)

        logger.debug(res)
        return res

    def end_request(self):
        logger.debug(f"end_request of BTSession {self}")
        return self.status == self.DONE


class BLESession(Session):
    """
    Manage a session for Bluetooth Low Energy device such as micro:bit
    """

    INITIAL = 1
    DISCOVERY = 2
    CONNECTED = 3
    DONE = 4

    ADTYPE_COMP_16B = 0x3
    ADTYPE_COMP_128B = 0x7

    class BLEThread(threading.Thread):
        """
        Separated thread to control notifications to Scratch.
        It handles device discovery notification in DISCOVERY status
        and notifications from BLE devices in CONNECTED status.
        """
        def __init__(self, session):
            threading.Thread.__init__(self)
            self.session = session

        def run(self):
            while True:
                logger.debug("loop in BLE thread")
                if self.session.status == self.session.DISCOVERY:
                    logger.debug("send out found devices")
                    devices = self.session.found_devices
                    for d in devices:
                        params = { 'rssi': d.rssi }
                        params['peripheralId'] = devices.index(d)
                        params['name'] = d.getValueText(0x9)
                        self.session.notify('didDiscoverPeripheral', params)
                    time.sleep(1)
                elif self.session.status == self.session.CONNECTED:
                    logger.debug("in connected status:")
                    delegate = self.session.delegate
                    if delegate and len(delegate.handles) > 0:
                        if not delegate.restart_notification_event.is_set():
                            delegate.restart_notification_event.wait()
                        try:
                            self.session.lock.acquire()
                            self.session.perip.waitForNotifications(1.0)
                            self.session.lock.release()
                        except Exception as e:
                            logger.error(e)
                            self.session.close()
                            break
                    else:
                        time.sleep(0.0)
                    # To avoid repeated lock by this single thread,
                    # yield CPU to other lock waiting threads.
                    time.sleep(0)
                else:
                    # Nothing to do:
                    time.sleep(1)

    class BLEDelegate(DefaultDelegate):
        """
        A bluepy handler to receive notifictions from BLE devices.
        """
        def __init__(self, session):
            DefaultDelegate.__init__(self)
            self.session = session
            self.handles = {}
            self.restart_notification_event = threading.Event()
            self.restart_notification_event.set()

        def add_handle(self, serviceId, charId, handle):
            logger.debug(f"add handle for notification: {handle}")
            params = { 'serviceId': UUID(serviceId).getCommonName(),
                       'characteristicId': charId,
                       'encoding': 'base64' }
            self.handles[handle] = params

        def handleNotification(self, handle, data):
            logger.debug(f"BLE notification: {handle} {data}")
            if not self.restart_notification_event.is_set():
                return
            params = self.handles[handle]
            params['message'] = base64.standard_b64encode(data).decode('ascii')
            self.session.notify('characteristicDidChange', params)

    def __init__(self, websocket, loop):
        super().__init__(websocket, loop)
        self.status = self.INITIAL
        self.found_devices = []
        self.device = None
        self.perip = None
        self.delegate = None

    def close(self):
        self.status = self.DONE
        if self.perip:
            logger.info(f"disconnect to BLE peripheral: {self.perip}")
            self.perip.disconnect()

    def __del__(self):
        self.close()

    def matches(self, dev, filters):
        """
        Check if the found BLE device mathces the filters Scracth specifies.
        """
        logger.debug(f"in matches {dev} {filters}")
        for f in filters:
            if 'services' in f:
                for s in f['services']:
                    logger.debug(f"sevice to check: {s}")
                    given_uuid = s
                    logger.debug(f"given: {given_uuid}")
                    service_class_uuid = dev.getValueText(self.ADTYPE_COMP_128B)
                    logger.debug(f"adtype 128b: {service_class_uuid}")
                    if not service_class_uuid:
                        service_class_uuid = dev.getValueText(self.ADTYPE_COMP_16B)
                        logger.debug(f"adtype 16b: {service_class_uuid}")
                        if not service_class_uuid:
                            continue
                    dev_uuid = UUID(service_class_uuid)
                    logger.debug(f"dev: {dev_uuid}")
                    logger.debug(given_uuid == dev_uuid)
                    if given_uuid == dev_uuid:
                        logger.debug("match...")
                        return True
            if 'name' in f or 'manufactureData' in f:
                logger.error("name/manufactureData filters not implemented")
                # TODO: implement other filters defined:
                # ref: https://github.com/LLK/scratch-link/blob/develop/Documentation/BluetoothLE.md
        return False

    def handle_request(self, method, params):
        """Handle requests from Scratch"""
        if self.delegate:
            # Do not allow notification during request handling to avoid
            # websocket server errors
            self.delegate.restart_notification_event.clear()

        logger.debug("handle request to BLE device")
        logger.debug(method)
        if len(params) > 0:
            logger.debug(params)

        res = { "jsonrpc": "2.0" }

        if self.status == self.INITIAL and method == 'discover':
            scanner = Scanner()
            devices = scanner.scan(1.0)
            for dev in devices:
                if self.matches(dev, params['filters']):
                    self.found_devices.append(dev)
            if len(self.found_devices) == 0:
                err_msg = f"BLE service not found for {params['filters']}"
                res["error"] = { "message": err_msg }
                self.status = self.DONE
            else:
                res["result"] = None
                self.status = self.DISCOVERY
                self.ble_thread = self.BLEThread(self)
                self.ble_thread.start()

        elif self.status == self.DISCOVERY and method == 'connect':
            logger.debug("connecting to the BLE device")
            self.device = self.found_devices[params['peripheralId']]
            try:
                self.perip = Peripheral(self.device.addr,
                                        self.device.addrType)
                logger.info(f"connect to BLE peripheral: {self.perip}")
            except BTLEDisconnectError as e:
                logger.error(f"failed to connect to BLE device: {e}")
                self.status = self.DONE

            if self.perip:
                res["result"] = None
                self.status = self.CONNECTED
                self.delegate = self.BLEDelegate(self)
                self.perip.withDelegate(self.delegate)
            else:
                err_msg = f"BLE connect failed :{self.device}"
                res["error"] = { "message": err_msg }
                self.status = self.DONE

        elif self.status == self.CONNECTED and method == 'read':
            logger.debug("handle read request")
            service_id = params['serviceId']
            chara_id = params['characteristicId']
            charas = self.perip.getCharacteristics(uuid=chara_id)
            c = charas[0]
            if c.uuid != UUID(chara_id):
                logger.error("Failed to get characteristic {chara_id}")
                self.status = self.DONE
            else:
                self.lock.acquire()
                b = c.read()
                self.lock.release()
                message = base64.standard_b64encode(b).decode('ascii')
                res['result'] = { 'message': message, 'encode': 'base64' }
            if params['startNotifications'] == True:
                logger.debug(f"start notification for {chara_id}")
                service = self.perip.getServiceByUUID(UUID(service_id))
                chas = service.getCharacteristics(forUUID=chara_id)
                handle = chas[0].getHandle()
                # prepare notification handler
                self.delegate.add_handle(service_id, chara_id, handle)
                # request notification to the BLE device
                self.lock.acquire()
                self.perip.writeCharacteristic(chas[0].getHandle() + 1,
                                               b"\x01\x00", True)
                self.lock.release()

        elif self.status == self.CONNECTED and method == 'write':
            logger.debug("handle write request")
            service_id = params['serviceId']
            chara_id = params['characteristicId']
            charas = self.perip.getCharacteristics(uuid=chara_id)
            c = charas[0]
            if c.uuid != UUID(chara_id):
                logger.error("Failed to get characteristic {chara_id}")
                self.status = self.DONE
            else:
                if params['encoding'] != 'base64':
                    logger.error("encoding other than base 64 is not "
                                 "yet supported: ", params['encoding'])
                msg_bstr = params['message'].encode('ascii')
                data = base64.standard_b64decode(msg_bstr)
                self.lock.acquire()
                c.write(data)
                self.lock.release()
                res['result'] = len(data)

        logger.debug(res)
        return res

    def end_request(self):
        logger.debug("end_request of BLESession")
        if self.delegate:
            self.delegate.restart_notification_event.set()
        return self.status == self.DONE

# kick start WSS server
ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
localhost_pem = pathlib.Path(__file__).with_name("scratch-device-manager.pem")
ssl_context.load_cert_chain(localhost_pem)
sessionTypes = { '/scratch/ble': BLESession, '/scratch/bt': BTSession }

async def ws_handler(websocket):
    path = None
    try:
        path = websocket.request.path
        logger.info(f"Start session for web socket path: {path}")
        session = sessionTypes[path](websocket, asyncio.get_running_loop())
        await session.handle()
    except Exception as e:
        logger.error(f"Failure in session for web socket path: {path}")
        logger.error(e)

async def main():
    while True:
        try:
            async with websockets.serve(
                ws_handler,
                "device-manager.scratch.mit.edu",
                20110,
                ssl=ssl_context
            ):
                await asyncio.Future()  # run forever
        except Exception as e:
            logger.info("restart server...")

if __name__ == "__main__":
    asyncio.run(main())