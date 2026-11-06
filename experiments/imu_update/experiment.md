# IMU update and readback

Build and flash the MPU6050 emulator, confirm it is ready, then build and flash the OBC with the hardware IMU selection. Verify UTC synchronization and prove a post-stimulus I2C readback differs from a normal baseline read and matches the expected bytes. Both configured targets require explicit SWD probe serials.
