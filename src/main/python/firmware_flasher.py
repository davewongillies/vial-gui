# SPDX-License-Identifier: GPL-2.0-or-later

import datetime
import hashlib
import struct
import time
import threading

from PyQt5.QtCore import pyqtSignal, QCoreApplication
from PyQt5.QtGui import QFontDatabase
from PyQt5.QtWidgets import QHBoxLayout, QLineEdit, QToolButton, QPlainTextEdit, QProgressBar,QFileDialog, QDialog

from basic_editor import BasicEditor
from util import tr, chunks, find_vial_devices
from vial_device import VialBootloader, VialKeyboard


BL_SUPPORTED_VERSION = 0


def send_retries(dev, data, retries=20):
    """ Sends usb packet up to 'retries' times, returns True if success, False if failed """

    for x in range(retries):
        ret = dev.send(data)
        if ret == len(data) + 1:
            return True
        elif ret < 0:
            time.sleep(0.1)
        else:
            return False
    return False


CHUNK = 64


def bl_get_version(dev):
    dev.send(b"VC\x00")
    data = dev.recv(8)
    return data[0]


def bl_get_uid(dev):
    dev.send(b"VC\x01")
    data = dev.recv(8)
    return data


def cmd_flash(device, firmware, log_cb, progress_cb, complete_cb, error_cb):
    if firmware[0:8] != b"VIALFW00":
        return error_cb("Error: Invalid signature")

    fw_uid = firmware[8:16]
    fw_ts = struct.unpack("<Q", firmware[16:24])[0]
    log_cb("* Firmware build date: {} (UTC)".format(datetime.datetime.utcfromtimestamp(fw_ts)))

    fw_hash = firmware[32:64]
    fw_payload = firmware[64:]

    if hashlib.sha256(fw_payload).digest() != fw_hash:
        return error_cb("Error: Firmware failed integrity check\n\texpected={}\n\tgot={}".format(
            fw_hash.hex(),
            hashlib.sha256(fw_payload).hexdigest()
        ))

    # Check bootloader is correct version
    ver = bl_get_version(device)
    log_cb("* Bootloader version: {}".format(ver))
    if ver != BL_SUPPORTED_VERSION:
        return error_cb("Error: Unsupported bootloader version")

    uid = bl_get_uid(device)
    log_cb("* Vial ID: {}".format(uid.hex()))

    if uid == b"\xFF" * 8:
        log_cb("\n\n\n!!! WARNING !!!\nBootloader UID is not set, make sure to configure it"
               " before releasing production firmware\n!!! WARNING !!!\n\n")

    if uid != fw_uid:
        return error_cb("Error: Firmware package was built for different device\n\texpected={}\n\tgot={}".format(
            fw_uid.hex(),
            uid.hex()
        ))

    # OK all checks complete, we can flash now
    while len(fw_payload) % CHUNK != 0:
        fw_payload += b"\x00"

    # Flash
    log_cb("Flashing...")
    device.send(b"VC\x02" + struct.pack("<H", len(fw_payload) // CHUNK))
    total = 0
    for part in chunks(fw_payload, CHUNK):
        if len(part) < CHUNK:
            part += b"\x00" * (CHUNK - len(part))
        if not send_retries(device, part):
            return error_cb("Error while sending data, firmware is corrupted")
        total += len(part)
        progress_cb(total / len(fw_payload))

    # Reboot
    log_cb("Rebooting...")
    device.send(b"VC\x03")

    complete_cb("Done!")


class FirmwareFlasher(BasicEditor):
    log_signal = pyqtSignal(object)
    progress_signal = pyqtSignal(object)
    complete_signal = pyqtSignal(object)
    error_signal = pyqtSignal(object)

    def __init__(self, main, parent=None):
        super().__init__(parent)

        self.main = main

        self.log_signal.connect(self._on_log)
        self.progress_signal.connect(self._on_progress)
        self.complete_signal.connect(self._on_complete)
        self.error_signal.connect(self._on_error)

        self.selected_firmware_path = ""

        file_selector = QHBoxLayout()
        self.txt_file_selector = QLineEdit()
        self.txt_file_selector.setReadOnly(True)
        file_selector.addWidget(self.txt_file_selector)
        self.btn_select_file = QToolButton()
        self.btn_select_file.setText(tr("Flasher", "Select file..."))
        self.btn_select_file.clicked.connect(self.on_click_select_file)
        file_selector.addWidget(self.btn_select_file)
        self.addLayout(file_selector)
        self.txt_logger = QPlainTextEdit()
        self.txt_logger.setReadOnly(True)
        self.txt_logger.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.addWidget(self.txt_logger)
        progress_flash = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        progress_flash.addWidget(self.progress_bar)
        self.btn_flash = QToolButton()
        self.btn_flash.setText(tr("Flasher", "Flash"))
        self.btn_flash.clicked.connect(self.on_click_flash)
        progress_flash.addWidget(self.btn_flash)
        self.addLayout(progress_flash)

        self.device = None

    def rebuild(self, device):
        super().rebuild(device)
        self.txt_logger.clear()

        if not self.valid():
            return

        if isinstance(self.device, VialBootloader):
            self.log("Valid Vial Bootloader device at {}".format(self.device.desc["path"].decode("utf-8")))
        elif isinstance(self.device, VialKeyboard):
            self.log("Vial keyboard detected")

    def valid(self):
        return isinstance(self.device, VialBootloader) or\
               isinstance(self.device, VialKeyboard) and self.device.keyboard.vibl

    def on_click_select_file(self):
        dialog = QFileDialog()
        dialog.setDefaultSuffix("vfw")
        dialog.setAcceptMode(QFileDialog.AcceptOpen)
        dialog.setNameFilters(["Vial Firmware (*.vfw)"])
        if dialog.exec_() == QDialog.Accepted:
            self.selected_firmware_path = dialog.selectedFiles()[0]
            self.txt_file_selector.setText(self.selected_firmware_path)
            self.log("Firmware update package: {}".format(self.selected_firmware_path))

    def on_click_flash(self):
        if not self.selected_firmware_path:
            self.log("Error: Please select a firmware update package")
            return

        with open(self.selected_firmware_path, "rb") as inf:
            firmware = inf.read()

        if len(firmware) > 10 * 1024 * 1024:
            self.log("Error: Firmware is too large. Check you've selected the correct file")
            return

        self.log("Preparing to flash...")
        self.lock_ui()

        # TODO: this needs to switch to the secure assisted-reset feature before public release
        if isinstance(self.device, VialKeyboard):
            uid = self.device.keyboard.get_uid()

            self.log("Restarting in bootloader mode...")
            self.device.keyboard.reset()

            # watch for bootloaders to appear and ask them for their UID, return one that matches the keyboard
            while True:
                self.log("Looking for devices...")
                QCoreApplication.processEvents()
                time.sleep(1)
                devices = find_vial_devices()
                found = None
                for dev in devices:
                    if isinstance(dev, VialBootloader):
                        dev.open()
                        # TODO: update version check before release
                        if bl_get_version(dev) != BL_SUPPORTED_VERSION or bl_get_uid(dev) != uid:
                            dev.close()
                        found = dev
                        break
                if found:
                    self.log("Found Vial Bootloader device at {}".format(found.desc["path"].decode("utf-8")))
                    self.device = found
                    break

        threading.Thread(target=lambda: cmd_flash(
            self.device, firmware, self.on_log, self.on_progress, self.on_complete, self.on_error)).start()

    def on_log(self, line):
        self.log_signal.emit(line)

    def on_progress(self, progress):
        self.progress_signal.emit(progress)

    def on_complete(self, msg):
        self.complete_signal.emit(msg)

    def on_error(self, msg):
        self.error_signal.emit(msg)

    def _on_log(self, msg):
        self.log(msg)

    def _on_progress(self, progress):
        self.progress_bar.setValue(int(progress * 100))

    def _on_complete(self, msg):
        self.log(msg)
        self.progress_bar.setValue(100)
        self.unlock_ui()

    def _on_error(self, msg):
        self.log(msg)
        self.unlock_ui()

    def log(self, line):
        self.txt_logger.appendPlainText("[{}] {}".format(datetime.datetime.now(), line))

    def lock_ui(self):
        self.btn_select_file.setEnabled(False)
        self.btn_flash.setEnabled(False)
        self.main.lock_ui()

    def unlock_ui(self):
        self.btn_select_file.setEnabled(True)
        self.btn_flash.setEnabled(True)
        self.main.unlock_ui()
