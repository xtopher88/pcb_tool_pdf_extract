# Component Profile Schema

**Version:** 1.0
**Date:** 2026-03-23
**Purpose:** Standardized format for extracting IC datasheet information for schematic validation and firmware code generation.

Each component profile is a YAML file. This document defines every field, its type, valid values, and which component categories require it.

---

## Component Categories

| Value | Description |
|-------|-------------|
| `sensor` | Sensor IC with digital interface and register map (IMU, magnetometer, etc.) |
| `power` | Power management IC (LDO, buck, charger, load switch) |
| `mcu` | Microcontroller or microprocessor |
| `mux` | Bus multiplexer or switch (I2C mux, analog mux) |
| `interface` | Interface IC (USB bridge, level shifter, transceiver) |
| `passive` | Discrete passive (crystal, ferrite, fuse) |

---

## Required Sections by Category

| Section | sensor | power | mcu | mux | interface | passive |
|---------|:------:|:-----:|:---:|:---:|:---------:|:-------:|
| `component` | R | R | R | R | R | R |
| `schematic.supply` | R | R | R | R | R | R |
| `schematic.pinout` | R | R | R | R | R | O |
| `schematic.decoupling` | R | R | R | O | O | — |
| `schematic.design_checks` | R | R | R | R | O | — |
| `schematic.required_external` | O | R | O | O | O | — |
| `schematic.feedback_network` | — | R* | — | — | — | — |
| `schematic.enable_logic` | — | R* | — | — | — | — |
| `software.interface` | R | — | — | R | R | — |
| `software.addressing` | R* | — | — | R | R* | — |
| `software.registers` | R | — | — | O | O | — |
| `software.init_sequence` | R | — | — | R | O | — |
| `software.data_output` | R* | — | — | — | — | — |
| `software.driver_notes` | O | — | — | O | O | — |

**R** = Required, **O** = Optional, **—** = Not applicable
**R\*** = Required only if the feature exists on the device

---

## Full Field Reference

### `component` block — Identity (all categories)

```yaml
component:
  part_number: string        # Exact manufacturer part number, e.g. "BMM350"
  manufacturer: string       # Manufacturer name, e.g. "Bosch Sensortec"
  description: string        # One-line functional description
  category: enum             # See category table above
  package: string            # Package type, e.g. "LGA-14", "SOT-23-5", "TQFP-100"
  datasheet_file: string     # Filename in /datasheets/, e.g. "bst-bmm350-ds001.pdf"
  datasheet_version: string  # Document version/revision from datasheet cover
  profile_date: date         # ISO 8601, date this profile was created/updated
  profile_completeness:      # List of namespaces populated; omit if section not filled
    - schematic
    - software
```

---

### `schematic` block — Electrical & PCB validation

#### `schematic.supply` — Power rails

```yaml
schematic:
  supply:
    - rail: string           # Rail name as labeled in datasheet, e.g. "VDD", "VDDIO", "VIN"
      pin: string            # Pin name(s), e.g. "VDD" or "VDD,VDD2" for multiple
      min: float             # Minimum voltage (V)
      typ: float             # Typical voltage (V); omit if not specified
      max: float             # Maximum voltage (V)
      unit: V                # Always "V"
      note: string           # Optional: e.g. "Must power up before VDDIO"
```

#### `schematic.current` — Supply current

```yaml
  current:
    - mode: string           # Operating mode name, e.g. "active", "standby", "shutdown"
      typ: float
      max: float             # Omit if not specified
      unit: enum             # "mA" | "uA" | "nA"
      condition: string      # Optional: condition from datasheet, e.g. "400kHz I2C, ODR=100Hz"
```

#### `schematic.absolute_max` — Absolute maximum ratings

```yaml
  absolute_max:
    - param: string          # Parameter name, e.g. "VDD", "IO voltage", "ESD"
      value: string          # Value as string to allow ranges and units, e.g. "4.8" or "2kV HBM"
      unit: string           # e.g. "V", "mA", "°C" — omit if already in value
```

#### `schematic.pinout` — Pin descriptions

```yaml
  pinout:
    - pin: int|string        # Pin number (int) or name for BGA/LGA (e.g. "A1")
      name: string           # Pin name as in datasheet
      type: enum             # PWR | GND | IN | OUT | BIDIR | OD | ANALOG | CLK | NC
      description: string    # Functional description; include config options if applicable
```

**Pin type values:**

| Value | Meaning |
|-------|---------|
| `PWR` | Power supply input |
| `GND` | Ground |
| `IN` | Digital input |
| `OUT` | Digital output (push-pull) |
| `BIDIR` | Bidirectional digital (e.g. I2C SDA) |
| `OD` | Open-drain output |
| `ANALOG` | Analog signal pin |
| `CLK` | Clock input or output |
| `NC` | No connect |

#### `schematic.decoupling` — Bypass capacitor requirements

```yaml
  decoupling:
    - rail: string           # Supply rail name this decoupling applies to
      value: string          # Capacitance, e.g. "100nF", "10uF"
      type: string           # "ceramic" | "electrolytic" | "tantalum"
      voltage_rating: string # Minimum voltage rating, e.g. "10V" — omit if obvious
      placement: string      # Optional: placement note, e.g. "Within 0.5mm of VDD pin"
```

#### `schematic.required_external` — Mandatory external components

```yaml
  required_external:
    - description: string    # e.g. "10k pull-up on EN pin"
      reason: string         # Why it's required
```

#### `schematic.feedback_network` — Power ICs only

```yaml
  feedback_network:
    pin: string              # Feedback pin name
    vref: float              # Internal reference voltage (V)
    formula: string          # Vout formula, e.g. "Vout = Vref * (1 + R1/R2)"
    example:
      vout: float            # Target output voltage
      r1: string             # Top resistor, e.g. "100k"
      r2: string             # Bottom resistor
```

#### `schematic.enable_logic` — Power ICs only

```yaml
  enable_logic:
    pin: string              # Enable pin name
    active: enum             # "high" | "low"
    threshold_high: float    # Logic high threshold (V)
    threshold_low: float     # Logic low threshold (V)
    internal_pullup: bool    # true if pin has internal pull-up
    note: string             # e.g. "Float enables output by default"
```

#### `schematic.design_checks` — Rules to verify on schematic

```yaml
  design_checks:
    - string                 # Human-readable check, written as a condition to verify.
                             # e.g. "ADDR pin must not float — tie high or low explicitly"
                             # e.g. "INT requires external pull-up if used"
```

---

### `software` block — Interface & firmware

#### `software.interface` — Communication protocol

```yaml
software:
  interface:
    protocol: enum           # I2C | SPI | I2C_SPI | UART | PWM | analog | 1-Wire
    max_clock:
      value: int
      unit: enum             # kHz | MHz
    logic_level:
      value: float
      unit: V
    spi_mode: int            # SPI only: CPOL/CPHA mode number (0-3)
    spi_bit_order: enum      # SPI only: "MSB" | "LSB"
    spi_word_size: int       # SPI only: bits per word, typically 8
    i2c_supports_10bit: bool # I2C only: true if 10-bit addressing supported
```

#### `software.addressing` — I2C/SPI device addressing

```yaml
  addressing:
    default: string          # Default address in hex, e.g. "0x14"
    alternate: string        # Alternate address if address pin option exists
    range: string            # For configurable address ICs, e.g. "0x70-0x77"
    config_pin: string       # Pin name that selects address
    config_table:            # Optional: full table if >2 options
      - pin_state: string    # e.g. "GND" | "VDD" | "float"
        address: string
    note: string             # Any special addressing notes
```

#### `software.interrupt` — Interrupt output behavior

```yaml
  interrupt:
    pins:
      - name: string         # Pin name
        active: enum         # "high" | "low"
        type: enum           # "push-pull" | "open-drain"
        latched: bool        # true if interrupt latches until cleared
        clear_method: string # How to clear: "read_status" | "read_data" | "write_clear"
        sources: [string]    # List of events that trigger this interrupt
```

#### `software.registers` — Register map

```yaml
  registers:
    - addr: string           # Hex address, e.g. "0x00"
      name: string           # Register name as in datasheet
      access: enum           # R | W | RW | RC (read-clear)
      reset: string          # Reset value in hex, e.g. "0x00"
      description: string    # What the register does
      fields:                # Optional: bit-field breakdown
        - bits: string       # Bit range, e.g. "7:4" or "0"
          name: string       # Field name
          access: enum       # R | W | RW
          reset: string      # Reset value for this field
          description: string
          values:            # Optional: enumerated values
            - value: string  # e.g. "0b00"
              meaning: string
```

#### `software.init_sequence` — Power-on initialization steps

```yaml
  init_sequence:
    - step: int
      action: enum           # power_on | delay | write | read | verify | assert
      register: string       # Register name (for write/read/verify actions)
      value: string          # Value to write (hex), for write action
      expected: string       # Expected value (hex), for verify action
      mask: string           # Optional: bitmask to apply before comparing
      delay_ms: float        # For delay action
      note: string           # Human-readable explanation
```

**Action values:**

| Value | Meaning |
|-------|---------|
| `power_on` | Apply power, wait for POR |
| `delay` | Wait a fixed time |
| `write` | Write value to register |
| `read` | Read register (result used in next step) |
| `verify` | Read register and assert expected value |
| `assert` | Check a condition (e.g. INT pin state) |

#### `software.data_output` — Sensor data format (sensor category)

```yaml
  data_output:
    axes: [string]           # e.g. ["X", "Y", "Z"] or ["temperature"]
    format: string           # e.g. "16-bit signed two's complement"
    registers: [string]      # Register names containing output data, in read order
    lsb_per_unit: float      # Sensitivity, LSB per physical unit — omit if varies by config
    unit: string             # Physical unit, e.g. "uT", "mdps", "mg", "°C"
    scale_note: string       # If sensitivity is config-dependent, describe here
```

#### `software.driver_notes` — Implementation notes

```yaml
  driver_notes:
    - string                 # Important note for driver implementer.
                             # e.g. "Always verify CHIP_ID before writing configuration"
                             # e.g. "Suspend mode is default — must explicitly start measurements"
```

---

## Naming Conventions

- All hex values: lowercase with `0x` prefix, e.g. `0x14`, `0xff`
- All register names: UPPER_SNAKE_CASE matching the datasheet exactly
- All pin names: match datasheet exactly (case-sensitive)
- Voltages: always in volts as float, never strings like "3V3"
- Frequencies: always numeric with explicit `unit` field
- Dates: ISO 8601 `YYYY-MM-DD`

---

## Example: Minimal sensor profile

```yaml
component:
  part_number: "EXAMPLE_SENSOR"
  manufacturer: "Acme Corp"
  description: "3-axis accelerometer with I2C interface"
  category: sensor
  package: "LGA-8"
  datasheet_file: "example_sensor.pdf"
  datasheet_version: "Rev 1.0"
  profile_date: "2026-03-23"
  profile_completeness: [schematic, software]

schematic:
  supply:
    - rail: VDD
      pin: VDD
      min: 1.7
      typ: 1.8
      max: 3.6
      unit: V

  current:
    - mode: active
      typ: 0.18
      unit: mA
      condition: "ODR=100Hz"

  absolute_max:
    - param: VDD
      value: "4.8"
      unit: V

  pinout:
    - pin: 1
      name: VDD
      type: PWR
      description: "Supply voltage"
    - pin: 2
      name: GND
      type: GND
      description: Ground
    - pin: 5
      name: INT
      type: OD
      description: "Interrupt output, active low, open-drain. Requires external pull-up."

  decoupling:
    - rail: VDD
      value: 100nF
      type: ceramic
      placement: "As close as possible to VDD pin"

  design_checks:
    - "Verify VDD is within 1.7–3.6V operating range"
    - "INT requires external pull-up resistor if used; leave floating if unused"

software:
  interface:
    protocol: I2C
    max_clock:
      value: 400
      unit: kHz
    logic_level:
      value: 1.8
      unit: V

  addressing:
    default: "0x18"
    alternate: "0x19"
    config_pin: SDO
    note: "SDO low = 0x18, SDO high = 0x19"

  interrupt:
    pins:
      - name: INT
        active: low
        type: open-drain
        latched: true
        clear_method: read_status

  registers:
    - addr: "0x0f"
      name: WHO_AM_I
      access: R
      reset: "0x33"
      description: "Device identity register. Read to confirm correct device."

  init_sequence:
    - step: 1
      action: power_on
      delay_ms: 5
      note: "Wait for boot"
    - step: 2
      action: verify
      register: WHO_AM_I
      expected: "0x33"
      note: "Confirm correct device before proceeding"

  data_output:
    axes: [X, Y, Z]
    format: "16-bit signed two's complement"
    registers: [OUT_X_L, OUT_X_H, OUT_Y_L, OUT_Y_H, OUT_Z_L, OUT_Z_H]
    lsb_per_unit: 0.061
    unit: mg

  driver_notes:
    - "Verify WHO_AM_I before writing any configuration"
```

---

## Changelog

| Version | Date | Notes |
|---------|------|-------|
| 1.0 | 2026-03-23 | Initial schema |
