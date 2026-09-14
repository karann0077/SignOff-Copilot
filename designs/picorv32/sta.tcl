# designs/picorv32/sta.tcl
# ─────────────────────────────────────────────────────────────────────────────
# OpenSTA static timing analysis script for picorv32
#
# Usage (called by orchestrator.py via subprocess):
#   opensta -exit designs/picorv32/sta.tcl
#
# Environment variables expected:
#   LIBERTY      — path to Sky130 Liberty file
#   NETLIST      — path to gate-level netlist (from Yosys)
#   SDC          — path to SDC constraints file
#   TIMING_RPT   — output path for timing report
# ─────────────────────────────────────────────────────────────────────────────

# ── Read Liberty ─────────────────────────────────────────────────────────────
read_liberty -min $env(LIBERTY)
read_liberty -max $env(LIBERTY)

# ── Read netlist ─────────────────────────────────────────────────────────────
read_verilog $env(NETLIST)
link_design  $env(DESIGN_TOP)

# ── Apply constraints ────────────────────────────────────────────────────────
read_sdc $env(SDC)

# ── Run timing analysis ──────────────────────────────────────────────────────
check_setup -verbose

# ── Generate reports (written to stdout and captured by orchestrator) ─────────
# Full path report — top 20 worst paths, max delay (setup check)
report_checks \
    -path_delay       max \
    -format           full_clock_expanded \
    -fields           {slew cap input nets fanout} \
    -no_line_splits \
    -group_count      20

# WNS / TNS summary lines — LOG PARSER READS THESE EXACT KEYWORDS
report_wns
report_tns
report_check_types

# Hold analysis
report_checks \
    -path_delay       min \
    -format           full_clock_expanded \
    -group_count      5

exit
