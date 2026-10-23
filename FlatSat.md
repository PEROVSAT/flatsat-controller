# FlatSat Specification — Initial Draft

Status: Working draft. Numerical limits and test configurations remain to be agreed.

## Purpose

The FlatSat provides a physical test environment for evaluating flight software interacting with spacecraft hardware.

Its eventual purpose is to test the timing and accuracy of nominal operation, and fault tolerance when hardware or its inputs behave abnormally.

The initial scope is narrower: demonstrate that the flight software acquires nominal AMU and MPU6050 readings and reports the expected values through its radio interface within an agreed time.

## Test environment

The FlatSat contains:

- **A real OBC**, running the flight software under test.
- **Real AMUs and photovoltaic cells**, exposed to illumination from the solar simulator.
- **A solar simulator**, providing a controlled physical representation of the selected solar stimulus.
- **An emulated MPU6050**, presenting the expected I2C interface to the OBC and supplying selected sensor values.
- **An emulated radio endpoint**, receiving transmissions from the OBC for observation and supporting packet delivery to the OBC for commanding.
- **Supporting host software**, configuring the test environment, collecting radio evidence, and evaluating results.

Physical AMU and cell responses are measured by the flight software. MPU6050 readings are supplied by emulation.

The eventual system may use Basilisk to supply changing simulated conditions. The initial static test does not require a running dynamics simulation.

## Initial test capability

For a specified static test configuration:

- The solar simulator maintains a selected illumination setting throughout the observation interval.
- The MPU6050 emulator supplies selected constant sensor values, subject to the device’s configuration and numerical representation.
- The OBC acquires readings through its normal hardware interfaces.
- The flight software produces radio telemetry containing the AMU and MPU6050 readings.
- The radio endpoint records the transmitted packets and when they were observed.

The test evaluates flight software behavior using only evidence received through the radio endpoint. Emulator diagnostics, debug consoles, and direct bus observations do not establish a passing flight software result.

The setup may use separate measurements or diagnostics to establish the applied stimulus and whether the test environment is functioning.

## Accuracy

A qualifying telemetry report contains the required AMU and MPU6050 readings, correctly associated with their source channels and interpreted according to the telemetry format.

For the MPU6050, expected values follow from the selected emulator inputs, device configuration, and flight software’s documented conversion and encoding.

For the AMUs, expected values or acceptable ranges are established for the physical cells under the selected illumination and test conditions. A constant solar-simulator setting alone does not establish an exact expected electrical reading.

The test configuration identifies the required readings, expected values or ranges, and permitted tolerances. These are fixed before evaluating the run.

## Timing

A qualifying telemetry report must be observed at the radio endpoint within **X** of a defined reference event.

The reference event, observation timestamp meaning, and value of **X** must be agreed before this requirement can be verified. Possible reference events include OBC startup or a command requesting readings; the selected event must be identifiable with sufficient accuracy.

This initial requirement measures the elapsed time until telemetry is observed. Packet-creation time, individual acquisition times, and sample age are evaluated only if the radio telemetry provides sufficient evidence to establish them.

## Results and evidence

The retained evidence includes the test configuration, flight software version, expected readings and tolerances, timing reference, received radio packets, and their observation times.

Results distinguish:

- **PASS:** the required readings satisfy the accuracy and timing criteria.
- **FAIL:** a valid test establishes that one or more criteria were not met.
- **INVALID:** the stimulus, radio capture, timing reference, or other necessary test conditions were not established sufficiently to judge the flight software.

## Scope boundaries and unresolved choices

This increment covers static nominal inputs. Dynamic simulation, injected faults, fault-recovery behavior, and broader timing guarantees are later work.

Before implementation, resolve:

- Required telemetry fields and whether one or several packets constitute a complete report.
- AMU reference measurements, operating conditions, and tolerances.
- MPU6050 input values and comparison tolerances.
- Timing reference and deadline **X**.
- Acceptable differences between the tested flight software configuration and the intended flight configuration.

Component responsibilities, communication protocols, preparation and execution sequences, and implementation details belong in the architecture and test procedure.
