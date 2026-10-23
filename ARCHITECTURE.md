# FlatSat Architecture — Initial Draft

Status: Proposed implementation direction for [FlatSat.md](FlatSat.md).
Start with static inputs, flashing, and manual OBC observation; extend this to
the radio-based test as the flight software and shared protocol become ready.
Earlier component specs remain design references, not a checklist of features
needed before this first implementation can work.

## What each component provides

### Emulated devices

The MPU6050 emulator presents the sensor's I2C interface and serves constant
configured readings. Initially, select those values at build time. It needs
no live host input or Basilisk connection.

The radio emulator presents the radio interface and behavior exercised by the
flight software. It receives the OBC's transmitted packets and exports their
original bytes over a separate debug UART. It also provides a path for packets
from the host to reach the OBC as received radio traffic when commanding is
implemented. It is an endpoint, with no RF or changing environmental model.

Device debug output can support bring-up. Structured packet records and
diagnostic text must be distinguishable once they share a UART.

### Bridge

A small Python process reads configured serial ports, timestamps received data
with the host's monotonic clock, and publishes observations for other programs.
It opens the USB serial interfaces exposed by the Raspberry Pi debug probes.

Initially it handles raw byte chunks, including ordinary OBC console output.
When structured device records exist, it can assemble their framing and wrap
them without interpreting the contained flight telemetry.

Use one ZeroMQ PUB endpoint, bound to localhost, with a topic per configured
source, such as `obc.console` or `radio0.rx`. Subscribers select topics; each
device does not need its own IPC port.

The bridge also appends observations to a local capture file before publishing
them. The file supports inspection after the run and avoids making a live
subscription the only copy of the evidence. This is ordinary capture, not a
durable messaging or replay service. Serial and capture errors are surfaced;
known capture failures prevent a valid test verdict.

### Controller

The implementation structure and task breakdown are in
[CONTROLLER_DESIGN.md](CONTROLLER_DESIGN.md).

The implemented controller uses a small typed Bench containing independent
firmware, operator, and named-target capabilities. Runtime owns evidence and
process lifetime; firmware owns build/flash behavior and source metadata; one
assembly module wires them. Experiments have their own typed parameters and
explicit registration. Installation settings and named build requests are
separate typed objects even while they share one YAML file.

The current capability builds through west dbuild, flashes through OpenOCD, and
records operator confirmations. Every build and flash gets an independent
operation directory. Per-target probe serials are supported, preserving the
existing runner default when omitted. Bridge startup, capture, and subscriptions
remain planned work. Manual power and solar-simulator operation are sufficient
initially.

As radio telemetry becomes available, the controller decodes packets using the
shared protocol library and compares readings and elapsed times with the test
configuration. It retains the configuration, firmware identifiers, capture,
and result together. Final evaluation can read the capture file.

### Basilisk

Basilisk is outside V1. Eventually it publishes simulated device inputs through
IPC. A bridge input path will route those messages to device UARTs. The
controller may also subscribe for observability. These interfaces need no
implementation or finalized message catalogue now.

### Shared protocol library

The existing library owns shared message definitions and serialization,
including flight telemetry and commanding. Extend it with host observation
wrappers and, later, simulated device inputs when those paths are implemented.

Use its recursive composition to represent an observation containing an
original radio packet. The bridge understands the outer framing and wrapper;
the controller interprets the packet. Raw log capture does not depend on the
library being complete.

## How components interface

### OBC and physical or emulated hardware

The OBC communicates directly with real AMUs and the emulated MPU6050 over
their hardware bus interfaces. It communicates directly with the radio
emulator over the radio's hardware interface. The host bridge is outside
these bus paths. Solar illumination is applied physically to the cells.

### Devices, probes, and host

Each debug probe supplies two independent functions: SWD for OpenOCD flashing,
and a USB serial interface for UART traffic. Explicit configuration identifies
which probe and serial port belong to each board.

The bridge owns a serial port while capturing it. Manual interaction uses
the bridge's displayed/captured output, or a terminal after releasing that
port. A terminal and the bridge must not compete to read the same port.

### Bridge and subscribers

An observation identifies its source and payload kind, carries the received
bytes unchanged, and includes a monotonic host receive timestamp. A capture
belongs to one bridge invocation; record its identity so timestamps from
unrelated captures are not accidentally compared.

Timestamp immediately after each serial read. A complete framed record uses
the timestamp of the read containing its final byte. This measures host
receipt, including device logging, UART, USB, and host buffering delays; it
does not establish the OBC's packet-creation time or exact radio-emulator
receipt time. Controller timing markers use the same host monotonic clock.
Choosing the timing reference and measuring whether this observation path
is adequate for deadline X remain necessary before claiming timing accuracy.

During bring-up, a simple JSON-lines capture/envelope with base64 byte payloads
is sufficient. Structured protocol wrappers replace this temporary encoding
when integrated; the source, timestamp, and original-payload meanings stay
the same. UART record boundaries must be defined by the protocol framing,
not inferred from USB read boundaries.

ZeroMQ pub-sub is a live observation path and can lose messages, including
while subscribers connect. The local capture is the basis for final analysis;
its presence does not prove that upstream UART delivery was lossless.

The future commanding path is explicit host-to-device traffic through the
bridge, separate from observation topics. Define a request/reply interface
when implementing it; a bridge acknowledgement will mean only the stated
local delivery step, not that the OBC accepted or executed the command.

## First implementation checkpoint

Flash a static MPU6050 emulator and the OBC, set the solar simulator manually,
and inspect captured OBC output. Deliver working flashing and capture scripts
before automated verdicts, radio commanding, or Basilisk integration.

This checkpoint is bring-up evidence. The V1 test in FlatSat.md is complete
only when the required readings and timing are verified through radio packets.

## Interface references

- [Raspberry Pi Debug Probe documentation](https://www.raspberrypi.com/documentation/microcontrollers/debug-probe.html): SWD and USB serial interfaces.
- [ZeroMQ guide: pub-sub limitations](https://zguide.zeromq.org/docs/chapter5/): connection and delivery limitations.
