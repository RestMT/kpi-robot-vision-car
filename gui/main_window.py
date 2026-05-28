"""Головне PyQt-вікно для ручного керування ESP32-CAM роботом."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PyQt5.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QCloseEvent, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from backend.discovery import RobotDiscoveryResult, discover_robots
from backend.robot_client import RobotClient
from backend.video_client import VideoStreamThread
from backend.wifi import WifiProvisioningClient
from config.settings import (
    APP_NAME,
    DEFAULT_HOST,
    DEFAULT_SETUP_HOST,
    DEFAULT_SPEED,
    DEFAULT_STREAM_PORT,
    DEFAULT_WS_PORT,
    MAX_SPEED,
    MIN_SPEED,
    MOTION_KEEPALIVE_INTERVAL_MS,
    ROBOT_AUTO_RECONNECT_ENABLED,
    ROBOT_ERROR_THRESHOLD,
    ROBOT_RECONNECT_INTERVAL_MS,
    VIDEO_PLACEHOLDER_TEXT,
    WIFI_TIMEOUT_SECONDS,
)


class WorkerSignals(QObject):
    """Qt-сигнали для результатів фонової команди."""

    success = pyqtSignal(object)
    error = pyqtSignal(str)


class CommandWorker(QRunnable):
    """Виконувати коротку мережеву дію поза потоком інтерфейсу."""

    def __init__(self, action: Callable[[], Any]) -> None:
        """Зберегти дію, яку потрібно виконати у QThreadPool."""

        super().__init__()
        self.signals = WorkerSignals()
        self._action = action

    @pyqtSlot()
    def run(self) -> None:
        """Виконати дію та передати результат або помилку через Qt-сигнали."""

        try:
            result = self._action()
        except Exception as exc:  # noqa: BLE001 - інтерфейс показує мережеві помилки.
            self.signals.error.emit(str(exc))
        else:
            self.signals.success.emit(result)


class MainWindow(QMainWindow):
    """Головне вікно з підключенням, відео та ручним керуванням."""

    def __init__(self) -> None:
        """Ініціалізувати стан інтерфейсу, таймери та основні елементи."""

        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(980, 700)

        # QThreadPool виконує короткі мережеві дії, щоб Qt event loop не блокувався.
        self._thread_pool = QThreadPool.globalInstance()

        # Базові посилання на активне підключення, відеопотік і знайдених роботів.
        self._connected = False
        self.robot_client: RobotClient | None = None
        self._video_thread: VideoStreamThread | None = None
        self._discovered_robots: list[RobotDiscoveryResult] = []

        # Стан руху і прапорці команд не дають одночасно відправляти несумісні WebSocket-запити.
        self._active_motion_command: str | None = None
        self._motion_command_in_flight = False
        self._stop_in_flight = False
        self._pending_stop = False
        self._led_in_flight = False
        self._speed_in_flight = False

        # Прапорці відключення і відновлення розрізняють ручне відключення та втрату зв’язку.
        self._disconnect_requested = False
        self._manual_disconnect_requested = False
        self._recovering_connection = False
        self._reconnect_attempts = 0
        self._reconnect_in_flight = False

        # Лічильник помилок запускає recovery лише після кількох послідовних збоїв.
        self._robot_error_count = 0
        self._max_robot_errors = ROBOT_ERROR_THRESHOLD

        # Keep-alive повторює активну команду руху частіше, ніж спрацьовує watchdog прошивки.
        self._motion_keepalive_timer = QTimer(self)
        self._motion_keepalive_timer.setInterval(MOTION_KEEPALIVE_INTERVAL_MS)
        self._motion_keepalive_timer.timeout.connect(self._repeat_motion_command)

        # Reconnect timer робить окремі спроби підключення після втрати зв’язку.
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(ROBOT_RECONNECT_INTERVAL_MS)
        self._reconnect_timer.timeout.connect(self._try_auto_reconnect)

        # Останній запис журналу зберігається, щоб не дублювати однакові статуси.
        self._last_log_entry: tuple[str, str] | None = None
        self._build_ui()
        self._connect_signals()
        self._set_connected(False)
        self._set_status("Готово. Вкажіть IP ESP32-CAM і натисніть «Підключитися».")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - назва API Qt.
        """Коректно зупинити робота, відео та перепідключення перед закриттям."""

        self._manual_disconnect_requested = True
        self._reconnect_timer.stop()
        self._recovering_connection = False
        self._disconnect_requested = True
        self._active_motion_command = None
        self._motion_keepalive_timer.stop()
        if self._has_active_robot_request():
            self._pending_stop = True
        else:
            self._send_stop_blocking()
            self._close_robot_client(send_stop=False)
        self._stop_video()
        if self._video_thread is not None and self._video_thread.isRunning():
            event.ignore()
            self._set_status(
                "Відеопотік ще завершується. "
                "Повторіть закриття через кілька секунд.",
                level="WARNING",
            )
            return
        event.accept()

    def _build_ui(self) -> None:
        """Побудувати основний макет головного вікна."""

        central_widget = QWidget(self)
        root_layout = QVBoxLayout(central_widget)

        # Верхній блок містить усі параметри мережевого підключення.
        root_layout.addWidget(self._build_connection_group())

        # Відео займає більшу частину ширини, а кнопки руху лишаються праворуч.
        content_layout = QHBoxLayout()
        content_layout.addWidget(self._build_video_group(), stretch=3)
        content_layout.addWidget(self._build_motion_group(), stretch=1)
        root_layout.addLayout(content_layout, stretch=1)
        root_layout.addWidget(self._build_log_group(), stretch=0)
        self.setCentralWidget(central_widget)

    def _build_connection_group(self) -> QGroupBox:
        """Створити блок параметрів підключення та Wi-Fi."""

        group = QGroupBox("Параметри підключення")
        layout = QGridLayout(group)

        # Поля SSID/пароля використовуються тільки для provisioning setup-точки ESP32-CAM.
        self.ssid_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)

        # setup_host може відрізнятися від host робота після discovery або ручного вводу.
        self.setup_host_edit = QLineEdit(DEFAULT_SETUP_HOST)
        self.host_edit = QLineEdit(DEFAULT_HOST)

        # Порти лишаються редагованими, бо discovery може повернути інші значення.
        self.command_port_spin = QSpinBox()
        self.command_port_spin.setRange(1, 65535)
        self.command_port_spin.setValue(DEFAULT_WS_PORT)
        self.stream_port_spin = QSpinBox()
        self.stream_port_spin.setRange(1, 65535)
        self.stream_port_spin.setValue(DEFAULT_STREAM_PORT)

        # ComboBox заповнюється UDP discovery і дозволяє вибрати одного з кількох роботів.
        self.robot_combo = QComboBox()
        self.robot_combo.setMinimumWidth(260)
        self.robot_combo.setToolTip("Список роботів, знайдених у поточній Wi-Fi мережі")

        # Швидкість надсилається окремою командою speed:<value>, а не в кожній команді руху.
        self.speed_spin = QSpinBox()
        self.speed_spin.setRange(MIN_SPEED, MAX_SPEED)
        self.speed_spin.setValue(DEFAULT_SPEED)
        self.speed_spin.setSuffix(" PWM")
        form_left = QFormLayout()
        form_left.addRow("SSID:", self.ssid_edit)
        form_left.addRow("Пароль:", self.password_edit)
        form_left.addRow("Setup host:", self.setup_host_edit)
        form_right = QFormLayout()
        form_right.addRow("Знайдений робот:", self.robot_combo)
        form_right.addRow("IP ESP32-CAM:", self.host_edit)
        form_right.addRow("Порт команд:", self.command_port_spin)
        form_right.addRow("Порт відео:", self.stream_port_spin)
        form_right.addRow("Швидкість:", self.speed_spin)
        self.wifi_button = QPushButton("Передати Wi-Fi налаштування")
        self.wifi_reset_button = QPushButton("Скинути Wi-Fi")
        self.discovery_button = QPushButton("Знайти робота")
        self.connect_button = QPushButton("Підключитися")
        self.disconnect_button = QPushButton("Відключитися")
        buttons_layout = QHBoxLayout()
        buttons_layout.addWidget(self.wifi_button)
        buttons_layout.addWidget(self.wifi_reset_button)
        buttons_layout.addWidget(self.discovery_button)
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(self.connect_button)
        buttons_layout.addWidget(self.disconnect_button)
        layout.addLayout(form_left, 0, 0)
        layout.addLayout(form_right, 0, 1)
        layout.addLayout(buttons_layout, 1, 0, 1, 2)
        return group

    def _build_video_group(self) -> QGroupBox:
        """Створити блок перегляду MJPEG-відео."""

        group = QGroupBox("Відео з ESP32-CAM")
        layout = QVBoxLayout(group)
        self.video_label = QLabel(VIDEO_PLACEHOLDER_TEXT)
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(640, 360)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.setFrameShape(QFrame.StyledPanel)
        self.video_label.setStyleSheet("background-color: #111; color: #eee;")
        layout.addWidget(self.video_label)
        return group

    def _build_motion_group(self) -> QGroupBox:
        """Створити блок кнопок ручного керування роботом."""

        group = QGroupBox("Ручне керування")
        layout = QGridLayout(group)
        self.forward_button = QPushButton("Вперед")
        self.backward_button = QPushButton("Назад")
        self.left_button = QPushButton("Ліворуч")
        self.right_button = QPushButton("Праворуч")
        self.stop_button = QPushButton("Стоп")
        self.led_button = QPushButton("Світлодіод: вимкнено")
        self.led_button.setCheckable(True)

        # Однакова висота кнопок робить натискання руху передбачуваним.
        for button in (
            self.forward_button,
            self.backward_button,
            self.left_button,
            self.right_button,
            self.stop_button,
            self.led_button,
        ):
            button.setMinimumHeight(48)
        self.stop_button.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.forward_button, 0, 1)
        layout.addWidget(self.left_button, 1, 0)
        layout.addWidget(self.stop_button, 1, 1)
        layout.addWidget(self.right_button, 1, 2)
        layout.addWidget(self.backward_button, 2, 1)
        layout.addWidget(self.led_button, 3, 0, 1, 3)
        return group

    def _build_log_group(self) -> QGroupBox:
        """Створити блок журналу статусів і помилок."""

        group = QGroupBox("Журнал статусу і помилок")
        layout = QVBoxLayout(group)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.NoWrap)

        # Ліміт рядків не дає журналу необмежено рости під час довгої сесії.
        self.log_edit.setMaximumBlockCount(1000)
        self.log_edit.setMinimumHeight(130)
        self.log_edit.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self.copy_log_button = QPushButton("Копіювати журнал")
        self.clear_log_button = QPushButton("Очистити журнал")
        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(self.copy_log_button)
        buttons_layout.addWidget(self.clear_log_button)
        layout.addWidget(self.log_edit)
        layout.addLayout(buttons_layout)
        return group

    def _connect_signals(self) -> None:
        """Під’єднати Qt-сигнали елементів керування до обробників."""

        # Кнопки налаштувань запускають короткі HTTP/UDP дії у фонових worker-ах.
        self.wifi_button.clicked.connect(self._send_wifi_credentials)
        self.wifi_reset_button.clicked.connect(self._reset_wifi_credentials)
        self.discovery_button.clicked.connect(self._discover_robot)
        self.connect_button.clicked.connect(self._connect_robot)
        self.disconnect_button.clicked.connect(self._disconnect_robot)
        self.copy_log_button.clicked.connect(self._copy_log_to_clipboard)
        self.clear_log_button.clicked.connect(self._clear_log)
        self.speed_spin.editingFinished.connect(self._send_current_speed)
        self.robot_combo.currentIndexChanged.connect(self._select_discovered_robot)

        # Рух триває тільки поки кнопку утримують: press надсилає напрямок, release надсилає stop.
        self.forward_button.pressed.connect(lambda: self._start_motion("forward"))
        self.forward_button.released.connect(self._send_stop)
        self.backward_button.pressed.connect(lambda: self._start_motion("backward"))
        self.backward_button.released.connect(self._send_stop)
        self.left_button.pressed.connect(lambda: self._start_motion("left"))
        self.left_button.released.connect(self._send_stop)
        self.right_button.pressed.connect(lambda: self._start_motion("right"))
        self.right_button.released.connect(self._send_stop)
        self.stop_button.clicked.connect(self._send_stop)
        self.led_button.toggled.connect(self._toggle_led)

    def _connect_robot(self) -> None:
        """Почати підключення до робота і перевірити WebSocket командою ping."""

        # Нове ручне підключення скасовує режим автоматичного перепідключення.
        self._manual_disconnect_requested = False
        self._reconnect_timer.stop()
        self._recovering_connection = False
        self._reconnect_in_flight = False
        try:
            # Старий клієнт закривається без stop, бо новий ping/stop виконається одразу нижче.
            self._close_robot_client(send_stop=False)
            self.robot_client = self._create_robot_client()
        except ValueError as exc:
            self._set_status(str(exc), level="ERROR")
            QMessageBox.warning(self, "Помилка параметрів", str(exc))
            return
        self._set_connected(False)
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)

        # Локальна змінна client фіксує саме той об’єкт, для якого стартував worker.
        client = self.robot_client
        self._set_status("Перевіряю WebSocket-з’єднання з ESP32-CAM...")

        def action() -> str:
            """Перевірити WebSocket і зупинити робота після підключення."""

            client.ping()
            client.stop()
            return "Підключення налаштовано. Натисніть і утримуйте кнопку руху."

        def on_success(message: object) -> None:
            """Оновити стан інтерфейсу після успішного підключення."""

            if self._manual_disconnect_requested or self.robot_client is not client:
                # Ігноруємо запізнілий результат, якщо користувач уже
                # відключився або створив інший клієнт.
                self._close_robot_client(send_stop=False)
                self._set_connected(False)
                return

            # Після успішного ping/stop усі transient-прапорці повертаються в початковий стан.
            self._disconnect_requested = False
            self._pending_stop = False
            self._stop_in_flight = False
            self._motion_command_in_flight = False
            self._led_in_flight = False
            self._speed_in_flight = False
            self._robot_error_count = 0
            self._set_connected(True)
            self._show_status_success(message)

            # Швидкість передається окремо, щоб прошивка мала актуальний currentSpeed.
            self._send_current_speed()
            QTimer.singleShot(800, self._start_video)

        def on_error(message: str) -> None:
            """Очистити клієнт і показати помилку підключення."""

            if self.robot_client is not client:
                return
            self._close_robot_client(send_stop=False)
            self._set_connected(False)
            self._set_status(f"Не вдалося підключитися до ESP32-CAM: {message}", level="ERROR")

        self._run_worker(action, on_success, on_error)

    def _disconnect_robot(self) -> None:
        """Почати ручне відключення з надсиланням stop і зупинкою відео."""

        # Ручне відключення вимикає автоматичне відновлення зв’язку.
        self._manual_disconnect_requested = True
        was_recovering = self._recovering_connection or self._reconnect_in_flight
        self._reconnect_timer.stop()
        self._recovering_connection = False
        self._reconnect_in_flight = False
        if was_recovering:
            self._close_robot_client(send_stop=False)
            self._stop_video()
            self._set_connected(False)
            self._set_status("Відключено.")
            return
        if self.robot_client is None:
            self._stop_video()
            self._set_connected(False)
            self._set_status("Відключено.")
            return
        self._pending_stop = False
        self._disconnect_requested = True

        # Stop відправляється до закриття клієнта, щоб робот не продовжив рух.
        self._send_stop()
        self._stop_video()
        if not self._has_active_robot_request() and not self._pending_stop:
            self._finalize_disconnect()
        else:
            self._set_connected(False)
            self._set_status("Відключення: очікую завершення активної WebSocket-команди.")

    def _send_wifi_credentials(self) -> None:
        """Запустити фонове передавання Wi-Fi налаштувань."""

        # Значення зчитуються до запуску worker-а, щоб фонова дія бачила стабільний snapshot.
        ssid = self.ssid_edit.text().strip()
        password = self.password_edit.text()
        setup_host = self.setup_host_edit.text().strip()
        port = self.command_port_spin.value()
        self._run_worker(
            lambda: self._send_wifi_action(setup_host, port, ssid, password),
            self._show_status_success,
            self._show_status_error,
        )

    def _send_wifi_action(self, setup_host: str, port: int, ssid: str, password: str) -> str:
        """Передати Wi-Fi налаштування через provisioning client."""

        client = WifiProvisioningClient(setup_host, port=port, timeout=WIFI_TIMEOUT_SECONDS)
        client.send_credentials(ssid, password)
        return "Wi-Fi налаштування передано. ESP32-CAM може перезавантажитися."

    def _discover_robot(self) -> None:
        """Запустити UDP-пошук роботів і оновити список в інтерфейсі."""

        # Поки discovery працює, кнопка вимикається, щоб не стартувати кілька слухачів порту.
        self.discovery_button.setEnabled(False)
        self.robot_combo.clear()
        self._discovered_robots = []
        self._set_status("Пошук роботів ESP32-CAM у локальній мережі через UDP broadcast...")

        def on_success(result: object) -> None:
            """Заповнити список роботів після успішного UDP-пошуку."""

            self.discovery_button.setEnabled(True)
            robots = list(result or [])

            if not robots:
                self._set_status(
                    "Роботів не знайдено. Перевірте, що ПК і ESP32-CAM "
                    "перебувають у тій самій Wi-Fi мережі.",
                    level="WARNING",
                )
                return

            self._discovered_robots = robots
            self.robot_combo.blockSignals(True)
            self.robot_combo.clear()
            for robot in robots:
                self.robot_combo.addItem(robot.display_name)
            self.robot_combo.setCurrentIndex(0)
            self.robot_combo.blockSignals(False)

            # Перший знайдений робот одразу підставляється в поля для швидкого підключення.
            self._apply_discovered_robot(robots[0])

            if len(robots) == 1:
                self._set_status(f"Знайдено 1 робота: {robots[0].display_name}.")
            else:
                self._set_status(
                    f"Знайдено роботів: {len(robots)}. "
                    "Оберіть потрібного робота зі списку."
                )

        def on_error(message: str) -> None:
            """Повернути кнопку пошуку і показати помилку discovery."""

            self.discovery_button.setEnabled(True)
            self._set_status(f"Помилка пошуку роботів: {message}", level="ERROR")

        self._run_worker(discover_robots, on_success, on_error)

    def _apply_discovered_robot(self, robot: RobotDiscoveryResult) -> None:
        """Заповнити поля підключення параметрами знайденого робота."""

        self.host_edit.setText(robot.ip)
        self.setup_host_edit.setText(robot.ip)
        self.command_port_spin.setValue(robot.websocket_port)
        self.stream_port_spin.setValue(robot.stream_port)

    def _select_discovered_robot(self, index: int) -> None:
        """Обробити вибір робота зі списку знайдених пристроїв."""

        if index < 0 or index >= len(self._discovered_robots):
            return

        robot = self._discovered_robots[index]
        self._apply_discovered_robot(robot)
        self._set_status(f"Обрано робота: {robot.display_name}.")

    def _reset_wifi_credentials(self) -> None:
        """Підтвердити і запустити скидання Wi-Fi налаштувань ESP32-CAM."""

        # Для reset підходить setup host, а якщо він порожній, використовуємо основний host.
        host = self.setup_host_edit.text().strip() or self.host_edit.text().strip()
        port = self.command_port_spin.value()

        if not host:
            QMessageBox.warning(
                self,
                "Скидання Wi-Fi",
                "Вкажіть IP ESP32-CAM або знайдіть робота автоматично.",
            )
            return

        reply = QMessageBox.question(
            self,
            "Скидання Wi-Fi",
            "Скинути Wi-Fi налаштування ESP32-CAM? "
            "Після перезавантаження модуль запустить точку доступу "
            "KPI-Robot-Car-Setup.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply != QMessageBox.Yes:
            return

        # Кнопка блокується до завершення worker-а, щоб не відправити reset двічі.
        self.wifi_reset_button.setEnabled(False)

        def reset_action() -> str:
            """Виконати HTTP-запит скидання Wi-Fi налаштувань."""

            client = WifiProvisioningClient(host, port=port, timeout=WIFI_TIMEOUT_SECONDS)
            client.reset_credentials()
            return (
                "Wi-Fi налаштування скинуто. ESP32-CAM перезавантажується у setup-режим. "
                "Підключіться до Wi-Fi мережі KPI-Robot-Car-Setup і передайте "
                "нові SSID/пароль через форму."
            )

        def on_success(message: object) -> None:
            """Повернути кнопку скидання після успішного запиту."""

            self.wifi_reset_button.setEnabled(True)
            self._show_status_success(message)

        def on_error(message: str) -> None:
            """Повернути кнопку скидання після помилки."""

            self.wifi_reset_button.setEnabled(True)
            self._set_status(f"Не вдалося скинути Wi-Fi: {message}", level="ERROR")

        self._run_worker(reset_action, on_success, on_error)

    def _start_motion(self, command: str) -> None:
        """Почати утримувану команду руху та запустити keep-alive."""

        if self._recovering_connection:
            self._set_status("Триває автоматичне перепідключення до ESP32-CAM.", level="WARNING")
            return
        if not self._connected or self.robot_client is None:
            self._set_status("Спочатку натисніть «Підключитися».", level="WARNING")
            return
        if self._disconnect_requested:
            self._set_status("Зараз виконується відключення від робота.", level="WARNING")
            return

        # Активна команда запам’ятовується, щоб keep-alive таймер міг повторювати її.
        self._active_motion_command = command
        self._set_status(f"Утримується команда {command}. Відпустіть кнопку для stop.")
        self._send_motion(command, show_success=False)
        self._motion_keepalive_timer.start()

    def _repeat_motion_command(self) -> None:
        """Повторити активну команду руху для підтримки watchdog."""

        if self._active_motion_command is None:
            self._motion_keepalive_timer.stop()
            return
        self._send_motion(self._active_motion_command, show_success=False)

    def _send_motion(self, command: str, *, show_success: bool = True) -> None:
        """Надіслати команду руху у фоновому потоці."""

        if command == "stop":
            self._send_stop()
            return
        if not self._connected or self.robot_client is None or self._disconnect_requested:
            return

        # Поки інша команда в польоті, новий рух пропускається,
        # щоб не перемішати відповіді WebSocket.
        if (
            self._motion_command_in_flight
            or self._stop_in_flight
            or self._pending_stop
            or self._led_in_flight
        ):
            return
        client = self.robot_client
        speed = self.speed_spin.value()
        self._motion_command_in_flight = True

        def action() -> str:
            """Надіслати одну команду руху з поточною швидкістю."""

            client.set_speed(speed)
            result = client.send_motion_command(command)
            response = f": {result.response_text}" if result.response_text else ""
            return f"Команда {command} виконана{response}"

        def on_success(message: object) -> None:
            """Обробити успішне виконання команди руху."""

            self._motion_command_in_flight = False
            self._record_robot_success()
            if show_success:
                self._show_status_success(message)
            self._after_robot_request_finished()

        def on_error(message: str) -> None:
            """Зупинити повторення руху після помилки команди."""

            self._motion_command_in_flight = False
            self._active_motion_command = None
            self._motion_keepalive_timer.stop()
            if self._record_robot_error(message):
                return
            self._after_robot_request_finished()

        self._run_worker(action, on_success, on_error)

    def _send_stop(self) -> None:
        """Надіслати stop або відкласти його до завершення активної команди."""

        self._active_motion_command = None
        self._motion_keepalive_timer.stop()
        if self.robot_client is None:
            if self._disconnect_requested:
                self._finalize_disconnect()
            return
        if self._stop_in_flight:
            return
        if self._motion_command_in_flight or self._led_in_flight or self._speed_in_flight:
            self._pending_stop = True
            return
        self._pending_stop = False
        self._stop_in_flight = True
        client = self.robot_client

        # Stop має окремі callbacks, бо він завершує рух і може фіналізувати disconnect.
        self._run_worker(lambda: self._stop_action(client), self._stop_success, self._stop_error)

    def _stop_action(self, client: RobotClient) -> str:
        """Виконати команду stop через поточний клієнт робота."""

        result = client.stop()
        response = f": {result.response_text}" if result.response_text else ""
        return f"Команда stop виконана{response}"

    def _stop_success(self, message: object) -> None:
        """Обробити успішне завершення stop-команди."""

        self._stop_in_flight = False
        self._record_robot_success()
        self._show_status_success(message)
        self._after_robot_request_finished()

    def _stop_error(self, message: str) -> None:
        """Обробити помилку stop-команди."""

        self._stop_in_flight = False
        if self._record_robot_error(message):
            return
        self._after_robot_request_finished()

    def _send_current_speed(self) -> None:
        """Надіслати поточне значення швидкості, якщо робот готовий."""

        if (
            not self._connected
            or self.robot_client is None
            or self._disconnect_requested
            or self._speed_in_flight
            or self._has_active_robot_request()
        ):
            return
        client = self.robot_client
        speed = self.speed_spin.value()
        self._speed_in_flight = True

        def action() -> str:
            """Надіслати поточну швидкість до робота."""

            result = client.send_speed(speed)
            response = f": {result.response_text}" if result.response_text else ""
            return f"Швидкість встановлено {speed}{response}"

        self._run_worker(action, self._speed_success, self._speed_error)

    def _speed_success(self, message: object) -> None:
        """Обробити успішне встановлення швидкості."""

        self._speed_in_flight = False
        self._record_robot_success()
        self._show_status_success(message)
        self._after_robot_request_finished()

    def _speed_error(self, message: str) -> None:
        """Показати помилку встановлення швидкості."""

        self._speed_in_flight = False
        self._set_status(f"Не вдалося встановити швидкість: {message}", level="WARNING")
        self._after_robot_request_finished()

    def _toggle_led(self, checked: bool) -> None:
        """Увімкнути або вимкнути світлодіод через WebSocket."""

        if not self._connected or self.robot_client is None:
            self._set_led_checked(False)
            self._set_status("Спочатку натисніть «Підключитися».", level="WARNING")
            return
        if self._has_active_robot_request() or self._pending_stop:
            self._set_led_checked(not checked)
            self._set_status(
                "WebSocket-команда ще виконується. "
                "Повторіть перемикання світлодіода пізніше.",
                level="WARNING",
            )
            return
        client = self.robot_client

        # Візуальний стан кнопки оновлюється одразу, а при помилці повертається назад.
        self._set_led_checked(checked)
        self.led_button.setEnabled(False)
        self._led_in_flight = True
        self._run_worker(
            lambda: self._led_action(client, checked),
            lambda msg: self._led_success(msg, checked),
            lambda msg: self._led_error(msg, checked),
        )

    def _led_action(self, client: RobotClient, checked: bool) -> str:
        """Виконати команду LED для переданого стану."""

        result = client.led_on() if checked else client.led_off()
        response = f": {result.response_text}" if result.response_text else ""
        return f"Світлодіод {'увімкнено' if checked else 'вимкнено'}{response}"

    def _led_success(self, message: object, checked: bool) -> None:
        """Оновити інтерфейс після успішної LED-команди."""

        self._led_in_flight = False
        self._record_robot_success()
        self._set_led_checked(checked)
        self.led_button.setEnabled(self._connected)
        self._show_status_success(message)
        self._after_robot_request_finished()

    def _led_error(self, message: str, checked: bool) -> None:
        """Відкотити стан кнопки LED після помилки."""

        self._led_in_flight = False
        self._set_led_checked(not checked)
        self.led_button.setEnabled(self._connected)
        self._set_status(f"Не вдалося перемкнути світлодіод: {message}", level="WARNING")
        self._after_robot_request_finished()

    def _send_stop_blocking(self) -> None:
        """Синхронно надіслати stop під час закриття вікна."""

        if self.robot_client is None or self._has_active_robot_request():
            return
        try:
            self.robot_client.stop()
        except Exception:
            return

    def _start_video(self) -> None:
        """Запустити фоновий MJPEG-потік, якщо підключення активне."""

        if not self._connected or self._disconnect_requested:
            return

        # Перед новим запуском просимо старий потік завершитися і перевіряємо, чи він справді зник.
        self._stop_video()
        if self._video_thread is not None:
            self._set_status(
                "Попередній відеопотік ще завершується. "
                "Новий потік не запущено.",
                level="WARNING",
            )
            return
        self._video_thread = VideoStreamThread(
            host=self.host_edit.text().strip(),
            stream_port=self.stream_port_spin.value(),
        )
        self._video_thread.frame_ready.connect(self._update_video_frame)
        self._video_thread.status_changed.connect(self._set_status)
        self._video_thread.start()

    def _stop_video(self) -> None:
        """Зупинити MJPEG-потік і повернути placeholder відео."""

        if self._video_thread is not None:
            stopped = self._video_thread.stop()
            if stopped:
                self._video_thread = None
            else:
                self._set_status(
                    "Відеопотік не завершився коректно. "
                    "Потік залишено активним до завершення.",
                    level="WARNING",
                )

        # Після stop завжди очищаємо QLabel, навіть якщо фоновий потік завершується із затримкою.
        self.video_label.setPixmap(QPixmap())
        self.video_label.setText(VIDEO_PLACEHOLDER_TEXT)

    def _update_video_frame(self, image: QImage) -> None:
        """Показати отриманий кадр у QLabel з масштабуванням."""

        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            self.video_label.size(),
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.video_label.setPixmap(scaled)

    def _create_robot_client(self) -> RobotClient:
        """Створити RobotClient із поточних полів інтерфейсу."""

        client = RobotClient(
            self.host_edit.text().strip(),
            command_port=self.command_port_spin.value(),
        )
        client.set_speed(self.speed_spin.value())
        return client

    def _has_active_robot_request(self) -> bool:
        """Перевірити, чи зараз виконується будь-який запит до робота."""

        return (
            self._motion_command_in_flight
            or self._stop_in_flight
            or self._led_in_flight
            or self._speed_in_flight
            or self._reconnect_in_flight
        )

    def _record_robot_success(self) -> None:
        """Скинути лічильник послідовних помилок робота."""

        self._robot_error_count = 0

    def _record_robot_error(self, message: str) -> bool:
        """Зафіксувати помилку робота і запустити відновлення за порогом."""

        self._robot_error_count += 1
        self._show_status_error(message)
        if self._robot_error_count >= self._max_robot_errors:
            self._handle_robot_connection_lost()
            return True
        return False

    def _handle_robot_connection_lost(self) -> None:
        """Обробити втрату зв’язку та за потреби запустити перепідключення."""

        self._motion_keepalive_timer.stop()
        self._active_motion_command = None
        self._pending_stop = False
        self._motion_command_in_flight = False
        self._stop_in_flight = False
        self._led_in_flight = False
        self._speed_in_flight = False
        self._reconnect_in_flight = False
        self._disconnect_requested = False
        self._robot_error_count = 0

        self._stop_video()
        self._close_robot_client(send_stop=False)

        if ROBOT_AUTO_RECONNECT_ENABLED and not self._manual_disconnect_requested:
            # Автоматичне відновлення закриває старий клієнт і пробує створити новий за таймером.
            self._recovering_connection = True
            self._reconnect_attempts = 0
            self._set_connected(False)
            self._set_status(
                "Зв’язок із ESP32-CAM тимчасово втрачено. Виконую автоматичне перепідключення...",
                level="WARNING",
            )
            self._reconnect_timer.start()
            return

        self._recovering_connection = False
        self._set_connected(False)
        self._set_status(
            "Зв’язок із ESP32-CAM втрачено. "
            "Натисніть «Підключитися» повторно.",
            level="ERROR",
        )

    def _try_auto_reconnect(self) -> None:
        """Виконати одну спробу автоматичного перепідключення."""

        if self._manual_disconnect_requested:
            self._reconnect_timer.stop()
            self._recovering_connection = False
            self._reconnect_in_flight = False
            self._set_connected(False)
            return

        if self.robot_client is not None or self._has_active_robot_request():
            return

        # Кожна спроба створює новий RobotClient, тому не використовує пошкоджений socket.
        self._reconnect_attempts += 1
        self._reconnect_in_flight = True
        self._set_status(f"Спроба автоматичного перепідключення #{self._reconnect_attempts}...")

        try:
            self.robot_client = self._create_robot_client()
        except ValueError as exc:
            self._reconnect_timer.stop()
            self._recovering_connection = False
            self._reconnect_in_flight = False
            self._set_connected(False)
            self._set_status(str(exc), level="ERROR")
            return

        client = self.robot_client

        def action() -> str:
            """Перевірити WebSocket під час автоматичного перепідключення."""

            client.ping()
            client.stop()
            return "Підключення до ESP32-CAM відновлено."

        def on_success(message: object) -> None:
            """Відновити стан GUI після успішного перепідключення."""

            if self._manual_disconnect_requested or self.robot_client is not client:
                # Запізнілий reconnect-результат не повинен оживити вручну закрите підключення.
                self._close_robot_client(send_stop=False)
                self._set_connected(False)
                return
            self._reconnect_timer.stop()
            self._recovering_connection = False
            self._reconnect_in_flight = False
            self._robot_error_count = 0
            self._disconnect_requested = False
            self._pending_stop = False
            self._set_connected(True)
            self._show_status_success(message)

            # Після reconnect синхронізуємо швидкість і повертаємо відео з невеликою паузою.
            self._send_current_speed()
            QTimer.singleShot(800, self._start_video)

        def on_error(message: str) -> None:
            """Підготувати наступну спробу після помилки перепідключення."""

            if self.robot_client is not client:
                self._reconnect_in_flight = False
                return
            self._reconnect_in_flight = False
            self._close_robot_client(send_stop=False)
            self._set_connected(False)
            self._set_status(f"Автоматичне перепідключення не вдалося: {message}", level="WARNING")

        self._run_worker(action, on_success, on_error)

    def _after_robot_request_finished(self) -> None:
        """Запустити відкладений stop або фіналізацію відключення."""

        if self._pending_stop and not self._has_active_robot_request():
            self._send_stop()
            return
        if self._disconnect_requested and not self._has_active_robot_request():
            self._finalize_disconnect()

    def _finalize_disconnect(self) -> None:
        """Завершити стан ручного відключення після всіх команд."""

        self._pending_stop = False
        self._disconnect_requested = False
        self._close_robot_client(send_stop=False)
        self._set_connected(False)
        self._set_status("Відключено. Команду stop надіслано, якщо ESP32-CAM доступна.")

    def _close_robot_client(self, *, send_stop: bool = True) -> None:
        """Закрити поточного RobotClient і очистити посилання."""

        if self.robot_client is not None:
            self.robot_client.close(send_stop=send_stop)
            self.robot_client = None

    def _run_worker(
        self,
        action: Callable[[], Any],
        on_success: Callable[[object], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Запустити дію в QThreadPool і під’єднати обробники результату."""

        worker = CommandWorker(action)
        worker.signals.success.connect(on_success)
        worker.signals.error.connect(on_error)
        self._thread_pool.start(worker)

    def _show_status_success(self, message: object) -> None:
        """Показати успішне статусне повідомлення."""

        self._set_status(str(message))

    def _show_status_error(self, message: str) -> None:
        """Показати статусне повідомлення про помилку."""

        self._set_status(message, level="ERROR")

    def _set_status(self, message: str, level: str = "INFO") -> None:
        """Оновити status bar і додати запис у журнал без дублювання."""

        # Нормалізація пробілів робить повідомлення компактними в status bar і журналі.
        clean_message = " ".join(str(message).split())
        if not clean_message:
            return
        level = level.upper()
        self.statusBar().showMessage(clean_message)
        log_key = (level, clean_message)
        if log_key == self._last_log_entry:
            return
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_edit.appendPlainText(f"{timestamp} [{level:<7}] {clean_message}")

        # Після додавання запису журнал прокручується до останнього рядка.
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum()
        )
        self._last_log_entry = log_key

    def _copy_log_to_clipboard(self) -> None:
        """Скопіювати весь журнал статусів у буфер обміну."""

        QApplication.clipboard().setText(self.log_edit.toPlainText())
        self._set_status("Журнал скопійовано в буфер обміну.")

    def _clear_log(self) -> None:
        """Очистити журнал статусів і скинути останній запис."""

        self.log_edit.clear()
        self._last_log_entry = None
        self._set_status("Журнал очищено.")

    def _set_led_checked(self, checked: bool) -> None:
        """Оновити позначення LED-кнопки без запуску обробника toggled."""

        # blockSignals потрібен, щоб програмна зміна checked не відправила LED-команду повторно.
        self.led_button.blockSignals(True)
        self.led_button.setChecked(checked)
        self.led_button.setText(f"Світлодіод: {'увімкнено' if checked else 'вимкнено'}")
        self.led_button.blockSignals(False)

    def _set_connected(self, connected: bool) -> None:
        """Оновити доступність кнопок відповідно до стану підключення."""

        self._connected = connected

        # Під час recovery підключення вже неактивне, але кнопка "Відключитися" має зупинити спроби.
        self.connect_button.setEnabled(not connected and not self._recovering_connection)
        self.disconnect_button.setEnabled(connected or self._recovering_connection)
        for button in (
            self.forward_button,
            self.backward_button,
            self.left_button,
            self.right_button,
        ):
            button.setEnabled(connected)
        self.led_button.setEnabled(connected)
        if not connected:
            self._set_led_checked(False)
        self.stop_button.setEnabled(True)
