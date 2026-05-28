"""Центральні налаштування контролера ESP32-CAM робота."""

# Порти та URL-шляхи мають збігатися з HTTP/WebSocket handlers у прошивці.
DEFAULT_WS_PORT = 80
DEFAULT_STREAM_PORT = 81
DEFAULT_STREAM_PATH = "/stream"
DEFAULT_WS_PATH = "/ws"
DEFAULT_ACTION_PATH = "/action"
DEFAULT_WIFI_PATH = "/wifi"
DEFAULT_WIFI_RESET_PATH = "/wifi/reset"

# DEFAULT_SETUP_HOST використовується для setup-точки доступу після скидання Wi-Fi.
# DEFAULT_HOST є початковою адресою робота у графічному інтерфейсі.
DEFAULT_SETUP_HOST = "192.168.4.1"
DEFAULT_HOST = "192.168.4.1"

# UDP discovery приймає broadcast-повідомлення з таким підписом на цьому порту.
DISCOVERY_PORT = 4210
DISCOVERY_TIMEOUT_SECONDS = 5.0
DISCOVERY_SIGNATURE = "KPI_ROBOT_CAR"

# Обмеження PWM-швидкості повторюють допустимий діапазон прошивки.
DEFAULT_SPEED = 170
MIN_SPEED = 85
MAX_SPEED = 255

# Timeout-и короткі, щоб GUI не зависав на недоступному роботі.
WS_CONNECT_TIMEOUT_SECONDS = 1.5
WS_RESPONSE_TIMEOUT_SECONDS = 1.5
WIFI_TIMEOUT_SECONDS = 3.0
VIDEO_RECONNECT_DELAY_SECONDS = 1.0

# Watchdog і keep-alive визначають, як довго робот рухається без нової команди.
MOTION_WATCHDOG_MS = 500
MOTION_KEEPALIVE_INTERVAL_MS = 300

# Поріг помилок і таймер перепідключення керують автоматичним відновленням зв’язку.
ROBOT_ERROR_THRESHOLD = 5
ROBOT_RECONNECT_INTERVAL_MS = 2000
ROBOT_AUTO_RECONNECT_ENABLED = True

# Тексти інтерфейсу зібрані тут, щоб не дублювати сталі значення у GUI.
APP_NAME = "KPI Robot Vision Car"
VIDEO_PLACEHOLDER_TEXT = "Відеопотік не запущено"
