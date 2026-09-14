# designs/picorv32/constraints.sdc
# ─────────────────────────────────────────────────────────────────────────────
# Synopsys Design Constraints for picorv32 @ 4 ns clock (250 MHz target)
# Used by OpenSTA for static timing analysis.
# ─────────────────────────────────────────────────────────────────────────────

# ── Clock definition ────────────────────────────────────────────────────────
# 4 ns period = 250 MHz.  Waveform: rise at 0, fall at 2.
create_clock -name clk -period 4.0 -waveform {0 2.0} [get_ports clk]

# Mark clock network as ideal (no clock-tree uncertainty for this analysis)
set_clock_uncertainty 0.0 [get_clocks clk]
set_clock_latency     0.0 [get_clocks clk]

# ── I/O delays ──────────────────────────────────────────────────────────────
# Assume external logic drives inputs 0.5 ns after the rising clock edge
# and expects outputs to be stable 0.5 ns before the next rising edge.

set_input_delay  -clock clk -max 0.5 [all_inputs]
set_output_delay -clock clk -max 0.5 [all_outputs]

# Exclude the clock port itself from I/O delay constraints
set_input_delay  -clock clk -max 0.0 [get_ports clk]
set_input_delay  -clock clk -max 0.0 [get_ports resetn]
