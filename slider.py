import collections
import enum
import itertools
import logging
import os
import queue
import sys
import threading
from typing import NamedTuple, Optional, Sequence

import serial

import win32con
import win32gui

# Disable Kivy's logging via its "normal" methods
os.environ["KIVY_NO_FILELOG"] = "1"
os.environ["KIVY_NO_CONSOLELOG"] = "1"
# Prevent Kivy from borking Python's regular logging
old_root_logger = logging.root
old_stderr = sys.stderr

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button

logging.root = old_root_logger
sys.stderr = old_stderr


LOGLEVEL = logging.WARNING
COM_PORT = 'COM12'


SLIDER_SYNC = 0xff
SLIDER_ESCAPE = 0xfd


class SliderCommand(enum.IntEnum):
    SLIDER_REPORT = 0x01
    LED_REPORT = 0x02
    ENABLE_SLIDER_REPORT = 0x03
    DISABLE_SLIDER_REPORT = 0x04
    MAGIC_09 = 0x09
    MAGIC_0A = 0x0a
    RESET = 0x10
    EXCEPTION = 0xee
    GET_HW_INFO = 0xf0


class Packet(NamedTuple):
    command: int
    payload: bytes


class SliderDecoder:
    _logger = logging.getLogger('slider.decoder')

    def __init__(self):
        self._decode_buffer = bytearray()
        self._target_packet_len = 0
        self._next_byte_is_escaped = False

    def _reset_decoding_state(self):
        self._decode_buffer.clear()
        self._target_packet_len = 0
        self._next_byte_is_escaped = False

    def decode_byte(self, b: int) -> Optional[Packet]:
        if b == SLIDER_SYNC:
            if self._decode_buffer:
                self._logger.warning('SYNC in middle of packet, discarding existing data')
                self._reset_decoding_state()
            self._decode_buffer.append(b)
            self._logger.debug('Received SYNC')
            return
        if self._next_byte_is_escaped:
            self._decode_buffer.append(b + 1)
            self._next_byte_is_escaped = False
        else:
            if b == SLIDER_ESCAPE:
                self._next_byte_is_escaped = True
                return
            else:
                self._decode_buffer.append(b)
        if len(self._decode_buffer) == 3:
            self._target_packet_len = self._decode_buffer[2] + 4
        elif len(self._decode_buffer) > 3:
            assert self._target_packet_len
            if len(self._decode_buffer) == self._target_packet_len:
                self._logger.debug('Received complete packet: %r', self._decode_buffer)
                decoded_packet = None
                if sum(self._decode_buffer) % 256 != 0:
                    self._logger.warning('Packet does not have correct checksum')
                else:
                    decoded_packet = Packet(self._decode_buffer[1], bytes(self._decode_buffer[3:-1]))
                self._reset_decoding_state()
                return decoded_packet


def _escape_byte(b: int) -> Sequence[int]:
    if b in (SLIDER_SYNC, SLIDER_ESCAPE):
        return SLIDER_ESCAPE, b - 1
    return b,


def encode_packet(packet: Packet) -> bytes:
    if len(packet.payload) > 255:
        raise ValueError('Payload too long')
    raw_bytes = [SLIDER_SYNC, packet.command, len(packet.payload)]
    raw_bytes.extend(packet.payload)
    checksum = (-sum(raw_bytes)) % 256
    raw_bytes.append(checksum)
    escaped_bytes = [SLIDER_SYNC]
    escaped_bytes.extend(itertools.chain.from_iterable(_escape_byte(b) for b in raw_bytes[1:]))
    return bytes(escaped_bytes)


class SliderSerialController:
    _read_write_interval = 1
    _write_timeout = 0.1
    _reset_if_no_read_intervals = 10
    _sensor_report_interval = 0.012
    _logger = logging.getLogger('slider.slider')

    def __init__(self, port: str):
        self._decoder = SliderDecoder()

        self._serial = serial.Serial(
            port, 115200, timeout=self._read_write_interval, write_timeout=self._write_timeout)
        self._logger.debug('Opened serial port')

        self._stop_read_write_threads = threading.Event()
        self._write_queue = queue.Queue(maxsize=3)
        self._read_thread = threading.Thread(target=self._read_job, daemon=True)
        self._read_thread.start()
        self._write_thread = threading.Thread(target=self._write_job, daemon=True)
        self._write_thread.start()
        self._no_read_intervals = 0

        self._stop_sensor_thread = threading.Event()
        self._sensor_thread = None

        self._slider_values = [0] * 32
        self._incoming_slider_values = collections.deque(maxlen=1)

        self.app = None

    def close(self):
        self._stop_read_write_threads.set()
        self._stop_sensor_thread.set()
        self._logger.debug('Waiting for read thread')
        self._read_thread.join()
        self._logger.debug('Waiting for write thread')
        self._write_thread.join()
        self._logger.debug('Closing serial port')
        self._serial.close()

    def _read_job(self):
        self._logger.debug('Reader thread running')
        while not self._stop_read_write_threads.is_set():
            data = self._serial.read(1)
            self._logger.debug('Read %r', data)
            if data:
                self._no_read_intervals = 0
                for b in data:
                    packet = self._decoder.decode_byte(b)
                    if packet:
                        self._logger.debug('Received packet %r', packet)
                        self._process_packet(packet)
            else:
                self._no_read_intervals += 1
                if self._no_read_intervals >= self._reset_if_no_read_intervals:
                    self._logger.warning('Serial received no data, resetting')
                    self._reset()

    def _write_job(self):
        self._logger.debug('Writer thread running')
        while not self._stop_read_write_threads.is_set():
            try:
                packet = self._write_queue.get(timeout=self._read_write_interval)
            except queue.Empty:
                self._logger.debug('Nothing to write')
                continue
            self._logger.debug('Writing packet %r', packet)
            try:
                self._serial.write(encode_packet(packet))
            except serial.SerialTimeoutException:
                self._logger.warning('Serial write timed out')

    def _write_packet(self, packet: Packet):
        try:
            self._write_queue.put_nowait(packet)
        except queue.Full:
            self._logger.warning('Serial write queue full, discarding packet')

    def _process_packet(self, packet: Packet):
        if packet.command == SliderCommand.RESET:
            self._logger.info('Received Reset')
            self._reset()
            self._write_packet(Packet(SliderCommand.RESET, b''))
        elif packet.command == SliderCommand.MAGIC_09:
            self._logger.info('Received 0x09 packet')
            self._write_packet(Packet(SliderCommand.MAGIC_09, b''))
        elif packet.command == SliderCommand.MAGIC_0A:
            self._logger.info('Received 0x0A packet')
            self._write_packet(Packet(SliderCommand.MAGIC_0A, b''))
        elif packet.command == SliderCommand.GET_HW_INFO:
            self._logger.info('Received GetHWInfo')
            self._write_packet(Packet(SliderCommand.GET_HW_INFO, b'15275   \xa006687\xff\x90\x00d'))
        elif packet.command == SliderCommand.ENABLE_SLIDER_REPORT:
            self._logger.info('Received EnableSliderReport')
            if self._sensor_thread is None or not self._sensor_thread.is_alive():
                self._stop_sensor_thread.clear()
                self._sensor_thread = threading.Thread(target=self._slider_job, daemon=True)
                self._sensor_thread.start()
        elif packet.command == SliderCommand.DISABLE_SLIDER_REPORT:
            self._logger.info('Received DisableSliderReport')
            self._stop_sensor_thread.set()
        elif packet.command == SliderCommand.EXCEPTION:
            self._logger.warning('Received Exception packet: %r', packet)
        elif packet.command == SliderCommand.LED_REPORT:
            self._logger.info('Received LED report: %r', packet)
            self._process_led_report(packet.payload)
        else:
            self._logger.warning('Received unknown packet %r', packet)

    def _process_led_report(self, payload: bytes):
        assert len(payload) == 0x61
        brightness = payload[0] / 63
        rgba_values = [(payload[3 * i + 2] / 255, payload[3 * i + 3] / 255, payload[3 * i + 1] / 255, brightness) for i in range(32)]
        self._logger.info('LED colors: %r', rgba_values)
        if self.app:
            self.app.set_slider_colors_threadsafe(rgba_values)

    def set_slider_values_threadsafe(self, values):
        self._incoming_slider_values.append(values)

    def _slider_job(self):
        self._logger.debug('Slider thread running')
        while not self._stop_sensor_thread.is_set():
            try:
                new_values = self._incoming_slider_values.popleft()
                for i in range(32):
                    self._slider_values[i] = new_values[i]
            except IndexError:
                pass
            self._logger.info('Slider values: %r', self._slider_values)
            self._write_packet(Packet(SliderCommand.SLIDER_REPORT, bytes(self._slider_values)))
            serial.time.sleep(self._sensor_report_interval)

    def _reset(self):
        self._no_read_intervals = 0
        self._stop_sensor_thread.set()
        if self._sensor_thread is not None and self._sensor_thread.is_alive():
            self._sensor_thread.join()
        while True:
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                break
        if self.app is not None:
            self.app.reset()


class SliderApp(App):
    _logger = logging.getLogger('slider.app')

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._slider_buttons = None
        self._incoming_colors = collections.deque(maxlen=1)
        self.slider_controller = None
        self._touches = {}

    def reset(self):
        self._logger.info('Reset app')
        for button in self._slider_buttons:
            button.background_color = (0, 0, 0, 1)

    def _set_noactivate(self):
        hwnd = win32gui.GetActiveWindow()
        current_exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        new_exstyle = (current_exstyle
                       # | win32con.WS_EX_TOPMOST
                       | win32con.WS_EX_TOOLWINDOW
                       | win32con.WS_EX_NOACTIVATE
                       | win32con.WS_EX_APPWINDOW)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, new_exstyle)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)

    def build(self):
        Window.size = 1280, 200
        slider_layout = BoxLayout(
            orientation='horizontal',
            padding=[40],
        )
        slider_layout.bind(
            on_touch_down=self._on_touch_down,
            on_touch_move=self._on_touch_move,
            on_touch_up=self._on_touch_up,
        )
        buttons = []
        for i in range(32):
            btn = Button(
                text=str(i),
                background_color=(0, 0, 0, 1),
                background_normal='',
                background_down='',
            )
            buttons.append(btn)
            slider_layout.add_widget(btn)
        self._slider_buttons = buttons
        Clock.schedule_interval(self._set_slider_colors, 1/60)
        self._set_noactivate()
        return slider_layout

    def _collide_touch_buttons(self, touch):
        for i, btn in enumerate(self._slider_buttons):
            if btn.collide_point(touch.x, touch.y):
                return i
        return -1

    def _update_touches_to_controller(self):
        if self.slider_controller is not None:
            values = [0] * 32
            for sensor in self._touches.values():
                if sensor >= 0:
                    values[sensor] = 0xfe
            self._logger.info('Sending sensor values %r', values)
            self.slider_controller.set_slider_values_threadsafe(values)

    def _on_touch_down(self, instance, touch):
        self._logger.debug('Touch down %d (%r, %r)', touch.uid, touch.x, touch.y)
        touch.grab(instance)
        self._touches[touch.uid] = self._collide_touch_buttons(touch)
        self._logger.debug('Touches: %r', self._touches)
        self._update_touches_to_controller()

    def _on_touch_move(self, instance, touch):
        if touch.grab_current is instance:
            self._logger.debug('Touch move %d (%r, %r)', touch.uid, touch.x, touch.y)
            self._touches[touch.uid] = self._collide_touch_buttons(touch)
            self._logger.debug('Touches: %r', self._touches)
            self._update_touches_to_controller()

    def _on_touch_up(self, instance, touch):
        if touch.grab_current is instance:
            touch.ungrab(instance)
            self._logger.debug('Touch up %d (%r, %r)', touch.uid, touch.x, touch.y)
            self._touches.pop(touch.uid, None)
            self._logger.debug('Touches: %r', self._touches)
            self._update_touches_to_controller()

    def set_slider_colors_threadsafe(self, colors):
        self._incoming_colors.append(colors)

    def _set_slider_colors(self, dt):
        try:
            new_colors = self._incoming_colors.popleft()
        except IndexError:
            return
        for button, color in zip(self._slider_buttons, new_colors):
            button.background_color = color


def main():
    # logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(name)s %(message)s')
    root_logger = logging.getLogger()
    formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(name)s %(message)s')
    root_logger.setLevel(LOGLEVEL)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(stream_handler)
    root_logger.info('Logging configured')

    slider = SliderSerialController(COM_PORT)
    app = SliderApp()
    slider.app = app
    app.slider_controller = slider
    try:
        app.run()
    finally:
        slider.close()


if __name__ == '__main__':
    main()
