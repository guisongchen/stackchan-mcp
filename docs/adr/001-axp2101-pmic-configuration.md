# ADR 001: AXP2101 PMIC Configuration for CoreS3

## Status

Accepted

## Context

The M5Stack CoreS3 (StackChan board) uses an AXP2101 PMIC (Power Management IC). The firmware's PMIC initialization was based on the kevin-box-2 reference board rather than the M5Unified library's official CoreS3 configuration. This caused two problems:

1. **Battery boot failure**: Device could not boot on battery alone — screen flashed briefly then went dark. Required USB power to start.
2. **Missing boost converter enable**: The SY7088 boost converter, which is critical for battery-powered system voltage on CoreS3, was never enabled in the firmware initialization sequence.

## Decision

### 1. Align PMIC registers with M5Unified official CoreS3 configuration

**Decision:** Replace the custom PMIC register initialization with the M5Unified library's tested configuration for `board_M5StackCoreS3` / `board_M5StackChan`.

**Rationale:**
- The M5Unified configuration has been validated on thousands of real CoreS3 devices
- Our custom configuration mixed kevin-box-2 register values with CoreS3-specific LDO config, producing an untested hybrid

**Key register changes:**

| Register | Before (custom) | After (M5Unified) | Purpose |
|----------|----------------|-------------------|---------|
| `0x10` | *not set* | `0x30` | PMU common config: battery detection + internal off-discharge |
| `0x30` | `0b111111` | `0x0F` | VBUS/ADC control — `0b111111` was an illegal all-bits-set value |
| `0x69` | `0b00110101` | `0x11` | CHGLED setting |
| `0x27` | `0x10` | `0x00` | Power key hold time |
| `0x14-0x16` | *set to kevin-box-2 values* | *not set* | Charging/VBUS parameters not validated for CoreS3 |
| `0x22, 0x24` | *set* | *not set* | Power button/Vsys threshold not validated for CoreS3 |
| `0x50, 0x61-0x64` | *set* | *not set* | TS pin/charger params not validated for CoreS3 |
| `0x92` | *not set* | `13` | ALDO1 = 1.8V (for AW88298 audio amp) |
| `0x93` | *not set* | `28` | ALDO2 = 3.3V (for ES7210 ADC) |

**Location:** `firmware/main/boards/stackchan/stackchan.cc:67-82`

### 2. Enable SY7088 boost converter via AW9523 IO expander

**Decision:** During AW9523 initialization, set the IO expander bit that enables the SY7088 boost converter, matching M5Unified's `bitOn(AW9523, 0x03, 0x80)`.

**Rationale:**
- The SY7088 provides the boosted 5V bus voltage that CoreS3 needs for stable system operation on battery
- Without it, the AXP2101 alone cannot maintain adequate VSYS when the battery voltage drops under load
- This is what caused the "screen flashes briefly then dies" symptom — the device powered up on residual charge but immediately collapsed under boot current

**Location:** `firmware/main/boards/stackchan/stackchan.cc:117-118` (Aw9523::EnableBoost), `:2312` (InitializeAw9523)

## Consequences

### Positive

- **Battery boot works**: Device now boots reliably on battery power alone
- **Validated configuration**: PMIC registers match M5Unified's tested CoreS3 defaults
- **Cleaner init**: Removed 11 unvalidated register writes, simplified Pmic constructor

### Negative

- **Loss of explicit charging parameters** (`0x14-0x16`, `0x61-0x64`): These revert to AXP2101 factory defaults. While M5Unified does not customize these for CoreS3, they may need future tuning if charging behavior is suboptimal
- **Loss of explicit Vsys PWROFF threshold** (`0x24`): Factory default is 2.6V, which is lower than the kevin-box-2's 3.2V. Over-discharge protection relies on the PMIC's built-in battery detection (`0x10 = 0x30`)

## Alternatives Considered

### Alternative 1: Keep kevin-box-2 charging parameters

Keep the explicitly configured charging registers (`0x14-0x16`, `0x61-0x64`) while adopting the M5Unified LDO and power-path settings.

**Rejected:**
- kevin-box-2 is a different board with different battery capacity and power topology
- The PMIC's factory defaults for charging have been sufficient on CoreS3 in practice (M5Unified ships without overriding them)
- Mixing configs from two boards creates an untested hybrid

### Alternative 2: Add only `0x10 = 0x30` and `0x30 = 0x0F`

Fix the two critically wrong registers without doing a full realignment.

**Rejected:**
- Leaves the unvalidated charging registers from kevin-box-2 in place
- Inconsistent approach — better to align fully with the official reference

## Implementation Notes

### When updating PMIC configuration

Always cross-reference M5Unified's `Power_Class.cpp` for `board_M5StackCoreS3` / `board_M5StackChan`:

https://github.com/m5stack/M5Unified/blob/master/src/utility/Power_Class.cpp

Relevant section uses:
- `Axp2101.begin()` — initializes AXP2101 at address `0x34`
- Registers written: `0x90, 0x92, 0x93, 0x94, 0x95, 0x27, 0x69, 0x10, 0x30`
- AW9523 boost: `bitOn(AW9523_ADDR, 0x03, 0x80)`

### General lesson

**Never mix PMIC configurations from different boards.** Each PMIC-register combination is part of a holistic power topology that is validated for a specific PCB layout, battery chemistry, and peripheral set. Guessing register values without the reference implementation leads to subtle, hard-to-diagnose power issues.

## References

- [M5Unified Power_Class.cpp — CoreS3 AXP2101 init](https://github.com/m5stack/M5Unified/blob/master/src/utility/Power_Class.cpp)
- `firmware/main/boards/stackchan/stackchan.cc` — Pmic class and Aw9523 class
