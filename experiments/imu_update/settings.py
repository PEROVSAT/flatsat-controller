"""Code-owned stimulus and timing settings for the IMU update proof."""

BOOT_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 20.0
UTC_TIMEOUT_S = 30.0
TOLERANCE_MS = 10.0
ACCEL_RANGE_G = 2
GYRO_RANGE_DPS = 250
SAMPLE = {"sequence": 1701, "accel_m_s2": [0.0, 0.0, 9.80665], "gyro_rad_s": [0.15, -0.25, 0.35], "temperature_c": 25.0}
FLIGHT_SELECTIONS = {"IMU": "hardware", "AMU": "public-mock", "FLASH": "virtual", "CONSOLE": "uart"}
